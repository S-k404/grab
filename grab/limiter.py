"""Total speed cap shared by every download.

yt-dlp's own limit applies per connection (8 parallel fragments = 8x the limit) and can't change
mid-download, and aria2c / gallery-dl each have their own. So instead every download is routed
through this tiny local proxy (127.0.0.1 only), which meters the bytes coming back with one shared
token bucket. The limit can be changed at any time and applies instantly to everything running.
"""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.parse

CHUNK = 256 * 1024
_HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


class SpeedLimiter:
    def __init__(self, rate: float | None = None):
        self.rate = rate            # bytes per second, None = unlimited
        self.port: int | None = None
        self.bytes = 0              # total bytes delivered (for measuring)
        self._tokens = 0.0
        self._last = time.monotonic()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "SpeedLimiter":
        threading.Thread(target=self._run, daemon=True, name="speed-limiter").start()
        self._ready.wait(5)
        return self

    def set_rate(self, rate: float | None):
        self.rate = float(rate) if rate else None

    # ---- internals
    def _run(self):
        self._loop = asyncio.new_event_loop()
        srv = self._loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0, limit=1 << 20))
        self.port = srv.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    async def _take(self, n: int):
        """Token bucket shared by all connections. Debt is allowed, so total throughput == rate."""
        rate = self.rate
        if not rate:
            return
        now = time.monotonic()
        self._tokens = min(rate * 0.25, self._tokens + (now - self._last) * rate)
        self._last = now
        self._tokens -= n
        if self._tokens < 0:
            await asyncio.sleep(-self._tokens / rate)

    async def _pipe(self, src, dst, metered: bool):
        try:
            while data := await src.read(CHUNK):
                if metered:
                    await self._take(len(data))
                    self.bytes += len(data)
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            try:
                dst.close()
            except Exception:  # noqa: BLE001
                pass

    async def _client(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        try:
            if method.upper() == "CONNECT":  # HTTPS: tunnel the encrypted bytes and meter them
                host, _, port = target.rpartition(":")
                up_r, up_w = await asyncio.open_connection(host.strip("[]"), int(port or 443), limit=1 << 20)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:  # plain HTTP: forward one request per connection
                u = urllib.parse.urlsplit(target)
                up_r, up_w = await asyncio.open_connection(u.hostname, u.port or 80, limit=1 << 20)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                hdrs = [l for l in lines[1:] if l and l.split(":", 1)[0].strip().lower() not in _HOP_HEADERS]
                up_w.write(f"{method} {path} {version}\r\n".encode("latin-1")
                           + "".join(h + "\r\n" for h in hdrs + ["Connection: close"]).encode("latin-1") + b"\r\n")
                await up_w.drain()
        except (OSError, ValueError) as e:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nX-Grab-Error: {type(e).__name__}\r\n\r\n".encode())
            await writer.drain()
            writer.close()
            return
        await asyncio.gather(self._pipe(reader, up_w, metered=False), self._pipe(up_r, writer, metered=True))

"""Headless Chromium/Helium CDP sniffer for dynamic, JS-rendered, and protected pages."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from websockets.sync.client import connect as ws_connect

_BROWSER_SEM = threading.Semaphore(2)

_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

_BROWSER_PATHS = (
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
    "/Applications/Helium.app/Contents/MacOS/Helium",
)

_BROWSER_NAMES = ("brave-browser", "google-chrome", "chromium", "chromium-browser", "microsoft-edge")

_INIT_STEALTH_JS = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.__grabDrm = '';
    if (navigator.requestMediaKeySystemAccess) {
      const orig = navigator.requestMediaKeySystemAccess.bind(navigator);
      navigator.requestMediaKeySystemAccess = function(keySystem, configs) {
        window.__grabDrm = String(keySystem || 'EME');
        return orig(keySystem, configs);
      };
    }
  } catch (_) {}
})();
"""

_TRIGGER_MEDIA_JS = """
(() => {
  const roots = [document];
  const mediaEls = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.tagName === 'VIDEO' || el.tagName === 'AUDIO') mediaEls.push(el);
      if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); }
    }
  };
  try { walk(document); } catch (_) {}
  const urls = [];
  let hasMedia = false;
  for (const el of mediaEls) {
    const src = el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '';
    if (src && !src.startsWith('blob:') && !src.startsWith('data:')) {
      urls.push({url: src, kind: el.tagName === 'AUDIO' ? 'audio' : 'video'});
    }
    hasMedia = true;
    try { el.muted = true; el.play().catch(() => {}); } catch (_) {}
  }
  if (!hasMedia) {
    const sel = 'button[aria-label*="play" i], [role="button"][aria-label*="play" i], '
      + '.vjs-big-play-button, .plyr__control--overlaid, [data-testid*="play" i], .jw-display-icon-container';
    for (const r of roots) {
      const btn = r.querySelector(sel);
      if (btn) { try { btn.click(); } catch (_) {} break; }
    }
  }
  try { window.scrollBy(0, 300); } catch (_) {}
  return {
    title: document.title || '',
    href: location.href,
    html: document.documentElement ? document.documentElement.outerHTML : '',
    drm: window.__grabDrm || '',
    urls,
  };
})();
"""

_READ_DOM_JS = """
(() => ({
  title: document.title || '',
  href: location.href,
  html: document.documentElement ? document.documentElement.outerHTML : '',
  drm: window.__grabDrm || '',
}))();
"""

_SEGMENT_EXTS = (".ts", ".m4s", ".cmf", ".cmfv", ".cmfa")


@dataclass
class SniffResult:
    """Network streams, manifests, JSON payloads, DOM HTML, and cookies captured from a page."""

    url: str
    title: str = ""
    html: str = ""
    media_urls: list[dict] = field(default_factory=list)
    manifests: list[dict] = field(default_factory=list)
    json_payloads: list[dict] = field(default_factory=list)
    cookies_text: str = ""
    drm: str = ""
    live: bool = False


def headless_disabled() -> bool:
    """Return True if headless browser capture is explicitly disabled by env."""
    return os.environ.get("GRAB_NO_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def find_browser() -> str | None:
    """Locate an installed Chromium-based browser binary (Helium, Brave, Chrome, etc.)."""
    env_bin = os.environ.get("GRAB_BROWSER", "").strip()
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    for name in _BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    home = Path.home()
    candidates = [*_BROWSER_PATHS, *(str(home / p.lstrip("/")) for p in _BROWSER_PATHS)]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _parse_netscape_cookies(cookie_file: str | Path) -> list[dict]:
    """Parse a Netscape cookie file (including #HttpOnly_ lines) into CDP cookie dicts."""
    cookies: list[dict] = []
    try:
        lines = Path(cookie_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return cookies
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires_s, name, value = parts[:7]
        if not domain or not name:
            continue
        item: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        }
        try:
            exp = float(expires_s)
            if exp > 0:
                item["expires"] = exp
        except ValueError:
            pass
        cookies.append(item)
    return cookies


def _format_netscape_cookies(cookies: list[dict]) -> str:
    """Serialize CDP Network.getAllCookies entries to Netscape cookie-jar text."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = c.get("domain", "")
        name = c.get("name", "")
        if not domain or not name:
            continue
        subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = int(float(c.get("expires") or 0))
        if exp < 0:
            exp = 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append(f"{prefix}{domain}\t{subdomains}\t{path}\t{secure}\t{exp}\t{name}\t{c.get('value', '')}")
    return "\n".join(lines) + "\n"


def _ext_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    if "." not in path.rsplit("/", 1)[-1]:
        return ""
    return "." + path.rsplit(".", 1)[-1]


def _ext_from_disposition(disp: str) -> str:
    m = re.search(r"""filename\*?=(?:UTF-8'')?["']?[^"';\r\n]+?(\.[a-z0-9]{2,5})(?:["';\s]|$)""", disp, re.I)
    return m.group(1).lower() if m else ""


def _classify_response(url: str, mime: str, headers: dict, size: int) -> str | None:
    """Classify a captured network response into stream/video/audio/json or None."""
    low_mime = (mime or "").split(";")[0].strip().lower()
    disp = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    ext = _ext_from_url(url) or _ext_from_disposition(disp)
    low_url = url.lower()
    if low_url.startswith(("data:", "blob:", "chrome:", "devtools:")):
        return None
    if ext in _SEGMENT_EXTS or "init.mp4" in low_url or "init.m4s" in low_url:
        return None

    if "mpegurl" in low_mime or ext == ".m3u8":
        return "hls"
    if "dash+xml" in low_mime or ext == ".mpd":
        return "dash"
    if "vnd.ms-sstr+xml" in low_mime or (".ism" in low_url and "/manifest" in low_url):
        return "smooth"
    if "f4m" in low_mime or ext == ".f4m":
        return "hds"
    if low_mime in ("application/json", "text/json") or (
        ("graphql" in low_url or "/api/" in low_url) and "json" in low_mime
    ):
        return "json"

    if "attachment" in disp.lower() and not low_mime.startswith("text/"):
        return "file"

    if low_mime.startswith("video/") or ext in (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".flv", ".avi", ".ogv"):
        # Skip tiny tracking pixels / preview slivers unless Content-Range indicates a larger media file
        crange = headers.get("content-range") or headers.get("Content-Range") or ""
        total = 0
        if "/" in crange:
            try:
                total = int(crange.rsplit("/", 1)[-1])
            except ValueError:
                total = 0
        if max(size, total) and max(size, total) < 120_000 and "bytestart=" not in low_url:
            return None
        return "video"
    if low_mime.startswith("audio/") or ext in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"):
        if size and size < 20_000:
            return None
        return "audio"
    if low_mime in ("text/plain", "application/xml", "text/xml") and size <= 512_000 and ext in ("", ".txt", ".xml", ".m3u"):
        return "maybe_manifest"
    return None


def _inspect_manifest_flags(kind: str, body: str) -> tuple[str, bool]:
    """Detect DRM encryption and live stream markers in an HLS or DASH manifest body."""
    drm = ""
    live = False
    if kind == "hls":
        low = body.lower()
        if (
            "method=sample-aes" in low
            or "com.widevine" in low
            or "com.apple.streamingkeydelivery" in low
            or "com.microsoft.playready" in low
            or any(u in low for u in ("edef8ba9", "9a04f079", "94ce86fb"))
        ):
            drm = "Encrypted HLS stream (DRM)"
        if "#EXT-X-ENDLIST" not in body and "#EXT-X-TARGETDURATION" in body:
            live = True
    elif kind == "dash":
        low = body.lower()
        if "contentprotection" in low or "cenc:default_kid" in low or "widevine" in low:
            drm = "Encrypted DASH stream (DRM)"
        if 'type="dynamic"' in low:
            live = True
    return drm, live


def _open_page_ws(port: int, url: str) -> str:
    """Query CDP /json/list on the given port to locate the initial page WebSocket URL."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1.0) as resp:
                targets = json.loads(resp.read().decode("utf-8", "replace"))
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for DevTools page target on port {port} for {url}")


class _CDPSession:
    """Thin synchronous wrapper over a Chromium CDP WebSocket with multiplexed iframe sessions."""

    def __init__(self, ws):
        self.ws = ws
        self._next_id = 1
        self._pending: dict[int, dict] = {}
        self.events: list[dict] = []

    def send(self, method: str, params: dict | None = None, session_id: str | None = None) -> int:
        cmd_id = self._next_id
        self._next_id += 1
        msg: dict = {"id": cmd_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        return cmd_id

    def poll(self, timeout: float = 0.2) -> None:
        try:
            raw = self.ws.recv(timeout=timeout)
        except TimeoutError:
            return
        except Exception:  # noqa: BLE001
            return
        msg = json.loads(raw)
        if "id" in msg:
            self._pending[msg["id"]] = msg
        elif "method" in msg:
            self.events.append(msg)

    def call(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 4.0,
    ) -> dict:
        cmd_id = self.send(method, params, session_id=session_id)
        deadline = time.monotonic() + timeout
        while cmd_id not in self._pending and time.monotonic() < deadline:
            self.poll(timeout=min(0.2, max(0.02, deadline - time.monotonic())))
        reply = self._pending.pop(cmd_id, None)
        if not reply:
            return {}
        return reply.get("result") or {}


def _launch_browser(browser_bin: str, profile_dir: str, headers: dict | None) -> tuple[subprocess.Popen, int]:
    """Launch headless Chromium with remote debugging on an ephemeral port."""
    ua = (headers or {}).get("User-Agent") or _CHROME_UA
    cmd = [
        browser_bin,
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        "--mute-audio",
        "--disable-dev-shm-usage",
        "--window-size=1280,800",
        f"--user-agent={ua}",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    port = 0
    deadline = time.monotonic() + 8.0
    assert proc.stderr is not None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Headless browser exited immediately with code {proc.returncode}")
        line = proc.stderr.readline()
        if not line:
            time.sleep(0.05)
            continue
        m = re.search(r"DevTools listening on ws://127\.0\.0\.1:(\d+)/", line)
        if m:
            port = int(m.group(1))
            break
    if not port:
        raise RuntimeError("Headless browser failed to expose DevTools port")
    # Drain remaining stderr so a verbose Chromium process never blocks on the pipe.
    threading.Thread(target=proc.stderr.read, daemon=True).start()
    return proc, port


def sniff_page(
    url: str,
    cookie_file: str | Path | None = None,
    headers: dict | None = None,
    timeout: float = 14.0,
    cancel_event: threading.Event | None = None,
    note=None,
) -> SniffResult:
    """Render *url* in headless Chromium/Helium and capture streams, manifests, JSON, DOM, and cookies."""
    if headless_disabled():
        return SniffResult(url=url)
    browser_bin = find_browser()
    if not browser_bin:
        return SniffResult(url=url)

    res = SniffResult(url=url)
    if note:
        note(f"[grab] opening page in headless browser ({Path(browser_bin).name})")

    # Chromium's helper processes can still be flushing into the profile just after the main one exits; a
    # leftover temp dir must never turn a successful sniff into a failed job.
    with _BROWSER_SEM, tempfile.TemporaryDirectory(prefix="grab-browser-", ignore_cleanup_errors=True) as profile_dir:
        proc: subprocess.Popen | None = None
        try:
            proc, port = _launch_browser(browser_bin, profile_dir, headers)
            ws_url = _open_page_ws(port, url)
            with ws_connect(ws_url, max_size=32 * 1024 * 1024) as ws:
                cdp = _CDPSession(ws)
                cdp.call("Page.enable")
                cdp.call("Network.enable")
                cdp.call("Runtime.enable")
                cdp.call("Target.setAutoAttach", {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": False,
                    "flatten": True,
                })
                cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": _INIT_STEALTH_JS})

                extra_headers = {
                    k: v for k, v in (headers or {}).items()
                    if k.lower() not in ("user-agent", "host", "content-length")
                }
                if extra_headers:
                    cdp.call("Network.setExtraHTTPHeaders", {"headers": extra_headers})

                if cookie_file:
                    cookies = _parse_netscape_cookies(cookie_file)
                    if cookies:
                        cdp.call("Network.setCookies", {"cookies": cookies})

                cdp.events.clear()
                cdp.call("Page.navigate", {"url": url})
                _collect_cdp_session(cdp, res, timeout=timeout, cancel_event=cancel_event)

                all_cookies = cdp.call("Network.getAllCookies", timeout=2.0).get("cookies") or []
                if all_cookies:
                    res.cookies_text = _format_netscape_cookies(all_cookies)
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

    return res


def _decode_cdp_body(body_res: dict) -> str:
    """Decode CDP Network.getResponseBody result into UTF-8 text."""
    body = body_res.get("body") or ""
    if body_res.get("base64Encoded"):
        try:
            return base64.b64decode(body).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""
    return body


def _collect_cdp_session(
    cdp: _CDPSession,
    res: SniffResult,
    timeout: float,
    cancel_event: threading.Event | None,
) -> None:
    """Event loop reading network responses, firing media triggers, and capturing DOM state."""
    start = time.monotonic()
    deadline = start + timeout
    main_navigated = False
    load_fired_at: float | None = None
    triggered = False
    last_activity = start
    await_body: dict[str, tuple[str, str, dict, str | None]] = {}
    pending_body: dict[int, tuple[str, str, dict, str | None]] = {}
    requests_by_id: dict[str, dict] = {}
    child_sessions: list[str] = []
    triggered_children: set[str] = set()
    seen_media_urls: set[str] = set()
    seen_manifests: set[str] = set()

    def record_media(m_url: str, kind: str, mime: str = "", hdrs: dict | None = None, size: int = 0):
        nonlocal last_activity
        if not m_url or m_url in seen_media_urls:
            return
        seen_media_urls.add(m_url)
        res.media_urls.append({
            "url": m_url,
            "kind": kind,
            "mime": mime,
            "headers": hdrs or {},
            "size": size,
        })
        last_activity = time.monotonic()

    def trigger_session(sid: str | None) -> None:
        nonlocal last_activity
        eval_res = cdp.call(
            "Runtime.evaluate",
            {"expression": _TRIGGER_MEDIA_JS, "returnByValue": True},
            session_id=sid,
            timeout=2.0,
        )
        drain_events()
        val = ((eval_res.get("result") or {}).get("value")) or {}
        if isinstance(val, dict):
            if sid is None:
                res.title = val.get("title") or res.title
                res.url = val.get("href") or res.url
                res.html = val.get("html") or res.html
            if val.get("drm") and not res.drm:
                res.drm = f"Encrypted stream ({val['drm']})"
            for item in val.get("urls") or []:
                record_media(item.get("url", ""), item.get("kind", "video"))
        last_activity = time.monotonic()

    def drain_events():
        nonlocal main_navigated, load_fired_at, triggered, last_activity
        while cdp.events:
            ev = cdp.events.pop(0)
            method = ev.get("method")
            params = ev.get("params") or {}
            sid = ev.get("sessionId")

            if method == "Target.attachedToTarget":
                child_sid = params.get("sessionId")
                if child_sid:
                    child_sessions.append(child_sid)
                    cdp.send("Network.enable", session_id=child_sid)
                    cdp.send("Page.enable", session_id=child_sid)
                    cdp.send("Runtime.enable", session_id=child_sid)
            elif method == "Page.javascriptDialogOpening":
                cdp.send("Page.handleJavaScriptDialog", {"accept": False}, session_id=sid)
            elif method == "Page.frameNavigated" and not sid:
                frame = params.get("frame") or {}
                if not frame.get("parentId") and frame.get("url") not in ("", "about:blank"):
                    if triggered:
                        # A reload / JS challenge redirect happened; re-trigger after the new page settles
                        triggered = False
                        load_fired_at = None
                        triggered_children.clear()
                    main_navigated = True
                    last_activity = time.monotonic()
            elif method == "Page.loadEventFired" and not sid:
                if main_navigated or (time.monotonic() - start) >= 0.15:
                    main_navigated = True
                    load_fired_at = time.monotonic()
                    last_activity = load_fired_at
            elif method == "Network.requestWillBeSent":
                req_id = params.get("requestId", "")
                req = params.get("request") or {}
                req_url = req.get("url", "")
                entry = requests_by_id.setdefault(req_id, {"url": req_url, "headers": {}})
                entry["url"] = req_url or entry["url"]
                for k, v in (req.get("headers") or {}).items():
                    entry["headers"][k] = v
                ext = _ext_from_url(req_url)
                if ext in (".m3u8", ".mpd"):
                    last_activity = time.monotonic()
            elif method == "Network.requestWillBeSentExtraInfo":
                req_id = params.get("requestId", "")
                entry = requests_by_id.setdefault(req_id, {"url": "", "headers": {}})
                for k, v in (params.get("headers") or {}).items():
                    lk = k.lower()
                    if lk.startswith(":") or lk in ("cookie", "host", "content-length"):
                        continue
                    entry["headers"].setdefault(k, v)
            elif method == "Network.responseReceived":
                req_id = params.get("requestId", "")
                resp = params.get("response") or {}
                r_url = resp.get("url") or (requests_by_id.get(req_id) or {}).get("url", "")
                r_hdrs = resp.get("headers") or {}
                req_hdrs = (requests_by_id.get(req_id) or {}).get("headers") or {}
                mime = resp.get("mimeType") or ""
                size = 0
                try:
                    size = int(r_hdrs.get("content-length") or r_hdrs.get("Content-Length") or 0)
                except ValueError:
                    size = 0
                kind = _classify_response(r_url, mime, r_hdrs, size)
                if not kind:
                    continue
                if kind in ("hls", "dash"):
                    record_media(r_url, "stream", mime=mime, hdrs=req_hdrs, size=size)
                    if r_url not in seen_manifests:
                        seen_manifests.add(r_url)
                        await_body[req_id] = (kind, r_url, req_hdrs, sid)
                elif kind in ("smooth", "hds"):
                    record_media(r_url, "stream", mime=mime, hdrs=req_hdrs, size=size)
                elif kind in ("json", "maybe_manifest"):
                    if size <= 2_500_000:
                        await_body[req_id] = (kind, r_url, req_hdrs, sid)
                else:
                    record_media(r_url, kind, mime=mime, hdrs=req_hdrs, size=size)
            elif method == "Network.loadingFinished":
                req_id = params.get("requestId", "")
                info = await_body.pop(req_id, None)
                if info:
                    kind, r_url, req_hdrs, body_sid = info
                    cmd_id = cdp.send("Network.getResponseBody", {"requestId": req_id}, session_id=body_sid)
                    pending_body[cmd_id] = (kind, r_url, req_hdrs, body_sid)
            elif method == "Network.loadingFailed":
                await_body.pop(params.get("requestId", ""), None)

        for cmd_id in list(pending_body):
            reply = cdp._pending.pop(cmd_id, None)
            if reply is None:
                continue
            kind, r_url, req_hdrs, _sid = pending_body.pop(cmd_id)
            body = _decode_cdp_body(reply.get("result") or {})
            if not body:
                continue
            if kind == "maybe_manifest":
                head = body.lstrip()[:500]
                if head.startswith("#EXTM3U"):
                    kind = "hls"
                elif "<MPD" in head or "<mpd" in head:
                    kind = "dash"
                else:
                    continue
                record_media(r_url, "stream", hdrs=req_hdrs)
            if kind in ("hls", "dash"):
                drm, live = _inspect_manifest_flags(kind, body)
                if drm and not res.drm:
                    res.drm = drm
                if live:
                    res.live = True
                res.manifests.append({"url": r_url, "kind": kind, "body": body, "headers": req_hdrs})
            elif kind == "json":
                res.json_payloads.append({"url": r_url, "body": body})
            last_activity = time.monotonic()

    while time.monotonic() < deadline:
        if cancel_event and cancel_event.is_set():
            return
        cdp.poll(timeout=0.15)
        drain_events()
        now = time.monotonic()

        if not triggered and ((load_fired_at is not None and now - load_fired_at >= 0.4) or (now - start >= 5.0)):
            triggered = True
            trigger_session(None)
        if triggered:
            for child_sid in list(child_sessions):
                if child_sid not in triggered_children:
                    triggered_children.add(child_sid)
                    trigger_session(child_sid)

        # Finish promptly once load + media trigger finished and traffic/bodies settled
        quiet = now - last_activity
        if triggered and not await_body and not pending_body:
            if (res.media_urls or res.manifests) and quiet >= 1.0:
                break
            if quiet >= 2.5 and load_fired_at is not None and (now - load_fired_at) >= 2.5:
                break
            if quiet >= 2.5 and (now - start) >= 7.0:
                break

    dom_res = cdp.call("Runtime.evaluate", {"expression": _READ_DOM_JS, "returnByValue": True}, timeout=2.0)
    dom_val = ((dom_res.get("result") or {}).get("value")) or {}
    if isinstance(dom_val, dict):
        res.title = dom_val.get("title") or res.title
        res.url = dom_val.get("href") or res.url
        if dom_val.get("html"):
            res.html = dom_val["html"]
        if dom_val.get("drm") and not res.drm:
            res.drm = f"Encrypted stream ({dom_val['drm']})"

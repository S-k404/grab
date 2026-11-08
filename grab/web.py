"""GUI server: a tiny local web server (127.0.0.1 only) around the same Manager the CLI uses.
Used by Grab.app (inside a native window) and by `grab-gui` (in your browser)."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .core import BROWSER_NAMES, BROWSERS, MODES, QUALITIES, Manager, Options, installed_browsers, tool_status
from .schedule import Scheduler
from .updater import Updater
from .watch import Watcher

UI = Path(__file__).with_name("ui.html")

# The "Grab Stream Catcher" browser extension has a fixed ID (from the key in its manifest). Browsers
# stamp every request an extension makes with "Origin: chrome-extension://<id>", and web pages can't
# fake that, so this is what lets the extension - and only it - add downloads.
EXTENSION_ORIGINS = {"chrome-extension://hejmghgkkgadcieojoganellldeehhog"}

# Same presets as the buttons in the UI, so links from the extension use the one you picked in Grab.
PRESETS = {
    "best": {"mode": "auto", "quality": "best"}, "2160": {"mode": "auto", "quality": "2160"},
    "1440": {"mode": "auto", "quality": "1440"}, "480": {"mode": "auto", "quality": "480"}, "1080": {"mode": "auto", "quality": "1080"},
    "720": {"mode": "auto", "quality": "720"}, "mp3": {"mode": "audio", "audio_format": "mp3"},
    "m4a": {"mode": "audio", "audio_format": "m4a"}, "images": {"mode": "images"}, "file": {"mode": "direct"},
}


def _supported_site(url: str) -> str | None:
    """Name of the yt-dlp extractor for this page, if it's a known site (not the generic fallback)."""
    from yt_dlp.extractor import gen_extractor_classes
    if not hasattr(_supported_site, "ies"):
        _supported_site.ies = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
    for ie in _supported_site.ies:
        try:
            if ie.suitable(url) and ie.working():
                return ie.ie_key().replace("Tab", "")
        except Exception:  # noqa: BLE001
            continue
    return None


from .core import state_dir  # noqa: E402  (kept importable from here for the CLI)


class State:
    mgr: Manager
    defaults: Options
    prefs_file: Path
    updater: Updater
    scheduler: Scheduler
    watcher: Watcher

    @classmethod
    def send_key(cls) -> str:
        """Private key for 'Send to Grab' links: links carrying it download right away, without asking."""
        prefs = cls.prefs()
        if not prefs.get("send_key"):
            import secrets
            prefs["send_key"] = secrets.token_urlsafe(16)
            cls.save_prefs(prefs)
        return prefs["send_key"]

    @classmethod
    def base_options(cls) -> Options:
        o = Options.from_dict({**cls.defaults.to_dict(), **cls.prefs()})
        o.out_dir = str(Path(o.out_dir or cls.defaults.out_dir).expanduser())
        return o

    @classmethod
    def prefs(cls) -> dict:
        try:
            return json.loads(cls.prefs_file.read_text())
        except (OSError, ValueError):
            return {}

    @classmethod
    def save_prefs(cls, prefs: dict):
        cls.prefs_file.parent.mkdir(parents=True, exist_ok=True)
        cls.prefs_file.write_text(json.dumps(prefs, indent=1))


def _open(path: str, reveal: bool):
    p = Path(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if reveal and p.exists() else ["open", str(p)])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(p)] if reveal else ["explorer", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent if reveal else p)])


class Handler(BaseHTTPRequestHandler):
    server_version = f"grab/{__version__}"

    def log_message(self, *_):
        pass

    def _send(self, code: int, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html", "text/plain")) else "max-age=3600")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _extension(self) -> str | None:
        origin = self.headers.get("Origin", "")
        return origin if origin in EXTENSION_ORIGINS else None

    def do_OPTIONS(self):  # CORS preflight from the extension
        origin = self._extension()
        if not origin:
            return self._send(403, {"error": "forbidden"})
        self._send(204, b"", "text/plain", {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, POST",
                                            "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"})

    def _ext_send(self, code, body):
        self._send(code, body, headers={"Access-Control-Allow-Origin": self._extension() or "null", "Vary": "Origin"})

    def _same_origin(self) -> bool:
        # Block other websites from driving the downloader through your browser.
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host.split(":")[0] in ("127.0.0.1", "localhost") and (origin is None or origin == f"http://{host}")

    def _host_ok(self) -> bool:
        # only answer requests addressed to this machine (blocks DNS-rebinding tricks from web pages)
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/add":
            # "Send to Grab" bookmarklet. With Grab's private key it downloads right away;
            # without it (e.g. a link some website made) it only pre-fills the box for you to confirm.
            import hmac
            link = (q.get("url") or [""])[0]
            if hmac.compare_digest((q.get("key") or [""])[0], State.send_key()) and link.startswith(("http://", "https://")):
                prefs = State.prefs()
                opts = Options.from_dict({**State.base_options().to_dict(), **PRESETS.get(prefs.get("preset", "best"), {})})
                State.mgr.add([link], opts)
                return self._send(302, b"", "text/plain", {"Location": "/#added=" + urllib.parse.quote(link, safe="")})
            return self._send(302, b"", "text/plain", {"Location": "/#add=" + urllib.parse.quote(link, safe="")})
        if u.path == "/api/ext/ping":
            # read-only "is Grab running?" - browsers don't put an Origin on extension GETs, and web
            # pages can't read the answer anyway (no CORS header for them)
            return self._ext_send(200, {"app": "grab", "version": __version__,
                                        "preset": State.prefs().get("preset", "best")})
        if u.path == "/api/ext/check":  # read-only: "does Grab know this site?"
            page = (q.get("url") or [""])[0]
            return self._ext_send(200, {"site": _supported_site(page) if page.startswith("http") else None})
        if u.path == "/api/jobs":
            return self._send(200, {"jobs": State.mgr.snapshot(), "workers": State.mgr.workers, "learned": State.mgr.learned,
                                    "update": State.updater.status, "schedule": State.scheduler.status,
                                    "watch": State.watcher.snapshot()})
        if u.path == "/api/config":
            return self._send(200, {
                "defaults": State.defaults.to_dict(), "tools": tool_status(), "version": __version__,
                "modes": MODES, "qualities": QUALITIES, "browsers": BROWSERS, "workers": State.mgr.workers,
                "prefs": State.prefs(), "port": self.server.server_address[1],
                "installed_browsers": [{"id": b, "name": BROWSER_NAMES[b]} for b in installed_browsers()],
                "send_key": State.send_key(),
                "log_file": str(__import__("grab.core", fromlist=["state_dir"]).state_dir() / "logs" / "grab.log"),
                "shared_logins": __import__("grab.core", fromlist=["shared_logins_info"]).shared_logins_info(),
                "extension_dir": next((str(p) for p in (state_dir() / "browser-extension",
                                                        Path(__file__).resolve().parent.parent / "browser-extension")
                                       if (p / "manifest.json").is_file()), ""),
            })
        if u.path == "/api/details":  # "Copy details" on a failed download
            from .core import job_details
            job = State.mgr.get(int((q.get("id") or ["0"])[0] or 0))
            if not job:
                return self._send(404, {"error": "no such download"})
            return self._send(200, job_details(job).encode(), "text/plain; charset=utf-8")
        if u.path.startswith("/api/thumb/"):
            job = State.mgr.get(int(u.path.rsplit("/", 1)[-1] or 0))
            if job and job.thumbnail in job.files and Path(job.thumbnail).is_file():
                ctype = mimetypes.guess_type(job.thumbnail)[0] or "application/octet-stream"
                return self._send(200, Path(job.thumbnail).read_bytes(), ctype)
            return self._send(404, {"error": "no thumbnail"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/ext/add":
            return self._ext_add()
        if urllib.parse.urlparse(self.path).path == "/api/ext/add_many":
            return _ext_add_many(self)
        if urllib.parse.urlparse(self.path).path == "/api/ext/cookies":
            # logins shared by the extension (only for the sites it's set to share)
            if not self._extension():
                return self._send(403, {"error": "forbidden"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._ext_send(400, {"error": "bad json"})
            from .core import clear_shared_cookies, save_shared_cookies
            if body.get("clear"):
                clear_shared_cookies()
                return self._ext_send(200, {"ok": True, "cleared": True})
            n = save_shared_cookies(list(body.get("cookies") or [])[:5000], [str(x) for x in body.get("sites") or []][:20],
                                    str(body.get("browser") or "")[:40])
            return self._ext_send(200, {"ok": True, "saved": n})
        if urllib.parse.urlparse(self.path).path == "/api/ext/hello":
            if not self._extension():
                return self._send(403, {"error": "forbidden"})
            return self._ext_send(200, {"key": State.send_key()})  # so its grab:// fallback downloads right away too
        if not self._same_origin():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        mgr, path, jid = State.mgr, urllib.parse.urlparse(self.path).path, body.get("id")

        if path == "/api/add":
            opts = Options.from_dict({**State.defaults.to_dict(), **(body.get("options") or {})})
            opts.out_dir = str(Path(opts.out_dir or State.defaults.out_dir).expanduser())
            if body.get("workers"):
                mgr.set_workers(body["workers"])
            jobs = mgr.add(body.get("urls", ""), opts)
            if body.get("now"):  # "Start right away" even outside the download window
                for j in jobs:
                    mgr.start_now(j.id)
            return self._send(200, {"added": len(jobs)})
        if path == "/api/quick_add":
            # menu-bar "Paste Link and Download": your saved format and options, no window needed
            prefs = State.prefs()
            opts = Options.from_dict({**State.base_options().to_dict(), **PRESETS.get(prefs.get("preset", "best"), {})})
            jobs = mgr.add(body.get("urls", ""), opts)
            return self._send(200, {"added": len(jobs)})
        if path == "/api/prefs":
            State.save_prefs(body.get("prefs") or {})
            if body.get("prefs", {}).get("workers"):
                mgr.set_workers(body["prefs"]["workers"])
            State.scheduler.tick()  # apply new speed cap / download window right away
            return self._send(200, {"ok": True})
        if path == "/api/workers":
            mgr.set_workers(body.get("workers", 4))
            return self._send(200, {"workers": mgr.workers})
        actions = {"/api/cancel": mgr.cancel, "/api/pause": mgr.pause, "/api/resume": mgr.resume, "/api/retry": mgr.retry,
                   "/api/start_now": mgr.start_now}
        if path in actions:
            actions[path](jid)
            return self._send(200, {"ok": True})
        if path == "/api/retry_login":
            # "Use your login from <browser>": retry this download with that browser's cookies
            job, browser = mgr.get(jid), str(body.get("browser") or "")
            if not job or browser not in BROWSERS or not browser:
                return self._send(400, {"error": "unknown download or browser"})
            job.opts.cookies_browser = browser
            mgr.retry(jid)
            if body.get("default") and not State.prefs().get("cookies_browser"):
                State.save_prefs({**State.prefs(), "cookies_browser": browser})  # remember it for future downloads
            return self._send(200, {"ok": True})
        if path == "/api/remove":
            mgr.remove(jid)
            return self._send(200, {"ok": True})
        if path == "/api/doctor":
            from .doctor import run_checks
            return self._send(200, {"checks": run_checks(body.get("out_dir") or State.defaults.out_dir,
                                                         cookies_browser=State.prefs().get("cookies_browser", ""))})
        if path == "/api/analyze":
            from .analyze import analyze
            try:
                return self._send(200, analyze(str(body.get("url") or ""), State.base_options()))
            except Exception as e:  # noqa: BLE001
                from .core import _clean_err, _tip, needs_login
                msg = _clean_err(e)
                return self._send(200, {"kind": "error", "error": msg, "tip": _tip(msg).strip().removeprefix("Tip: "),
                                        "needs_login": needs_login(msg)})
        if path == "/api/update/check":
            State.updater.request()
            return self._send(200, {"ok": True})
        if path.startswith("/api/watch/"):
            w, sid = State.watcher, body.get("id")
            try:
                if path == "/api/watch/add":
                    src = w.add(body.get("url", ""), **{k: body.get(k) for k in ("name", "every_hours", "start", "latest", "preset", "media", "cookies_browser", "scan")})
                    threading.Thread(target=w.tick, daemon=True).start()  # first check right away
                    return self._send(200, {"ok": True, "source": src.to_dict()})
                if path == "/api/watch/update":
                    return self._send(200, {"ok": True, "source": w.update(sid, **{k: v for k, v in body.items() if k != "id"}).to_dict()})
                if path == "/api/watch/remove":
                    w.remove(sid)
                    return self._send(200, {"ok": True})
                if path == "/api/watch/check":
                    w.check_now(sid)
                    w.tick()
                    return self._send(200, {"ok": True})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e).strip("'")})
        if path == "/api/forget":
            mgr.forget()
            return self._send(200, {"ok": True})
        if path == "/api/clear":  # {"all": true} also clears failed downloads
            return self._send(200, {"ok": True, "cleared": mgr.clear_finished(failed=bool(body.get("all")))})
        if path == "/api/reveal_log":
            from .core import state_dir
            _open(str(state_dir() / "logs" / "grab.log"), reveal=True)
            return self._send(200, {"ok": True})
        if path in ("/api/reveal", "/api/open"):
            job = mgr.get(jid) if jid else None
            if job and job.files:
                target = next((f for f in reversed(job.files) if Path(f).exists()), job.files[-1])
                _open(target, reveal=path == "/api/reveal")
            elif not jid:
                # the output folder the page is set to (only folders, never files)
                folder = Path(body.get("path") or State.defaults.out_dir).expanduser()
                folder.mkdir(parents=True, exist_ok=True)
                if folder.is_dir():
                    _open(str(folder), reveal=False)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def _ext_add_impl(handler: Handler):
    if not handler._extension():
        return handler._send(403, {"error": "forbidden"})
    try:
        body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length") or 0)) or b"{}")
    except ValueError:
        return handler._ext_send(400, {"error": "bad json"})
    url, page = str(body.get("url") or ""), str(body.get("page") or "")
    if not url.startswith(("http://", "https://")):
        return handler._ext_send(400, {"error": "only http(s) links can be downloaded"})
    if body.get("auto") and State.mgr.has_url(url):  # auto-download: a video you already have isn't fetched twice
        return handler._ext_send(200, {"added": 0, "ids": [], "duplicate": True})
    prefs = State.prefs()
    opts = Options.from_dict({**State.defaults.to_dict(), **prefs, **PRESETS.get(prefs.get("preset", "best"), {})})
    opts.out_dir = str(Path(opts.out_dir or State.defaults.out_dir).expanduser())
    if url != page:  # a raw stream: it needs the page as Referer, and the page's title as its name
        opts.referer = page if page.startswith("http") else ""
        opts.title = str(body.get("title") or "")[:200]
        ua = str(body.get("user_agent") or "")
        if ua:
            opts.headers = {**(opts.headers or {}), "User-Agent": ua}  # same browser identity that played it
    jobs = State.mgr.add([url], opts)
    if body.get("now"):
        for j in jobs:
            State.mgr.start_now(j.id)
    return handler._ext_send(200, {"added": len(jobs), "ids": [j.id for j in jobs]})


Handler._ext_add = _ext_add_impl


def _ext_add_many(handler: Handler):
    """"Everything on this page" in the extension: the pictures and videos you ticked, as one group in the
    list, saved in a folder named after the page, each under its own name."""
    if not handler._extension():
        return handler._send(403, {"error": "forbidden"})
    try:
        body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length") or 0)) or b"{}")
    except ValueError:
        return handler._ext_send(400, {"error": "bad json"})
    from .core import batch_names, unwrap_url
    page, title = str(body.get("page") or ""), str(body.get("title") or "")[:200]
    items, seen = [], set()
    for i in (body.get("items") or [])[:2000]:
        url = unwrap_url(str(i.get("url") or "")) if isinstance(i, dict) else ""
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            items.append((url, "video" if i.get("kind") == "video" else "image"))
    if not items:
        return handler._ext_send(400, {"error": "nothing to download"})
    opts = State.base_options()
    opts.mode, opts.aria2, opts.title = "direct", False, ""  # one plain connection each, named by batch_names
    opts.referer = page if page.startswith(("http://", "https://")) else ""
    ua = str(body.get("user_agent") or "")
    if ua:
        opts.headers = {**(opts.headers or {}), "User-Agent": ua}
    host = urllib.parse.urlparse(page).netloc.removeprefix("www.")
    opts.subfolder = (title or host or "page").encode()[:80].decode(errors="ignore").strip()
    group = State.mgr.add_group(page or items[0][0], f"{len(items)} files from {title or host or 'a page'}",
                                [([u for u, _ in items], opts, batch_names(items))],
                                thumbnail=next((u for u, k in items if k == "image"), ""))
    return handler._ext_send(200, {"added": group.children, "id": group.id})


def main(argv=None):
    p = argparse.ArgumentParser(prog="grab-gui", description="GUI for grab (opens in your browser)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--out", default=Options().out_dir)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--state-dir", default=str(state_dir()), help="where history and settings are kept")
    p.add_argument("--no-auto-update", action="store_true", help="never update yt-dlp/gallery-dl by itself")
    p.add_argument("--exit-with-parent", action="store_true", help="quit when the launching app goes away (used by Grab.app)")
    a = p.parse_args(argv)

    sd = Path(a.state_dir).expanduser()
    os.environ["GRAB_STATE_DIR"] = str(sd)  # everything (history, settings, shared logins) lives in this one folder
    State.prefs_file = sd / "prefs.json"
    from .core import log, start_log_file
    start_log_file(sd / "logs" / "grab.log")
    log.info("engine %s started (yt-dlp %s)", __version__, tool_status()["yt-dlp"])
    workers = State.prefs().get("workers") or a.jobs
    State.mgr = Manager(int(workers), state_file=str(sd / "history.json"))
    State.defaults = Options(out_dir=str(Path(a.out).expanduser()))

    srv = None
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("grab-gui: no free port")
    srv.daemon_threads = True

    def restart():
        """Re-exec ourselves so a freshly installed yt-dlp / gallery-dl is actually loaded.
        Same process id and port, so Grab.app and open pages just reconnect."""
        State.mgr.save()
        srv.server_close()
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, "-m", "grab.web", "--no-browser", "--port", str(srv.server_address[1]),
                                  "--jobs", str(a.jobs), "--out", a.out, "--state-dir", a.state_dir]
                 + (["--exit-with-parent"] if a.exit_with_parent else []))

    State.scheduler = Scheduler(State.mgr, State.prefs).start()
    State.watcher = Watcher(State.mgr, sd / "watch.json", State.base_options).start()
    State.updater = Updater(State.mgr, sd / "update.json", State.prefs, restart)
    State.updater.after_restart()
    State.mgr.outdated_hook = State.updater.request
    if a.no_auto_update:
        State.updater.status.update(state="off", message="Automatic updates are disabled for this session")
    else:
        State.updater.start()

    if a.exit_with_parent:
        # If Grab.app is force-quit or crashes it can't stop us; notice and shut down cleanly ourselves.
        parent = os.getppid()

        def watch_parent():
            while os.getppid() == parent:
                time.sleep(2)
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=watch_parent, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"grab GUI running at {url}  (Ctrl-C to quit)", flush=True)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)  # a parent may have started us with SIGINT ignored
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        # Pause (not cancel) so unfinished downloads show up as resumable next launch.
        State.mgr.pause()
        deadline = time.time() + 2.5
        while State.mgr.running and time.time() < deadline:
            time.sleep(0.1)
        State.mgr.save()
        print("\nbye")


if __name__ == "__main__":
    main()

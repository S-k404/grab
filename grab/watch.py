"""Watch list: channels, playlists, subreddits and profiles that Grab checks by itself for new posts.

Each source is checked every few hours. A check is an ordinary download job with the download
archive switched on, so only things not downloaded before are fetched, and with a scan depth so a
check only looks at the newest posts. The first check can:
  "new"     - mark everything that exists today as already seen, then only download future posts
  "latest"  - download the newest N, then only new ones
  "all"     - download the whole back catalogue
Checks go through the normal queue, so the download window and speed cap apply to them too.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import yt_dlp

from . import core
from .core import Manager, Options, friendly_name, prefers_gallery

PRESETS = {  # same as the buttons in the UI
    "best": {"mode": "auto", "quality": "best"}, "2160": {"mode": "auto", "quality": "2160"},
    "1440": {"mode": "auto", "quality": "1440"}, "480": {"mode": "auto", "quality": "480"}, "1080": {"mode": "auto", "quality": "1080"},
    "720": {"mode": "auto", "quality": "720"}, "mp3": {"mode": "audio", "audio_format": "mp3"},
    "m4a": {"mode": "audio", "audio_format": "m4a"}, "images": {"mode": "images"},
}


@dataclasses.dataclass
class Source:
    url: str
    name: str = ""
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex[:8])
    every_hours: float = 6
    start: str = "new"            # first check: "new" | "latest" | "all"
    latest: int = 10              # for start == "latest"
    preset: str = "best"
    media: str = "all"            # "all" | "images" | "videos"
    cookies_browser: str = ""     # login cookies just for this source ("" = use Grab's setting)
    scan: int = 30                # newest N posts looked at per check
    enabled: bool = True
    created: float = dataclasses.field(default_factory=time.time)
    last_check: float | None = None
    next_check: float | None = None
    state: str = "new"            # new | setting up | checking | idle | error
    last_result: str = ""
    new_total: int = 0
    check_started: float | None = None
    primed: bool = False          # first-check setup ("only new" / "latest N") done

    def to_dict(self):
        d = dataclasses.asdict(self)
        d["needs_login"] = self.state == "error" and core.needs_login(self.last_result)
        d["cookie_problem"] = core.cookie_problem(self.last_result) if self.state == "error" else ""
        return d


class Watcher:
    def __init__(self, mgr: Manager, state_file, base_options, tick_seconds: float = 30):
        self.mgr, self.state_file, self.base_options = mgr, Path(state_file), base_options
        self.tick_seconds = tick_seconds
        self.lock = threading.RLock()
        self.sources: dict[str, Source] = {}
        self._load()

    # ---------------------------------------------------------------- storage
    def _load(self):
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        fields = {f.name for f in dataclasses.fields(Source)}
        for d in data.get("sources", []):
            src = Source(**{k: v for k, v in d.items() if k in fields})
            if src.state in ("checking", "setting up"):  # interrupted by quitting: just check again soon
                src.state, src.next_check = "idle", time.time() + 60
            self.sources[src.id] = src

    def save(self):
        with self.lock:
            data = {"version": 1, "sources": [s.to_dict() for s in self.sources.values()]}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.state_file)

    # ---------------------------------------------------------------- editing
    def add(self, url: str, **kw) -> Source:
        url = core.parse_urls(url)[0] if core.parse_urls(url) else url
        with self.lock:
            for s in self.sources.values():
                if s.url == url:
                    raise ValueError(f"already watching {s.name}")
            fields = {f.name for f in dataclasses.fields(Source)} - {"id", "url", "created"}
            src = Source(url=url, **{k: v for k, v in kw.items() if k in fields and v is not None})
            src.name = src.name or friendly_name(url)
            src.next_check = time.time()  # first check right away
            self.sources[src.id] = src
        self.save()
        return src

    def update(self, sid: str, **kw) -> Source:
        with self.lock:
            src = self.sources[sid]
            for k, v in kw.items():
                if k in ("name", "every_hours", "preset", "media", "cookies_browser", "scan", "enabled") and v is not None:
                    setattr(src, k, v)
            if kw.get("enabled") and src.next_check is None:
                src.next_check = time.time()
        self.save()
        return src

    def remove(self, sid: str):
        with self.lock:
            self.sources.pop(sid, None)
        self.save()

    def check_now(self, sid: str | None = None):
        with self.lock:
            for s in ([self.sources[sid]] if sid else list(self.sources.values())):
                if s.state not in ("checking", "setting up"):
                    s.next_check = time.time()
        self.save()

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [s.to_dict() for s in sorted(self.sources.values(), key=lambda s: s.created)]

    # ---------------------------------------------------------------- checking
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="watcher").start()
        return self

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad source must not stop the others
                pass
            time.sleep(self.tick_seconds)

    def tick(self):
        now = time.time()
        with self.lock:
            sources = list(self.sources.values())
        for src in sources:
            if src.state == "checking":
                self._collect(src)
            elif src.enabled and src.state != "setting up" and src.next_check and src.next_check <= now:
                self._begin(src)

    def options_for(self, src: Source) -> Options:
        o = self.base_options()
        o = dataclasses.replace(o, **PRESETS.get(src.preset, {}))
        o.archive, o.media, o.watch, o.scan = True, src.media, src.id, src.scan
        o.items = ""
        if src.cookies_browser:
            o.cookies_browser = src.cookies_browser
        return o

    def _begin(self, src: Source):
        o = self.options_for(src)
        if not src.primed and src.start in ("new", "latest"):
            # mark what exists today as seen (all of it, or all but the newest N) - then normal checks
            # only ever pick up posts newer than that
            src.state, src.last_result = "setting up", "Looking at what's already there…"
            self.save()
            keep = max(1, int(src.latest)) if src.start == "latest" else 0
            threading.Thread(target=self._baseline, args=(src, o, keep), daemon=True).start()
            return
        if not src.primed and src.start == "all":
            o.scan = 0
        src.primed = True
        src.state, src.check_started = "checking", time.time()
        src.last_result = "Checking…"
        self.mgr.add([src.url], o)
        self.save()

    def _collect(self, src: Source):
        """When all downloads of the current check are finished, record what came in."""
        mine = [j for j in self.mgr.snapshot() if j["opts_watch"] == src.id and (j["created"] or 0) >= (src.check_started or 0)]
        if any(j["status"] in ("queued", "running", "paused") for j in mine):
            return
        new = sum(len(j["files"]) for j in mine if j["status"] == "done" and not j["children"])
        errors = [j for j in mine if j["status"] == "error"]
        src.last_check = time.time()
        src.next_check = src.last_check + max(0.25, float(src.every_hours)) * 3600
        src.new_total += new
        if errors and not new:
            src.state, src.last_result = "error", errors[0]["error"].split("\n")[0][:200]
        else:
            src.state = "idle"
            src.last_result = (f"{new} new file{'s' if new != 1 else ''}" if new else "Nothing new") + \
                (f", {len(errors)} failed" if errors else "")
        # use the channel/playlist title once we know it
        parent = next((j for j in mine if j["children"] and j["title"] and j["title"] != j["url"]), None)
        if parent and src.name == friendly_name(src.url):
            src.name = parent["title"]
        # tidy the downloads list: a check re-looks at the newest posts every time, so drop its
        # "already had" rows and the playlist rows that brought nothing new
        parent_of = {j["id"]: j["parent"] for j in mine}
        keep: set[int] = set()
        for j in mine:
            if j["status"] in ("done", "error") and not j["children"]:
                p = j["parent"]
                while p is not None and p not in keep:  # the channel -> "Videos" tab -> video chain stays
                    keep.add(p)
                    p = parent_of.get(p)
        for j in mine:
            if j["status"] == "skipped" or (j["children"] and j["id"] not in keep):
                self.mgr.remove(j["id"])
        self.save()

    def _baseline(self, src: Source, o: Options, keep_newest: int = 0):
        """Record what exists today as already downloaded (except the newest `keep_newest`), without downloading."""
        try:
            gallery = prefers_gallery(src.url) or o.mode == "images"
            n = (_baseline_gallery if gallery else _baseline_ytdlp)(src.url, o, keep_newest)
            src.primed = True
            if keep_newest:  # now download the newest N right away
                src.state, src.next_check = "idle", time.time()
                src.last_result = f"Getting the latest {keep_newest}…"
                self.save()
                return
            src.state, src.last_result = "idle", f"Watching. {n} existing posts skipped; new ones will download"
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0][:180]
            src.state, src.last_result = "error", f"Couldn't read the source: {msg}" + core._tip(msg).replace("\nTip:", " —")
        src.last_check = time.time()
        src.next_check = src.last_check + max(0.25, float(src.every_hours)) * 3600
        self.save()


def _baseline_gallery(url: str, o: Options, keep_newest: int = 0) -> int:
    Path(o.out_dir).mkdir(parents=True, exist_ok=True)
    cmd = [core.GALLERY_DL, "--no-download", "--download-archive", os.path.join(o.out_dir, ".grab-gallery.sqlite3"),
           "--range", f"{keep_newest + 1}-{max(keep_newest + 1, o.scan)}", "-d", o.out_dir, *core.gallery_filter_args(o)]
    with core.login_for(o) as (cookie_file, browser):
        if browser:
            cmd += ["--cookies-from-browser", f"{browser[0]}:{browser[1]}" if browser[1] else browser[0]]
        if cookie_file:
            cmd += ["--cookies", cookie_file]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    seen = [l for l in r.stdout.splitlines() if l.strip()]
    if r.returncode != 0 and not seen:
        raise RuntimeError(next((l for l in reversed(r.stderr.splitlines()) if "error" in l.lower()), r.stderr.strip()[-200:] or "gallery-dl failed"))
    return len(seen)


def _baseline_ytdlp(url: str, o: Options, keep_newest: int = 0) -> int:
    Path(o.out_dir).mkdir(parents=True, exist_ok=True)
    archive = os.path.join(o.out_dir, ".grab-archive.txt")
    params = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": max(1, o.scan),
              "skip_download": True}
    lines: list[str] = []
    with core.login_for(o) as (cookie_file, browser):
        if browser:
            params["cookiesfrombrowser"] = browser
        if cookie_file:
            params["cookiefile"] = cookie_file
        with yt_dlp.YoutubeDL(params) as ydl:
            def walk(u, depth):
                info = ydl.extract_info(u, download=False)
                for e in (info or {}).get("entries") or []:
                    if not e:
                        continue
                    key = e.get("ie_key") or e.get("extractor_key") or ""
                    nested = e.get("_type") == "playlist" or key.endswith(("Tab", "Playlist")) or "list=" in (e.get("url") or "")
                    if nested and depth < 2:
                        walk(e.get("url") or e.get("webpage_url"), depth + 1)
                    elif e.get("id") and key:
                        lines.append(f"{key.lower()} {e['id']}")
                    elif e.get("url"):  # e.g. podcast/RSS items: yt-dlp's generic id is the file name
                        stem = os.path.splitext(urllib.parse.unquote(urllib.parse.urlsplit(e["url"]).path.rsplit("/", 1)[-1]))[0]
                        if stem:
                            lines.append(f"generic {stem}")
                if not (info or {}).get("entries") and info and info.get("id"):  # a single video
                    lines.append(f"{(info.get('extractor_key') or '').lower()} {info['id']}")
            walk(url, 0)
    lines = lines[keep_newest:]
    existing = set()
    if os.path.exists(archive):
        existing = set(Path(archive).read_text().splitlines())
    with open(archive, "a") as f:
        for line in lines:
            if line not in existing:
                f.write(line + "\n")
    return len(lines)


# ---------------------------------------------------------------- command line: grab watch ...

def _app_api(path, body=None, port=8765):
    """Talk to a running Grab app/GUI (so the app and the CLI never overwrite each other's list)."""
    import urllib.error
    import urllib.request
    if os.environ.get("GRAB_NO_APP"):
        return None
    for p in range(port, port + 6):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{p}{path}", method="POST" if body is not None else "GET",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)
        except OSError:
            continue
    return None


def cli(argv) -> int:
    import argparse
    import sys
    from .web import State, state_dir

    p = argparse.ArgumentParser(prog="grab watch", description="Download new posts from channels, playlists, subreddits and profiles automatically.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="start watching a link")
    a.add_argument("url")
    a.add_argument("--every", type=float, default=6, help="hours between checks (default 6)")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--only-new", dest="start", action="store_const", const="new", help="skip what exists today (default)")
    g.add_argument("--latest", type=int, metavar="N", help="download the newest N first")
    g.add_argument("--all", dest="start", action="store_const", const="all", help="download the whole back catalogue first")
    a.add_argument("--format", choices=list(PRESETS), default="best")
    m = a.add_mutually_exclusive_group()
    m.add_argument("--photos", dest="media", action="store_const", const="images")
    m.add_argument("--videos", dest="media", action="store_const", const="videos")
    a.add_argument("--cookies", default="", help="browser to take this site's login from (chrome, safari, ...)")
    a.add_argument("--name", default="")
    sub.add_parser("list", help="show watched sources")
    r = sub.add_parser("remove", help="stop watching (by id or name)")
    r.add_argument("which")
    run = sub.add_parser("run", help="check sources now (in the app if it's open, else here)")
    run.add_argument("--due", action="store_true", help="only sources whose check is due (for cron/launchd)")
    args = p.parse_args(argv)

    sd = state_dir()
    app = _app_api("/api/jobs")
    sources = (app or {}).get("watch")

    def local() -> Watcher:
        State.prefs_file = sd / "prefs.json"
        State.defaults = Options()
        return Watcher(Manager(4), sd / "watch.json", State.base_options, tick_seconds=1)

    if args.cmd == "add":
        kw = {"url": args.url, "every_hours": args.every, "start": "latest" if args.latest else (args.start or "new"),
              "latest": args.latest or 10, "preset": args.format, "media": args.media or "all",
              "cookies_browser": args.cookies, "name": args.name}
        if app is not None:
            res = _app_api("/api/watch/add", kw)
            if res.get("error"):
                print(f"grab watch: {res['error']}")
                return 1
            print(f"Watching {res['source']['name']} (in the Grab app), checking every {args.every:g} h")
        else:
            try:
                src = local().add(**kw)
            except ValueError as e:
                print(f"grab watch: {e}")
                return 1
            print(f"Watching {src.name}, checking every {args.every:g} h. Run `grab watch run` (or open Grab) to check.")
        return 0

    if args.cmd == "list":
        rows = sources if sources is not None else local().snapshot()
        if not rows:
            print("Not watching anything. Add one with: grab watch add URL")
        for s in rows:
            when = time.strftime("%d %b %H:%M", time.localtime(s["last_check"])) if s["last_check"] else "never"
            print(f"{s['id']}  {s['name']}\n          {s['url']}\n          every {s['every_hours']:g} h · last check {when}: "
                  f"{s['last_result'] or '-'} · {s['new_total']} downloaded{'' if s['enabled'] else ' · PAUSED'}")
        return 0

    if args.cmd == "remove":
        rows = sources if sources is not None else local().snapshot()
        hit = [s for s in rows if args.which in (s["id"], s["name"])]
        if not hit:
            print(f"grab watch: nothing called {args.which!r} (see `grab watch list`)")
            return 1
        if app is not None:
            _app_api("/api/watch/remove", {"id": hit[0]["id"]})
        else:
            local().remove(hit[0]["id"])
        print(f"Stopped watching {hit[0]['name']}")
        return 0

    if args.cmd == "run":
        if app is not None:
            for s in sources:
                if s["enabled"] and (not args.due or (s["next_check"] or 0) <= time.time()):
                    _app_api("/api/watch/check", {"id": s["id"]})
            print("Checks started in the Grab app - progress is shown there.")
            return 0
        w = local()
        if not args.due:
            w.check_now()
        w.tick()
        busy = lambda: any(s.state in ("checking", "setting up") for s in w.sources.values())  # noqa: E731
        while busy() or not w.mgr.idle():
            time.sleep(1)
            w.tick()
        for s in w.sources.values():
            print(f"{s.name}: {s.last_result}")
        return 0
    return 0

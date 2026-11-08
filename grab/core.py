"""Download engine shared by the CLI and the web GUI.

Each URL becomes a Job. A Manager runs up to `workers` jobs at the same time.
For every job the engine tries, in order:

  1. yt-dlp      - video/audio from ~1800 sites, handles HLS (m3u8), DASH (mpd),
                   ISM, RTMP etc. HLS/DASH fragments are fetched in parallel.
  2. gallery-dl  - image/gallery posts (Reddit, Twitter/X, Imgur, Instagram,
                   Pixiv, DeviantArt, boorus, ...), used when yt-dlp finds no video.
  3. direct      - anything else that isn't an HTML page (zip, pdf, iso, ...),
                   fetched with aria2c multi-connection or a plain HTTP stream.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import itertools
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadCancelled, parse_bytes, sanitize_filename

MODES = ("auto", "video", "audio", "images", "direct")
QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360")
BROWSERS = ("", "extension", "chrome", "safari", "firefox", "zen", "brave", "edge", "chromium", "opera", "vivaldi")

# browser id -> app names to look for in /Applications (to only offer browsers you actually have)
BROWSER_APPS = {
    "chrome": ["Google Chrome"], "safari": ["Safari"], "firefox": ["Firefox"], "zen": ["Zen", "Zen Browser"],
    "brave": ["Brave Browser"], "edge": ["Microsoft Edge"], "chromium": ["Chromium"], "opera": ["Opera"], "vivaldi": ["Vivaldi"],
}
BROWSER_NAMES = {"extension": "Logins shared by the Brave extension", "chrome": "Chrome", "safari": "Safari", "firefox": "Firefox", "zen": "Zen", "brave": "Brave",
                 "edge": "Edge", "chromium": "Chromium", "opera": "Opera", "vivaldi": "Vivaldi"}


# Every download's progress notes and errors also go to a log file (see start_log_file), so a failure can be
# looked into after the fact. Nothing is printed when no file is set up (the CLI and tests).
log = logging.getLogger("grab")
log.addHandler(logging.NullHandler())


def start_log_file(path: Path | None = None) -> Path:
    """Log to <state dir>/logs/grab.log, kept small: 1 MB, plus two older files."""
    from logging.handlers import RotatingFileHandler
    path = Path(path or state_dir() / "logs" / "grab.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    for h in [h for h in log.handlers if isinstance(h, RotatingFileHandler)]:
        log.removeHandler(h)
        h.close()
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return path


def state_dir() -> Path:
    """Where Grab keeps history, settings and shared logins."""
    if os.environ.get("GRAB_STATE_DIR"):  # tests / portable setups
        return Path(os.environ["GRAB_STATE_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Grab"
    return Path.home() / ".config" / "grab"


# ---- logins shared by the browser extension (no access to the browser's own files needed) ----

def shared_cookies_path() -> Path:
    return state_dir() / "logins" / "extension-cookies.txt"


def shared_logins_info() -> dict:
    try:
        return json.loads((state_dir() / "logins" / "extension-cookies.json").read_text())
    except (OSError, ValueError):
        return {}


def save_shared_cookies(cookies: list[dict], sites: list[str], browser: str = "") -> int:
    """Write the extension's cookies as a private Netscape cookies.txt (what yt-dlp / gallery-dl read)."""
    d = state_dir() / "logins"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    lines = ["# Netscape HTTP Cookie File", "# Shared by the Grab browser extension. Private: readable by your user only.", ""]
    n = 0
    for c in cookies:
        name, value, domain = str(c.get("name", "")), str(c.get("value", "")), str(c.get("domain", ""))
        if not name or not domain or any(ch in name + value + domain for ch in "\t\n\r"):
            continue
        host_only = bool(c.get("hostOnly"))
        dom = domain.lstrip(".") if host_only else ("." + domain.lstrip("."))
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        exp = int(c.get("expirationDate") or 0)
        lines.append("\t".join([prefix + dom, "FALSE" if host_only else "TRUE", str(c.get("path") or "/"),
                                "TRUE" if c.get("secure") else "FALSE", str(exp), name, value]))
        n += 1
    tmp = d / "extension-cookies.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, shared_cookies_path())
    meta = {"sites": sorted(set(sites)), "cookies": n, "browser": browser, "updated": time.time()}
    (d / "extension-cookies.json").write_text(json.dumps(meta))
    return n


def clear_shared_cookies():
    for name in ("extension-cookies.txt", "extension-cookies.json"):
        (state_dir() / "logins" / name).unlink(missing_ok=True)


def resolve_login(o: "Options") -> tuple[str | None, tuple | None]:
    """(cookies file, browser) to use: your cookies.txt > a browser Grab can read > logins shared by the extension."""
    shared = str(shared_cookies_path()) if shared_cookies_path().is_file() else None
    if o.cookies_file:
        return o.cookies_file, None
    if o.cookies_browser == "extension":
        return shared, None
    if o.cookies_browser and not (browser_data_blocked(o.cookies_browser) and shared):
        return None, cookie_source(o.cookies_browser)
    return shared, None  # nothing chosen (or macOS blocks that browser): use what the extension shared, if anything


@contextlib.contextmanager
def login_for(o: "Options"):
    """Yields (private temp copy of the cookies file, browser spec). The copy is deleted afterwards
    (yt-dlp and gallery-dl write cookies back to their file, so parallel downloads each get their own)."""
    import tempfile
    src, browser = resolve_login(o)
    tmp = None
    if src and os.path.isfile(src):
        d = state_dir() / "logins"
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="job-", suffix=".txt", dir=d)
        os.close(fd)
        os.chmod(tmp, 0o600)
        shutil.copyfile(src, tmp)
    try:
        yield tmp, browser
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def installed_browsers() -> list[str]:
    roots = [Path("/Applications"), Path("/System/Applications"), Path("/System/Volumes/Preboot/Cryptexes/App/System/Applications"),
             Path.home() / "Applications"]
    return [b for b, apps in BROWSER_APPS.items() if any((r / f"{a}.app").exists() for r in roots for a in apps)]


def _zen_profile() -> str | None:
    """Zen is Firefox-based: yt-dlp/gallery-dl read it as 'firefox' pointed at Zen's profile folder."""
    import configparser
    base = Path.home() / "Library/Application Support/zen"
    ini = configparser.ConfigParser()
    try:
        ini.read(base / "profiles.ini")
    except configparser.Error:
        pass
    for section in ini.sections():  # [Install...] Default= is the profile Zen actually opens
        if section.startswith("Install") and ini[section].get("Default"):
            return str(base / ini[section]["Default"])
    profiles = sorted(base.glob("Profiles/*/cookies.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(profiles[0].parent) if profiles else None


def cookie_source(browser: str) -> tuple | None:
    """yt-dlp's cookiesfrombrowser tuple: (browser, profile, keyring, container)."""
    if not browser:
        return None
    if browser == "zen":
        prof = _zen_profile()
        return ("firefox", prof, None, None) if prof else ("firefox", None, None, None)
    return (browser, None, None, None)


def cookie_arg(browser: str) -> str:
    """The same, as gallery-dl's --cookies-from-browser value."""
    src = cookie_source(browser)
    return f"{src[0]}:{src[1]}" if src and src[1] else (src[0] if src else "")


_LOGIN_HINTS = ("sign in", "log in", "login", "logged-in", "authenticat", "cookies", "private", "members-only",
                "confirm your age", "not be found", "blocked by network security", "unavailable", "authoriz", "401", "403")


def needs_login(msg: str) -> bool:
    """Would picking a logged-in browser probably fix this error?"""
    m = (msg or "").lower()
    if classify(m) == "gone" and "instagram" not in m:
        return False
    return any(k in m for k in _LOGIN_HINTS) and not cookie_problem(m)


BROWSER_DATA = {  # where each browser keeps its profiles
    "chrome": "Google/Chrome", "brave": "BraveSoftware/Brave-Browser", "edge": "Microsoft Edge", "chromium": "Chromium",
    "opera": "com.operasoftware.Opera", "vivaldi": "Vivaldi", "firefox": "Firefox", "zen": "zen",
}
FULL_DISK_ACCESS = ("open System Settings → Privacy & Security → Full Disk Access, switch on Grab "
                    "(use + to add it from your Applications folder if it isn't listed), then press Retry")


def browser_data_blocked(browser: str) -> bool:
    """Is macOS keeping Grab out of this browser's data folder (privacy protection)?"""
    d = BROWSER_DATA.get(browser)
    if not d:
        return False
    path = Path.home() / "Library/Application Support" / d
    try:
        os.listdir(path)
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def cookie_problem(msg: str) -> str:
    """Explain a failure to *read* the chosen browser's cookies (macOS permissions etc.), or ''."""
    m = (msg or "").lower()
    if "safari" in m and ("operation not permitted" in m or "permission" in m or "binarycookies" in m):
        return ("Safari keeps its cookies private: open System Settings → Privacy & Security → Full Disk Access and turn on "
                "Grab (and Terminal, for the command line), or pick another browser you're logged in with.")
    if "keyring" in m or "safe storage" in m or "decrypt" in m or "keychain" in m:
        return ("macOS asks once before Grab may read this browser's logins (a Keychain prompt). Press Retry and choose "
                "'Always Allow'.")
    if "operation not permitted" in m or (("could not find" in m) and "cookie" in m and
                                          any(browser_data_blocked(b) for b in BROWSER_DATA if b in m)):
        return "macOS is protecting that browser's data from other apps. To let Grab use your login, " + FULL_DISK_ACCESS + "."
    if ("could not find" in m or "no such file" in m) and "cookie" in m:
        return "Grab couldn't find that browser's data. Open it once and log in to the site, then retry."
    return ""

# Links ending in these go straight to aria2c instead of yt-dlp / gallery-dl.
DIRECT_EXTS = {
    "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "dmg", "pkg", "exe", "msi",
    "apk", "apks", "xapk", "ipa", "deb", "rpm", "appimage", "pdf", "epub", "mobi", "cbz", "cbr",
    "torrent", "bin", "dat", "img", "raw", "csv", "json", "txt", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "psd", "ai", "blend", "stl", "obj", "fbx", "glb", "safetensors", "ckpt", "gguf",
    "jpg", "jpeg", "png", "gif", "webp", "avif", "heic", "bmp", "tif", "tiff", "svg",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _bin(name: str) -> str | None:
    """Find a tool next to the running interpreter first (the venv), then on PATH."""
    local = Path(sys.executable).parent / name
    if local.exists():
        return str(local)
    return shutil.which(name)


ARIA2C = _bin("aria2c")
GALLERY_DL = _bin("gallery-dl")
FFMPEG = _bin("ffmpeg")


@dataclasses.dataclass
class Options:
    out_dir: str = str(Path.home() / "Downloads" / "grab")
    mode: str = "auto"
    quality: str = "best"
    container: str = "mp4"          # mp4 | mkv | webm
    audio_format: str = "m4a"       # m4a | mp3 | opus | flac | wav
    subtitles: bool = False
    cookies_browser: str = ""       # load login cookies from this browser
    cookies_file: str = ""          # or from a Netscape cookies.txt
    referer: str = ""
    headers: dict = dataclasses.field(default_factory=dict)
    fragments: int = 8              # parallel HLS/DASH fragment downloads per video
    aria2: bool = True              # use aria2c (multi-connection) for direct file downloads
    aria2_streams: bool = False     # also hand yt-dlp's plain-HTTP video streams to aria2c (no live %)
    aria2_conns: int = 16
    archive: bool = True            # skip things already downloaded
    expand_playlists: bool = True   # split playlists/channels into parallel jobs
    items: str = ""                 # only these playlist/gallery items, e.g. "1-20" or "1,4,9-12"
    smart: bool = True              # on errors, automatically retry with other download strategies
    folders: str = "site"           # "site": Youtube/, Reddit/, ... sub-folders  |  "none": everything in out_dir
    title: str = ""                 # name to use when the link itself has none (raw streams from the browser extension)
    media: str = "all"              # profiles/galleries: "all" | "images" (photos only) | "videos" (videos only)
    scan: int = 0                   # only look at the newest N entries of each playlist/profile level (0 = all)
    watch: str = ""                 # id of the watched source this download came from (see watch.py)
    compat: bool = True             # best quality, but as H.264/HEVC MP4 that plays everywhere (AV1/VP9 get converted)
    clip: str = ""                  # only this part, e.g. "1:20-2:45" (end may be empty = to the end)
    sponsorblock: bool = True       # YouTube: cut sponsor segments, self-promotion and "subscribe" reminders
    chapters: bool = True           # keep chapter markers
    thumbnail: bool = True          # embed the thumbnail as cover art
    sub_langs: str = ""             # subtitles to embed: "en", "en,es", "all" ("" = none)
    polite: bool = True             # Instagram, X, TikTok...: one download at a time per site, with random pauses
    rate_limit: str = ""            # e.g. "5M" per job
    subfolder: str = ""             # set for playlist children

    @classmethod
    def from_dict(cls, d: dict) -> "Options":
        fields = {f.name: f for f in dataclasses.fields(cls)}
        kw = {}
        for k, v in (d or {}).items():
            if k not in fields or v is None:
                continue
            t = fields[k].type
            if t == "int":
                v = int(v)
            elif t == "bool":
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            kw[k] = v
        return cls(**kw)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


_ids = itertools.count(1)


@dataclasses.dataclass
class Job:
    url: str
    opts: Options
    id: int = dataclasses.field(default_factory=lambda: next(_ids))
    parent: int | None = None
    status: str = "queued"          # queued | running | paused | done | skipped | error | cancelled
    stage: str = ""
    tool: str = ""
    title: str = ""
    site: str = ""
    thumbnail: str = ""             # remote URL, or a local image path (galleries)
    uploader: str = ""
    duration: float | None = None
    size: int | None = None         # bytes on disk once finished
    children: int = 0               # playlist parents: number of items queued
    strategy: str = ""              # fallback that made it work, e.g. "fewer connections"
    force: bool = False             # "Start now": ignore the download window
    auto_paused: bool = False       # paused because the download window closed (resumes when it opens)
    proxy: str = ""                 # speed-limiter proxy this download goes through
    min_height: int | None = None   # fallback strategies must not quietly drop below this quality
    created: float = dataclasses.field(default_factory=time.time)
    pause_requested: bool = False
    progress: float | None = None
    downloaded: int = 0
    total: int | None = None
    speed: float | None = None
    eta: int | None = None
    files: list = dataclasses.field(default_factory=list)
    error: str = ""
    log: collections.deque = dataclasses.field(default_factory=lambda: collections.deque(maxlen=60))
    started: float | None = None
    finished: float | None = None
    cancel_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None

    def __post_init__(self):
        if not self.site:
            self.site = urllib.parse.urlparse(self.url).netloc.removeprefix("www.").removeprefix("m.")

    def snapshot(self) -> dict:
        return {
            "id": self.id, "url": self.url, "parent": self.parent, "status": self.status,
            "stage": self.stage, "tool": self.tool, "title": self.title or self.url, "has_title": bool(self.title),
            "site": self.site, "thumbnail": self.thumbnail, "uploader": self.uploader,
            "duration": self.duration, "size": self.size, "children": self.children, "strategy": self.strategy,
            "force": self.force, "auto_paused": self.auto_paused,
            "progress": self.progress, "downloaded": self.downloaded, "total": self.total,
            "speed": self.speed, "eta": self.eta, "files": list(self.files),
            "error": self.error, "log": list(self.log)[-12:], "created": self.created,
            "finished": self.finished, "mode": self.opts.mode, "opts_watch": self.opts.watch,
            "cookies_browser": self.opts.cookies_browser,
            "needs_login": self.status == "error" and needs_login(self.error),
            "cookie_problem": cookie_problem(self.error) if self.status == "error" else "",
            "elapsed": ((self.finished or time.time()) - self.started) if self.started else None,
        }

    # persisted between app launches
    _KEEP = ("id", "url", "parent", "status", "stage", "tool", "title", "site", "thumbnail", "uploader",
             "duration", "size", "children", "strategy", "force", "auto_paused", "progress", "files", "error", "created",
             "started", "finished")

    def to_record(self) -> dict:
        rec = {k: getattr(self, k) for k in self._KEEP}
        rec["opts"] = self.opts.to_dict()
        if self.status == "error":
            rec["log"] = list(self.log)  # so "Copy details" still works after a restart
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> "Job":
        job = cls(url=rec["url"], opts=Options.from_dict(rec.get("opts") or {}), id=rec["id"])
        for k in cls._KEEP:
            if k in rec and k not in ("id", "url"):
                setattr(job, k, rec[k])
        job.log.extend(str(x) for x in rec.get("log") or [])
        if job.status in ("queued", "running", "paused"):
            job.status, job.stage = "paused", "paused (app was closed)"
        return job

    def reset(self):
        self.status, self.stage, self.error = "queued", "", ""
        self.progress, self.speed, self.eta, self.downloaded, self.total = None, None, None, 0, None
        self.files, self.size, self.started, self.finished, self.strategy = [], None, None, None, ""
        self.cancel_event, self.pause_requested, self.proc = threading.Event(), False, None

    def note(self, msg: str):
        self.log.append(msg)
        log.info("#%d %s", self.id, msg)


class Manager:
    """Queue of jobs with a configurable number of parallel workers."""

    HISTORY_LIMIT = 1000

    def __init__(self, workers: int = 4, state_file: str | None = None):
        self.workers = max(1, workers)
        self.jobs: dict[int, Job] = {}
        self.queue: collections.deque[int] = collections.deque()
        self.running: set[int] = set()
        self.cv = threading.Condition()
        self.state_file = Path(state_file) if state_file else None
        self.learned: dict[str, str] = {}   # site -> strategy that fixed errors there last time
        self.hold = False                   # True while the updater installs: queued jobs wait
        self.window_open = True             # False outside the download window (see schedule.py)
        self.proxy: str | None = None       # total-speed-limiter proxy, when a cap is configured
        self.polite_ready: dict[str, float] = {}  # site group -> earliest time its next download may start
        self.outdated_hook = None           # called with a job id when an error suggests yt-dlp is outdated
        if self.state_file:
            self._load()
            threading.Thread(target=self._autosave, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()

    # ---- public API -------------------------------------------------------
    def add(self, urls, opts: Options, parent: int | None = None, front: bool = False,
            titles: dict[str, str] | None = None) -> list[Job]:
        """titles: names to use for links that don't carry their own (e.g. podcast feed items)."""
        new = []
        for u in parse_urls(urls):
            job = Job(url=u, opts=dataclasses.replace(opts, title=(titles or {}).get(u) or opts.title), parent=parent)
            new.append(job)
        with self.cv:
            for job in new:
                self.jobs[job.id] = job
            ids = [j.id for j in new]
            if front:
                self.queue.extendleft(reversed(ids))
            else:
                self.queue.extend(ids)
            self.cv.notify_all()
        return new

    def add_group(self, url: str, title: str, batches: list[tuple[list[str], Options, dict[str, str]]],
                  thumbnail: str = "") -> Job:
        """One row in the list (e.g. "23 files from <page>") with its downloads underneath.
        batches: (links, options, names) - links that share options, and the file name for each."""
        parent = Job(url=url, opts=batches[0][1], title=title, status="done", progress=100.0, thumbnail=thumbnail)
        with self.cv:
            self.jobs[parent.id] = parent
        kids = [k for urls, o, names in batches if urls for k in self.add(urls, o, parent=parent.id, titles=names)]
        parent.children, parent.stage, parent.finished = len(kids), f"page · {len(kids)} files", time.time()
        return parent

    def set_workers(self, n: int):
        with self.cv:
            self.workers = max(1, min(32, int(n)))
            self.cv.notify_all()

    def cancel(self, job_id: int | None = None):
        with self.cv:
            targets = [self.jobs[job_id]] if job_id in self.jobs else list(self.jobs.values()) if job_id is None else []
            for job in targets:
                if job.status == "queued":
                    job.status = "cancelled"
                    if job.id in self.queue:
                        self.queue.remove(job.id)
                elif job.status == "paused":
                    job.status, job.stage = "cancelled", ""
                elif job.status == "running":
                    job.cancel_event.set()
                    if job.proc and job.proc.poll() is None:
                        job.proc.terminate()
            self.cv.notify_all()

    def pause(self, job_id: int | None = None, auto: bool = False):
        """Stop a job but keep its partial download so resume continues where it left off."""
        with self.cv:
            targets = [self.jobs[job_id]] if job_id in self.jobs else list(self.jobs.values()) if job_id is None else []
            for job in targets:
                if job.status in ("queued", "running"):
                    job.auto_paused = auto
                if job.status == "queued":
                    job.status, job.stage = "paused", "paused"
                    if job.id in self.queue:
                        self.queue.remove(job.id)
                elif job.status == "running":
                    job.pause_requested = True
                    job.cancel_event.set()
                    if job.proc and job.proc.poll() is None:
                        job.proc.terminate()
            self.cv.notify_all()

    def resume(self, job_id: int | None = None, statuses=("paused",)):
        with self.cv:
            targets = [self.jobs[job_id]] if job_id in self.jobs else list(self.jobs.values()) if job_id is None else []
            for job in targets:
                if job.status in statuses and job.id not in self.running:
                    job.reset()
                    self.queue.append(job.id)
            self.cv.notify_all()

    def start_now(self, job_id: int | None = None):
        """Ignore the download window for this job (or all waiting jobs)."""
        with self.cv:
            targets = [self.jobs[job_id]] if job_id in self.jobs else list(self.jobs.values()) if job_id is None else []
            for job in targets:
                if job.status in ("queued", "paused"):
                    job.force, job.auto_paused = True, False
                    if job.status == "queued":
                        job.stage = ""
                        if job.id in self.queue:
                            self.queue.remove(job.id)
                        self.queue.appendleft(job.id)
            self.cv.notify_all()
        for job in targets:
            if job.status == "paused":
                self.resume(job.id)

    def retry(self, job_id: int | None = None):
        self.resume(job_id, statuses=("error", "cancelled", "paused"))

    def remove(self, job_id: int):
        self.cancel(job_id)
        with self.cv:
            for jid in [job_id] + [j.id for j in self.jobs.values() if j.parent == job_id and j.status != "running"]:
                if jid in self.queue:
                    self.queue.remove(jid)
                self.jobs.pop(jid, None)

    def clear_finished(self, failed: bool = False) -> int:
        """Take finished jobs off the list (their files stay where they are). failed=True ("Clear all")
        clears failed ones too. Queued, downloading and paused jobs stay, and so do playlists still downloading."""
        gone = ("done", "skipped", "cancelled") + (("error",) if failed else ())
        with self.cv:
            busy = {j.parent for j in self.jobs.values() if j.status not in gone}
            ids = [j.id for j in self.jobs.values() if j.status in gone and j.id not in busy]
            for jid in ids:
                del self.jobs[jid]
        try:
            self.save()  # gone for good, even if Grab quits right away
        except OSError:
            pass
        return len(ids)

    def has_url(self, url: str) -> bool:
        """Is this link already queued, downloading or downloaded? (Failed and cancelled ones don't count.)"""
        with self.cv:
            return any(j.url == url and j.status not in ("error", "cancelled") for j in self.jobs.values())

    def release_hold(self):
        with self.cv:
            self.hold = False
            self.cv.notify_all()

    def learn(self, site: str, strategy: str | None):
        with self.cv:
            if strategy:
                self.learned[site] = strategy
            else:
                self.learned.pop(site, None)

    def forget(self):
        with self.cv:
            self.learned.clear()

    def get(self, job_id) -> Job | None:
        with self.cv:
            return self.jobs.get(job_id)

    def snapshot(self) -> list[dict]:
        with self.cv:
            jobs = list(self.jobs.values())
        return [j.snapshot() for j in jobs]

    def idle(self) -> bool:
        with self.cv:
            waiting = any(j.status == "paused" and j.auto_paused for j in self.jobs.values())
            return not self.queue and not self.running and not waiting

    def wait(self, poll=0.3, on_tick=None):
        while not self.idle():
            if on_tick:
                on_tick()
            time.sleep(poll)
        if on_tick:
            on_tick()

    # ---- persistence --------------------------------------------------------
    def _load(self):
        global _ids
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self.learned = dict(data.get("learned") or {})
        for rec in data.get("jobs", [])[-self.HISTORY_LIMIT:]:
            try:
                job = Job.from_record(rec)
            except Exception:  # noqa: BLE001 - skip unreadable history rows
                continue
            self.jobs[job.id] = job
        if self.jobs:
            _ids = itertools.count(max(self.jobs) + 1)

    def save(self):
        if not self.state_file:
            return
        with self.cv:
            recs = [j.to_record() for j in self.jobs.values()][-self.HISTORY_LIMIT:]
        tmp = self.state_file.with_suffix(".tmp")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"version": 1, "jobs": recs, "learned": self.learned}))
        tmp.replace(self.state_file)

    def _autosave(self):
        last = None
        while True:
            time.sleep(2)
            with self.cv:
                sig = (tuple((j.id, j.status, len(j.files), j.title, j.thumbnail) for j in self.jobs.values()),
                       tuple(sorted(self.learned.items())))
            if sig != last:
                try:
                    self.save()
                    last = sig
                except OSError:
                    pass

    # ---- internals --------------------------------------------------------
    def _next(self) -> int | None:
        """First queued job allowed to start now (download window, update hold, worker limit, polite mode)."""
        if self.hold or len(self.running) >= self.workers:
            return None
        now = time.time()
        busy = {polite_group(self.jobs[r].url) for r in self.running if r in self.jobs}
        for jid in list(self.queue):
            job = self.jobs.get(jid)
            if job is None or job.status != "queued":
                self.queue.remove(jid)
                continue
            if not (self.window_open or job.force):
                continue
            group = polite_group(job.url) if job.opts.polite else None
            if group and (group in busy or now < self.polite_ready.get(group, 0)):
                job.stage = f"polite mode: one {group} download at a time"
                continue
            if job.stage.startswith("polite mode"):
                job.stage = ""
            return jid
        return None

    def _scheduler(self):
        while True:
            with self.cv:
                while (jid := self._next()) is None:
                    self.cv.wait(timeout=2)
                self.queue.remove(jid)
                job = self.jobs[jid]
                job.proxy = self.proxy or ""
                job.status = "running"
                job.started = time.time()
                self.running.add(job.id)
            threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job: Job):
        o = job.opts
        log.info("#%d start %s (mode %s, quality %s, login %s%s)", job.id, job.url, o.mode, o.quality,
                 o.cookies_browser or ("cookies.txt" if o.cookies_file else "none"), f", from {o.referer}" if o.referer else "")
        try:
            run_job(job, self)
        except DownloadCancelled:
            job.status = "cancelled"
        except Exception as e:  # noqa: BLE001 - surface everything to the UI
            job.status = "error"
            job.error = job.error or str(e)
            job.stage = ""
            log.error("#%d failed: %s", job.id, job.error, exc_info=True)
            if self.outdated_hook:
                from .updater import looks_outdated
                if looks_outdated(job.error + " " + " ".join(job.log)):
                    self.outdated_hook(job.id)
        finally:
            if job.cancel_event.is_set() and job.status != "done":
                job.status = "paused" if job.pause_requested else "cancelled"
                job.stage = "paused" if job.pause_requested else ""
            job.finished = time.time()
            job.speed = None
            job.eta = None
            job.proc = None
            group = polite_group(job.url) if job.opts.polite else None
            if group:
                self.polite_ready[group] = job.finished + random.uniform(*POLITE_GAP)
            sizes = [os.path.getsize(f) for f in job.files if os.path.isfile(f)]
            job.size = sum(sizes) if sizes else job.size
            log.info("#%d %s%s%s", job.id, job.status, f" with {job.strategy}" if job.strategy else "",
                     f": {', '.join(job.files)}" if job.status == "done"
                     else f": {job.error.splitlines()[0] if job.error else ''}" if job.status == "error" else "")
            with self.cv:
                self.running.discard(job.id)
                self.cv.notify_all()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def parse_urls(text) -> list[str]:
    """Accept a string or list; one URL per line, '#' comments allowed, de-duplicated."""
    if isinstance(text, str):
        text = text.splitlines()
    seen, out = set(), []
    for line in text:
        for part in str(line).split():
            part = part.strip().strip("<>\"'")
            if not part or part.startswith("#"):
                break
            if not re.match(r"^[a-z][a-z0-9+.-]*://", part, re.I):
                if "." in part and "/" in part:
                    part = "https://" + part
                else:
                    continue
            host = urllib.parse.urlparse(part).hostname or ""
            if not ("." in host or host == "localhost" or ":" in host):
                continue
            part = unwrap_url(part)
            if part not in seen:
                seen.add(part)
                out.append(part)
    return out


# Pages that just wrap or redirect to another link: host -> query parameters that hold the real URL.
_WRAPPERS = {
    "reddit.com": ("url",),            # reddit.com/media?url=https://i.redd.it/x.jpeg (Reddit's image viewer)
    "out.reddit.com": ("url",),
    "google.com": ("q", "url"),        # google.com/url?q=...
    "l.facebook.com": ("u",),
    "lm.facebook.com": ("u",),
    "l.instagram.com": ("u",),
    "youtube.com": ("q",),             # youtube.com/redirect?q=...
    "steamcommunity.com": ("url",),    # /linkfilter/?url=...
    "t.umblr.com": ("z",),
    "href.li": (),
}
_WRAPPER_PATHS = ("/media", "/url", "/l.php", "/redirect", "/linkfilter", "/")


def unwrap_url(url: str, _depth: int = 0) -> str:
    """Turn wrapper/redirect/preview links into the link that actually has the media.

    reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fabc.jpeg  ->  https://i.redd.it/abc.jpeg
    preview.redd.it/abc.jpg?width=640&s=...                 ->  https://i.redd.it/abc.jpg   (full size)
    pbs.twimg.com/media/abc?format=jpg&name=small           ->  ...&name=orig              (full size)
    google.com/url?q=..., l.facebook.com/l.php?u=..., youtube.com/redirect?q=... -> the target
    """
    if _depth > 3:
        return url
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("old.").removeprefix("new.")
    q = urllib.parse.parse_qs(p.query)
    if host == "href.li" and p.query.startswith("http"):
        return unwrap_url(urllib.parse.unquote(p.query), _depth + 1)
    keys = _WRAPPERS.get(host) or (_WRAPPERS.get("google.com") if host.startswith("google.") else None)
    if keys and (p.path.rstrip("/") + "/").startswith(tuple(w.rstrip("/") + "/" for w in _WRAPPER_PATHS)):
        if host == "reddit.com" and not p.path.startswith("/media"):
            keys = ()
        if host == "youtube.com" and not p.path.startswith("/redirect"):
            keys = ()
        for k in keys:
            target = (q.get(k) or [""])[0]
            if re.match(r"^https?://", target):
                return unwrap_url(target, _depth + 1)
    if host == "preview.redd.it" and re.search(r"\.(jpe?g|png|gif|webp)$", p.path, re.I):
        name = p.path.rsplit("/", 1)[-1].split("-v0-")[-1]  # "some-title-v0-abc123.jpeg" -> "abc123.jpeg"
        return f"https://i.redd.it/{name}"
    if host == "pbs.twimg.com" and p.path.startswith("/media/") and "name" in q:
        q["name"] = ["orig"]
        return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode({k: v[0] for k, v in q.items()})))
    return url


# Hosts (including their CDNs) -> the folder name used for that site, so direct links like
# i.redd.it/x.jpg land next to Reddit videos instead of in a generic folder.
SITE_FOLDERS = {
    "reddit.com": "Reddit", "redd.it": "Reddit", "redditmedia.com": "Reddit", "redditstatic.com": "Reddit",
    "x.com": "Twitter", "twitter.com": "Twitter", "twimg.com": "Twitter",
    "youtube.com": "Youtube", "youtu.be": "Youtube", "googlevideo.com": "Youtube", "ytimg.com": "Youtube",
    "instagram.com": "Instagram", "cdninstagram.com": "Instagram", "facebook.com": "Facebook", "fbcdn.net": "Facebook",
    "imgur.com": "Imgur", "tiktok.com": "TikTok", "tiktokcdn.com": "TikTok", "vimeo.com": "Vimeo", "vimeocdn.com": "Vimeo",
    "twitch.tv": "Twitch", "flickr.com": "Flickr", "staticflickr.com": "Flickr", "tumblr.com": "Tumblr",
    "pinterest.com": "Pinterest", "pinimg.com": "Pinterest", "discordapp.com": "Discord", "discordapp.net": "Discord",
    "bsky.app": "Bluesky", "bsky.network": "Bluesky", "dailymotion.com": "DailyMotion", "soundcloud.com": "Soundcloud",
}
_NOT_A_SITE = {"Generic", "HTML5MediaEmbed", "HTML5MediaEmbedIE", "GenericIE"}


def site_folder(url: str, extractor_key: str | None = None) -> str:
    """Folder name for a download: yt-dlp's site name when it has one, else derived from the link's host."""
    if extractor_key and extractor_key not in _NOT_A_SITE:
        return extractor_key.replace("Tab", "")
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for domain, name in SITE_FOLDERS.items():
        if host == domain or host.endswith("." + domain):
            return name
    if not host or re.fullmatch(r"[\d.]+|.*:.*", host) or "." not in host:  # IP address / localhost
        return host or "Other"
    labels = host.removeprefix("www.").split(".")
    # keep "bbc.co.uk"-style names whole, otherwise the main domain: cdn.kernel.org -> kernel.org
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "ac", "gov") else 2
    return ".".join(labels[-keep:]) or "Other"


def target_dir(o: "Options", url: str, extractor_key: str | None = None) -> str:
    if o.folders == "none":
        return o.out_dir
    if o.referer and (extractor_key is None or extractor_key in _NOT_A_SITE):
        url = o.referer  # a raw stream from cdn.example.net belongs with the page it played on
    return os.path.join(o.out_dir, sanitize_filename(site_folder(url, extractor_key)))


def _hint_name(o: "Options", url: str) -> str | None:
    """'Page title [ab12cd].%(ext)s' for raw streams whose own name is useless (master.m3u8, index...)."""
    if not o.title:
        return None
    import hashlib
    title = sanitize_filename(o.title).encode()[:150].decode(errors="ignore")  # bytes: macOS allows 255 per name
    # "%" would be read as a template field by yt-dlp
    return f"{title.replace('%', '%%')} [{hashlib.sha1(url.encode()).hexdigest()[:6]}]{_clip_suffix(o)}.%(ext)s"


def batch_names(items: list[tuple[str, str]]) -> dict[str, str]:
    """File names for links downloaded together ([(url, "image"|"video")]): the link's own name, with an
    extension when it has none (X: ...?format=jpg), and "name (2).jpg" when two would clash."""
    names, seen = {}, set()
    for url, kind in items:
        u = urllib.parse.urlparse(url)
        base = sanitize_filename(urllib.parse.unquote(u.path.rsplit("/", 1)[-1])) or "file"
        stem, dot, ext = base.rpartition(".")
        if not dot or not re.fullmatch(r"[A-Za-z0-9]{2,5}", ext):
            fmt = (urllib.parse.parse_qs(u.query).get("format") or [""])[0].lower()
            stem, ext = base, fmt if re.fullmatch(r"[a-z0-9]{2,5}", fmt) else ("mp4" if kind == "video" else "jpg")
        stem = stem.encode()[:120].decode(errors="ignore") or "file"
        name, n = f"{stem}.{ext}", 2
        while name.lower() in seen:
            name, n = f"{stem} ({n}).{ext}", n + 1
        seen.add(name.lower())
        names[url] = name
    return names


def name_pattern(o: "Options") -> str:
    return f"%(title).150B [%(id)s]{_clip_suffix(o)}.%(ext)s"


def _sec(t: str) -> float | None:
    t = (t or "").strip()
    if not t:
        return None
    parts = [float(x) for x in t.split(":")]
    if len(parts) > 3 or any(p < 0 for p in parts):
        raise ValueError(f"bad time {t!r}")
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def parse_clip(spec: str) -> tuple[str, str] | None:
    """'1:20-2:45' -> ('80', '165');  '90-' -> ('90', 'inf');  '' -> None."""
    spec = (spec or "").replace(" ", "").replace("–", "-")
    if not spec:
        return None
    a, _, b = spec.partition("-")
    start, end = _sec(a) or 0, _sec(b)
    if end is not None and end <= start:
        raise ValueError("the clip must end after it starts")
    return (f"{start:g}", "inf" if end is None else f"{end:g}")


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _clip_suffix(o: "Options") -> str:
    try:
        c = parse_clip(o.clip)
    except ValueError:
        return ""
    return f" (clip {_fmt_time(float(c[0]))}-{'end' if c[1] == 'inf' else _fmt_time(float(c[1]))})" if c else ""


def _sub_langs(spec: str) -> str:
    """'en, es' -> 'en.*,es.*' ; 'all' -> every language except live chat."""
    spec = (spec or "").replace(" ", "")
    if spec.lower() == "all":
        return "all,-live_chat"
    return ",".join(f"{l}.*" if "*" not in l and "-" not in l else l for l in spec.split(",") if l)


# Links to a whole account / feed, where gallery-dl (all posts: photos + videos) beats yt-dlp.
_PROFILE_PATTERNS = [
    (r"(^|\.)instagram\.com$", r"^/(?!p/|reel/|reels/|tv/|stories/|explore/)[^/]+/?(tagged/|reels/|saved/)?$"),
    (r"(^|\.)instagram\.com$", r"^/stories/"),
    (r"(^|\.)reddit\.com$", r"^/(r|u|user)/[^/]+/?((hot|new|top|rising|controversial|submitted|gilded)/?)?$"),
    (r"(^|\.)(x|twitter)\.com$", r"^/(?!i/|home|search|explore|settings|messages|notifications)[^/]+/?(media|likes|with_replies|highlights)?/?$"),
    (r"^bsky\.app$", r"^/profile/[^/]+/?(media|likes)?/?$"),
    (r"(^|\.)(tumblr\.com|pinterest\.[a-z.]+|deviantart\.com|flickr\.com|artstation\.com|pixiv\.net|behance\.net|"
     r"imgur\.com|danbooru\.donmai\.us|gelbooru\.com|furaffinity\.net|newgrounds\.com|kemono\.(su|party|cr)|"
     r"coomer\.(su|party|st)|redgifs\.com|vsco\.co|threads\.net|mastodon\.social|weibo\.com)$", r""),
]


def prefers_gallery(url: str) -> bool:
    p = urllib.parse.urlsplit(url)
    host, path = (p.hostname or "").lower().removeprefix("www.").removeprefix("old."), p.path or "/"
    return any(re.search(h, host) and re.search(pth, path) for h, pth in _PROFILE_PATTERNS)


# Sites that watch for automated activity on logged-in accounts. Polite mode = one download at a
# time per site, a random gap between downloads, and pauses between requests.
POLITE_SITES = {
    "instagram.com": "Instagram", "cdninstagram.com": "Instagram", "x.com": "X", "twitter.com": "X", "twimg.com": "X",
    "tiktok.com": "TikTok", "tiktokcdn.com": "TikTok", "facebook.com": "Facebook", "fb.watch": "Facebook",
    "reddit.com": "Reddit", "redd.it": "Reddit", "threads.net": "Threads", "threads.com": "Threads",
}
POLITE_GAP = (4.0, 12.0)  # seconds between two downloads from the same site


def polite_group(url: str) -> str | None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return next((name for dom, name in POLITE_SITES.items() if host == dom or host.endswith("." + dom)), None)


def friendly_name(url: str) -> str:
    """Short name for a link: '@nasa (Instagram)', 'r/pics', '@NASA (YouTube)'..."""
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("old.").removeprefix("m.")
    parts = [x for x in p.path.split("/") if x]
    site = site_folder(url)
    if "reddit.com" in host and len(parts) >= 2 and parts[0] in ("r", "u", "user"):
        return f"{'r' if parts[0] == 'r' else 'u'}/{parts[1]}"
    if host == "bsky.app" and len(parts) >= 2:
        return f"@{parts[1]} (Bluesky)"
    if parts and parts[0].startswith("@"):
        return f"{parts[0]} ({'YouTube' if site == 'Youtube' else site})"
    if host.endswith("instagram.com") and parts and parts[0] in ("p", "reel", "reels", "tv"):
        return "Instagram post"
    if host.endswith(("instagram.com", "x.com", "twitter.com", "tiktok.com", "threads.net")) and parts:
        return f"@{parts[0].lstrip('@')} ({site})"
    if site == "Youtube":
        q = urllib.parse.parse_qs(p.query)
        if "list" in q:
            return f"YouTube playlist {q['list'][0][:12]}"
        if len(parts) >= 2 and parts[0] in ("channel", "c", "user"):
            return f"{parts[1]} (YouTube)"
    return f"{parts[-1]} ({site})" if parts else site


def _ext(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def run_job(job: Job, mgr: Manager):
    o = job.opts
    Path(o.out_dir).mkdir(parents=True, exist_ok=True)
    parse_clip(o.clip)  # a bad time range fails right away with a clear message
    real = unwrap_url(job.url)  # also covers retries of links added before unwrapping existed
    if real != job.url:
        job.note(f"[grab] unwrapped link: {job.url} -> {real}")
        job.url, job.site = real, site_key(real)

    if o.mode == "direct" or (o.mode == "auto" and _ext(job.url) in DIRECT_EXTS):
        return run_direct(job, mgr)
    if o.mode == "images":
        return run_gallery_smart(job)
    if o.mode == "auto" and prefers_gallery(job.url):
        # whole profiles / subreddits / boards: gallery-dl gets every post's photos AND videos
        try:
            return run_gallery_smart(job)
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if job.cancel_event.is_set():
                raise
            gdl_first = str(e)
            job.note(f"gallery-dl: {gdl_first} -> trying yt-dlp")
            job.files, job.progress = [], None
            try:
                return run_ytdlp_smart(job, mgr)
            except DownloadCancelled:
                raise
            except Exception as e2:  # noqa: BLE001
                raise RuntimeError(f"gallery-dl: {gdl_first}\nyt-dlp: {_clean_err(e2)}" + _tip(gdl_first + " " + str(e2))) from None

    try:
        return run_ytdlp_smart(job, mgr)
    except DownloadCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        yt_err = _clean_err(e)
        if job.cancel_event.is_set() or o.mode in ("video", "audio") or classify(yt_err) == "gone" or o.clip:
            # (gallery-dl / plain downloads can't cut a clip: better to fail than to quietly fetch everything)
            raise RuntimeError(yt_err + _tip(yt_err)) from None
        job.note(f"yt-dlp: {yt_err}")

    # yt-dlp found no video -> image/gallery post?
    gdl_err = ""
    if GALLERY_DL:
        job.progress = None
        try:
            return run_gallery_smart(job)
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            gdl_err = str(e)
            job.note(f"gallery-dl: {gdl_err}")

    # Last resort: a raw file that isn't a web page.
    ctype = _content_type(job.url, o)
    if ctype and "html" not in ctype:
        return run_direct(job, mgr)

    lines = [f"yt-dlp: {yt_err}"] + ([f"gallery-dl: {gdl_err}"] if gdl_err else [])
    raise RuntimeError("\n".join(lines) + _tip(yt_err + " " + gdl_err))


# ---------------------------------------------------------------------------
# Smart fallback: when a download fails, look at *why* and retry with a strategy that fixes that
# kind of failure. What worked is remembered per site and tried first next time.
# ---------------------------------------------------------------------------

def classify(msg: str) -> str:
    m = msg.lower()
    if "not a bot" in m:  # YouTube's bot check: another player often gets past it, else a login does
        return "bot"
    if any(k in m for k in ("sign in", "login", "log in", "authenticat", "cookies", "private video", "members-only",
                            "age-restricted", "confirm your age", "inappropriate for some users")):
        return "login"
    if any(k in m for k in ("unsupported url", "no video", "no media", "there's no video", "no downloadable")):
        return "novideo"
    if any(k in m for k in ("requested format", "format is not available", "ffmpeg", "postprocessing", "merging",
                            "conversion failed", "invalid data found")):
        return "format"
    if "404" in m or ("not found" in m and "format" not in m) or "does not exist" in m:
        return "gone"
    if any(k in m for k in ("429", "too many requests", "403", "forbidden", "timed out", "timeout", "connection reset",
                            "connection aborted", "remote end closed", "incompleteread", "incomplete read", "fragment",
                            "unable to download video data", "http error 5", "ssl", "temporary failure",
                            "giving up after", "broken pipe", "content too short", "did not get any data", "network")):
        return "network"
    return "other"


def _tip(msg: str) -> str:
    kind, m = classify(msg), msg.lower()
    if "[instagram]" in m and any(k in m for k in ("not found", "not be found", "login", "redirect", "401", "403")):
        return "\nTip: Instagram only shows profiles to logged-in users - choose your browser under 'Logged-in cookies' (you must be logged in to Instagram there)."
    if ("[reddit]" in m and "blocked" in m) or ("[twitter]" in m and ("unavailable" in m or "authoriz" in m or "login" in m)):
        return "\nTip: this site blocks logged-out downloads from your network - choose your browser under 'Logged-in cookies' (you must be logged in to the site there)."
    if kind == "login":
        return "\nTip: this needs you logged in - pick your browser under 'Logged-in cookies' (CLI: -c chrome)."
    if kind == "gone":
        return "\nTip: the page wasn't found (404) - check that the link is complete and still online."
    if "429" in m or "too many requests" in m:
        return "\nTip: the site is rate-limiting you - lower 'Downloads at the same time' and retry in a few minutes."
    if kind == "network":
        return "\nTip: every download strategy hit network errors - check your connection or VPN, then press Retry."
    if re.search(r"\.(jpe?g|png|gif|webp|avif)\b", m):
        return ("\nTip: this looks like an image link. Try the 'Images' or 'Direct file' preset, "
                "or open the image on its own and copy its address.")
    if kind == "novideo" or "unsupported url" in m:
        return ("\nTip: if the video plays in your browser, open DevTools > Network, filter 'm3u8' or 'mpd' or 'mp4', "
                "copy that request URL and add it with this page as Referer (CLI: -r PAGE_URL). "
                "DRM-protected services (Netflix, Prime, Disney+...) can't be downloaded.")
    return ""


def _is_youtube(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host.endswith("youtube.com") or host.endswith("youtu.be")


def site_key(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return "youtube.com" if host.endswith("youtu.be") else host.removeprefix("www.").removeprefix("m.")


def _use_aria2(p: dict, conns: int):
    p["external_downloader"] = {"http": ARIA2C}
    p["external_downloader_args"] = {"aria2c": [f"-x{conns}", f"-s{conns}", "-k1M", "--summary-interval=0",
                                                "--console-log-level=error", "--quiet=true"]}
    p.pop("http_chunk_size", None)


def _use_native(p: dict):
    p.pop("external_downloader", None)
    p.pop("external_downloader_args", None)
    p["http_chunk_size"] = 10 * 1024 * 1024


# name -> (label shown in the UI, how it changes the yt-dlp options)
STRATEGIES = {
    "gentle": ("fewer connections", lambda p: p.update(
        concurrent_fragment_downloads=2, http_chunk_size=2 * 1024 * 1024, retries=20, fragment_retries=20,
        sleep_interval_requests=0.75, socket_timeout=60)),
    "client": ("another YouTube player", lambda p: p.update(
        extractor_args={"youtube": {"player_client": ["tv", "web_safari", "mweb"]}})),
    "aria2": ("aria2c multi-connection", lambda p: (_use_aria2(p, 4), p.update(continuedl=False))),
    "native": ("built-in downloader", lambda p: (_use_native(p), p.update(continuedl=False))),
    "simple": ("simpler format", lambda p: p.update(
        format="b/bv*+ba/best", format_sort=[], continuedl=False)),
}


def ladder(kind: str, job: Job, tried: list[str]) -> list[str]:
    """Strategies worth trying for this kind of error, in order."""
    o = job.opts
    switch_dl = "native" if (o.aria2_streams and ARIA2C) else ("aria2" if ARIA2C else None)
    yt = "client" if _is_youtube(job.url) else None
    order = {
        "network": ["gentle", yt, switch_dl, "simple"],
        "bot": [],  # other YouTube players only offer 360p without a login; the fix is a browser login
        "format": ["simple", yt],
        "other": ["gentle", yt],
    }.get(kind, [])
    return [s for s in order if s and s not in tried]


def run_ytdlp_smart(job: Job, mgr: Manager):
    o, site = job.opts, site_key(job.url)
    start = mgr.learned.get(site) if o.smart else None
    start = start if start in STRATEGIES else None
    tried: list[str] = [start] if start else []
    try:
        run_ytdlp(job, mgr, start)
        if start:
            job.strategy = STRATEGIES[start][0]
        return
    except DownloadCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        err = _clean_err(e)
        if not o.smart or job.cancel_event.is_set():
            raise
    if start:  # what worked last time didn't this time - try the normal way next
        tried.insert(0, "")
    kind = classify(err)
    steps = ([""] if start else []) + ladder(kind, job, tried)
    for i, name in enumerate(steps, 1):
        label = STRATEGIES[name][0] if name else "standard settings"
        job.note(f"[grab] {kind} error: {err} -> retrying with {label}")
        job.stage = f"retrying with {label} ({i}/{len(steps)})"
        job.progress, job.speed, job.eta, job.files = None, None, None, []
        try:
            run_ytdlp(job, mgr, name or None)
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            err = _clean_err(e)
            if job.cancel_event.is_set():
                raise
            new_kind = classify(err)
            if new_kind in ("login", "gone", "novideo"):
                break
            continue
        job.strategy = label if name else ""
        mgr.learn(site, name or None)
        job.note(f"[grab] {label} worked - will use it first for {site} next time" if name else "[grab] standard settings worked")
        return
    raise RuntimeError(err)


def run_gallery_smart(job: Job):
    try:
        return run_gallery(job)
    except DownloadCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        if not job.opts.smart or job.cancel_event.is_set() or classify(str(e)) != "network":
            raise
        job.note(f"[grab] gallery-dl: {e} -> retrying slower")
        job.stage = "retrying slower"
    run_gallery(job, gentle=True)
    job.strategy = "slower requests"


def _clean_err(e: Exception) -> str:
    msg = str(e).replace("\x1b[0;31m", "").replace("\x1b[0m", "")
    msg = re.sub(r"^ERROR:\s*", "", msg)
    return msg.split("\n")[0][:400]


def _headers(o: Options) -> dict:
    h = {"User-Agent": USER_AGENT}
    h.update(o.headers or {})
    if o.referer:
        h["Referer"] = o.referer
        p = urllib.parse.urlparse(o.referer)
        h.setdefault("Origin", f"{p.scheme}://{p.netloc}")
    return h


def _content_type(url: str, o: Options) -> str:
    try:
        req = urllib.request.Request(url, method="HEAD", headers=_headers(o))
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.headers.get("Content-Type", "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------------

class _Log:
    def __init__(self, job: Job):
        self.job = job

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        if "has already been recorded in the archive" in msg or "has already been downloaded" in msg:
            self.job.stage = "already downloaded"
            self.job.status = "skipped" if self.job.status == "running" else self.job.status
        self.job.note(msg)

    info = debug

    def warning(self, msg):
        self.job.note(f"WARNING: {msg}")

    def error(self, msg):
        self.job.note(msg)


def ytdlp_options(job: Job, strategy: str | None = None, login: tuple = (None, None)) -> dict:
    o = job.opts
    sub = sanitize_filename(o.subfolder) if o.subfolder else ""
    if o.folders == "none":
        sub = ""
    tmpl = os.path.join(target_dir(o, job.url), sub, name_pattern(o))

    if o.mode == "audio":
        fmt = "ba/b"
        sort = []
    else:
        fmt = "bv*+ba/b"
        res = "res" if o.quality == "best" else f"res:{o.quality}"
        if o.compat:
            # highest resolution first; at equal resolution prefer H.264 + AAC (no conversion needed)
            sort = [res, "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
        else:
            sort = [] if o.quality == "best" else [res]
            if o.container == "mp4":
                sort += ["vcodec:h264", "ext:mp4:m4a"] if o.quality != "best" else ["ext:mp4:m4a"]

    # post-processing, built with yt-dlp's own option parser so it behaves exactly like its CLI flags
    args = ["--embed-metadata"]
    if o.mode == "audio":
        args += ["-x", "--audio-format", o.audio_format, "--audio-quality", "0"]
    if o.chapters:
        args += ["--embed-chapters"]
    if o.sponsorblock:
        args += ["--sponsorblock-remove", "sponsor,selfpromo,interaction"]
    if o.thumbnail:
        args += ["--embed-thumbnail"]
    langs = _sub_langs(o.sub_langs or ("en" if o.subtitles else ""))
    if langs and o.mode != "audio":
        args += ["--embed-subs", "--write-subs", "--write-auto-subs", "--sub-langs", langs]
    clip = parse_clip(o.clip)
    if clip:
        args += ["--download-sections", f"*{clip[0]}-{clip[1]}", "--force-keyframes-at-cuts"]
    extra = yt_dlp.parse_options(args).ydl_opts
    pps = extra["postprocessors"]

    params = {
        **{k: extra[k] for k in ("writethumbnail", "writesubtitles", "writeautomaticsub", "subtitleslangs",
                                 "download_ranges", "force_keyframes_at_cuts") if extra.get(k) is not None},
        "outtmpl": {"default": tmpl},
        "format": fmt,
        "format_sort": sort,
        "merge_output_format": ("mp4" if o.compat else o.container) if o.mode != "audio" else None,
        "postprocessors": pps,
        "concurrent_fragment_downloads": max(1, o.fragments),
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "noprogress": True,
        "quiet": False,
        "no_warnings": False,
        "logger": _Log(job),
        "http_headers": _headers(o) if (o.referer or o.headers) else {},
        "noplaylist": False,
        "windowsfilenames": False,
        # no "trim_file_name": yt-dlp counts the whole folder path in it, which cut the [id] off names in
        # deep folders. name_pattern/_hint_name keep titles to 150 bytes instead.
        "progress_hooks": [lambda d: _progress(job, d)],
        "postprocessor_hooks": [lambda d: _pp_progress(job, d)],
    }
    if FFMPEG:
        params["ffmpeg_location"] = FFMPEG
    if job.proxy:
        params["proxy"] = job.proxy
    if o.polite and polite_group(job.url):
        params.update(sleep_interval_requests=1.5, sleep_interval=2, max_sleep_interval=6)
    if o.items:
        params["playlist_items"] = o.items
    if o.archive and not o.clip:  # a clip isn't "the video" - don't mark the whole video as downloaded
        params["download_archive"] = os.path.join(o.out_dir, ".grab-archive.txt")
    if login[1]:
        params["cookiesfrombrowser"] = login[1]
    if login[0]:
        params["cookiefile"] = login[0]
    if o.rate_limit:
        params["ratelimit"] = parse_bytes(o.rate_limit)
    if o.aria2_streams and ARIA2C:
        n = str(max(1, min(16, o.aria2_conns)))
        params["external_downloader"] = {"http": ARIA2C}  # HLS/DASH stay on the native parallel-fragment downloader
        params["external_downloader_args"] = {"aria2c": [
            f"-x{n}", f"-s{n}", "-k1M", "--summary-interval=0", "--console-log-level=error", "--quiet=true",
        ]}
    else:
        params["http_chunk_size"] = 10 * 1024 * 1024  # avoids YouTube throttling on the native downloader
    if strategy:
        STRATEGIES[strategy][1](params)
    return params


def _progress(job: Job, d: dict):
    if job.cancel_event.is_set():
        raise DownloadCancelled()
    info = d.get("info_dict") or {}
    h = info.get("height")
    if job.min_height and h and h < job.min_height and info.get("vcodec") not in (None, "none"):
        raise RuntimeError(f"only {h}p was available this way (wanted at least {job.min_height}p)")
    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        fi, fc = d.get("fragment_index"), d.get("fragment_count")
        if total:
            job.progress = min(100.0, done * 100.0 / total)
        elif fc:
            job.progress = min(100.0, (fi or 0) * 100.0 / fc)
        job.downloaded, job.total = done, total
        job.speed, job.eta = d.get("speed"), d.get("eta")
        vc, ac = info.get("vcodec"), info.get("acodec")
        kind = "audio" if vc == "none" else "video" if ac == "none" else "media"
        proto = (info.get("protocol") or "").split("+")[0]
        tag = {"m3u8_native": "HLS", "m3u8": "HLS", "http_dash_segments": "DASH"}.get(proto, "")
        job.stage = f"downloading {kind}" + (f" ({tag} x{job.opts.fragments})" if tag else "")
        if info.get("title") and (not job.title or not job.thumbnail):
            _meta(job, info)
    elif d["status"] == "finished":
        job.progress = 100.0
        job.stage = "processing"


def _pp_progress(job: Job, d: dict):
    if job.cancel_event.is_set():
        raise DownloadCancelled()
    if d["status"] == "started":
        job.stage = {"Merger": "merging video+audio", "FFmpegExtractAudio": "converting audio",
                     "FFmpegMetadata": "writing metadata", "FFmpegEmbedSubtitle": "embedding subtitles"
                     }.get(d.get("postprocessor"), "processing")
    elif d["status"] == "finished" and d.get("postprocessor") == "MoveFiles":
        fp = (d.get("info_dict") or {}).get("filepath")
        if fp and fp not in job.files:
            job.files.append(fp)


COMPATIBLE_VIDEO = {"h264", "hevc", "mpeg4", "prores", "mjpeg"}


def _probe(path: str) -> dict:
    r = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                        "stream=index,codec_type,codec_name,pix_fmt,color_transfer,color_primaries,color_space,disposition:format=duration",
                        "-of", "json", path], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout or "{}")


def make_compatible(job: Job, path: str) -> str:
    """AV1/VP9 video -> HEVC (hardware encoder on Macs) in MP4, keeping resolution, HDR, audio, chapters,
    subtitles and the cover image. Anything already H.264/HEVC is left untouched. Returns the new path."""
    if not FFMPEG or not os.path.isfile(path):
        return path
    try:
        info = _probe(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return path
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video or video.get("codec_name") in COMPATIBLE_VIDEO:
        return path
    duration = float((info.get("format") or {}).get("duration") or 0)
    ten_bit = "10" in (video.get("pix_fmt") or "") or "12" in (video.get("pix_fmt") or "")
    vt = "hevc_videotoolbox" if sys.platform == "darwin" else "libx265"
    vi = video["index"]
    out = str(Path(path).with_suffix("")) + ".compatible.mp4"
    cmd = [FFMPEG, "-v", "error", "-y", "-i", path, "-map", "0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy",
           f"-c:{vi}", vt, f"-tag:{vi}", "hvc1"]
    if vt == "hevc_videotoolbox":
        cmd += [f"-q:{vi}", "62", f"-profile:{vi}", "main10" if ten_bit else "main", f"-pix_fmt:{vi}", "p010le" if ten_bit else "nv12"]
    else:
        cmd += [f"-crf:{vi}", "20", f"-pix_fmt:{vi}", "yuv420p10le" if ten_bit else "yuv420p"]
    for key, flag in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"), ("color_space", "-colorspace")):
        if video.get(key) and video[key] != "unknown":
            cmd += [f"{flag}:{vi}", video[key]]  # keep HDR looking right
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if any(s.get("codec_name") not in ("aac", "mp3", "alac", "ac3", "eac3") for s in audio):  # Opus in MP4 won't play in QuickTime
        cmd += ["-c:a", "aac", "-b:a", "256k"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    if subs:
        cmd += ["-c:s", "mov_text"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
    job.stage = f"making it compatible: {video.get('codec_name', '').upper()} → HEVC"
    job.progress, job.speed, job.eta = 0.0, None, None
    job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.time()
    for line in job.proc.stdout:
        if job.cancel_event.is_set():
            job.proc.terminate()
            break
        if line.startswith("out_time_us=") and duration:
            try:
                done = int(line.split("=")[1]) / 1e6
            except ValueError:
                continue
            job.progress = min(99.9, done * 100 / duration)
            rate = done / max(time.time() - t0, 0.01)
            job.eta = int((duration - done) / rate) if rate > 0 else None
    rc = job.proc.wait()
    err = job.proc.stderr.read() if job.proc.stderr else ""
    job.proc = None
    if job.cancel_event.is_set():
        Path(out).unlink(missing_ok=True)
        raise DownloadCancelled()
    if rc != 0 or not os.path.isfile(out):
        Path(out).unlink(missing_ok=True)
        job.note(f"[grab] couldn't convert to HEVC, kept the original: {err.strip()[-300:]}")
        return path
    final = str(Path(path).with_suffix(".mp4"))
    os.replace(out, final)
    if final != path:
        Path(path).unlink(missing_ok=True)
    job.note(f"[grab] converted {video.get('codec_name')} → HEVC for compatibility")
    job.progress = 100.0
    return final


def _pick(entries, spec: str) -> list:
    """Apply an items spec like '1-20,25,30-' to a (possibly lazy, huge) entry list."""
    if not spec:
        return list(entries)
    ranges = []
    for part in spec.replace(" ", "").split(","):
        a, _, b = part.partition("-")
        lo = int(a) if a else 1
        hi = (int(b) if b else None) if "-" in part else lo
        ranges.append((lo, hi))
    stop = None if any(hi is None for _, hi in ranges) else max(hi for _, hi in ranges)
    out = []
    for i, e in enumerate(itertools.islice(entries, stop), 1):
        if any(lo <= i and (hi is None or i <= hi) for lo, hi in ranges):
            out.append(e)
    return out


def _meta(job: Job, info: dict):
    """Copy display metadata (title, thumbnail, uploader...) from a yt-dlp info dict."""
    job.title = info.get("title") or info.get("fulltitle") or job.title
    if not job.thumbnail:
        thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
        # prefer a mid-size jpg/webp over the largest image
        mid = [t for t in thumbs if (t.get("width") or 0) and 300 <= t["width"] <= 800]
        job.thumbnail = info.get("thumbnail") or (mid or thumbs or [{}])[-1].get("url", "")
    job.uploader = info.get("uploader") or info.get("channel") or info.get("creator") or job.uploader
    job.duration = info.get("duration") or job.duration
    if info.get("extractor_key") and info["extractor_key"] != "Generic":
        job.site = info["extractor_key"].replace("Tab", "")
    if job.opts.title and (info.get("extractor_key") or info.get("ie_key")) in (_NOT_A_SITE | {None}):
        job.title = job.opts.title  # raw stream: its own title is just "master"/"index"


def run_ytdlp(job: Job, mgr: Manager, strategy: str | None = None):
    job.tool = "yt-dlp"
    o0 = job.opts
    job.min_height = None
    if strategy in ("client", "simple") and o0.mode != "audio" and (o0.quality == "best" or int(o0.quality or 0) >= 720):
        job.min_height = 720  # a "fix" that only gets 360p isn't a fix
    if not job.stage.startswith("retrying"):
        job.stage = "extracting"
    o = job.opts
    with login_for(o) as login, yt_dlp.YoutubeDL(ytdlp_options(job, strategy, login)) as ydl:
        info = ydl.extract_info(job.url, download=False, process=False)
        if info is None:
            if job.status == "skipped":  # yt-dlp found it in the download archive
                job.progress = 100.0
                return
            raise RuntimeError("extraction returned nothing")
        _meta(job, info)
        # now that we know the site, point the output at its folder (yt-dlp reads this when naming files)
        sub = sanitize_filename(o.subfolder) if (o.subfolder and o.folders != "none") else ""
        key = info.get("extractor_key") or info.get("ie_key")
        name = (_hint_name(o, job.url) if key in _NOT_A_SITE or key is None else None) or name_pattern(o)
        if o.title and (key in _NOT_A_SITE or key is None):
            job.title = o.title
        ydl.params["outtmpl"]["default"] = os.path.join(target_dir(o, job.url, key), sub, name)

        # Fan playlists/channels out into separate jobs so they download in parallel.
        if info.get("_type") in ("playlist", "multi_video"):
            entries = _pick(info.get("entries") or [], o.items or (f"1-{o.scan}" if o.scan else ""))
            info["entries"] = entries
            urls = [e.get("url") for e in entries if e and e.get("_type") in ("url", "url_transparent")]
            if o.expand_playlists and len(entries) > 1 and len(urls) == len(entries):
                child = dataclasses.replace(o, items="", subfolder=info.get("title") or info.get("id") or "playlist")
                titles = {unwrap_url(e["url"]): e.get("title") or "" for e in entries if e.get("url")}
                kids = mgr.add(urls, child, parent=job.id, front=True, titles=titles)
                if not job.thumbnail:
                    first = entries[0] or {}
                    job.thumbnail = (first.get("thumbnails") or [{}])[-1].get("url", "") or first.get("thumbnail", "")
                for k, e in zip(kids, entries):
                    k.title = e.get("title") or ""
                    k.duration = e.get("duration")
                    k.thumbnail = (e.get("thumbnails") or [{}])[-1].get("url", "") or e.get("thumbnail") or ""
                    k.uploader = e.get("uploader") or e.get("channel") or job.uploader
                    k.site = job.site
                job.children = len(kids)
                job.stage = f"playlist · {len(urls)} items"
                job.progress = 100.0
                job.status = "done"
                return

        ydl.process_ie_result(info, download=True)

    if o.compat and o.mode != "audio" and job.status == "running":
        job.files = [make_compatible(job, f) for f in job.files]
    if job.status == "running":
        job.status = "done"
    job.progress = 100.0
    job.stage = "" if job.status == "done" else "already downloaded"


# ---------------------------------------------------------------------------
# gallery-dl
# ---------------------------------------------------------------------------

def gallery_filter_args(o: Options) -> list[str]:
    """Photos-only / videos-only. (gallery-dl's --range counts what passes this filter, so every
    run over the same source - including the watch list's first-check setup - must use the same one.)"""
    if o.media == "images":
        return ["--filter", "extension in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'avif', 'heic', 'bmp', 'tif', 'tiff')"]
    if o.media == "videos":
        return ["--filter", "extension in ('mp4', 'webm', 'mov', 'm4v', 'mkv', 'avi', 'flv', 'ts', 'm3u8')"]
    return []


def run_gallery(job: Job, gentle: bool = False):
    if not GALLERY_DL:
        raise RuntimeError("gallery-dl is not installed")
    o = job.opts
    job.tool, job.stage, job.progress = "gallery-dl", "fetching images" + (" (slower)" if gentle else ""), None
    where = ["-D", o.out_dir] if o.folders == "none" else ["-d", o.out_dir]  # -d keeps gallery-dl's site/user folders
    cmd = [GALLERY_DL, *where, "--retries", "8" if gentle else "4", "--http-timeout", "60" if gentle else "30",
           "-o", f"user-agent={USER_AGENT}"]
    if gentle:
        cmd += ["--sleep-request", "1.5-3", "--sleep", "0.5-1.5"]
    if o.items:
        cmd += ["--range", o.items]
    elif o.scan:
        cmd += ["--range", f"1-{o.scan}"]
    cmd += gallery_filter_args(o)
    if o.watch:
        cmd += ["--abort", "15"]  # newest first: after 15 already-downloaded files in a row, nothing older is new
    if o.archive:
        cmd += ["--download-archive", os.path.join(o.out_dir, ".grab-gallery.sqlite3")]
    login_cm = login_for(o)
    cookie_file, browser = login_cm.__enter__()
    if browser:
        cmd += ["--cookies-from-browser", f"{browser[0]}:{browser[1]}" if browser[1] else browser[0]]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    if o.referer:
        cmd += ["-o", f"headers.Referer={o.referer}"]
    if o.rate_limit:
        cmd += ["--limit-rate", o.rate_limit]
    if job.proxy:
        cmd += ["--proxy", job.proxy]
    if o.polite and polite_group(job.url):
        cmd += ["--sleep", "1.5-4", "--sleep-request", "1.5-3"]  # human-ish pace through a profile
    cmd.append(job.url)

    try:
        return _gallery_proc(job, cmd)
    finally:
        login_cm.__exit__(None, None, None)  # delete the private cookies copy


def _gallery_proc(job: Job, cmd: list[str]):
    job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    skipped = 0
    err_lines: list[str] = []

    def _drain_err():
        for line in job.proc.stderr:
            err_lines.append(line.rstrip())
            job.note(line.rstrip())

    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()
    for line in job.proc.stdout:
        line = line.rstrip()
        if job.cancel_event.is_set():
            job.proc.terminate()
            raise DownloadCancelled()
        if line.startswith("# "):
            skipped += 1
        elif line:
            job.files.append(line)
            if not job.title:
                job.title = Path(line).parent.name
            if not job.thumbnail and Path(line).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"):
                job.thumbnail = line
        job.stage = f"{len(job.files)} files" + (f", {skipped} already had" if skipped else "")
    rc = job.proc.wait()
    t.join(2)
    if job.cancel_event.is_set():
        raise DownloadCancelled()
    if not job.files and not skipped:
        msg = next((l for l in reversed(err_lines) if "error" in l.lower()), err_lines[-1] if err_lines else f"exit {rc}")
        raise RuntimeError(re.sub(r"^\[[^\]]+\]\[error\]\s*", "", msg))
    job.status = "skipped" if not job.files else "done"
    job.stage = "already downloaded" if not job.files else (f"{skipped} already had" if skipped else "")
    job.progress = 100.0


# ---------------------------------------------------------------------------
# direct HTTP (aria2c multi-connection, or a plain stream)
# ---------------------------------------------------------------------------

_ARIA_RE = re.compile(r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+)(?:.*?ETA:(\w+))?")
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def _bytes(s: str) -> int:
    m = re.match(r"([\d.]+)(\w+)", s)
    return int(float(m.group(1)) * _UNITS.get(m.group(2), 1)) if m else 0


def _eta(s: str | None) -> int | None:
    if not s:
        return None
    total = 0
    for n, u in re.findall(r"(\d+)([hms])", s):
        total += int(n) * {"h": 3600, "m": 60, "s": 1}[u]
    return total


def run_direct(job: Job, mgr: Manager | None = None):
    o = job.opts
    site = site_key(job.url)
    out = Path(target_dir(o, job.url))
    if o.subfolder and o.folders != "none":  # e.g. everything from one page goes in a folder named after it
        out = out / sanitize_filename(o.subfolder)
    out.mkdir(parents=True, exist_ok=True)
    job.tool = "aria2c" if (o.aria2 and ARIA2C) else "http"
    job.stage = "downloading"
    if not job.title:
        job.title = o.title or urllib.parse.unquote(urllib.parse.urlparse(job.url).path.rsplit("/", 1)[-1]) or job.url
    if job.tool == "aria2c":
        try:
            return _direct_aria2_with_fallback(job, out, mgr, o, site)
        finally:
            _image_thumb(job)
    try:
        return _direct_http(job, out)
    finally:
        _image_thumb(job)


def _image_thumb(job: Job):
    """A downloaded picture is its own thumbnail."""
    for f in job.files:
        if Path(f).suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif") and Path(f).is_file():
            job.thumbnail = f
            return


def _direct_aria2_with_fallback(job: Job, out: Path, mgr, o: Options, site: str):
    """aria2c with many connections, then 1 connection (some servers refuse more), then plain HTTP."""
    err = None
    first = 1 if (mgr and o.smart and mgr.learned.get("direct:" + site) == "one-connection") else max(1, min(16, o.aria2_conns))
    for conns in dict.fromkeys((first, 1)):
        try:
            _direct_aria2(job, out, conns)
            if conns == 1 and first != 1:
                job.strategy = "1 connection"
                if mgr and o.smart:
                    mgr.learn("direct:" + site, "one-connection")
            return
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - some servers refuse many connections; back off
            err = e
            job.note(f"aria2c x{conns} failed: {e}")
            job.stage = "retrying with 1 connection"
    job.tool = "http"
    job.strategy = "plain HTTP"
    try:
        return _direct_http(job, out)
    except DownloadCancelled:
        raise
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{err}; plain HTTP: {e}") from None


def _direct_aria2(job: Job, out: Path, conns: int):
    o = job.opts
    n = str(conns)
    job.stage = f"downloading ({conns} connection{'s' if conns > 1 else ''})"
    cmd = [ARIA2C, f"-x{n}", f"-s{n}", "-k1M", "-c", "--file-allocation=none", "--summary-interval=1",
           "--console-log-level=notice", "--show-console-readout=true", "--auto-file-renaming=false",
           "--allow-overwrite=false", "--content-disposition-default-utf8=true", "-d", str(out)]
    for k, v in _headers(o).items():
        cmd.append(f"--header={k}: {v}")
    if o.rate_limit:
        cmd.append(f"--max-download-limit={o.rate_limit}")
    if o.cookies_file:
        cmd.append(f"--load-cookies={o.cookies_file}")
    if job.proxy:
        cmd.append(f"--all-proxy={job.proxy}")
    cmd.append(job.url)
    job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buf, tail = b"", collections.deque(maxlen=20)
    while True:
        chunk = job.proc.stdout.read1(4096) if hasattr(job.proc.stdout, "read1") else job.proc.stdout.read(4096)
        if not chunk:
            break
        if job.cancel_event.is_set():
            job.proc.terminate()
            raise DownloadCancelled()
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            m = _ARIA_RE.search(line)
            if m:
                job.downloaded, job.total = _bytes(m.group(1)), _bytes(m.group(2))
                job.progress = float(m.group(3))
                job.speed = _bytes(m.group(4))
                job.eta = _eta(m.group(5))
            else:
                tail.append(line)
                job.note(line)
                done = re.search(r"Download complete: (.+)$", line)
                if done:
                    job.files.append(done.group(1).strip())
    rc = job.proc.wait()
    if job.cancel_event.is_set():
        raise DownloadCancelled()
    if rc != 0:
        # 13 = file already exists
        if rc == 13:
            job.status, job.stage = "skipped", "already downloaded"
            return
        detail = next((l.split("errorCode=", 1)[-1] for l in reversed(tail) if l.startswith("->")), "")
        raise RuntimeError(f"aria2c: {re.sub(r'^\d+\s*', '', detail) or f'exit code {rc}'}")
    job.status, job.progress, job.stage = "done", 100.0, ""


def _direct_http(job: Job, out: Path):
    req = urllib.request.Request(job.url, headers=_headers(job.opts))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": job.proxy, "https": job.proxy} if job.proxy else {}))
    with opener.open(req, timeout=30) as r:
        name = None
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        if m:
            name = urllib.parse.unquote(m.group(1))
        name = sanitize_filename(name or job.title or "download")
        dest = out / name
        if dest.exists():
            job.status, job.stage = "skipped", "already downloaded"
            return
        total = int(r.headers.get("Content-Length") or 0) or None
        job.total = total
        tmp = dest.with_suffix(dest.suffix + ".part")
        t0, done = time.time(), 0
        with open(tmp, "wb") as f:
            while True:
                if job.cancel_event.is_set():
                    raise DownloadCancelled()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                job.downloaded = done
                el = max(time.time() - t0, 1e-3)
                job.speed = done / el
                if total:
                    job.progress = done * 100.0 / total
                    job.eta = int((total - done) / job.speed) if job.speed else None
        tmp.rename(dest)
        job.files.append(str(dest))
    job.status, job.progress, job.stage = "done", 100.0, ""


# ---------------------------------------------------------------------------
# helpers for UIs
# ---------------------------------------------------------------------------

def human_bytes(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def human_eta(s) -> str:
    if s is None:
        return ""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


_SECRET_HEADERS = ("cookie", "authorization", "proxy-authorization", "x-csrf-token")


def job_details(job: Job) -> str:
    """Everything needed to see why a download failed, as plain text to paste somewhere. Login cookies and
    other secrets are never included (only where the login came from)."""
    import platform
    from importlib import metadata

    from . import __version__
    def version(pkg):
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "not installed"
    o = job.opts
    when = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"  # noqa: E731
    headers = {k: ("(hidden)" if k.lower() in _SECRET_HEADERS else v) for k, v in (o.headers or {}).items()}
    login = o.cookies_browser or ("your cookies.txt" if o.cookies_file else "none")
    lines = [
        f"Grab {__version__} · yt-dlp {yt_dlp.version.__version__} · gallery-dl {version('gallery-dl')} · "
        f"macOS {platform.mac_ver()[0] or platform.platform()} ({platform.machine()})",
        f"Link: {job.url}",
        f"Title: {job.title or '-'}   Site: {job.site}",
        f"Status: {job.status}{f' ({job.stage})' if job.stage else ''}   Tool: {job.tool or '-'}   Auto-fix: {job.strategy or '-'}",
        f"Added {when(job.created)} · started {when(job.started)} · ended {when(job.finished)}",
        f"Options: mode {o.mode}, quality {o.quality}, container {o.container}, compatible {'on' if o.compat else 'off'}, "
        f"smart {'on' if o.smart else 'off'}, polite {'on' if o.polite else 'off'}, aria2 {'on' if o.aria2 else 'off'}"
        + (f", clip {o.clip}" if o.clip else "") + (f", items {o.items}" if o.items else ""),
        f"Login: {login}" + (f"   Referer: {o.referer}" if o.referer else "") + (f"   Headers: {headers}" if headers else ""),
        f"Saving to: {o.out_dir}",
    ]
    if job.files:
        lines.append("Files: " + ", ".join(job.files))
    lines += ["", "Error:", job.error or "-", "", f"Log (last {len(job.log)} lines):", *job.log]
    return "\n".join(lines)


def tool_status() -> dict:
    return {
        "yt-dlp": yt_dlp.version.__version__,
        "gallery-dl": bool(GALLERY_DL),
        "aria2c": bool(ARIA2C),
        "ffmpeg": bool(FFMPEG),
    }

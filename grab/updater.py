"""Keeps yt-dlp and gallery-dl current.

Sites change all the time and yt-dlp / gallery-dl ship fixes within days, so the GUI checks PyPI at
launch and every few hours. A new version is installed only while nothing is downloading (new
downloads wait a few seconds), and then the GUI restarts itself so the new code is actually used.
Errors that usually mean "yt-dlp is out of date" trigger a check right away, and those downloads
are retried after the update.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from importlib import metadata
from pathlib import Path

PACKAGES = {"yt-dlp": "yt-dlp[default,curl-cffi]", "gallery-dl": "gallery-dl"}
CHECK_EVERY = 6 * 3600
OUTDATED_HINTS = ("unable to extract", "please report this issue", "latest version", "yt-dlp -u", "nsig",
                  "signature extraction", "unable to decode", "player response", "challenge solving",
                  "failed to parse json", "unable to download api page")


def looks_outdated(msg: str) -> bool:
    m = (msg or "").lower()
    return any(h in m for h in OUTDATED_HINTS)


def _vt(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def installed(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def latest(pkg: str, nightly: bool = False) -> str:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=20) as r:
        data = json.load(r)
    if not nightly:
        return data["info"]["version"]
    return max((v for v, files in data["releases"].items() if files), key=_vt)


def find_uv() -> str | None:
    home = Path.home()
    for p in (shutil.which("uv"), home / ".local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv", home / ".cargo/bin/uv"):
        if p and os.access(str(p), os.X_OK):
            return str(p)
    return None


def available_updates(nightly: bool = False) -> dict[str, tuple[str | None, str]]:
    out = {}
    for pkg in PACKAGES:
        cur, new = installed(pkg), latest(pkg, nightly)
        if cur is None or _vt(new) > _vt(cur):
            out[pkg] = (cur, new)
    return out


def install(pkgs, nightly: bool = False, python: str = sys.executable) -> tuple[bool, str]:
    uv = find_uv()
    specs = [PACKAGES[p] for p in pkgs]
    if uv:
        cmd = [uv, "pip", "install", "--python", python, "-U", *specs] + (["--prerelease", "allow"] if nightly else [])
    else:
        cmd = [python, "-m", "pip", "install", "-U", *specs] + (["--pre"] if nightly else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-800:]


class Updater:
    """Background auto-updater for the GUI server."""

    def __init__(self, mgr, state_file: Path, prefs, restart):
        self.mgr, self.state_file, self.prefs, self.restart = mgr, Path(state_file), prefs, restart
        self.wake = threading.Event()
        self.retry_ids: set[int] = set()
        self.status = {"state": "idle", "message": "", "last_check": None,
                       "versions": {p: installed(p) for p in PACKAGES}, **self._saved()}

    # -- persisted bits: last update (for the "updated" toast) + jobs to resume/retry after restart
    def _saved(self) -> dict:
        try:
            d = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            d = {}
        return {"last_update": d.get("last_update"), "last_check": d.get("last_check")}

    def _write(self, **kw):
        try:
            d = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            d = {}
        d.update(kw)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(d, indent=1))

    def after_restart(self):
        """Called at startup: put back downloads that were waiting when we restarted for an update."""
        try:
            d = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        for jid in d.pop("resume", []):
            self.mgr.resume(jid)
        for jid in d.pop("retry", []):
            self.mgr.retry(jid)
        self.state_file.write_text(json.dumps(d, indent=1))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def request(self, job_id: int | None = None):
        """A download failed in a way that suggests an outdated yt-dlp: check now."""
        if job_id is not None:
            self.retry_ids.add(job_id)
        self.wake.set()

    def _set(self, state, message=""):
        self.status.update(state=state, message=message)

    def _loop(self):
        time.sleep(15)  # let the app finish starting first
        while True:
            forced = self.wake.is_set()
            self.wake.clear()
            try:
                self.check(force=forced)
            except Exception as e:  # noqa: BLE001 - offline etc.; try again later
                self._set("error", f"Couldn't check for updates: {e}")
            self.wake.wait(CHECK_EVERY)

    def check(self, force=False):
        prefs = self.prefs()
        if not prefs.get("auto_update", True) and not force:
            self._set("off", "Automatic updates are off")
            return
        nightly = prefs.get("update_channel") == "nightly"
        self._set("checking", "Checking for updates…")
        ups = available_updates(nightly)
        self.status["last_check"] = time.time()
        self._write(last_check=self.status["last_check"])
        if not ups:
            self.retry_ids.clear()
            self._set("current", "Everything is up to date")
            return
        names = ", ".join(f"{p} {new}" for p, (_, new) in ups.items())
        # wait until nothing is downloading, then hold the queue while installing
        while True:
            self.mgr.hold = True
            if not self.mgr.running:
                break
            self.mgr.hold = False
            self._set("waiting", f"{names} available - installs when current downloads finish")
            time.sleep(20)
        self._set("installing", f"Installing {names}…")
        ok, log = install(ups, nightly)
        if not ok:
            self.mgr.release_hold()
            self._set("error", f"Update failed: {log.splitlines()[-1] if log else 'unknown error'}")
            return
        self._set("restarting", f"Installed {names} - restarting engine")
        with self.mgr.cv:
            waiting = list(self.mgr.queue)
        self._write(last_update={"at": time.time(), "packages": {p: [old, new] for p, (old, new) in ups.items()}},
                    resume=waiting, retry=sorted(self.retry_ids))
        self.restart()

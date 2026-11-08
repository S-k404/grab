"""Self-check: is everything grab needs installed, current and working right now?

Used by `grab --doctor` and the "Run self-check" button in Settings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import yt_dlp

from . import core
from .updater import PACKAGES, _vt, installed, latest

TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo": first YouTube video, never removed


def _check(name, fn):
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e).split("\n")[0][:200]
    return {"name": name, "ok": ok, "detail": detail, "secs": round(time.time() - t0, 1)}


def _tool(path, label, why):
    def fn():
        if not path:
            return False, f"not found - {why}. Run ./setup.sh"
        return True, path
    return fn


def _deno():
    exe = shutil.which("deno") or next((p for p in ("/opt/homebrew/bin/deno", "/usr/local/bin/deno") if os.path.exists(p)), None)
    if not exe:
        return False, "not found - some YouTube videos need it. Run: brew install deno"
    v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.split("\n")[0]
    return True, v


def _versions():
    parts, stale = [], []
    for pkg in PACKAGES:
        cur = installed(pkg)
        try:
            new = latest(pkg)
        except OSError:
            new = None
        parts.append(f"{pkg} {cur}" + (f" (latest {new})" if new and cur and _vt(new) > _vt(cur) else ""))
        if new and cur and _vt(new) > _vt(cur):
            stale.append(pkg)
    detail = ", ".join(parts) + (" - run `grab --update` or wait for auto-update" if stale else " - up to date")
    return not stale, detail


def _folder(out_dir):
    def fn():
        p = Path(out_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".grab-write-test"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(p).free
        return free > 2 * 1024 ** 3, f"{p} is writable, {core.human_bytes(free)} free" + (" - getting low!" if free <= 2 * 1024 ** 3 else "")
    return fn


def _youtube(cookies_browser: str = ""):
    class _Quiet:  # errors are explained below instead of printed raw
        def debug(self, m): pass
        warning = error = info = debug
    params = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _Quiet()}
    try:
        with core.login_for(core.Options(cookies_browser=cookies_browser)) as (cookie_file, browser):
            if browser:
                params["cookiesfrombrowser"] = browser
            if cookie_file:
                params["cookiefile"] = cookie_file
                cookies_browser = "extension"
            with yt_dlp.YoutubeDL(params) as ydl:
                info = ydl.extract_info(TEST_VIDEO, download=False)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "not a bot" in msg:
            if not cookies_browser:
                return False, ("YouTube wants to check you're not a bot. Choose the browser you're logged in to YouTube with "
                               "under 'Logged-in cookies' (CLI: -c brave), then run this again")
            return False, f"YouTube still asks for a bot check while using your {core.BROWSER_NAMES.get(cookies_browser, cookies_browser)} login. Open YouTube in that browser, make sure you're logged in, then try again"
        problem = core.cookie_problem(msg)
        if problem:
            return False, problem
        raise
    fmts = [f for f in info.get("formats") or [] if f.get("vcodec") not in (None, "none")]
    if not fmts:
        return False, "reached YouTube but got no video formats - usually fixed by an update"
    via = (" (using the logins shared by the Brave extension)" if cookies_browser == "extension"
           else f" (using your {core.BROWSER_NAMES.get(cookies_browser, cookies_browser)} login)" if cookies_browser else "")
    return True, f"'{info.get('title')}' - {len(fmts)} video formats available{via}"


def run_checks(out_dir: str | None = None, online: bool = True, cookies_browser: str = "") -> list[dict]:
    checks = [
        _check("ffmpeg", _tool(core.FFMPEG, "ffmpeg", "needed to merge video+audio and convert audio")),
        _check("aria2c", _tool(core.ARIA2C, "aria2c", "needed for fast multi-connection file downloads")),
        _check("gallery-dl", _tool(core.GALLERY_DL, "gallery-dl", "needed for image posts and galleries")),
        _check("deno", _deno),
        _check("download folder", _folder(out_dir or core.Options().out_dir)),
    ]
    if online:
        checks += [_check("versions", _versions), _check("YouTube", lambda: _youtube(cookies_browser))]
    return checks


def main(out_dir=None, cookies_browser: str = "") -> int:
    print("grab self-check\n")
    results = run_checks(out_dir, cookies_browser=cookies_browser)
    for r in results:
        print(f"  {'✓' if r['ok'] else '✗'} {r['name']:<16} {r['detail']}")
    bad = [r for r in results if not r["ok"]]
    print("\nAll good." if not bad else f"\n{len(bad)} problem(s) found - see above.")
    return 1 if bad else 0

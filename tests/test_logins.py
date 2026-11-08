"""Login quick-fix: which errors offer it, Zen support, and the retry-with-browser flow.
Uses a fake home folder throughout, so your real browsers and logins are never touched."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

import pytest

from grab import core


@pytest.mark.parametrize("msg,login", [
    ("[instagram][error] NotFoundError: Requested user could not be found", True),
    ('[reddit][error] "You\'ve been blocked by network security."', True),
    ("Sign in to confirm your age. This video may be inappropriate", True),
    ("[twitter][error] 'Unavailable'", True),
    ("Private video. Sign in if you've been granted access", True),
    ("HTTP Error 404: Not Found", False),
    ("Unsupported URL: https://example.com", False),
    ("Requested format is not available", False),
])
def test_which_errors_offer_login_fix(msg, login):
    assert core.needs_login(msg) is login


@pytest.mark.parametrize("msg,kind", [
    ("Operation not permitted: '/Users/x/Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies'", "Full Disk Access"),
    ("failed to decrypt cookie (AES-GCM) because the key is wrong / Chrome Safe Storage", "Always Allow"),
    ("could not find firefox cookies database in /Users/x/Library/Application Support/Firefox", "Open it once"),
])
def test_cookie_access_problems_are_explained(msg, kind, monkeypatch):
    monkeypatch.setattr(core, "browser_data_blocked", lambda b: False)  # independent of this Mac's privacy settings
    assert kind in core.cookie_problem(msg)
    assert not core.needs_login(msg)          # don't offer the same fix again for these


def test_zen_reads_as_firefox_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    prof = tmp_path / "Library/Application Support/zen/Profiles/abc.Default (release)"
    prof.mkdir(parents=True)
    (prof / "cookies.sqlite").write_bytes(b"")
    (tmp_path / "Library/Application Support/zen/profiles.ini").write_text(
        "[Profile0]\nPath=Profiles/abc.Default (release)\n\n[Install1234]\nDefault=Profiles/abc.Default (release)\n")
    assert core.cookie_source("zen") == ("firefox", str(prof), None, None)
    assert core.cookie_arg("zen") == f"firefox:{prof}"
    assert core.cookie_source("chrome") == ("chrome", None, None, None) and core.cookie_source("") is None


def test_retry_with_browser_login(tmp_path, site):
    """A download fails needing a login -> 'Use Firefox' retries it with Firefox cookies (none here: fake home)."""
    env = {**os.environ, "HOME": str(tmp_path), "GRAB_STATE_DIR": str(tmp_path / "state")}
    proc = subprocess.Popen([sys.executable, "-m", "grab.web", "--no-browser", "--port", "0", "--no-auto-update",
                             "--out", str(tmp_path / "dl")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    base = "http://127.0.0.1:" + re.search(r":(\d+)/", proc.stdout.readline()).group(1)

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def wait_job():
        deadline = time.time() + 60
        while True:
            js = call("/api/jobs")["jobs"]
            if js and js[0]["status"] in ("error", "done"):
                return js[0]
            assert time.time() < deadline
            time.sleep(0.3)
    try:
        assert all(b["id"] in core.BROWSERS for b in call("/api/config")["installed_browsers"])
        call("/api/add", {"urls": f"{site}/protected/clip.mp4", "options": {"mode": "video", "smart": False}})  # 403: no access
        job = wait_job()
        assert job["status"] == "error" and job["needs_login"], job["error"]
        call("/api/retry_login", {"id": job["id"], "browser": "firefox", "default": True})
        job = wait_job()
        assert job["cookies_browser"] == "firefox"                    # retried with that browser's login
        assert job["status"] == "error" and "Open it once" in job["cookie_problem"], job["error"]  # fake home: no Firefox data
        assert call("/api/config")["prefs"]["cookies_browser"] == "firefox"  # remembered as the default
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)


def test_macos_privacy_block_points_to_full_disk_access(monkeypatch):
    monkeypatch.setattr(core, "browser_data_blocked", lambda b: b == "brave")
    msg = 'could not find brave cookies database in "/Users/x/Library/Application Support/BraveSoftware/Brave-Browser"'
    assert "Full Disk Access" in core.cookie_problem(msg) and not core.needs_login(msg)
    assert "Full Disk Access" in core.cookie_problem("PermissionError: [Errno 1] Operation not permitted: '/Users/x/Library/Application Support/Google/Chrome'")


# ---------------------------------------------------------------- logins shared by the browser extension

def _cookie(domain, name, value="v", **kw):
    return {"domain": domain, "name": name, "value": value, "path": "/", "secure": True, "httpOnly": False,
            "hostOnly": False, "expirationDate": 2000000000, **kw}


def test_shared_cookies_file_is_private_netscape(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAB_STATE_DIR", str(tmp_path))
    n = core.save_shared_cookies([_cookie(".youtube.com", "SID", "abc"), _cookie("www.instagram.com", "sessionid", "xyz", httpOnly=True, hostOnly=True),
                                  _cookie(".youtube.com", "bad\tname")], ["YouTube", "Instagram"], "Brave")
    f = core.shared_cookies_path()
    assert n == 2
    assert oct(f.stat().st_mode & 0o777) == "0o600" and oct(f.parent.stat().st_mode & 0o777) == "0o700"
    lines = [l for l in f.read_text().splitlines() if l and not l.startswith("# ")]
    assert ".youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc" in lines
    assert "#HttpOnly_www.instagram.com\tFALSE\t/\tTRUE\t2000000000\tsessionid\txyz" in lines
    import http.cookiejar
    jar = http.cookiejar.MozillaCookieJar(str(f))
    jar.load(ignore_discard=True, ignore_expires=True)          # the format yt-dlp/gallery-dl read
    assert {c.name for c in jar} == {"SID", "sessionid"}
    assert core.shared_logins_info()["sites"] == ["Instagram", "YouTube"]
    core.clear_shared_cookies()
    assert not f.exists() and core.shared_logins_info() == {}


def test_which_login_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAB_STATE_DIR", str(tmp_path))
    O = core.Options
    assert core.resolve_login(O()) == (None, None)                                  # nothing shared, nothing chosen
    core.save_shared_cookies([_cookie(".youtube.com", "SID")], ["YouTube"])
    shared = str(core.shared_cookies_path())
    assert core.resolve_login(O()) == (shared, None)                                # use what the extension shared
    assert core.resolve_login(O(cookies_browser="extension")) == (shared, None)
    monkeypatch.setattr(core, "browser_data_blocked", lambda b: b == "brave")
    assert core.resolve_login(O(cookies_browser="brave")) == (shared, None)        # macOS blocks Brave -> shared logins
    assert core.resolve_login(O(cookies_browser="firefox"))[1][0] == "firefox"     # a readable browser wins
    assert core.resolve_login(O(cookies_file="/x/c.txt", cookies_browser="firefox")) == ("/x/c.txt", None)


def test_each_download_gets_a_private_copy_that_is_deleted(tmp_path, monkeypatch, site):
    monkeypatch.setenv("GRAB_STATE_DIR", str(tmp_path))
    core.save_shared_cookies([_cookie("127.0.0.1", "SID", "s3cret", hostOnly=True)], ["Test"])
    seen = {}
    real = core.ytdlp_options

    def spy(job, strategy=None, login=(None, None)):
        p = real(job, strategy, login)
        seen["file"] = p.get("cookiefile")
        seen["content"] = open(p["cookiefile"]).read()
        return p
    monkeypatch.setattr(core, "ytdlp_options", spy)
    from conftest import run
    (job,) = run([f"{site}/files/clip.mp4"], tmp_path / "dl", archive=False)
    assert job.status == "done", job.error
    assert seen["file"] != str(core.shared_cookies_path()) and "s3cret" in seen["content"]  # a copy, with the login
    assert not os.path.exists(seen["file"])                                                    # ...gone afterwards
    assert core.shared_cookies_path().read_text().count("s3cret") == 1                         # original untouched

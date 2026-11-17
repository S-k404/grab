"""The GUI server: starts the real `grab-gui` process and talks to its HTTP API."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def gui(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "grab.web", "--no-browser", "--port", "0", "--no-auto-update",
                             "--state-dir", str(tmp_path / "state"), "--out", str(tmp_path / "dl")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
    assert m, line
    base = f"http://127.0.0.1:{m.group(1)}"
    yield base, proc, tmp_path
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        proc.wait(10)


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def post(url, body, origin=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def wait_jobs(base, timeout=90):
    deadline = time.time() + timeout
    while True:
        jobs = get(base + "/api/jobs")["jobs"]
        if jobs and all(j["status"] not in ("queued", "running") for j in jobs):
            return jobs
        assert time.time() < deadline, jobs
        time.sleep(0.3)


def test_page_and_config(gui):
    base, _, _ = gui
    html = urllib.request.urlopen(base + "/").read().decode()
    assert "<title>Grab</title>" in html
    cfg = get(base + "/api/config")
    assert cfg["tools"]["yt-dlp"] and "best" in cfg["qualities"]


def test_other_websites_cannot_control_it(gui):
    base, _, _ = gui
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/add", {"urls": "https://example.com/a.zip"}, origin="https://evil.example")
    assert e.value.code == 403
    assert get(base + "/api/jobs")["jobs"] == []


def test_send_to_grab_link_only_prefills(gui):
    base, _, _ = gui
    req = urllib.request.Request(base + "/add?url=https%3A%2F%2Fyoutu.be%2Fabc")
    opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                              {"redirect_request": lambda *a, **k: None}))
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(req)
    assert e.value.code == 302 and e.value.headers["Location"] == "/#add=https%3A%2F%2Fyoutu.be%2Fabc"
    assert get(base + "/api/jobs")["jobs"] == []  # nothing was downloaded


def test_prefs_are_saved(gui):
    base, _, _ = gui
    post(base + "/api/prefs", {"prefs": {"preset": "mp3", "workers": 6}})
    cfg = get(base + "/api/config")
    assert cfg["prefs"]["preset"] == "mp3" and cfg["workers"] == 6


def test_download_through_api_and_history_on_quit(gui, site):
    base, proc, tmp = gui
    assert post(base + "/api/add", {"urls": f"{site}/files/data.zip\n{site}/nothing-here"})["added"] == 2
    jobs = {j["url"].rsplit("/", 1)[-1]: j for j in wait_jobs(base)}
    assert jobs["data.zip"]["status"] == "done" and jobs["data.zip"]["files"]
    assert jobs["nothing-here"]["status"] == "error"
    # files that aren't a job's thumbnail can't be fetched through the thumbnail endpoint
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/api/thumb/{jobs['data.zip']['id']}")
    # remove + retry endpoints
    post(base + "/api/remove", {"id": jobs["nothing-here"]["id"]})
    assert len(get(base + "/api/jobs")["jobs"]) == 1
    proc.send_signal(signal.SIGTERM)
    proc.wait(10)
    saved = json.loads((tmp / "state" / "history.json").read_text())
    assert [j["status"] for j in saved["jobs"]] == ["done"]


def test_clear_all_hides_past_downloads_but_keeps_files(gui, site):
    base, _, tmp = gui
    post(base + "/api/add", {"urls": f"{site}/files/data.zip\n{site}/nothing-here"})
    jobs = {j["url"].rsplit("/", 1)[-1]: j for j in wait_jobs(base)}
    downloaded = Path(jobs["data.zip"]["files"][-1])
    # "Clear finished" keeps the failed one, so it can still be retried
    assert post(base + "/api/clear", {})["cleared"] == 1
    assert [j["status"] for j in get(base + "/api/jobs")["jobs"]] == ["error"]
    # "Clear all" empties the list, and history at once (no waiting for the next autosave or a quit)
    assert post(base + "/api/clear", {"all": True})["cleared"] == 1
    assert get(base + "/api/jobs")["jobs"] == []
    assert json.loads((tmp / "state" / "history.json").read_text())["jobs"] == []
    assert downloaded.is_file()  # only the list is cleared, never your files


def _start(state, out):
    proc = subprocess.Popen([sys.executable, "-m", "grab.web", "--no-browser", "--port", "0", "--no-auto-update",
                             "--state-dir", str(state), "--out", str(out)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc, "http://127.0.0.1:" + re.search(r":(\d+)/", proc.stdout.readline()).group(1)


def test_failed_download_details_and_log_file(gui, site):
    base, proc, tmp = gui
    post(base + "/api/add", {"urls": f"{site}/nothing-here", "options": {"headers": {"Cookie": "secret=1", "User-Agent": "UA"}}})
    (job,) = wait_jobs(base)
    assert job["status"] == "error"

    def details(b):
        with urllib.request.urlopen(f"{b}/api/details?id={job['id']}") as r:
            assert r.headers["Cache-Control"] == "no-store"
            return r.read().decode()
    text = details(base)
    assert f"Link: {site}/nothing-here" in text and "yt-dlp" in text and "Error:\n" in text and "Log (last" in text
    assert "secret=1" not in text and "'Cookie': '(hidden)'" in text and "'User-Agent': 'UA'" in text  # never a login
    # everything is in the log file too, which stays small
    log = (tmp / "state" / "logs" / "grab.log").read_text()
    assert f"#{job['id']} start {site}/nothing-here" in log and f"#{job['id']} error: " in log
    assert get(base + "/api/config")["log_file"] == str(tmp / "state" / "logs" / "grab.log")
    # a failed download keeps its log after Grab restarts, so Copy details still has something to say
    log_part = text.split("Log (last", 1)[1].split("\n", 1)[1]
    assert log_part.strip()
    proc.send_signal(signal.SIGTERM)
    proc.wait(10)
    proc2, base2 = _start(tmp / "state", tmp / "dl")
    try:
        assert details(base2).split("Log (last", 1)[1].split("\n", 1)[1] == log_part
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base2}/api/details?id=999")
    finally:
        proc2.send_signal(signal.SIGTERM)
        proc2.wait(10)


def test_only_the_extension_may_add_a_page_of_files(gui, site):
    base, _, _ = gui
    body = {"page": f"{site}/gallery.html", "title": "Holiday Photos", "items": [{"url": f"{site}/files/pics/big.png", "kind": "image"}]}
    for origin in (None, "https://evil.example"):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(base + "/api/ext/add_many", body, origin=origin)
        assert e.value.code == 403
    ext = "chrome-extension://hejmghgkkgadcieojoganellldeehhog"
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base + "/api/ext/add_many", {**body, "items": [{"url": "file:///etc/passwd"}, {"url": "javascript:alert(1)"}]}, origin=ext)
    assert e.value.code == 400  # only web links
    assert post(base + "/api/ext/add_many", body, origin=ext)["added"] == 1
    jobs = wait_jobs(base)
    assert [j["status"] for j in jobs] == ["done", "done"]
    assert jobs[-1]["files"][-1].endswith("/127.0.0.1/Holiday Photos/big.png")


def test_auto_download_skips_what_grab_already_has(gui, site):
    base, _, _ = gui
    ext = "chrome-extension://hejmghgkkgadcieojoganellldeehhog"
    url = f"{site}/files/data.zip"
    assert post(base + "/api/ext/add", {"url": url, "page": url, "auto": True}, origin=ext)["added"] == 1
    again = post(base + "/api/ext/add", {"url": url, "page": url, "auto": True}, origin=ext)
    assert again["added"] == 0 and again["duplicate"]
    assert post(base + "/api/ext/add", {"url": url, "page": url}, origin=ext)["added"] == 1  # a click always downloads
    assert [j["status"] for j in wait_jobs(base)] == ["done", "done"]


def _no_redirect(url, headers=None):
    opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                              {"redirect_request": lambda *a, **k: None}))
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(urllib.request.Request(url, headers=headers or {}))
    return e.value


def test_send_to_grab_with_key_downloads_right_away(gui, site):
    base, _, _ = gui
    key = get(base + "/api/config")["send_key"]
    assert len(key) >= 16
    link = urllib.parse.quote(f"{site}/files/data.zip", safe="")
    # a link some website made (no key / wrong key): only pre-fills, downloads nothing
    for k in ("", "&key=wrong"):
        r = _no_redirect(f"{base}/add?url={link}{k}")
        assert r.headers["Location"].startswith("/#add=")
    assert get(base + "/api/jobs")["jobs"] == []
    # your own Send to Grab button (has the key): downloads straight away
    r = _no_redirect(f"{base}/add?key={key}&url={link}")
    assert r.headers["Location"].startswith("/#added=")
    jobs = wait_jobs(base)
    assert [j["status"] for j in jobs] == ["done"]
    assert get(base + "/api/config")["send_key"] == key  # stable across requests


def test_requests_for_other_hosts_are_refused(gui):
    base, _, _ = gui
    req = urllib.request.Request(base + "/api/config", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_only_the_extension_gets_the_key(gui):
    base, _, _ = gui
    with pytest.raises(urllib.error.HTTPError):
        post(base + "/api/ext/hello", {}, origin="https://evil.example")
    req = urllib.request.Request(base + "/api/ext/hello", data=b"{}", method="POST",
                                 headers={"Origin": "chrome-extension://hejmghgkkgadcieojoganellldeehhog"})
    with urllib.request.urlopen(req) as r:
        assert json.load(r)["key"] == get(base + "/api/config")["send_key"]


def test_menu_bar_paste_uses_saved_format(gui, site):
    base, _, _ = gui
    post(base + "/api/prefs", {"prefs": {"preset": "mp3"}})
    assert post(base + "/api/quick_add", {"urls": f"see this {site}/files/clip.mp4 and"})["added"] == 1
    (job,) = wait_jobs(base)
    assert job["status"] == "done", job["error"]
    assert job["mode"] == "audio" and job["files"][-1].endswith(".mp3")   # your saved "MP3 audio" choice


def test_move_endpoint_changes_who_is_next(gui, site):
    base, _, _ = gui
    post(base + "/api/prefs", {"prefs": {"workers": 1}})
    post(base + "/api/add", {"urls": f"{site}/slow/big.bin", "options": {"mode": "direct", "aria2": False}})  # one slow connection holds the only worker
    deadline = time.time() + 20
    while not any(j["status"] == "running" for j in get(base + "/api/jobs")["jobs"]):
        assert time.time() < deadline
        time.sleep(0.1)
    post(base + "/api/add", {"urls": "\n".join(f"{site}/files/data.zip?n={n}" for n in (1, 2, 3))})

    def line():
        waiting = [j for j in get(base + "/api/jobs")["jobs"] if j["queue_pos"]]
        return [j["url"][-1] for j in sorted(waiting, key=lambda j: j["queue_pos"])]

    assert line() == ["1", "2", "3"]
    third = next(j for j in get(base + "/api/jobs")["jobs"] if j["url"].endswith("n=3"))
    assert post(base + "/api/move", {"id": third["id"], "to": "top"}) == {"ok": True}
    assert line() == ["3", "1", "2"]
    assert post(base + "/api/move", {"id": third["id"], "to": "bottom"}) == {"ok": True}
    assert line() == ["1", "2", "3"]
    assert post(base + "/api/move", {"id": third["id"], "to": "sideways"}) == {"ok": False}
    post(base + "/api/cancel", {})


def test_export_writes_a_list_that_can_be_imported_again(gui, site):
    base, _, tmp = gui
    assert post(base + "/api/export", {"which": "all"}) == {"count": 0}       # nothing yet: no empty file
    post(base + "/api/add", {"urls": f"{site}/files/data.zip\n{site}/nothing-here"})
    wait_jobs(base)
    failed = post(base + "/api/export", {"which": "failed"})
    assert failed["count"] == 1
    assert Path(failed["path"]).parent == tmp / "dl" and Path(failed["path"]).name.startswith("grab-links-failed-")
    assert Path(failed["path"]).read_text() == f"{site}/nothing-here\n"
    everything = post(base + "/api/export", {"which": "nonsense"})              # unknown view: everything
    assert Path(everything["path"]).read_text().split() == [f"{site}/files/data.zip", f"{site}/nothing-here"]


def test_history_switch_is_saved_and_cleans_the_file(gui, site):
    base, proc, tmp = gui
    post(base + "/api/add", {"urls": f"{site}/files/data.zip"})
    wait_jobs(base)
    history = tmp / "state" / "history.json"
    deadline = time.time() + 10
    while not (history.exists() and json.loads(history.read_text())["jobs"]):   # autosave runs every couple of seconds
        assert time.time() < deadline
        time.sleep(0.2)
    post(base + "/api/prefs", {"prefs": {"keep_history": False}})
    assert json.loads(history.read_text())["jobs"] == []
    assert len(get(base + "/api/jobs")["jobs"]) == 1                            # still on screen
    assert get(base + "/api/config")["prefs"]["keep_history"] is False


def test_extension_sends_a_cdn_video_with_no_extension_as_a_plain_file(gui, site):
    """A signed CDN link (no extension, application/octet-stream) is what yt-dlp can only save as .unknown_video."""
    base, _, tmp = gui
    ext = "chrome-extension://hejmghgkkgadcieojoganellldeehhog"
    page = f"{site}/octet.html"
    post(base + "/api/ext/add", {"url": f"{site}/octet/abc123?sig=1", "page": page, "title": "Octet page", "direct": True}, origin=ext)
    (done,) = wait_jobs(base)
    assert done["status"] == "done", (done["error"], done["log"])
    (file,) = done["files"]
    assert Path(file).parent == tmp / "dl" / "127.0.0.1" and Path(file).name == "Octet page.mp4"   # named by what it is
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", file],
                           capture_output=True, text=True)
    assert float(probe.stdout) > 5                                                                 # and it plays
    assert not list((tmp / "dl").rglob("*.unknown_video")) and not list((tmp / "dl").rglob("*.part"))

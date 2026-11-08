"""End-to-end: the Stream Catcher extension in a real Chromium browser, talking to a real Grab.

A headless Helium/Chromium loads the extension, opens the local test site's player page (which plays
an MP4 and an HLS stream whose video bytes need the page as Referer), and we check through the
DevTools protocol that the extension found the right streams, that its popup renders, and that
sending them makes Grab download playable files named after the page.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from conftest import duration

EXT_DIR = Path(os.environ.get("GRAB_EXT_DIR") or Path(__file__).resolve().parent.parent / "browser-extension")
EXT_ID = "hejmghgkkgadcieojoganellldeehhog"
BROWSERS = [  # branded Google Chrome no longer allows --load-extension, Chromium builds do
    "/Applications/Helium.app/Contents/MacOS/Helium",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _browser() -> str:
    for b in BROWSERS:
        if os.path.exists(b):
            return b
    pytest.skip("no Chromium-based browser that can load extensions (Helium, Chromium, Brave)")


class CDP:
    """Minimal Chrome DevTools Protocol client (websockets comes with yt-dlp)."""

    def __init__(self, ws_url):
        from websockets.sync.client import connect
        self.ws = connect(ws_url, max_size=50 * 2**20, open_timeout=10).__enter__()
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    def close(self):
        self.ws.close()


def _targets(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        return json.load(r)


def _wait(fn, timeout=30, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def grab_server(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "grab.web", "--no-browser", "--port", str(_free_port()), "--no-auto-update",
                             "--state-dir", str(tmp_path / "state"), "--out", str(tmp_path / "dl")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    port = int(re.search(r":(\d+)/", proc.stdout.readline()).group(1))
    yield port, tmp_path / "dl"
    proc.send_signal(signal.SIGTERM)
    proc.wait(10)


@pytest.fixture
def browser(tmp_path, site):
    exe, dbg = _browser(), _free_port()
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={dbg}", f"--user-data-dir={tmp_path / 'profile'}",
                             f"--load-extension={EXT_DIR}", f"--disable-extensions-except={EXT_DIR}",
                             "--no-first-run", "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait(lambda: _try_targets(dbg), 20, "browser to start")
        sw = _wait(lambda: next((t for t in _targets(dbg) if t["type"] == "service_worker" and EXT_ID in t["url"]), None),
                   20, "extension service worker (is the extension loading?)")
        yield dbg, sw
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _try_targets(port):
    try:
        return _targets(port) is not None
    except (urllib.error.URLError, ConnectionError, OSError):
        return False


def test_extension_finds_streams_and_grab_downloads_them(site, browser, grab_server, tmp_path):
    dbg, sw_target = browser
    grab_port, out = grab_server
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")  # talk to the test Grab, not your real one
    assert sw.js("grabCatcher.findGrab()") == grab_port
    with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/config") as r:
        key = json.load(r)["send_key"]
    _wait(lambda: sw.js("chrome.storage.local.get('grabKey').then(r => r.grabKey)") == key, 10, "extension to receive Grab's key")

    # open the player page in a new tab, like a user would
    page_url = f"{site}/player.html"
    with urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version") as r:
        browser_ws = json.load(r)["webSocketDebuggerUrl"]
    b = CDP(browser_ws)
    b.call("Target.createTarget", url=page_url)

    # the extension should list the HLS master (with its resolution) and the MP4, but not the preview clip
    def found():
        tabs = sw.js("JSON.stringify([...grabCatcher.tabs.values()])")
        items = [i for t in json.loads(tabs) for i in t["items"] if not i.get("hidden")]
        urls = {i["url"].split("/")[-1]: i for i in items}
        return urls if {"master.m3u8", "clip.mp4"} <= urls.keys() and urls["master.m3u8"].get("maxHeight") else None
    items = _wait(found, 30, "the extension to detect the page's streams")
    assert items["master.m3u8"]["kind"] == "HLS" and items["master.m3u8"]["maxHeight"] == 360
    _wait(lambda: found()["master.m3u8"].get("duration"), 10, "HLS duration")
    assert 5 < found()["master.m3u8"]["duration"] < 7  # read from the 360p playlist
    assert items["clip.mp4"]["kind"] == "MP4" and items["clip.mp4"]["size"] > 10_000_000
    assert "tiny-preview.mp4" not in items          # ignored: too small to be a real video
    assert "index.m3u8" not in items                # hidden: it's a quality level of master.m3u8

    # the toolbar badge shows how many streams were found
    tab_id = sw.js(f"chrome.tabs.query({{}}).then(ts => ts.find(t => t.url === '{page_url}').id)")
    assert sw.js(f"chrome.action.getBadgeText({{tabId: {tab_id}}})") == "2"

    # the popup lists them (screenshot saved next to the test output for a visual check)
    popup = b.call("Target.createTarget", url=f"chrome-extension://{EXT_ID}/popup.html?tab={tab_id}")["targetId"]
    pt = _wait(lambda: next((t for t in _targets(dbg) if t["id"] == popup), None), 10, "popup")
    pc = CDP(pt["webSocketDebuggerUrl"])
    _wait(lambda: pc.js("document.querySelectorAll('.item').length") == 2, 10, "popup to list 2 streams")
    assert "connected" in pc.js("document.querySelector('#conn').textContent")
    assert "My Test Video Page" in pc.js("document.querySelector('#ptitle').textContent")
    import base64
    pc.call("Emulation.setDeviceMetricsOverride", width=380, height=420, deviceScaleFactor=2, mobile=False)
    shot = pc.call("Page.captureScreenshot", format="png")["data"]
    (tmp_path / "popup.png").write_bytes(base64.b64decode(shot))
    os.environ["GRAB_POPUP_SHOT"] = str(tmp_path / "popup.png")

    # press Download on both (the same call the popup button makes)
    for name in ("master.m3u8", "clip.mp4"):
        r = sw.js(f"grabCatcher.send({{url: {json.dumps(items[name]['url'])}, page: {json.dumps(page_url)}, title: 'My Test Video Page'}})")
        assert r == {"ok": True, "via": "grab"}, r

    # Grab downloads both - which only works because the extension passed the page as Referer
    def jobs():
        with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/jobs") as r:
            js = json.load(r)["jobs"]
        return js if len(js) == 2 and all(j["status"] in ("done", "error") for j in js) else None
    done = _wait(jobs, 90, "Grab to finish both downloads")
    assert [j["status"] for j in done] == ["done", "done"], [j["error"] for j in done]
    for j in done:
        f = Path(j["files"][-1])
        assert f.parent == out / "127.0.0.1"                          # the page's site folder
        assert f.name.startswith("My Test Video Page [") and f.suffix == ".mp4"  # named after the page
        assert j["title"] == "My Test Video Page"
        assert duration(f) > 5
    for c in (pc, b, sw):
        c.close()


def test_auto_download_sends_what_the_page_plays_once(site, browser, grab_server):
    """Auto-download: off by default; switched on, the page's video goes to Grab by itself (waiting while
    Grab isn't running), and opening the page again doesn't download it twice."""
    dbg, sw_target = browser
    grab_port, out = grab_server
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")
    with urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version") as r:
        b = CDP(json.load(r)["webSocketDebuggerUrl"])
    page_url = f"{site}/player.html"

    def jobs():
        with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/jobs") as r:
            return json.load(r)["jobs"]

    def found():
        items = [i for t in json.loads(sw.js("JSON.stringify([...grabCatcher.tabs.values()])")) for i in t["items"]
                 if not i.get("hidden") and i.get("inspected", not i["manifest"])]
        return len(items) >= 2

    # off by default: the page plays, the extension lists its streams, nothing is downloaded
    b.call("Target.createTarget", url=page_url)
    _wait(found, 30, "the extension to detect the page's streams")
    time.sleep(4)
    assert jobs() == []
    tab_id = sw.js(f"chrome.tabs.query({{}}).then(ts => ts.find(t => t.url === '{page_url}').id)")

    # switched on while Grab isn't running: what the page already played waits in the extension
    sw.js(f"chrome.storage.local.set({{ports: [{_free_port()}]}})")
    status = sw.js(f"grabCatcher.setAuto({{tabId: {tab_id}, page: '{page_url}', site: true}})")
    assert status["on"] and status["site"] == "127.0.0.1" and not status["all"]
    pending = _wait(lambda: (lambda p: p if len(p) == 2 else None)(
        sw.js("chrome.storage.local.get('autoPending').then(r => r.autoPending || [])")), 20, "two videos to wait for Grab")
    assert {p["url"].rsplit("/", 1)[-1] for p in pending} == {"master.m3u8", "clip.mp4"}  # not the preview or the 360p level
    assert all(p["page"] == page_url and p["title"] == "My Test Video Page" for p in pending)
    assert jobs() == []

    # Grab is back: they go, and download like a click would have
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")
    assert sw.js("grabCatcher.findGrab()") == grab_port

    def finished():
        js = jobs()
        return js if len(js) == 2 and all(j["status"] in ("done", "error") for j in js) else None
    done = _wait(finished, 90, "Grab to finish the auto-downloads")
    assert [j["status"] for j in done] == ["done", "done"], [j["error"] for j in done]
    assert all(Path(j["files"][-1]).parent == out / "127.0.0.1" and j["title"] == "My Test Video Page" for j in done)
    assert sw.js("chrome.storage.local.get('autoPending').then(r => r.autoPending.length)") == 0

    # the same page again: the extension remembers these videos...
    b.call("Target.createTarget", url=page_url)
    time.sleep(7)
    assert len(jobs()) == 2
    # ...and if it forgot, Grab still wouldn't fetch them twice
    sw.js("chrome.storage.local.remove('autoSeen')")
    b.call("Target.createTarget", url=page_url)
    time.sleep(7)
    assert len(jobs()) == 2
    assert sw.js("chrome.storage.local.get('autoError').then(r => r.autoError)") == ""
    b.close()
    sw.close()


def test_everything_on_the_page(site, browser, grab_server, tmp_path):
    """The popup's "Everything on page": finds the page's pictures and videos (full-size and lazy ones too),
    hides icons, and Grab saves the ticked ones in a folder named after the page, each under its own name."""
    dbg, sw_target = browser
    grab_port, out = grab_server
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")
    with urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version") as r:
        b = CDP(json.load(r)["webSocketDebuggerUrl"])
    page_url = f"{site}/gallery.html"
    b.call("Target.createTarget", url=page_url)
    tab_id = _wait(lambda: sw.js(f"chrome.tabs.query({{}}).then(ts => (ts.find(t => t.url === '{page_url}' && t.status === 'complete') || {{}}).id)"),
                   20, "the gallery page to load")
    time.sleep(1)  # the video's size arrives just after the page

    popup = b.call("Target.createTarget", url=f"chrome-extension://{EXT_ID}/popup.html?tab={tab_id}")["targetId"]
    pc = CDP(_wait(lambda: next((t for t in _targets(dbg) if t["id"] == popup), None), 10, "popup")["webSocketDebuggerUrl"])
    _wait(lambda: pc.js("!!document.querySelector('#scanBtn') && !document.querySelector('#scanBtn').disabled"), 10, "popup")
    pc.js("document.querySelector('#scanBtn').click()")
    names = lambda: pc.js("[...document.querySelectorAll('.tile')].map(t => t.dataset.url.split('/').pop())")  # noqa: E731
    expect = {"big.png", "wide.jpg", "lazy.jpg", "large.jpg", "image.jpg", "noext?format=png", "bg.jpg", "ep1.mp4"}
    _wait(lambda: expect <= {n for n in names()}, 15, "the popup to list the page's pictures and video")
    shown = names()
    assert "icon.png" not in shown and "thumb.jpg" not in shown and "small.jpg" not in shown  # icon; smaller copies
    assert len(shown) == 9 and shown.count("image.jpg") == 2
    assert pc.js("document.querySelector('#main, .page').hidden") and not pc.js("document.querySelector('#everything').hidden")

    pc.js("document.querySelector('#minSize').value = '0'; document.querySelector('#minSize').dispatchEvent(new Event('change'))")
    assert "icon.png" in names()                                      # any size: the icon too
    pc.js("document.querySelector('#minSize').value = '200'; document.querySelector('#minSize').dispatchEvent(new Event('change'))")
    pc.js("document.querySelector('[data-k=video]').click()")
    assert names() == ["ep1.mp4"]
    pc.js("document.querySelector('[data-k=all]').click()")
    pc.js("[...document.querySelectorAll('.tile')].find(t => t.dataset.url.endsWith('bg.jpg')).click()")  # untick one
    assert pc.js("document.querySelector('#getSel').textContent") == "Download 8"
    import base64
    pc.call("Emulation.setDeviceMetricsOverride", width=380, height=560, deviceScaleFactor=2, mobile=False)
    (tmp_path / "everything.png").write_bytes(base64.b64decode(pc.call("Page.captureScreenshot", format="png")["data"]))
    pc.js("document.querySelector('#getSel').click()")
    _wait(lambda: pc.js("document.querySelector('#getSel').textContent") == "Added 8 ✓", 10, "Grab to take them")

    def finished():
        with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/jobs") as r:
            js = json.load(r)["jobs"]
        kids = [j for j in js if j["parent"]]
        return js if len(kids) == 8 and all(j["status"] in ("done", "error", "skipped") for j in kids) else None
    jobs = _wait(finished, 60, "Grab to download them")
    (group,) = [j for j in jobs if not j["parent"]]
    assert group["title"] == "8 files from Holiday Photos" and group["children"] == 8
    assert all(j["status"] == "done" for j in jobs if j["parent"]), [(j["url"], j["error"]) for j in jobs]
    folder = out / "127.0.0.1" / "Holiday Photos"
    assert {f.name for f in folder.iterdir()} == {"big.png", "wide.jpg", "lazy.jpg", "large.jpg", "image.jpg", "image (2).jpg",
                                                  "noext.png", "ep1.mp4"}
    assert (folder / "image.jpg").read_bytes() != (folder / "image (2).jpg").read_bytes()  # two pictures, both kept
    for c in (pc, b, sw):
        c.close()


def test_only_the_extension_may_add(site, grab_server):
    port, _ = grab_server
    body = json.dumps({"url": f"{site}/files/data.zip"}).encode()
    for origin in ("https://evil.example", None, "chrome-extension://someotherextensionidxxxxxxxxxxx"):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ext/add", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
    ok = urllib.request.Request(f"http://127.0.0.1:{port}/api/ext/add", data=body, method="POST",
                                headers={"Content-Type": "application/json", "Origin": f"chrome-extension://{EXT_ID}"})
    with urllib.request.urlopen(ok) as r:
        assert json.load(r)["added"] == 1
        assert r.headers["Access-Control-Allow-Origin"] == f"chrome-extension://{EXT_ID}"


def test_detection_unit_tests_pass():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    r = subprocess.run(["node", "--test", str(Path(__file__).with_name("test_extension.mjs"))], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.online
def test_extension_on_a_real_player_site(browser):
    """A real third-party player (the hls.js demo page) - the extension should find its HLS stream."""
    dbg, sw_target = browser
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    with urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version") as r:
        b = CDP(json.load(r)["webSocketDebuggerUrl"])
    stream = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
    b.call("Target.createTarget", url=f"https://hlsjs.video-dev.org/demo/?src={urllib.parse.quote(stream, safe='')}")

    def found():
        items = [i for t in json.loads(sw.js("JSON.stringify([...grabCatcher.tabs.values()])")) for i in t["items"] if not i.get("hidden")]
        return next((i for i in items if i["url"] == stream and i.get("maxHeight")), None)
    item = _wait(found, 45, "the extension to spot the demo player's stream")
    assert item["kind"] == "HLS" and item["maxHeight"] >= 720
    assert not [i for t in json.loads(sw.js("JSON.stringify([...grabCatcher.tabs.values()])")) for i in t["items"]
                if not i.get("hidden") and i["url"].endswith(".ts")]  # no segment spam
    sw.close()
    b.close()


@pytest.mark.online
def test_popup_shows_the_video_on_a_youtube_page(browser, grab_server):
    """YouTube's streams are skipped on purpose, so the popup must show the video itself, not "Nothing yet",
    and its Download button sends just that video (no playlist, no start time)."""
    dbg, sw_target = browser
    grab_port, _ = grab_server
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")
    with urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version") as r:
        b = CDP(json.load(r)["webSocketDebuggerUrl"])
    b.call("Target.createTarget", url="https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb&t=3s")
    tab_id = _wait(lambda: sw.js("chrome.tabs.query({}).then(ts => (ts.find(t => t.url.includes('jNQXAC9IVRw')) || {}).id)"),
                   30, "the YouTube tab")
    popup = b.call("Target.createTarget", url=f"chrome-extension://{EXT_ID}/popup.html?tab={tab_id}")["targetId"]
    pc = CDP(_wait(lambda: next((t for t in _targets(dbg) if t["id"] == popup), None), 10, "popup")["webSocketDebuggerUrl"])
    _wait(lambda: pc.js("!!document.querySelector('#sendVideo')"), 15, "the video to show in the popup")
    assert pc.js("document.querySelector('#listHead').textContent") == "Video on this page"
    assert "Nothing yet" not in pc.js("document.body.textContent")
    pc.js("document.querySelector('#sendVideo').click()")

    def jobs():
        with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/jobs") as r:
            return json.load(r)["jobs"]
    (job,) = _wait(jobs, 15, "Grab to get the video")
    assert job["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    for c in (pc, b, sw):
        c.close()


def test_extension_shares_only_chosen_logins(browser, grab_server, tmp_path):
    """Fake logins in a throwaway browser profile: only the chosen sites reach Grab; logout/login re-shares; off deletes."""
    dbg, sw_target = browser
    grab_port, _ = grab_server
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")
    for url, name, value in [("https://www.youtube.com/", "SID", "fake-yt"), ("https://www.instagram.com/", "sessionid", "fake-ig"),
                             ("https://example.com/", "private", "do-not-share")]:
        sw.js(f"chrome.cookies.set({{url: '{url}', name: '{name}', value: '{value}', expirationDate: 2000000000}})")
    assert sw.js(f"grabCatcher.shareLogins({grab_port}, true)") == ["YouTube", "Instagram"]

    def shared():
        with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/config") as r:
            return json.load(r)["shared_logins"]
    state = Path(os.environ.get("GRAB_TEST_STATE", "")) if os.environ.get("GRAB_TEST_STATE") else None
    info = shared()
    assert info["sites"] == ["Instagram", "YouTube"] and info["browser"]
    cookies_txt = (tmp_path / "state" / "logins" / "extension-cookies.txt").read_text()
    assert "fake-yt" in cookies_txt and "fake-ig" in cookies_txt and "do-not-share" not in cookies_txt

    # logging in to X in the browser is picked up by itself within a few seconds
    sw.js("chrome.cookies.set({url: 'https://x.com/', name: 'auth_token', value: 'fake-x', expirationDate: 2000000000})")
    _wait(lambda: "X" in shared().get("sites", []), 15, "the new X login to be shared")

    # switching sharing off deletes Grab's copy
    sw.js("grabCatcher.stopSharing()")
    _wait(lambda: shared() == {}, 10, "Grab's copy to be deleted")
    assert not (tmp_path / "state" / "logins" / "extension-cookies.txt").exists()
    sw.close()


def test_extension_shares_by_itself_when_pages_load(site, browser, grab_server, tmp_path):
    """No popup, no button: loading any page while Grab runs is enough for the logins to reach Grab."""
    dbg, sw_target = browser
    grab_port, _ = grab_server
    sw = CDP(sw_target["webSocketDebuggerUrl"])
    sw.js(f"chrome.storage.local.set({{ports: [{grab_port}]}})")
    sw.js("chrome.cookies.set({url: 'https://www.youtube.com/', name: 'SID', value: 'fake-auto', expirationDate: 2000000000})")
    sw.js("grabCatcher.resetThrottle()")
    with urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version") as r:
        b = CDP(json.load(r)["webSocketDebuggerUrl"])
    b.call("Target.createTarget", url=f"{site}/page.html")       # just browsing

    def shared():
        with urllib.request.urlopen(f"http://127.0.0.1:{grab_port}/api/config") as r:
            return json.load(r)["shared_logins"].get("sites", [])
    _wait(lambda: "YouTube" in shared(), 20, "the extension to share by itself")
    assert sw.js("chrome.storage.local.get('shareError').then(r => r.shareError)") == ""
    b.close()
    sw.close()

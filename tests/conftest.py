"""Shared fixtures: a local test website (no internet needed) and helpers to run downloads.

The test site serves real media made with ffmpeg plus a few deliberately awkward endpoints:
  /files/...   normal static files with Range support
  /picky/...   rejects Range requests bigger than 4 MB with 503 (forces the auto-fix path)
  /slow/...    sends at ~1 MB/s so a download can be paused mid-way
  /protected/  video bytes only with the player page as Referer (403 otherwise), like many video CDNs
  /player.html a video page (for the browser-extension test) playing protected MP4 + HLS
  /feed.xml    an RSS feed (newest first) of the episodes listed in feed.txt - tests add episodes to it
  /page.html   a web page with no media in it
  anything missing -> 404
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from grab.core import Manager, Options


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_grab_folder(tmp_path_factory):
    """Tests get their own Grab folder; fail loudly if anything writes to the real one."""
    import os
    os.environ["GRAB_STATE_DIR"] = str(tmp_path_factory.mktemp("grab-state"))
    real = Path.home() / "Library/Application Support/Grab"
    before = {p: p.stat().st_mtime for p in real.rglob("*") if p.is_file() and "runtime" not in p.parts} if real.exists() else {}
    yield
    after = {p: p.stat().st_mtime for p in real.rglob("*") if p.is_file() and "runtime" not in p.parts} if real.exists() else {}
    app_files = {"history.json", "prefs.json", "update.json", "watch.json"}  # the running app saves these itself
    changed = [str(p) for p in after if (p not in before and p.name not in app_files and not p.name.endswith(".tmp"))
               or ("logins" in p.parts and before.get(p) != after[p])]
    assert not changed, f"tests touched the real Grab folder: {changed}"


def pytest_addoption(parser):
    parser.addoption("--online", action="store_true", help="also run tests that use real sites (YouTube, Flickr)")


def pytest_configure(config):
    config.addinivalue_line("markers", "online: needs the internet; run with --online")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--online"):
        return
    skip = pytest.mark.skip(reason="needs --online")
    for item in items:
        if "online" in item.keywords:
            item.add_marker(skip)


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> Path:
    """Generate the test site's files once per test run."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("site")
    (root / "files").mkdir()
    # 12 s noisy video (~13 MB) so it is bigger than one 10 MB download chunk
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "12", "-vf", "noise=alls=60:allf=t", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "10M",
            "-minrate", "10M", "-maxrate", "10M", "-bufsize", "4M", "-c:a", "aac", "-shortest", str(root / "files/clip.mp4"))
    # a short HLS stream (playlist + 1-second segments)
    (root / "files/hls").mkdir()
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=330", "-t", "6",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "hls", "-hls_time", "1",
            "-hls_playlist_type", "vod", str(root / "files/hls/index.m3u8"))
    (root / "files/data.zip").write_bytes(os.urandom(3 * 1024 * 1024))
    (root / "files/big.bin").write_bytes(os.urandom(6 * 1024 * 1024))
    (root / "page.html").write_text("<html><body><h1>No video here</h1><p>Just text.</p></body></html>")
    for n in range(1, 5):  # short episodes for the watch-list tests
        _ffmpeg("-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15", "-f", "lavfi", "-i", f"sine=frequency={200 + 100 * n}",
                "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(root / f"files/ep{n}.mp4"))
    (root / "feed.txt").write_text("ep2\nep1\n")
    # sources YouTube serves at 1440p/4K: VP9 (WebM, Opus audio) and 10-bit HDR AV1
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine=frequency=500", "-t", "3",
            "-c:v", "libvpx-vp9", "-b:v", "2M", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus", "-shortest",
            str(root / "files/vp9.webm"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i", "sine=frequency=600", "-t", "3",
            "-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p10le", "-svtav1-params",
            "color-primaries=9:transfer-characteristics=16:matrix-coefficients=9", "-c:a", "aac", "-shortest",
            str(root / "files/hdr_av1.mp4"))
    (root / "files/hls/master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nindex.m3u8\n")
    (root / "files/tiny-preview.mp4").write_bytes((root / "files/clip.mp4").read_bytes()[:100_000])
    (root / "player.html").write_text("""<!doctype html><html><head><title>My Test Video Page</title></head><body>
<h1>Player</h1>
<video id="v" src="/protected/clip.mp4" autoplay muted playsinline></video>
<script>
  // what a typical HLS player does: load the master playlist, then the quality it picks
  fetch("/protected/hls/master.m3u8").then(r => r.text()).then(() => fetch("/protected/hls/index.m3u8"));
  fetch("/files/tiny-preview.mp4");  // a hover-preview clip that should NOT be offered
</script></body></html>""")
    # a photo page for the extension's "Everything on this page"
    pics = root / "files/pics"
    for name, size in [("big.png", "1200x800"), ("icon.png", "16x16"), ("thumb.jpg", "160x90"), ("wide.jpg", "1600x900"),
                       ("lazy.jpg", "800x600"), ("small.jpg", "300x200"), ("large.jpg", "1200x800"), ("bg.jpg", "900x450"),
                       ("a/image.jpg", "400x300"), ("b/image.jpg", "500x300"), ("noext.png", "400x300")]:
        (pics / name).parent.mkdir(parents=True, exist_ok=True)
        _ffmpeg("-f", "lavfi", "-i", f"testsrc=size={size}", "-frames:v", "1", str(pics / name))
    (pics / "noext.png").rename(pics / "noext")  # like X's pictures: .../media/ID?format=png
    (root / "gallery.html").write_text("""<!doctype html><html><head><title>Holiday Photos</title>
<meta property="og:image" content="/files/pics/big.png">
<style>.hero { width: 600px; height: 300px; background: url('/files/pics/bg.jpg') center / cover; }</style></head><body>
<div class="hero"></div>
<img src="/files/pics/big.png" alt="Beach">
<img src="/files/pics/icon.png" width="16" height="16" alt="logo">
<a href="/files/pics/wide.jpg"><img src="/files/pics/thumb.jpg" alt="Sunset"></a>
<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" data-src="/files/pics/lazy.jpg" alt="Later">
<img src="/files/pics/small.jpg" srcset="/files/pics/small.jpg 300w, /files/pics/large.jpg 1200w" sizes="300px" alt="Mountain">
<img src="/files/pics/a/image.jpg"><img src="/files/pics/b/image.jpg">
<img src="/files/pics/noext?format=png">
<video src="/files/ep1.mp4" muted preload="metadata"></video>
</body></html>""")
    return root


class _Site(BaseHTTPRequestHandler):
    root: Path

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve()

    def _serve(self, head=False):
        path = self.path.split("?")[0]
        mode = "normal"
        if path == "/feed.xml":
            base = f"http://{self.headers.get('Host')}"
            eps = [e for e in (self.root / "feed.txt").read_text().split() if e]
            items = "".join(f"<item><title>Episode {e[2:]}</title><guid>{e}</guid>"
                            f'<enclosure url="{base}/files/{e}.mp4" type="video/mp4" length="1"/></item>' for e in eps)
            body = (f'<?xml version="1.0"?><rss version="2.0"><channel><title>Test Podcast</title>'
                    f"<link>{base}/</link>{items}</channel></rss>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return
        if path.startswith("/protected/"):
            path = "/files/" + path[len("/protected/"):]
            needs = f"http://{self.headers.get('Host')}/player.html"
            if not path.endswith(".m3u8") and self.headers.get("Referer") != needs:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        for prefix in ("/picky/", "/slow/"):
            if path.startswith(prefix):
                mode, path = prefix.strip("/"), "/files/" + path[len(prefix):]
        f = self.root / path.lstrip("/")
        if not f.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = f.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
            end = min(end, size - 1)
            if mode == "picky" and end - start + 1 > 4 * 1024 * 1024:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        ctype = {".mp4": "video/mp4", ".html": "text/html", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
                 ".zip": "application/zip", ".png": "image/png", ".jpg": "image/jpeg"}.get(f.suffix, "application/octet-stream")
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(64 * 1024, left))
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)
                if mode == "slow":
                    time.sleep(0.06)  # ~1 MB/s per connection


@pytest.fixture(scope="session")
def site(media):
    handler = type("Site", (_Site,), {"root": media})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def out(tmp_path) -> Path:
    return tmp_path / "downloads"


def run(urls, out_dir, timeout=120, mgr=None, **opts):
    """Download urls with a fresh Manager and return the finished jobs."""
    mgr = mgr or Manager(4)
    jobs = mgr.add(urls, Options(out_dir=str(out_dir), **opts))
    deadline = time.time() + timeout
    while not mgr.idle():
        assert time.time() < deadline, f"timed out: {[(j.status, j.stage) for j in mgr.jobs.values()]}"
        time.sleep(0.2)
    return jobs


def duration(path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())

"""Real-site checks. Skipped unless you run:  pytest --online

These break when a site changes (or blocks your network) - which is exactly what they're for:
if one fails, run `grab --update` and try again.
"""

from __future__ import annotations

import time

import pytest

from grab.core import Manager
from grab.updater import latest

from conftest import duration, run

pytestmark = pytest.mark.online


def test_youtube_video(out):
    (job,) = run(["https://www.youtube.com/watch?v=jNQXAC9IVRw"], out, quality="360", archive=False)
    assert job.status == "done", job.error
    assert job.title == "Me at the zoo" and job.thumbnail.startswith("https://")
    assert 18 < duration(job.files[-1]) < 20


def test_youtube_channel_fans_out_in_parallel(out):
    mgr = Manager(4)
    (parent,) = run(["https://www.youtube.com/@NASA/videos"], out, mgr=mgr, items="1-2", quality="360", archive=False, timeout=300)
    kids = [j for j in mgr.jobs.values() if j.parent == parent.id]
    assert parent.status == "done" and parent.children == 2
    assert [k.status for k in kids] == ["done", "done"], [k.error for k in kids]


def test_youtube_audio(out):
    (job,) = run(["https://www.youtube.com/watch?v=jNQXAC9IVRw"], out, mode="audio", audio_format="m4a", archive=False)
    assert job.status == "done", job.error
    assert job.files[-1].endswith(".m4a")


def test_image_gallery_falls_back_to_gallery_dl(out):
    (job,) = run(["https://www.flickr.com/photos/nasa2explore/"], out, items="1-2", archive=False, timeout=180)
    assert job.status == "done", job.error
    assert job.tool == "gallery-dl" and len(job.files) == 2


def test_pypi_reachable_for_updates():
    assert latest("yt-dlp")


def test_profile_download_photos_or_videos_only(out):
    (job,) = run(["https://bsky.app/profile/bsky.app"], out, items="1-3", media="videos", archive=False, timeout=180)
    assert job.status == "done", job.error
    assert job.tool == "gallery-dl" and job.files
    assert all(f.endswith((".mp4", ".webm", ".mov")) for f in job.files), job.files


def test_watch_youtube_channel_only_new(out, tmp_path):
    from grab.core import Options
    from grab.watch import Watcher
    w = Watcher(Manager(4), tmp_path / "watch.json", lambda: Options(out_dir=str(out), quality="360"), tick_seconds=0.5)
    src = w.add("https://www.youtube.com/@NASA/videos", start="new", scan=5)
    deadline = time.time() + 120
    w.tick()
    while w.sources[src.id].state != "idle":
        assert time.time() < deadline and w.sources[src.id].state != "error", w.sources[src.id].last_result
        time.sleep(0.5)
    assert "5 existing" in w.sources[src.id].last_result
    w.check_now(src.id)                     # a real check right after: nothing is new
    w.tick()
    while w.sources[src.id].state == "checking" or not w.mgr.idle():
        assert time.time() < deadline
        time.sleep(0.5)
        w.tick()
    assert w.sources[src.id].last_result == "Nothing new"
    assert not list(out.rglob("*.mp4"))
    assert w.mgr.snapshot() == []           # the check's "already had" rows were tidied away


def test_watch_bluesky_profile_latest(out, tmp_path):
    from grab.core import Options
    from grab.watch import Watcher
    w = Watcher(Manager(4), tmp_path / "watch.json", lambda: Options(out_dir=str(out)), tick_seconds=0.5)
    src = w.add("https://bsky.app/profile/bsky.app", start="latest", latest=2, media="videos", scan=30)
    deadline = time.time() + 180
    while True:
        w.tick()
        s = w.sources[src.id]
        if s.primed and s.state in ("idle", "error") and s.last_check and w.mgr.idle():
            break
        assert time.time() < deadline, (s.state, s.last_result)
        time.sleep(0.5)
    assert s.state == "idle" and s.new_total == 2, s.last_result   # exactly the newest 2 videos, nothing older
    assert all(f.endswith(".mp4") for j in w.mgr.snapshot() for f in j["files"])
    w.check_now(src.id)
    w.tick()
    while w.sources[src.id].state == "checking" or not w.mgr.idle():
        assert time.time() < deadline
        time.sleep(0.5)
        w.tick()
    assert w.sources[src.id].last_result == "Nothing new"


def test_youtube_4k_clip_is_compatible_with_cover_art(out):
    """10 s of a 4K video: clip + AV1/VP9 -> HEVC + thumbnail as cover art + SponsorBlock check."""
    import json as _json
    import subprocess as _sp
    (job,) = run(["https://www.youtube.com/watch?v=aqz-KE-bpKQ"], out, clip="0:30-0:40", archive=False, timeout=300)
    if "not a bot" in job.error:
        pytest.skip("YouTube is bot-checking this network; needs a browser login (Grab offers it as a one-click fix)")
    assert job.status == "done", job.error
    f = job.files[-1]
    streams = _json.loads(_sp.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,disposition", "-of", "json", f],
                                  capture_output=True, text=True).stdout)["streams"]
    video = [s for s in streams if s.get("width") and not s.get("disposition", {}).get("attached_pic")]
    cover = [s for s in streams if s.get("disposition", {}).get("attached_pic")]
    assert video[0]["codec_name"] in ("hevc", "h264") and video[0]["width"] == 3840  # exact cuts re-encode to H.264
    assert cover, "thumbnail wasn't embedded"
    assert 9 < duration(f) < 11.5
    assert any("SponsorBlock" in l for l in job.log)

"""Watch list: checks find only new posts, first-check options, persistence, CLI."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import grab.core as core
from grab.core import Manager, Options, friendly_name, prefers_gallery
from grab.watch import Watcher


@pytest.fixture
def feed(site, media):
    """The test feed; call feed.set('ep3', 'ep2', 'ep1') to publish episodes (newest first)."""
    original = (media / "feed.txt").read_text()

    class Feed:
        url = f"{site}/feed.xml"

        @staticmethod
        def set(*eps):
            (media / "feed.txt").write_text("\n".join(eps) + "\n")
    yield Feed
    (media / "feed.txt").write_text(original)


def watcher(tmp_path, out):
    mgr = Manager(3)
    return Watcher(mgr, tmp_path / "watch.json", lambda: Options(out_dir=str(out)), tick_seconds=0.2)


def run_check(w, sid, timeout=90):
    """Run one check of a source to completion."""
    w.check_now(sid)
    deadline = time.time() + timeout
    w.tick()
    while True:
        w.tick()
        src = w.sources[sid]
        if src.state in ("idle", "error") and w.mgr.idle():
            return src
        assert time.time() < deadline, (src.state, src.last_result)
        time.sleep(0.2)


def downloaded(out):
    return sorted(p.name.split(" [")[0] for p in Path(out).rglob("*.mp4"))


# ---------------------------------------------------------------- what counts as a profile / names

@pytest.mark.parametrize("url,profile", [
    ("https://www.instagram.com/nasa/", True), ("https://www.instagram.com/p/ABC/", False),
    ("https://www.reddit.com/r/pics/", True), ("https://www.reddit.com/r/pics/top/", True),
    ("https://www.reddit.com/r/pics/comments/abc/t/", False), ("https://www.reddit.com/user/spez/submitted/", True),
    ("https://x.com/NASA", True), ("https://x.com/NASA/media", True), ("https://x.com/NASA/status/1", False),
    ("https://bsky.app/profile/nasa.gov", True), ("https://www.youtube.com/@NASA", False),
])
def test_profile_links_go_to_gallery_dl(url, profile):
    assert prefers_gallery(url) is profile


@pytest.mark.parametrize("url,name", [
    ("https://www.reddit.com/r/pics/", "r/pics"), ("https://www.reddit.com/user/spez/", "u/spez"),
    ("https://www.instagram.com/nasa/", "@nasa (Instagram)"), ("https://x.com/NASA/media", "@NASA (Twitter)"),
    ("https://bsky.app/profile/nasa.gov", "@nasa.gov (Bluesky)"), ("https://www.youtube.com/@NASA", "@NASA (YouTube)"),
])
def test_friendly_names(url, name):
    assert friendly_name(url) == name


# ---------------------------------------------------------------- checks

def test_only_new_skips_existing_then_downloads_new_posts(tmp_path, out, feed):
    feed.set("ep2", "ep1")
    w = watcher(tmp_path, out)
    src = w.add(feed.url, start="new")
    src = run_check(w, src.id)                       # first check: just remembers ep1, ep2
    assert src.state == "idle" and "2 existing" in src.last_result, src.last_result
    assert downloaded(out) == []

    feed.set("ep3", "ep2", "ep1")                    # a new episode is published
    src = run_check(w, src.id)
    assert src.last_result == "1 new file", src.last_result
    assert downloaded(out) == ["Episode 3"]
    assert src.new_total == 1

    src = run_check(w, src.id)                       # nothing new since
    assert src.last_result == "Nothing new"
    assert downloaded(out) == ["Episode 3"]
    # the check's "already had" rows were tidied away; only the real download is listed
    assert [j["title"] for j in w.mgr.snapshot() if not j["children"]] == ["Episode 3"]


def test_latest_n_then_new(tmp_path, out, feed):
    feed.set("ep3", "ep2", "ep1")
    w = watcher(tmp_path, out)
    src = run_check(w, w.add(feed.url, start="latest", latest=2).id)
    assert downloaded(out) == ["Episode 2", "Episode 3"] and src.last_result == "2 new files"
    feed.set("ep4", "ep3", "ep2", "ep1")
    run_check(w, src.id)
    assert downloaded(out) == ["Episode 2", "Episode 3", "Episode 4"]  # ep1 stays skipped: older than "latest 2"


def test_everything(tmp_path, out, feed):
    feed.set("ep3", "ep2", "ep1")
    w = watcher(tmp_path, out)
    src = run_check(w, w.add(feed.url, start="all").id)
    assert downloaded(out) == ["Episode 1", "Episode 2", "Episode 3"] and src.new_total == 3


def test_audio_format_per_source(tmp_path, out, feed):
    feed.set("ep2", "ep1")
    w = watcher(tmp_path, out)
    run_check(w, w.add(feed.url, start="latest", latest=1, preset="mp3").id)
    assert [p.suffix for p in Path(out).rglob("Episode*")] == [".mp3"]


def test_broken_source_reports_error(tmp_path, out, site):
    w = watcher(tmp_path, out)
    src = run_check(w, w.add(f"{site}/no-such-feed.xml", start="all").id)
    assert src.state == "error" and "404" in src.last_result
    assert src.next_check > time.time()               # tries again later


def test_sources_survive_restart(tmp_path, out, feed):
    w = watcher(tmp_path, out)
    src = w.add(feed.url, every_hours=12, preset="m4a", media="videos")
    with pytest.raises(ValueError):
        w.add(feed.url)                                # no duplicates
    w.update(src.id, enabled=False)
    again = Watcher(Manager(1), tmp_path / "watch.json", lambda: Options(out_dir=str(out)))
    s = again.sources[src.id]
    assert (s.every_hours, s.preset, s.media, s.enabled, s.name) == (12, "m4a", "videos", False, src.name)


def test_disabled_sources_are_not_checked(tmp_path, out, feed):
    w = watcher(tmp_path, out)
    src = w.add(feed.url, start="all")
    w.update(src.id, enabled=False)
    w.tick()
    time.sleep(1)
    assert w.sources[src.id].state == "new" and w.mgr.idle() and downloaded(out) == []


def test_cli_add_list_run_remove(tmp_path, out, feed):
    feed.set("ep2", "ep1")
    env = {**os.environ, "GRAB_STATE_DIR": str(tmp_path / "state"), "GRAB_NO_APP": "1"}
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prefs.json").write_text(f'{{"out_dir": "{out}"}}')
    g = lambda *a: subprocess.run([sys.executable, "-m", "grab.cli", "watch", *a], capture_output=True, text=True, env=env, timeout=120)  # noqa: E731
    r = g("add", feed.url, "--latest", "1", "--name", "Test Podcast")
    assert r.returncode == 0 and "Watching Test Podcast" in r.stdout, r.stdout + r.stderr
    assert "Test Podcast" in g("list").stdout
    r = g("run")
    assert "Test Podcast: 1 new file" in r.stdout, r.stdout + r.stderr
    assert downloaded(out) == ["Episode 2"]
    assert "Stopped watching" in g("remove", "Test Podcast").stdout
    assert "Not watching anything" in g("list").stdout

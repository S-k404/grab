"""Download engine tests against the local test site (no internet needed)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import grab.core as core
from grab.core import Manager, Options, classify, ladder, parse_urls
from grab.updater import looks_outdated

from conftest import duration, run, sha


def test_deep_folders_keep_the_whole_file_name(site, tmp_path):
    """yt-dlp's trim_file_name counts the folder path too, which once cut "[id]" off names in deep folders."""
    deep = tmp_path / ("a-rather-long-folder-name-" * 4) / ("and-another-long-folder-" * 4)
    (job,) = run([f"{site}/files/hls/index.m3u8"], deep)
    assert job.status == "done", job.error
    f = job.files[-1]
    assert len(f) > 230 and Path(f).name.endswith("].mp4"), f


# ---------------------------------------------------------------- pure logic

def test_batch_names_are_unique_and_have_extensions():
    names = core.batch_names([("https://cdn.a.com/x/image.jpg", "image"), ("https://cdn.b.com/y/image.jpg?w=2", "image"),
                              ("https://cdn.a.com/z/IMAGE.jpg", "image"), ("https://pbs.twimg.com/media/GabcXYZ?format=png&name=orig", "image"),
                              ("https://v.site.com/play/123", "video"), ("https://s.com/caf%C3%A9.webp", "image")])
    assert list(names.values()) == ["image.jpg", "image (2).jpg", "IMAGE (3).jpg", "GabcXYZ.png", "123.mp4", "café.webp"]


def test_page_title_names_fit_and_stay_literal():
    """Names for raw streams come from the page title: kept under macOS's 255-byte limit even in Japanese or
    emoji, and a "%" in the title is kept as-is rather than read as a yt-dlp template field."""
    import yt_dlp
    for title in ("動画" * 200, "🎬" * 200, "Sale 50% off %(id)s"):
        name = core._hint_name(Options(title=title), "https://cdn.site.com/master.m3u8")
        real = yt_dlp.YoutubeDL({"quiet": True}).evaluate_outtmpl(name, {"ext": "mp4", "id": "ID", "title": "T"})
        assert len(real.encode()) < 255 and real.endswith(".mp4"), real
    assert real.startswith("Sale 50% off %(id)s [")  # what the page said, not "Sale 50% off ID ["


def test_parse_urls_handles_messy_input():
    text = """
    # comment line
    https://youtube.com/watch?v=a   https://x.com/u/status/1
    youtu.be/abc
    https://youtube.com/watch?v=a
    https://t
    <https://example.com/video.mp4>
    not a link
    """
    assert parse_urls(text) == [
        "https://youtube.com/watch?v=a", "https://x.com/u/status/1", "https://youtu.be/abc", "https://example.com/video.mp4",
    ]


@pytest.mark.parametrize("wrapped,real", [
    ("https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fio745donghvh1.jpeg", "https://i.redd.it/io745donghvh1.jpeg"),
    ("https://preview.redd.it/io745donghvh1.jpeg?width=640&auto=webp&s=abc", "https://i.redd.it/io745donghvh1.jpeg"),
    ("https://preview.redd.it/a-nice-title-v0-io745donghvh1.png?width=1080", "https://i.redd.it/io745donghvh1.png"),
    ("https://pbs.twimg.com/media/GabcXYZ?format=jpg&name=small", "https://pbs.twimg.com/media/GabcXYZ?format=jpg&name=orig"),
    ("https://www.google.com/url?q=https://youtu.be/abc&sa=D", "https://youtu.be/abc"),
    ("https://l.facebook.com/l.php?u=https%3A%2F%2Fvimeo.com%2F123&h=x", "https://vimeo.com/123"),
    ("https://www.youtube.com/redirect?event=x&q=https%3A%2F%2Fexample.com%2Fa.zip", "https://example.com/a.zip"),
    # normal links stay as they are
    ("https://www.reddit.com/r/pics/comments/abc/title/", "https://www.reddit.com/r/pics/comments/abc/title/"),
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
])
def test_wrapper_links_are_unwrapped(wrapped, real):
    assert core.unwrap_url(wrapped) == real
    assert parse_urls(wrapped) == [real]


def test_retry_of_old_wrapped_link_unwraps(site, media, out):
    from urllib.parse import quote
    wrapped = f"https://www.reddit.com/media?url={quote(site + '/files/data.zip', safe='')}"
    job = core.Job(url=wrapped, opts=Options(out_dir=str(out)))  # as if saved in history before the fix
    mgr = Manager(1)
    with mgr.cv:
        mgr.jobs[job.id] = job
        mgr.queue.append(job.id)
        mgr.cv.notify_all()
    while not mgr.idle():
        time.sleep(0.1)
    assert job.status == "done", job.error
    assert job.url == site + "/files/data.zip"
    assert sha(job.files[-1]) == sha(media / "files/data.zip")


def test_items_spec_selects_playlist_entries():
    entries = list(range(1, 51))
    assert core._pick(entries, "") == entries
    assert core._pick(entries, "1-3,10") == [1, 2, 3, 10]
    assert core._pick(entries, "48-") == [48, 49, 50]
    assert core._pick(iter(range(1, 10**9)), "2-4") == [2, 3, 4]  # stops early on huge channels


@pytest.mark.parametrize("msg,kind", [
    ("HTTP Error 429: Too Many Requests", "network"),
    ("Got error: HTTP Error 503. Giving up after 10 retries", "network"),
    ("HTTP Error 403: Forbidden", "network"),
    ("Sign in to confirm your age", "login"),
    ("Requested format is not available", "format"),
    ("Postprocessing: Conversion failed!", "format"),
    ("Unable to download webpage: HTTP Error 404: Not Found", "gone"),
    ("Unsupported URL: https://example.com", "novideo"),
    ("No video could be found in this tweet", "novideo"),
    ("something odd happened", "other"),
    ("Sign in to confirm you're not a bot. Use --cookies-from-browser", "bot"),
])
def test_error_classifier(msg, kind):
    assert classify(msg) == kind


def test_fallback_ladders():
    yt = core.Job(url="https://www.youtube.com/watch?v=x", opts=Options())
    other = core.Job(url="https://vimeo.com/1", opts=Options())
    assert ladder("network", yt, [])[:2] == ["gentle", "client"]
    assert "client" not in ladder("network", other, [])
    assert ladder("format", other, [])[0] == "simple"
    assert ladder("login", yt, []) == []
    assert ladder("network", other, ["gentle"])[0] != "gentle"


def test_outdated_detection():
    assert looks_outdated("Unable to extract nsig function code; please report this issue")
    assert not looks_outdated("HTTP Error 404: Not Found")


def test_options_roundtrip():
    o = Options(mode="audio", fragments=3, smart=False, items="1-5")
    assert Options.from_dict(o.to_dict()) == o
    assert Options.from_dict({"fragments": "7", "smart": "false", "bogus": 1}).fragments == 7
    assert Options.from_dict({"smart": "false"}).smart is False


# ---------------------------------------------------------------- real downloads from the local site

def test_progressive_mp4(site, out):
    (job,) = run([f"{site}/files/clip.mp4"], out)
    assert job.status == "done", job.error
    assert job.tool == "yt-dlp"
    f = Path(job.files[-1])
    assert f.suffix == ".mp4" and f.stat().st_size > 10_000_000
    assert abs(duration(f) - 12) < 0.5


def test_hls_stream_parallel_fragments(site, out):
    (job,) = run([f"{site}/files/hls/index.m3u8"], out, fragments=8)
    assert job.status == "done", job.error
    assert abs(duration(job.files[-1]) - 6) < 0.6


def test_audio_only_mp3(site, out):
    (job,) = run([f"{site}/files/clip.mp4"], out, mode="audio", audio_format="mp3")
    assert job.status == "done", job.error
    assert job.files[-1].endswith(".mp3")


def test_direct_file_with_aria2c(site, media, out):
    if not core.ARIA2C:
        pytest.skip("aria2c not installed")
    (job,) = run([f"{site}/files/data.zip"], out)
    assert job.status == "done", job.error
    assert job.tool == "aria2c"
    assert sha(job.files[-1]) == sha(media / "files/data.zip")


def test_direct_file_without_aria2c(site, media, out):
    (job,) = run([f"{site}/files/data.zip"], out, aria2=False)
    assert job.status == "done", job.error
    assert job.tool == "http"
    assert sha(job.files[-1]) == sha(media / "files/data.zip")


def test_archive_skips_second_download(site, out):
    mgr = Manager(2)
    (first,) = run([f"{site}/files/clip.mp4"], out, mgr=mgr)
    (second,) = run([f"{site}/files/clip.mp4"], out, mgr=mgr)
    assert first.status == "done" and second.status == "skipped"


def test_parallel_batch(site, out):
    urls = [f"{site}/files/clip.mp4", f"{site}/files/hls/index.m3u8", f"{site}/files/data.zip", f"{site}/files/big.bin"]
    jobs = run(urls, out, archive=False)
    assert [j.status for j in jobs] == ["done"] * 4


# ---------------------------------------------------------------- errors and auto-fix

def test_404_fails_fast_with_tip(site, out):
    t0 = time.time()
    (job,) = run([f"{site}/nothing-here"], out)
    assert job.status == "error"
    assert "404" in job.error and "Tip:" in job.error
    assert time.time() - t0 < 30  # no pointless retry ladder


def test_page_without_media_explains_what_to_do(site, out):
    (job,) = run([f"{site}/page.html"], out)
    assert job.status == "error"
    assert "DevTools" in job.error  # tells you how to find the real stream URL


def test_autofix_switches_strategy_and_remembers(site, out):
    mgr = Manager(1)
    (job,) = run([f"{site}/picky/clip.mp4?a=1"], out, mgr=mgr, archive=False)
    assert job.status == "done", job.error
    assert job.strategy == "fewer connections"
    assert mgr.learned.get("127.0.0.1") == "gentle"
    # next time the remembered fix is used straight away
    (again,) = run([f"{site}/picky/clip.mp4?a=2"], out / "second", mgr=mgr, archive=False)
    assert again.status == "done" and not any("retrying" in l for l in again.log)


def test_autofix_off_skips_retry_strategies(site, out):
    # video-only mode has no file fallback, so with auto-fix off the first failure is final
    (job,) = run([f"{site}/picky/clip.mp4?b=1"], out, archive=False, smart=False, mode="video")
    assert job.status == "error"
    assert job.strategy == "" and not any("retrying with" in l for l in job.log)


def test_auto_mode_falls_back_to_plain_file_download(site, media, out):
    # even without auto-fix, a media URL that yt-dlp can't fetch is still saved as a direct file
    (job,) = run([f"{site}/picky/clip.mp4?c=1"], out, archive=False, smart=False)
    assert job.status == "done", job.error
    assert job.tool in ("gallery-dl", "aria2c", "http")  # gallery-dl handles plain media links too
    assert sha(job.files[-1]) == sha(media / "files/clip.mp4")


# ---------------------------------------------------------------- pause / resume / history

def test_pause_and_resume_direct_download(site, media, out):
    if not core.ARIA2C:
        pytest.skip("aria2c not installed")
    mgr = Manager(1)
    (job,) = mgr.add([f"{site}/slow/big.bin"], Options(out_dir=str(out), aria2_conns=2))
    deadline = time.time() + 20
    while (job.progress or 0) < 10:
        assert time.time() < deadline, "download never got going"
        time.sleep(0.1)
    mgr.pause(job.id)
    while job.status == "running":
        time.sleep(0.1)
    assert job.status == "paused"
    paused_at = job.progress
    assert 0 < paused_at < 100
    mgr.resume(job.id)
    deadline = time.time() + 60
    while job.status != "done":
        assert job.status in ("queued", "running"), job.error
        assert time.time() < deadline
        time.sleep(0.2)
    assert sha(job.files[-1]) == sha(media / "files/big.bin")  # resumed file is byte-identical


def test_history_survives_restart(site, out, tmp_path):
    state = tmp_path / "state" / "history.json"
    mgr = Manager(1, state_file=str(state))
    (done,) = run([f"{site}/files/data.zip"], out, mgr=mgr)
    waiting = mgr.add(["https://example.com/later.zip"], Options(out_dir=str(out)))[0]
    mgr.pause(waiting.id)
    mgr.learn("example.org", "gentle")
    mgr.save()

    again = Manager(1, state_file=str(state))
    by_url = {j.url: j for j in again.jobs.values()}
    assert by_url[done.url].status == "done" and by_url[done.url].files == done.files
    assert by_url[waiting.url].status == "paused"
    assert again.learned == {"example.org": "gentle"}
    new = again.add(["https://example.com/x.zip"], Options(out_dir=str(out)))[0]
    assert new.id > max(j.id for j in again.jobs.values() if j is not new)  # ids keep counting up


def test_cancel_running_download(site, out):
    mgr = Manager(1)
    (job,) = mgr.add([f"{site}/slow/big.bin?c=1"], Options(out_dir=str(out), aria2_conns=1))
    while job.status != "running":
        time.sleep(0.05)
    time.sleep(1)
    mgr.cancel(job.id)
    deadline = time.time() + 10
    while job.status == "running":
        assert time.time() < deadline
        time.sleep(0.1)
    assert job.status == "cancelled"


# ---------------------------------------------------------------- where files are saved

@pytest.mark.parametrize("url,key,folder", [
    ("https://i.redd.it/io745donghvh1.jpeg", None, "Reddit"),
    ("https://preview.redd.it/x.jpg", None, "Reddit"),
    ("https://pbs.twimg.com/media/a?format=jpg&name=orig", None, "Twitter"),
    ("https://video.twimg.com/a.mp4", "Generic", "Twitter"),
    ("https://www.youtube.com/watch?v=x", "Youtube", "Youtube"),
    ("https://www.youtube.com/@NASA/videos", "YoutubeTab", "Youtube"),
    ("https://cdn.kernel.org/pub/a.tar.xz", None, "kernel.org"),
    ("https://downloads.bbc.co.uk/a.mp3", None, "bbc.co.uk"),
    ("http://127.0.0.1:8000/a.zip", None, "127.0.0.1"),
])
def test_site_folder_names(url, key, folder):
    assert core.site_folder(url, key) == folder


def test_files_go_into_site_folders(site, out):
    jobs = run([f"{site}/files/data.zip", f"{site}/files/clip.mp4"], out, archive=False)
    assert [j.status for j in jobs] == ["done", "done"], [j.error for j in jobs]
    for j in jobs:  # direct file and yt-dlp download both land in the site's folder, not "direct/" or "Generic/"
        assert Path(j.files[-1]).parent == out / "127.0.0.1", j.files


def test_no_subfolders_option(site, out):
    urls = [f"{site}/files/data.zip", f"{site}/files/clip.mp4", f"{site}/files/hls/index.m3u8"]
    jobs = run(urls, out, archive=False, folders="none")
    assert [j.status for j in jobs] == ["done"] * 3, [j.error for j in jobs]
    for j in jobs:
        assert Path(j.files[-1]).parent == out, j.files
    assert not [p for p in out.iterdir() if p.is_dir()]  # no sub-folders were created


def test_fallback_that_drops_quality_is_not_a_fix(monkeypatch):
    """A fallback must never quietly 'succeed' at 360p (and then get remembered for the site)."""
    calls = []

    def fake(job, mgr, strategy=None):
        calls.append(strategy or "default")
        if strategy is None:
            raise RuntimeError("HTTP Error 403: Forbidden")
        raise RuntimeError("only 360p was available this way (wanted at least 720p)")
    monkeypatch.setattr(core, "run_ytdlp", fake)
    mgr = Manager(1)
    job = core.Job(url="https://www.youtube.com/watch?v=x", opts=Options(out_dir="/tmp/x"))
    with pytest.raises(RuntimeError):
        core.run_ytdlp_smart(job, mgr)
    assert "client" in calls and mgr.learned == {}


def test_bot_check_goes_straight_to_login_fix():
    msg = "[youtube] x: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies"
    job = core.Job(url="https://www.youtube.com/watch?v=x", opts=Options())
    assert core.ladder(core.classify(msg), job, []) == [] and core.needs_login(msg)


# ---------------------------------------------------------------- polite mode

def test_polite_groups():
    assert core.polite_group("https://www.instagram.com/nasa/") == "Instagram"
    assert core.polite_group("https://scontent.cdninstagram.com/v/x.jpg") == "Instagram"
    assert core.polite_group("https://x.com/a/status/1") == core.polite_group("https://pbs.twimg.com/m.jpg") == "X"
    assert core.polite_group("https://www.youtube.com/watch?v=x") is None


def test_polite_mode_one_at_a_time_with_gap(site, out, monkeypatch):
    monkeypatch.setitem(core.POLITE_SITES, "127.0.0.1", "TestSite")
    monkeypatch.setattr(core, "POLITE_GAP", (1.5, 1.5))
    mgr = Manager(4)
    a, b = mgr.add([f"{site}/files/data.zip", f"{site}/files/big.bin"], Options(out_dir=str(out)))
    time.sleep(0.3)
    assert b.status == "queued" and "polite mode" in b.stage       # waits although 3 workers are free
    while not mgr.idle():
        time.sleep(0.1)
    assert a.status == b.status == "done"
    assert b.started - a.finished >= 1.4                             # a natural gap between them


def test_polite_mode_off_runs_in_parallel(site, out, monkeypatch):
    monkeypatch.setitem(core.POLITE_SITES, "127.0.0.1", "TestSite")
    mgr = Manager(4)
    a, b = mgr.add([f"{site}/slow/big.bin?p=1", f"{site}/slow/big.bin?p=2"], Options(out_dir=str(out / "x"), polite=False, archive=False, aria2_conns=1))
    time.sleep(1.5)
    assert a.status == b.status == "running"
    mgr.cancel()
    while not mgr.idle():
        time.sleep(0.1)

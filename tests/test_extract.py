"""Offline integration & unit tests for Threads, HTML/JS/iframe extraction, headless CDP sniffing, and LinkGrabber."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import duration, run
from grab.analyze import analyze
from grab.browser import find_browser
from grab.core import Manager, Options, friendly_name, prefers_gallery, site_folder, unwrap_url
from grab.browser import SniffResult
from grab.extract import (
    _merge_sniff_result,
    extract_from_html,
    extract_threads,
    parse_threads_payloads,
    missing_media_ext,
    resolve_file_host,
    sniff_media_ext,
    strip_byte_range,
)
from grab.web import _supported_site


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sample_mpd(video_url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">'
        '<Period><AdaptationSet mimeType="video/mp4">'
        '<Representation id="v1" width="320" height="240" codecs="avc1.4d401e" bandwidth="500000">'
        f'<BaseURL>{video_url}</BaseURL>'
        '</Representation></AdaptationSet></Period></MPD>'
    )


def _threads_html(video_url: str, image_url: str, *, include_other: bool = True, use_dash: bool = True) -> str:
    main_post = {
        "pk": "111",
        "code": "POST123",
        "user": {"username": "lifter"},
        "caption": {"text": "Clean bench set"},
        "video_versions": [{"url": video_url, "width": 320, "height": 240, "type": 101}],
        "image_versions2": {"candidates": [{"url": image_url, "width": 1200, "height": 800}]},
    }
    if use_dash:
        main_post["video_dash_manifest"] = _sample_mpd(video_url)
    items = [{"post": main_post}]
    if include_other:
        items.append({
            "post": {
                "pk": "999",
                "code": "OTHER999",
                "user": {"username": "someone_else"},
                "caption": {"text": "Suggested feed post"},
                "video_versions": [{"url": f"{video_url}?unrelated=1", "width": 640, "height": 480, "type": 101}],
            }
        })
    payload = json.dumps({"require": [["RelayPrefetchedStreamCache", "next", [], {"thread_items": items}]]})
    return f'<!doctype html><html><body><script type="application/json" data-sjs>{payload}</script></body></html>'


def test_threads_routing_and_metadata():
    url = "https://www.threads.com/@adixlifts/post/POST123?xmt=abc"
    assert site_folder(url) == "Threads"
    assert site_folder("https://www.threads.net/@adixlifts") == "Threads"
    assert _supported_site(url) == "Threads"
    assert not prefers_gallery(url)
    assert friendly_name(url) == "@adixlifts (Threads)"
    assert unwrap_url("https://l.threads.com/l.php?u=https%3A%2F%2Fexample.com%2Fv.mp4") == "https://example.com/v.mp4"


def test_parse_threads_payloads_target_code_isolation():
    html = _threads_html("https://cdn.example/main.mp4", "https://cdn.example/thumb.jpg", include_other=True)
    payload_str = html.split('<script type="application/json" data-sjs>')[1].split("</script>")[0]
    res = parse_threads_payloads([json.loads(payload_str)], "https://www.threads.com/@lifter/post/POST123")
    assert res.site == "Threads"
    assert res.title == "@lifter - Clean bench set"
    assert len(res.items) == 1
    assert res.items[0].kind == "video"
    assert res.items[0].archive_id == "threads POST123"
    assert "<MPD" in res.items[0].dash_xml
    assert res.items[0].fallback_url == "https://cdn.example/main.mp4"


def test_threads_dash_download_and_archive(site, media, out, monkeypatch):
    target_url = "https://www.threads.com/@lifter/post/POST123"
    html = _threads_html(f"{site}/files/ep1.mp4", f"{site}/files/pics/big.png")
    monkeypatch.setattr(
        "grab.extract.fetch_page_curl",
        lambda u, **_kw: (u, 200, {"content-type": "text/html"}, html, False, ""),
    )

    (job,) = run([target_url], out, archive=True)
    assert job.status == "done", job.error
    assert job.site == "Threads"
    out_file = Path(job.files[-1])
    assert out_file.parent == out / "Threads"
    assert "[POST123]" in out_file.name
    assert "@lifter - Clean bench set" in out_file.name
    assert duration(out_file) > 1.5

    # Re-running skips cleanly via .grab-archive.txt
    (again,) = run([target_url], out, archive=True)
    assert again.status == "skipped"


def test_threads_carousel_and_analyze(site, media, out, monkeypatch):
    target_url = "https://www.threads.net/@lifter/post/CAROUSEL1"
    carousel_payload = {
        "post": {
            "code": "CAROUSEL1",
            "user": {"username": "lifter"},
            "caption": {"text": "Mixed carousel"},
            "carousel_media": [
                {"video_versions": [{"url": f"{site}/files/ep1.mp4", "width": 320, "height": 240, "type": 101}]},
                {"image_versions2": {"candidates": [{"url": f"{site}/files/pics/big.png", "width": 1200, "height": 800}]}},
            ],
        }
    }
    html = f'<script type="application/json" data-sjs>{json.dumps(carousel_payload)}</script>'
    monkeypatch.setattr(
        "grab.extract.fetch_page_curl",
        lambda u, **_kw: (u, 200, {"content-type": "text/html"}, html, False, ""),
    )

    (job,) = run([target_url], out, archive=False)
    assert job.status == "done", job.error
    assert len(job.files) == 2
    exts = {Path(f).suffix.lower() for f in job.files}
    assert exts == {".mp4", ".png"}

    # Analyze on a single video DASH Threads post reports video resolution & site
    single_html = _threads_html(f"{site}/files/ep1.mp4", f"{site}/files/pics/big.png", include_other=False)
    monkeypatch.setattr(
        "grab.extract.fetch_page_curl",
        lambda u, **_kw: (u, 200, {"content-type": "text/html"}, single_html, False, ""),
    )
    info = analyze("https://www.threads.com/@lifter/post/POST123", Options(out_dir=str(out)))
    assert info["kind"] == "video" and info["site"] == "Threads"
    assert info["options"] and info["options"][0]["height"] == 240


def test_html_iframe_and_escaped_script_extraction(site, out):
    info = analyze(f"{site}/embed_host.html", Options(out_dir=str(out)))
    assert info["kind"] == "video" and info["title"] == "Hosted Video Article"
    assert info["options"] and info["options"][0]["height"] == 360

    (job,) = run([f"{site}/embed_host.html"], out, archive=False)
    assert job.status == "done", job.error
    out_file = Path(job.files[-1])
    assert duration(out_file) > 5.0
    assert "Hosted Video Article" in out_file.name


def test_js_spa_headless_browser_sniff(site, out):
    if not find_browser():
        pytest.skip("No Chromium-based browser installed")
    (job,) = run([f"{site}/spa.html"], out, archive=False)
    assert job.status == "done", job.error
    out_file = Path(job.files[-1])
    assert duration(out_file) > 5.0
    assert "Dynamic SPA Player" in out_file.name


def test_jdownloader_multi_file_link_grabbing(site, media, out):
    mgr = Manager(2)
    (parent,) = run([f"{site}/files_index.html"], out, mgr=mgr, archive=False)
    assert parent.status == "done", parent.error
    assert parent.children == 3
    kids = [j for j in mgr.jobs.values() if j.parent == parent.id]
    assert len(kids) == 3
    assert all(k.status == "done" for k in kids), [(k.url, k.error) for k in kids]
    names = {Path(k.files[-1]).name for k in kids}
    assert "data.zip" in names
    assert any(n.startswith("Episode One [") and n.endswith(".mp4") for n in names)
    assert any(n.startswith("Episode Two [") and n.endswith(".mp4") for n in names)
    zip_job = next(k for k in kids if Path(k.files[-1]).name == "data.zip")
    assert sha(zip_job.files[-1]) == sha(media / "files/data.zip")
    assert all(Path(k.files[-1]).parent.name == "Release Bundle" for k in kids)


def test_media_fragment_long_id_and_bytestart_fixes(site, media, out):
    # Temporal fragment #t=0.1 stripped -> downloads .mov cleanly
    (mov_job,) = run([f"{site}/files/ep1.mov#t=0.1"], out / "mov", archive=False)
    assert mov_job.status == "done", mov_job.error

    # >210 byte URL ID capped under 255-byte APFS component limit (no Errno 63)
    long_id = "x" * 220
    (long_job,) = run([f"{site}/longid/{long_id}.mp4"], out / "long", archive=False)
    assert long_job.status == "done", long_job.error
    assert len(Path(long_job.files[-1]).name.encode("utf-8")) <= 240

    # Meta-style ?bytestart=...&byteend=... stripped -> gets complete playable file, not 100-byte fragment
    (cdn_job,) = run([f"{site}/cdn-range/ep1.mp4?bytestart=0&byteend=99"], out / "cdn", archive=False)
    assert cdn_job.status == "done", cdn_job.error
    out_file = Path(cdn_job.files[-1])
    assert out_file.stat().st_size >= (media / "files/ep1.mp4").stat().st_size
    assert duration(out_file) > 1.5


def test_direct_download_sends_shared_cookies(site, media, out, tmp_path):
    protected = f"{site}/cookie-protected/data.zip"
    # Without cookies -> fails with 403
    (fail_job,) = run([protected], out / "fail", mode="direct", aria2=False, archive=False)
    assert fail_job.status == "error"

    # With Netscape cookie file -> succeeds in plain HTTP mode
    cf = tmp_path / "cookies.txt"
    cf.write_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsession\tok123\n")
    (ok_job,) = run([protected], out / "ok", mode="direct", aria2=False, cookies_file=str(cf), archive=False)
    assert ok_job.status == "done", ok_job.error
    assert sha(ok_job.files[-1]) == sha(media / "files/data.zip")


def test_file_host_rewrites_and_jsonld_html_extraction():
    assert resolve_file_host("https://www.dropbox.com/s/abc/file.zip?dl=0") == "https://www.dropbox.com/s/abc/file.zip?dl=1"
    assert "drive.usercontent.google.com/download?id=XYZ123" in resolve_file_host("https://drive.google.com/file/d/XYZ123/view")
    assert resolve_file_host("https://pixeldrain.com/u/abcd") == "https://pixeldrain.com/api/file/abcd?download"
    assert resolve_file_host("https://github.com/org/repo/blob/main/a.zip") == "https://raw.githubusercontent.com/org/repo/main/a.zip"
    assert strip_byte_range("https://cdn.example/v.mp4?efg=1&bytestart=914&byteend=1000") == "https://cdn.example/v.mp4?efg=1"

    html = '''<html><head><title>Player</title>
    <script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.example/ld.m3u8"}</script>
    <script>const p = atob("aHR0cHM6Ly9jZG4uZXhhbXBsZS9zZWNyZXQubXA0");</script>
    </head></html>'''
    res, _ifr, _dyn = extract_from_html("https://site.example/watch", html)
    urls = [it.url for it in res.items]
    assert "https://cdn.example/ld.m3u8" in urls
    assert "https://cdn.example/secret.mp4" in urls


# What a logged-out Threads page really contains: Meta's UI bundle (scripts, styles, sounds, images) named in its config.
_META_ASSETS = {
    "js": "https://static.cdninstagram.com/rsrc.php/v4/yN/r/xmVOQ7vmBBf.js",
    "css": "https://static.cdninstagram.com/rsrc.php/v5/yh/l/0,cross/lJt1nY1bojY.css",
    "mp3": "https://static.cdninstagram.com/rsrc.php/yZ/r/RrdRxxpsgqr.mp3",
    "mp4": "https://static.cdninstagram.com/rsrc.php/y_/r/i8Ek3A1myhR.mp4",
    "webp": "https://static.cdninstagram.com/rsrc.php/yB/r/kwqgbSOqEiw.webp",
}


def test_a_pages_own_scripts_styles_and_ui_sounds_are_never_media():
    real = "https://cdn.example.com/watch?token=abc"
    html = ("<html><head><title>Page</title></head><body><script>var cfg = "
            + json.dumps({"src": _META_ASSETS["js"], "file": real, "source": _META_ASSETS["css"], "sound": _META_ASSETS["mp3"],
                          "stream": _META_ASSETS["mp4"], "icon": _META_ASSETS["webp"]})
            + "</script></body></html>")
    res, _, _ = extract_from_html("https://example.com/post", html)
    assert [i.url for i in res.items] == [real]       # a player config's own URL still counts, with or without an extension


def test_browser_captures_skip_the_pages_own_files():
    real = "https://video.example.com/clip.mp4"
    sniff = SniffResult(
        url="https://example.com/post", title="Post",
        media_urls=[{"url": _META_ASSETS["mp4"], "kind": "video"}, {"url": real, "kind": "video"}],
        json_payloads=[{"url": "x", "body": json.dumps({"src": _META_ASSETS["js"], "file": _META_ASSETS["webp"]})}],
    )
    res = _merge_sniff_result(extract_from_html("https://example.com/post", "<html></html>")[0], sniff, "https://example.com/post")
    assert [i.url for i in res.items] == [real]


def test_threads_post_nobody_can_see_logged_out_says_so(monkeypatch):
    page = "https://www.threads.com/@someone/post/DfukAgqkV3p"
    shell = "<html><head><title>Threads</title></head><body>Threads</body></html>"
    monkeypatch.setattr("grab.extract.fetch_page_curl", lambda url, **kw: (url, 200, {}, shell, False, ""))
    monkeypatch.setattr("grab.extract.sniff_page", lambda url, **kw: SniffResult(
        url=url, title="Threads", html="<div>Log in</div><div>This content isn&#x27;t available to everyone</div>"))
    res = extract_threads(page)
    assert res.items == [] and "isn't available to everyone" in res.error


def test_a_download_with_no_extension_is_named_by_what_it_is():
    mp4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00" + b"\x00" * 64
    assert sniff_media_ext(mp4) == "mp4"
    assert sniff_media_ext(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32) == "mov"
    assert sniff_media_ext(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 32) == "m4a"
    assert sniff_media_ext(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32) == ""          # a picture, not a video
    assert sniff_media_ext(b"\x1a\x45\xdf\xa3" + b"\x42\x82\x84webm" + b"\x00" * 32) == "webm"
    assert sniff_media_ext(b"\x1a\x45\xdf\xa3" + b"\x42\x82\x88matroska" + b"\x00" * 32) == "mkv"
    assert sniff_media_ext(b"ID3\x04" + b"\x00" * 32) == "mp3" and sniff_media_ext(b"\xff\xfb\x90\x00" + b"\x00" * 32) == "mp3"
    assert sniff_media_ext(b"\xff\xf1\x50\x80" + b"\x00" * 32) == "aac"
    assert sniff_media_ext(b"RIFF\x00\x00\x00\x00AVI LIST") == "avi" and sniff_media_ext(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "wav"
    assert sniff_media_ext(b"G" + b"\x00" * 187 + b"G" + b"\x00" * 8) == "ts"
    assert sniff_media_ext(b"<!doctype html><html>" + b"\x00" * 32) == "" and sniff_media_ext(b"") == ""
    assert missing_media_ext("Episode 3", mp4) == "mp4" and missing_media_ext("abc123", mp4) == "mp4"
    assert missing_media_ext("Clip.mp4", mp4) == "" and missing_media_ext("Song.MP3", mp4) == ""   # it already has one

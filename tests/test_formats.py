"""Best quality in a compatible format: AV1/VP9 -> HEVC MP4, HDR kept, H.264 untouched."""

from __future__ import annotations

import json
import subprocess

from conftest import duration, run


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,profile,codec_tag_string,color_transfer,color_primaries",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    streams = json.loads(r.stdout)["streams"]
    return next(s for s in streams if s["codec_type"] == "video"), [s for s in streams if s["codec_type"] == "audio"]


def test_vp9_becomes_hevc_mp4_with_aac(site, out):
    (job,) = run([f"{site}/files/vp9.webm"], out, archive=False)
    assert job.status == "done", job.error
    f = job.files[-1]
    v, a = probe(f)
    assert f.endswith(".mp4") and len(job.files) == 1
    assert v["codec_name"] == "hevc" and v["codec_tag_string"] == "hvc1"     # hvc1 = plays in QuickTime/iPhone
    assert a and a[0]["codec_name"] == "aac"                                 # Opus -> AAC
    assert abs(duration(f) - 3) < 0.3
    assert not list(out.rglob("*.webm"))                                     # original replaced, not left behind
    assert any("converted vp9" in l for l in job.log)


def test_hdr_av1_keeps_hdr(site, out):
    (job,) = run([f"{site}/files/hdr_av1.mp4"], out, archive=False)
    assert job.status == "done", job.error
    v, _ = probe(job.files[-1])
    assert v["codec_name"] == "hevc" and v["profile"] == "Main 10"
    assert v["color_transfer"] == "smpte2084" and v["color_primaries"] == "bt2020"


def test_h264_is_left_alone(site, out):
    (job,) = run([f"{site}/files/clip.mp4"], out, archive=False)
    v, _ = probe(job.files[-1])
    assert v["codec_name"] == "h264" and not any("converted" in l for l in job.log)


def test_original_format_when_compat_off(site, out):
    (job,) = run([f"{site}/files/vp9.webm"], out, archive=False, compat=False)
    v, _ = probe(job.files[-1])
    assert v["codec_name"] == "vp9"


def test_clip_only_downloads_that_part(site, out):
    (job,) = run([f"{site}/files/clip.mp4"], out, clip="0:02-0:05", archive=False)
    assert job.status == "done", job.error
    f = job.files[-1]
    assert "(clip 0m02s-0m05s)" in f
    assert abs(duration(f) - 3) < 0.6


def test_clip_to_the_end(site, out):
    (job,) = run([f"{site}/files/clip.mp4"], out, clip="10-", archive=False)
    assert job.status == "done", job.error
    assert abs(duration(job.files[-1]) - 2) < 0.6 and "(clip 0m10s-end)" in job.files[-1]


def test_bad_clip_is_reported(site, out):
    (job,) = run([f"{site}/files/clip.mp4"], out, clip="0:05-0:02", archive=False)
    assert job.status == "error" and "end after it starts" in job.error

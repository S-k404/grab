"""Analyze a link without downloading it: what it is, which qualities exist, how big each would be.

Used by the Analyze button. The choices mirror what a download would really pick: with
"compatible format" on, H.264 is preferred at each resolution, and resolutions only offered as
AV1/VP9 are marked as "converted to HEVC".
"""

from __future__ import annotations

import yt_dlp

from .core import Options, friendly_name, login_for, prefers_gallery, site_folder, unwrap_url

CODECS = {"avc1": "H.264", "avc3": "H.264", "h264": "H.264", "hev1": "HEVC", "hvc1": "HEVC", "hevc": "HEVC",
          "av01": "AV1", "vp09": "VP9", "vp9": "VP9", "vp8": "VP8"}
COMPATIBLE = {"H.264", "HEVC"}


def codec_name(vcodec: str | None) -> str:
    v = (vcodec or "").lower()
    return next((name for key, name in CODECS.items() if v.startswith(key)), v.split(".")[0].upper() or "?")


def _size(f: dict, duration: float | None) -> int | None:
    s = f.get("filesize") or f.get("filesize_approx")
    if not s and f.get("tbr") and duration:
        s = f["tbr"] * 1000 / 8 * duration
    return int(s) if s else None


def analyze(url: str, o: Options) -> dict:
    url = unwrap_url(url)
    if prefers_gallery(url):
        return {"kind": "profile", "title": friendly_name(url), "site": site_folder(url), "url": url}
    params = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist", "noplaylist": False}
    if o.referer:
        params["http_headers"] = {"Referer": o.referer}
    with login_for(o) as (cookie_file, browser):
        if browser:
            params["cookiesfrombrowser"] = browser
        if cookie_file:
            params["cookiefile"] = cookie_file
        with yt_dlp.YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("nothing found at this link")

    site = (info.get("extractor_key") or "").replace("Tab", "")
    base = {"url": url, "title": info.get("title") or url, "uploader": info.get("uploader") or info.get("channel") or "",
            "site": site if site and site != "Generic" else site_folder(url), "thumbnail": info.get("thumbnail") or "",
            "youtube": site.lower().startswith("youtube")}
    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        return {**base, "kind": "playlist", "count": info.get("playlist_count") or len(entries),
                "first": [e.get("title") or e.get("url") for e in entries[:5]]}

    duration = info.get("duration")
    formats = info.get("formats") or [info]
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_audio = max(audios, key=lambda f: (f.get("ext") == "m4a", f.get("abr") or f.get("tbr") or 0), default=None)
    audio_size = _size(best_audio, duration) if best_audio else 0

    options = []
    for h in sorted({f["height"] for f in videos}, reverse=True):
        at_h = [f for f in videos if f["height"] == h]
        compatible = [f for f in at_h if codec_name(f.get("vcodec")) in COMPATIBLE]
        pick = max(compatible if (o.compat and compatible) else at_h, key=lambda f: (f.get("fps") or 0, f.get("tbr") or 0))
        codec = codec_name(pick.get("vcodec"))
        size = _size(pick, duration)
        if size and pick.get("acodec") in (None, "none"):
            size += audio_size or 0
        options.append({
            "height": h, "fps": round(pick.get("fps") or 0), "codec": codec, "size": size,
            "hdr": (pick.get("dynamic_range") or "SDR") != "SDR",
            "converts": o.compat and codec not in COMPATIBLE,
            "label": {4320: "8K", 2160: "4K", 1440: "1440p"}.get(h, f"{h}p"),
        })
    subs = sorted((info.get("subtitles") or {}).keys())
    auto = sorted((info.get("automatic_captions") or {}).keys())
    return {**base, "kind": "video", "duration": duration, "is_live": bool(info.get("is_live")),
            "options": options, "audio_size": audio_size or None,
            "audio_codec": codec_name(best_audio.get("acodec")) if best_audio else "",
            "subtitles": [s for s in subs if s != "live_chat"][:40],
            "auto_subtitles": bool(auto), "chapters": len(info.get("chapters") or [])}

"""Analyze a link without downloading it: what it is, which qualities exist, how big each would be.

Used by the Analyze button. The choices mirror what a download would really pick: with
"compatible format" on, H.264 is preferred at each resolution, and resolutions only offered as
AV1/VP9 are marked as "converted to HEVC".
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import yt_dlp

from .core import Options, classify, friendly_name, login_for, prefers_gallery, site_folder, state_dir, unwrap_url
from .extract import ExtractionResult, extract_media, is_threads_url, resolve_file_host

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


def _inspect_extracted(url: str, res: ExtractionResult, o: Options, base_cookie: str | None) -> dict:
    """Inspect an ExtractionResult into the format expected by the Analyze panel."""
    if not res.items:
        raise RuntimeError(res.error or "nothing found at this link")

    site = res.site or site_folder(url)
    base = {
        "url": url,
        "title": res.title or url,
        "uploader": "",
        "site": site,
        "thumbnail": "",
        "youtube": False,
    }
    if len(res.items) == 1 and res.items[0].kind in ("file", "image"):
        it = res.items[0]
        return {
            **base,
            "kind": "file",
            "title": it.title or res.title or url,
            "ext": (it.ext or "").upper(),
            "size": it.size or None,
            "file_kind": it.kind,
        }
    if len(res.items) > 1:
        item_kind = "files" if all(it.kind in ("file", "image", "audio") for it in res.items) else "videos"
        return {
            **base,
            "kind": "playlist",
            "item_kind": item_kind,
            "count": len(res.items),
            "first": [it.title or it.url for it in res.items[:5]],
        }

    item = res.items[0]
    tmp_cookie = None
    try:
        if res.cookies_text:
            d = state_dir() / "logins"
            d.mkdir(parents=True, exist_ok=True)
            fd, tmp_cookie = tempfile.mkstemp(prefix="an-", suffix=".txt", dir=d)
            os.close(fd)
            os.chmod(tmp_cookie, 0o600)
            Path(tmp_cookie).write_text(res.cookies_text, encoding="utf-8")
        cookie_use = tmp_cookie or base_cookie
        probe_params = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "http_headers": {**(item.headers or {}), "Referer": o.referer or url},
        }
        if cookie_use:
            probe_params["cookiefile"] = cookie_use

        info = None
        if item.dash_xml:
            with tempfile.TemporaryDirectory(prefix="grab-an-mpd-") as td:
                mpd = Path(td) / "manifest.mpd"
                mpd.write_text(item.dash_xml, encoding="utf-8")
                try:
                    with yt_dlp.YoutubeDL({**probe_params, "enable_file_urls": True}) as ydl:
                        info = ydl.extract_info(mpd.as_uri(), download=False)
                except Exception:  # noqa: BLE001
                    info = None
        if info is None and (item.fallback_url or item.url):
            probe_url = item.fallback_url or item.url
            imp_targets: tuple = (None, "chrome") if probe_url.startswith("http") else (None,)
            for imp in imp_targets:
                p = dict(probe_params)
                if imp:
                    from yt_dlp.networking.impersonate import ImpersonateTarget
                    p["impersonate"] = ImpersonateTarget.from_str(imp)
                try:
                    with yt_dlp.YoutubeDL(p) as ydl:
                        info = ydl.extract_info(probe_url, download=False)
                    if info:
                        break
                except Exception:  # noqa: BLE001
                    info = None
    finally:
        if tmp_cookie:
            Path(tmp_cookie).unlink(missing_ok=True)

    if info:
        if res.title:
            info["title"] = res.title
        if res.site:
            info["extractor_key"] = res.site
        return _format_video_info(url, info, o, live_override=res.live)

    ext = (item.ext or "MP4").upper()
    return {
        **base,
        "kind": "video",
        "title": item.title or res.title or url,
        "duration": None,
        "is_live": res.live,
        "options": [{
            "height": "best", "fps": 0, "codec": ext, "size": item.size or None,
            "hdr": False, "converts": False, "label": "Best available",
        }],
        "audio_size": None,
        "audio_codec": "",
        "subtitles": [],
        "auto_subtitles": False,
        "chapters": 0,
    }


def _format_video_info(url: str, info: dict, o: Options, live_override: bool = False) -> dict:
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
    videos = [f for f in formats if f.get("vcodec") != "none" and f.get("height")]
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
    if not options and any(f.get("vcodec") != "none" for f in formats):
        top = formats[-1]
        options.append({
            "height": "best", "fps": 0, "codec": (top.get("ext") or "MP4").upper(),
            "size": _size(top, duration), "hdr": False, "converts": False, "label": "Best available",
        })
    subs = sorted((info.get("subtitles") or {}).keys())
    auto = sorted((info.get("automatic_captions") or {}).keys())
    return {**base, "kind": "video", "duration": duration, "is_live": bool(info.get("is_live") or live_override),
            "options": options, "audio_size": audio_size or None,
            "audio_codec": codec_name(best_audio.get("acodec")) if best_audio else "",
            "subtitles": [s for s in subs if s != "live_chat"][:40],
            "auto_subtitles": bool(auto), "chapters": len(info.get("chapters") or [])}


class _SilentLog:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def analyze(url: str, o: Options) -> dict:
    url = unwrap_url(url)
    if prefers_gallery(url):
        return {"kind": "profile", "title": friendly_name(url), "site": site_folder(url), "url": url}
    params = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
              "noplaylist": False, "logger": _SilentLog()}
    if o.referer:
        params["http_headers"] = {"Referer": o.referer}
    with login_for(o) as (cookie_file, browser):
        if browser:
            params["cookiesfrombrowser"] = browser
        if cookie_file:
            params["cookiefile"] = cookie_file
        if not is_threads_url(url) and not resolve_file_host(url):
            try:
                with yt_dlp.YoutubeDL(params) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info:
                    return _format_video_info(url, info, o)
            except Exception as yt_err:  # noqa: BLE001
                if classify(str(yt_err)) in ("gone", "bot"):
                    raise
                res = extract_media(url, mode=o.mode, cookie_file=cookie_file, referer=o.referer)
                if res.drm and not res.items:
                    raise RuntimeError(f"{res.drm} — Grab can't download DRM-protected streams.") from None
                if res.items:
                    return _inspect_extracted(url, res, o, cookie_file)
                raise yt_err from None
        res = extract_media(url, mode=o.mode, cookie_file=cookie_file, referer=o.referer)
        if res.drm and not res.items:
            raise RuntimeError(f"{res.drm} — Grab can't download DRM-protected streams.")
        if res.items:
            return _inspect_extracted(url, res, o, cookie_file)
        raise RuntimeError(res.error or "nothing found at this link")

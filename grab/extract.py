"""Content extractors for Threads, dynamic pages, embedded players, and multi-file links."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import html as html_lib
import json
from pathlib import Path
import re
import threading
import urllib.parse

from curl_cffi import requests as cf_requests

from .browser import SniffResult, _parse_netscape_cookies, sniff_page

_CHROME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_THREADS_HOSTS = ("threads.com", "threads.net")

_VIDEO_EXTS = {"mp4", "webm", "mov", "m4v", "mkv", "flv", "avi", "ts", "ogv"}
_AUDIO_EXTS = {"mp3", "m4a", "aac", "ogg", "opus", "flac", "wav"}
_STREAM_EXTS = {"m3u8", "mpd", "f4m"}
_FILE_EXTS = {
    "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "zst", "iso", "dmg", "pkg", "exe", "msi",
    "apk", "apks", "xapk", "ipa", "deb", "rpm", "appimage", "pdf", "epub", "mobi", "azw3", "fb2", "djvu",
    "cbz", "cbr", "torrent", "bin", "img", "safetensors", "ckpt", "gguf", "onnx", "pt", "pth",
    "parquet", "jsonl", "srt", "vtt", "ass", "ttf", "otf", "woff2",
}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "avif", "heic"}

_SKIP_IFRAME_HOSTS = (
    "googlesyndication.com", "doubleclick.net", "googletagmanager.com", "google-analytics.com",
    "recaptcha.net", "hcaptcha.com", "challenges.cloudflare.com", "connect.facebook.net",
)

_MEDIA_URL_RE = re.compile(
    r"""(https?:(?:\\/|/){2}[^"'<>\s\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4a|mp3)(?:\?[^"'<>\s\\]*)?|"""
    r"""(?<=["'=])//[^"'<>\s\\]+\.[^"'<>\s\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4a|mp3)(?:\?[^"'<>\s\\]*)?)""",
    re.IGNORECASE,
)
_ENCODED_MEDIA_RE = re.compile(
    r"""(https?%3A%2F%2F[^"'<>\s&\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4a|mp3)(?:%3F[^"'<>\s&\\]*)?)""",
    re.IGNORECASE,
)
_PLAYER_KEY_RE = re.compile(
    r"""["'](?:hlsUrl|dashUrl|mp4Url|streamUrl|manifestUrl|videoUrl|video_url|contentUrl|"""
    r"""playback_url|playable_url|playable_url_quality_hd|hls|dash|stream|playlist|source|"""
    r"""mediaUrl|media_url|videoSrc|audioSrc|mp3|mp3Url|downloadUrl|download_url|fallbackUrl|"""
    r"""hd_src|sd_src|file|src)["']\s*:\s*["']"""
    r"""((?:https?:|/)[^"'\s<>\\]+?\.(?:m3u8|mpd|mp4|webm|mov|m4a|mp3)(?:\?[^"'\s<>\\]*)?|https?:[^"']+)["']""",
    re.IGNORECASE,
)
_ATOB_RE = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=]{16,})["']\s*\)""")
_TAG_ATTR_RE = re.compile(r"""([a-zA-Z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")


@dataclass
class ExtractedItem:
    """A single downloadable media stream, video, audio, image, or file discovered on a page."""

    url: str
    kind: str  # "stream" | "video" | "audio" | "image" | "file"
    title: str = ""
    ext: str = ""
    dash_xml: str = ""
    fallback_url: str = ""
    headers: dict = field(default_factory=dict)
    archive_id: str = ""
    size: int = 0


@dataclass
class ExtractionResult:
    """Full extraction output returned by Grab's built-in extractors and browser sniffer."""

    url: str
    site: str = ""
    title: str = ""
    items: list[ExtractedItem] = field(default_factory=list)
    cookies_text: str = ""
    drm: str = ""
    live: bool = False
    is_direct_content: bool = False
    status: int = 0
    error: str = ""


def _host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")


def _ext_of(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.lower()
    tail = path.rsplit("/", 1)[-1]
    if "." not in tail:
        return ""
    return tail.rsplit(".", 1)[-1]


def is_threads_url(url: str) -> bool:
    """Return True for threads.com or threads.net URLs."""
    host = _host_of(url)
    return host in _THREADS_HOSTS or host.endswith((".threads.com", ".threads.net"))


def threads_post_code(url: str) -> str:
    """Extract post shortcode from a Threads post URL, or empty string."""
    parts = [p for p in urllib.parse.urlsplit(url).path.split("/") if p]
    for i, part in enumerate(parts):
        if part in ("post", "t") and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def strip_byte_range(url: str) -> str:
    """Strip CDN byte-range query parameters (bytestart, byteend, range) from signed media URLs."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in q if k.lower() not in ("bytestart", "byteend", "range")]
    if len(filtered) == len(q):
        return url
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(filtered)))


_PAGE_ASSET_EXTS = {"js", "mjs", "css", "map", "woff", "woff2", "ttf", "otf", "eot", "svg", "ico"}


def _is_page_asset(url: str) -> bool:
    """A page's own scripts, styles and fonts, and Meta's static bundle (/rsrc.php/: UI sounds and animations).
    Never the media itself, however a player config names them."""
    return _ext_of(url) in _PAGE_ASSET_EXTS or "/rsrc.php/" in urllib.parse.urlsplit(url).path


def sniff_media_ext(head: bytes) -> str:
    """The extension a media file's first bytes give it ("" when they don't look like audio or video)."""
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevx", b"mif1", b"msf1", b"avif", b"avis"):
            return ""  # pictures share the container
        return "mov" if brand == b"qt  " else "m4a" if brand in (b"M4A ", b"M4B ") else "mp4"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm" if b"webm" in head[:64] else "mkv"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"FLV\x01":
        return "flv"
    if head[:4] == b"RIFF":
        return {b"AVI ": "avi", b"WAVE": "wav"}.get(head[8:12], "")
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "aac" if head[1] & 0x06 == 0 else "mp3"   # ADTS (AAC) or an MPEG audio frame
    if len(head) > 188 and head[0] == 0x47 and head[188] == 0x47:
        return "ts"
    return ""


def missing_media_ext(name: str, head: bytes) -> str:
    """The extension to add to a finished download whose name has none ("Episode 3", "abc123"): what it turned out to be."""
    if Path(name).suffix.lower().lstrip(".") in (_VIDEO_EXTS | _AUDIO_EXTS):
        return ""
    return sniff_media_ext(head)


def _filename_from_cd(cd: str) -> str:
    """Safely extract a basename filename from a Content-Disposition header."""
    if not cd:
        return ""
    m = re.search(r"""filename\*\s*=\s*([^';\r\n]+)'[^']*'([^;\r\n]+)""", cd, re.IGNORECASE)
    if m:
        raw = m.group(2).strip().strip('"').strip("'")
        name = urllib.parse.unquote(raw)
    else:
        m = re.search(r"""filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\r\n]+)""", cd, re.IGNORECASE)
        if not m:
            return ""
        name = urllib.parse.unquote((m.group(1) or m.group(2) or "").strip().strip("'"))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return "" if name in ("", ".", "..") else name


def extract_google_drive_confirm(html: str, base_url: str) -> str | None:
    """Extract the confirmed download URL from Google Drive's large-file virus-scan HTML warning."""
    if not html or "download-form" not in html:
        return None
    m = re.search(r"""<form\b([^>]*id=["']download-form["'][^>]*)>(.*?)</form>""", html, re.I | re.S)
    if not m:
        return None
    action = _parse_attrs(m.group(1)).get("action") or base_url
    action_url = urllib.parse.urljoin(base_url, action)
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(action_url).query, keep_blank_values=True))
    for inp in re.finditer(r"""<input\b([^>]+)>""", m.group(2), re.IGNORECASE):
        attrs = _parse_attrs(inp.group(1))
        name = attrs.get("name")
        if name:
            params[name] = attrs.get("value", "")
    if not params.get("id"):
        return None
    parts = urllib.parse.urlsplit(action_url)
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params)))


def resolve_file_host(url: str) -> str | None:
    """Rewrite common file-sharing viewer URLs (Dropbox, Drive, HuggingFace, GitHub, GitLab, etc.) to direct downloads."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path
    q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)

    if host.endswith("dropbox.com") and (path.startswith("/s/") or path.startswith("/scl/") or path.startswith("/sh/")):
        q["dl"] = ["1"]
        q.pop("raw", None)
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, doseq=True)))

    if host == "drive.google.com":
        m = re.match(r"^/file/d/([^/]+)", path)
        fid = m.group(1) if m else (q.get("id", [""])[0])
        if fid:
            return f"https://drive.usercontent.google.com/download?id={urllib.parse.quote(fid)}&export=download&confirm=t"

    if host == "docs.google.com":
        m = re.match(r"^/(document|spreadsheets|presentation)/d/([^/]+)", path)
        if m:
            doc_type, doc_id = m.group(1), urllib.parse.quote(m.group(2))
            fmt = {"document": "pdf", "spreadsheets": "xlsx", "presentation": "pptx"}[doc_type]
            return f"https://docs.google.com/{doc_type}/d/{doc_id}/export?format={fmt}"

    if host == "pixeldrain.com" and path.startswith("/u/"):
        fid = path[3:].strip("/")
        if fid:
            return f"https://pixeldrain.com/api/file/{fid}?download"

    if host == "github.com":
        segs = [s for s in path.split("/") if s]
        if len(segs) >= 5 and segs[2] in ("blob", "raw"):
            owner, repo, _kind, ref = segs[:4]
            rest = "/".join(segs[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{rest}"

    if host in ("huggingface.co", "hf.co"):
        segs = [s for s in path.split("/") if s]
        if "blob" in segs:
            idx = segs.index("blob")
            if idx >= 2 and idx + 2 < len(segs):
                new_segs = [*segs[:idx], "resolve", *segs[idx + 1:]]
                return f"https://huggingface.co/{'/'.join(new_segs)}?download=true"

    if host == "gitlab.com" and "/-/blob/" in path:
        new_path = path.replace("/-/blob/", "/-/raw/", 1)
        q["inline"] = ["false"]
        return urllib.parse.urlunsplit(parts._replace(path=new_path, query=urllib.parse.urlencode(q, doseq=True)))

    if host == "codeberg.org":
        segs = [s for s in path.split("/") if s]
        if len(segs) >= 6 and segs[2] == "src" and segs[3] in ("branch", "commit", "tag"):
            new_segs = [*segs[:2], "raw", *segs[3:]]
            return f"https://codeberg.org/{'/'.join(new_segs)}"

    if host == "commons.wikimedia.org" and path.startswith("/wiki/File:"):
        fname = path[len("/wiki/File:"):]
        if fname:
            return f"https://commons.wikimedia.org/wiki/Special:FilePath/{fname}"

    if (host == "onedrive.live.com" and ("resid" in q or "id" in q) and "/edit" not in path.lower()) or (
        host.endswith(".sharepoint.com") and re.search(r"/:[a-z]:/", path) and "/edit" not in path.lower()
    ):
        if "download" not in q:
            q["download"] = ["1"]
            return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, doseq=True)))

    return None


def _cookies_text_from_jar(jar) -> str:
    """Convert curl_cffi/http cookies to Netscape cookie-jar format."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in jar:
        domain = getattr(c, "domain", "") or ""
        name = getattr(c, "name", "") or ""
        if not domain or not name:
            continue
        subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        path = getattr(c, "path", None) or "/"
        secure = "TRUE" if getattr(c, "secure", False) else "FALSE"
        exp = int(getattr(c, "expires", None) or 0)
        lines.append(f"{domain}\t{subdomains}\t{path}\t{secure}\t{exp}\t{name}\t{getattr(c, 'value', '')}")
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""


def fetch_page_curl(
    url: str,
    cookie_file: str | Path | None = None,
    referer: str = "",
    timeout: float = 15.0,
) -> tuple[str, int, dict, str, bool, str]:
    """Fetch *url* with Chrome TLS impersonation, bailing early on non-HTML binary streams."""
    sess = cf_requests.Session(impersonate="chrome")
    if cookie_file:
        for c in _parse_netscape_cookies(cookie_file):
            try:
                sess.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
            except Exception:  # noqa: BLE001
                pass
    hdrs = dict(_CHROME_HEADERS)
    if referer:
        hdrs["Referer"] = referer
    try:
        resp = sess.get(url, headers=hdrs, timeout=timeout, stream=True, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        return url, 0, {}, "", False, ""

    try:
        status = int(resp.status_code)
        final_url = str(resp.url)
        r_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        ctype = r_headers.get("content-type", "").split(";")[0].strip().lower()
        cookies_text = _cookies_text_from_jar(sess.cookies.jar) if hasattr(sess.cookies, "jar") else ""

        is_html = (not ctype) or ("html" in ctype) or ("xhtml" in ctype) or ctype.startswith("text/plain")
        is_manifest = ("mpegurl" in ctype) or ("dash+xml" in ctype) or _ext_of(final_url) in _STREAM_EXTS
        is_json = "json" in ctype
        if status < 400 and ctype and not is_html and not is_manifest and not is_json:
            return final_url, status, r_headers, "", True, cookies_text

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content():
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= 6 * 1024 * 1024:
                break
        text = b"".join(chunks).decode("utf-8", errors="replace")
        cookies_text = _cookies_text_from_jar(sess.cookies.jar) if hasattr(sess.cookies, "jar") else cookies_text
        return final_url, status, r_headers, text, False, cookies_text
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def _parse_attrs(tag_text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _TAG_ATTR_RE.finditer(tag_text):
        val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
        attrs[m.group(1).lower()] = html_lib.unescape(val or "")
    return attrs


def _clean_escaped_url(raw: str, base_url: str) -> str:
    s = raw.strip().replace("\\/", "/").replace("\\x2f", "/").replace("\\x2F", "/")
    try:
        if "\\u" in s:
            s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    except Exception:  # noqa: BLE001
        pass
    s = html_lib.unescape(s)
    if s.startswith(("data:", "blob:", "javascript:", "#")):
        return ""
    return urllib.parse.urljoin(base_url, s)


def _iter_json_payloads(html: str) -> list:
    """Extract parsed JSON structures from <script type="application/json"> tags."""
    payloads = []
    for m in re.finditer(r"""<script\b([^>]*)>(.*?)</script>""", html, re.IGNORECASE | re.DOTALL):
        attrs = _parse_attrs(m.group(1))
        stype = attrs.get("type", "").lower()
        body = m.group(2).strip()
        if not body:
            continue
        if "json" in stype or "data-sjs" in attrs:
            try:
                payloads.append(json.loads(body))
                continue
            except Exception:  # noqa: BLE001
                pass
        if any(k in body for k in ("video_versions", "video_dash_manifest", "carousel_media")):
            for gm in re.finditer(r"(\{.*\})", body, re.DOTALL):
                try:
                    payloads.append(json.loads(gm.group(1)))
                    break
                except Exception:  # noqa: BLE001
                    continue
    return payloads


def _best_video_from_node(node: dict) -> tuple[str, str]:
    """Return (dash_xml, best_progressive_url) from a Threads/Instagram post or carousel item."""
    dash_xml = node.get("video_dash_manifest") or node.get("dash_manifest") or ""
    if isinstance(dash_xml, dict):
        dash_xml = dash_xml.get("xml") or ""
    versions = node.get("video_versions") or []
    best_url = ""
    if isinstance(versions, list) and versions:
        ranked = sorted(
            (v for v in versions if isinstance(v, dict) and v.get("url")),
            key=lambda v: (int(v.get("width") or 0) * int(v.get("height") or 0), -int(v.get("type") or 0)),
            reverse=True,
        )
        if ranked:
            best_url = ranked[0]["url"]
    return str(dash_xml), best_url


def _best_image_from_node(node: dict) -> str:
    """Return highest-resolution non-avatar candidate URL from a Threads/Instagram node."""
    img2 = node.get("image_versions2") or {}
    cands = img2.get("candidates") if isinstance(img2, dict) else []
    if not isinstance(cands, list) or not cands:
        return ""
    ranked = sorted(
        (c for c in cands if isinstance(c, dict) and c.get("url")),
        key=lambda c: int(c.get("width") or 0) * int(c.get("height") or 0),
        reverse=True,
    )
    return ranked[0]["url"] if ranked else ""


def _threads_items_from_post(post: dict, code: str, title: str, page_url: str) -> list[ExtractedItem]:
    """Convert one matched Threads post dict into ExtractedItem entries."""
    items: list[ExtractedItem] = []
    seen: set[str] = set()
    archive_id = f"threads {code}" if code else ""
    carousel = post.get("carousel_media")
    if isinstance(carousel, list) and carousel:
        for idx, slide in enumerate(carousel, 1):
            if not isinstance(slide, dict):
                continue
            slide_title = f"{title} ({idx})" if title and len(carousel) > 1 else title
            dash_xml, vid_url = _best_video_from_node(slide)
            if dash_xml or vid_url:
                key = vid_url or f"dash:{idx}"
                if key not in seen:
                    seen.add(key)
                    items.append(ExtractedItem(
                        url=vid_url or page_url,
                        kind="video",
                        title=slide_title,
                        dash_xml=dash_xml,
                        fallback_url=vid_url,
                        archive_id=archive_id if idx == 1 else f"{archive_id}_{idx}" if archive_id else "",
                    ))
                continue
            img_url = _best_image_from_node(slide)
            if img_url and img_url not in seen:
                seen.add(img_url)
                items.append(ExtractedItem(
                    url=img_url,
                    kind="image",
                    title=slide_title,
                    archive_id=archive_id if idx == 1 else f"{archive_id}_{idx}" if archive_id else "",
                ))
        return items

    dash_xml, vid_url = _best_video_from_node(post)
    if dash_xml or vid_url:
        items.append(ExtractedItem(
            url=vid_url or page_url,
            kind="video",
            title=title,
            dash_xml=dash_xml,
            fallback_url=vid_url,
            archive_id=archive_id,
        ))
        return items

    audio_node = post.get("audio")
    audio_url = audio_node.get("audio_src") if isinstance(audio_node, dict) else ""
    if audio_url:
        items.append(ExtractedItem(
            url=str(audio_url),
            kind="audio",
            title=title,
            archive_id=archive_id,
        ))
        return items

    img_url = _best_image_from_node(post)
    if img_url:
        items.append(ExtractedItem(
            url=img_url,
            kind="image",
            title=title,
            archive_id=archive_id,
        ))
    return items


def _collect_threads_posts(obj, current_code: str, out: list[tuple[str, dict]]) -> None:
    """Recursively gather post dicts carrying media along with their shortcode."""
    if isinstance(obj, list):
        for elem in obj:
            _collect_threads_posts(elem, current_code, out)
        return
    if not isinstance(obj, dict):
        return
    code = str(obj.get("code") or current_code or "")
    has_audio = isinstance(obj.get("audio"), dict) and bool(obj["audio"].get("audio_src"))
    has_media = has_audio or any(
        obj.get(k) for k in ("video_versions", "video_dash_manifest", "dash_manifest", "carousel_media", "image_versions2")
    )
    if has_media:
        out.append((code, obj))
        if obj.get("carousel_media"):
            return  # already owns its carousel slides
    for k, v in obj.items():
        if k in ("quoted_post", "reposted_post") and has_media:
            continue
        _collect_threads_posts(v, code, out)


def parse_threads_payloads(payloads: list, page_url: str, target_code: str = "") -> ExtractionResult:
    """Extract the target Threads post (or profile posts) video/DASH/carousel/photo/audio items."""
    code = target_code or threads_post_code(page_url)
    candidates: list[tuple[str, dict]] = []
    for p in payloads:
        _collect_threads_posts(p, "", candidates)

    res = ExtractionResult(url=page_url, site="Threads")
    if not candidates:
        return res

    chosen: list[tuple[str, dict]] = []
    if code:
        chosen = [(c, p) for c, p in candidates if c == code]
    if not chosen:
        if not code:
            # Profile / feed page: keep one entry per unique post shortcode
            seen_codes: set[str] = set()
            for c, p in candidates:
                key = c or str(p.get("pk") or len(seen_codes))
                if key not in seen_codes:
                    seen_codes.add(key)
                    chosen.append((c, p))
        else:
            # Only fall back to codeless payloads (e.g. embed payload) when no other post shortcodes exist
            has_other_codes = any(c for c, _p in candidates)
            if not has_other_codes:
                chosen = candidates[:1]

    for c, post in chosen:
        user = ((post.get("user") or {}) if isinstance(post.get("user"), dict) else {}).get("username", "")
        cap_obj = post.get("caption")
        caption = (cap_obj.get("text") if isinstance(cap_obj, dict) else str(cap_obj or "")).strip()
        caption_line = " ".join(caption.splitlines())[:80].strip()
        if user and caption_line:
            title = f"@{user} - {caption_line}"
        elif user:
            title = f"@{user} - {c or 'post'}"
        else:
            title = caption_line or f"Threads {c}"
        if not res.title:
            res.title = title
        res.items.extend(_threads_items_from_post(post, c or code, title, page_url))
    return res


def _res_score(attrs: dict[str, str], url: str) -> tuple[int, int, int]:
    """Score a <video>/<audio> candidate so adaptive streams and higher-resolution MP4 sources win."""
    stype = (attrs.get("type") or "").lower()
    ext = _ext_of(url)
    is_stream = int("mpegurl" in stype or "dash+xml" in stype or ext in _STREAM_EXTS)
    res_hint = 0
    for k in ("data-res", "data-height", "res", "height", "size", "label"):
        m = re.search(r"(\d{3,4})", attrs.get(k, ""))
        if m:
            res_hint = max(res_hint, int(m.group(1)))
    if not res_hint:
        m = re.search(r"(\d{3,4})p\b", url, re.IGNORECASE)
        if m:
            res_hint = int(m.group(1))
    is_mp4 = int(ext in ("mp4", "m4v", "mov", "m4a") or "mp4" in stype)
    return (is_stream, res_hint, is_mp4)


def extract_from_html(url: str, html: str, title_hint: str = "") -> tuple[ExtractionResult, list[str], bool]:
    """Parse HTML for streams, video/audio, JSON-LD, OpenGraph, iframes, script URLs, and downloadable files.

    Returns ``(result, iframe_urls, has_dynamic_scripts)``.
    """
    base_url = url
    base_m = re.search(r"""<base\b([^>]+)>""", html, re.IGNORECASE)
    if base_m:
        href = _parse_attrs(base_m.group(1)).get("href", "")
        if href:
            base_url = urllib.parse.urljoin(url, href)

    title_m = re.search(r"""<title[^>]*>(.*?)</title>""", html, re.IGNORECASE | re.DOTALL)
    doc_title = html_lib.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip()) if title_m else title_hint

    meta: dict[str, list[str]] = {}
    for m in re.finditer(r"""<(?:meta|link)\b([^>]+)>""", html, re.IGNORECASE):
        attrs = _parse_attrs(m.group(1))
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        val = attrs.get("content") or (attrs.get("href") if attrs.get("itemprop") else "") or ""
        if key and val:
            meta.setdefault(key, []).append(val)

    res = ExtractionResult(url=url, title=meta.get("og:title", [doc_title])[0] or doc_title)
    seen: set[str] = set()

    def add_item(raw_url: str, kind: str, title: str = "", fallback_url: str = ""):
        u = _clean_escaped_url(raw_url, base_url)
        if not u or u in seen or (kind != "file" and _is_page_asset(u)):
            return
        seen.add(u)
        fb = _clean_escaped_url(fallback_url, base_url) if fallback_url else ""
        res.items.append(ExtractedItem(url=u, kind=kind, title=title or res.title, ext=_ext_of(u), fallback_url=fb))

    # 1. JSON-LD VideoObject / AudioObject / MediaObject
    for m in re.finditer(r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""", html, re.I | re.S):
        try:
            ld = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        stack = [ld]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = str(node.get("@type") or "").lower()
                c_url = node.get("contentUrl") or ""
                if c_url:
                    kind = "audio" if "audio" in t else ("image" if "image" in t else "video")
                    if _ext_of(str(c_url)) in _STREAM_EXTS:
                        kind = "stream"
                    add_item(str(c_url), kind, str(node.get("name") or ""))
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)

    # 2. <video> / <audio> tags & nested <source> (collapse alternative encodings/resolutions per media element)
    media_block_re = re.compile(r"""<(video|audio)\b([^>]*)>(.*?)</\1>|<(video|audio)\b([^>]*)>""", re.I | re.S)
    for m in media_block_re.finditer(html):
        tag = (m.group(1) or m.group(4) or "video").lower()
        attrs = _parse_attrs(m.group(2) or m.group(5) or "")
        inner = m.group(3) or ""
        cands: list[tuple[tuple[int, int, int], str, str]] = []
        for attr_name in (
            "src", "data-src", "data-hls", "data-hls-url", "data-dash-url", "data-mp4", "data-mp4-url",
            "data-video", "data-video-url", "data-audio", "data-audio-url", "data-stream", "data-stream-url",
            "data-file", "data-url", "data-source", "data-playlist",
        ):
            raw = attrs.get(attr_name, "")
            if raw:
                ext = _ext_of(raw)
                kind = "stream" if (ext in _STREAM_EXTS or "hls" in attr_name or "dash" in attr_name) else (
                    "audio" if tag == "audio" or ext in _AUDIO_EXTS else "video"
                )
                cands.append((_res_score(attrs, raw), raw, kind))
        for cfg_attr in ("data-setup", "data-config", "data-player"):
            setup = attrs.get(cfg_attr, "")
            if setup.startswith("{"):
                try:
                    cfg = json.loads(setup)
                    for src_entry in cfg.get("sources") or []:
                        if isinstance(src_entry, dict) and src_entry.get("src"):
                            s_url = str(src_entry["src"])
                            cands.append((
                                _res_score({"type": str(src_entry.get("type") or ""), "label": str(src_entry.get("label") or "")}, s_url),
                                s_url,
                                "stream" if _ext_of(s_url) in _STREAM_EXTS else "video",
                            ))
                except Exception:  # noqa: BLE001
                    pass
        for sm in re.finditer(r"""<source\b([^>]+)>""", inner, re.IGNORECASE):
            s_attrs = _parse_attrs(sm.group(1))
            s_url = s_attrs.get("src") or s_attrs.get("data-src") or ""
            stype = (s_attrs.get("type") or "").lower()
            if s_url:
                if "mpegurl" in stype or "dash+xml" in stype or _ext_of(s_url) in _STREAM_EXTS:
                    kind = "stream"
                elif tag == "audio" or stype.startswith("audio/"):
                    kind = "audio"
                else:
                    kind = "video"
                cands.append((_res_score(s_attrs, s_url), s_url, kind))
        if cands:
            cands.sort(key=lambda x: x[0], reverse=True)
            best_url, best_kind = cands[0][1], cands[0][2]
            fb_url = next((u for _sc, u, k in cands[1:] if k != "stream"), "")
            add_item(best_url, best_kind, fallback_url=fb_url)

    # 3. Inline scripts: escaped URLs, player config JSON, URL-encoded URLs, and atob(...) URLs
    scripts = list(re.finditer(r"""<script\b([^>]*)>(.*?)</script>""", html, re.IGNORECASE | re.DOTALL))
    has_scripts = any(
        "src=" in _parse_attrs(s.group(1)) or "json" not in _parse_attrs(s.group(1)).get("type", "").lower()
        for s in scripts
    )
    for sm in scripts:
        s_body = sm.group(2)
        if not s_body:
            continue
        norm_body = (
            s_body.replace("\\/", "/")
            .replace("\\x2f", "/").replace("\\x2F", "/")
            .replace("\\u002f", "/").replace("\\u002F", "/")
            .replace("\\u0026", "&").replace("&amp;", "&")
        )
        for um in _PLAYER_KEY_RE.finditer(norm_body):
            u = um.group(1)
            add_item(u, "stream" if _ext_of(u) in _STREAM_EXTS else "video")
        for um in _MEDIA_URL_RE.finditer(norm_body):
            u = um.group(1)
            ext = _ext_of(u)
            kind = "stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video")
            add_item(u, kind)
        for em in _ENCODED_MEDIA_RE.finditer(norm_body):
            u = urllib.parse.unquote(em.group(1))
            ext = _ext_of(u)
            kind = "stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video")
            add_item(u, kind)
        for am in _ATOB_RE.finditer(s_body):
            try:
                dec = base64.b64decode(am.group(1)).decode("utf-8", "ignore").strip()
            except Exception:  # noqa: BLE001
                continue
            if dec.startswith(("http://", "https://", "//", "/")) and _ext_of(dec) in (_STREAM_EXTS | _VIDEO_EXTS | _AUDIO_EXTS):
                ext = _ext_of(dec)
                kind = "stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video")
                add_item(dec, kind)
            elif dec.startswith("{"):
                for um in _MEDIA_URL_RE.finditer(dec.replace("\\/", "/")):
                    u = um.group(1)
                    ext = _ext_of(u)
                    add_item(u, "stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video"))

    # 4. OpenGraph / Twitter / schema.org video & audio meta tags (skip passive og:video if a stream was already found)
    has_stream = any(it.kind == "stream" for it in res.items)
    if not has_stream:
        for k in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream", "contenturl"):
            for val in meta.get(k, []):
                ext = _ext_of(val)
                kind = "stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video")
                add_item(val, kind)
    for k in ("og:audio", "og:audio:url", "og:audio:secure_url"):
        for val in meta.get(k, []):
            add_item(val, "audio")

    # 5. Embedded iframes & twitter:player / object / embed
    iframes: list[str] = []

    def add_iframe(raw_src: str):
        u = _clean_escaped_url(raw_src, base_url)
        if not u:
            return
        host = _host_of(u)
        if host and not any(host.endswith(s) for s in _SKIP_IFRAME_HOSTS) and u not in iframes:
            iframes.append(u)

    for im in re.finditer(r"""<iframe\b([^>]+)>""", html, re.IGNORECASE):
        attrs = _parse_attrs(im.group(1))
        for attr in ("src", "data-src", "data-lazy-src", "data-litespeed-src", "data-embed", "data-iframe-src"):
            if attrs.get(attr):
                add_iframe(attrs[attr])
                break
    for em in re.finditer(r"""<(?:embed|object)\b([^>]+)>""", html, re.IGNORECASE):
        attrs = _parse_attrs(em.group(1))
        src = attrs.get("src") or attrs.get("data") or ""
        if src:
            ext = _ext_of(src)
            if ext in (_STREAM_EXTS | _VIDEO_EXTS | _AUDIO_EXTS):
                add_item(src, "stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video"))
            else:
                add_iframe(src)
    for tp in meta.get("twitter:player", []):
        add_iframe(tp)

    # 6. JDownloader-style downloadable <a href> files (only when no stream/video player was found)
    if not res.items:
        for am in re.finditer(r"""<a\b([^>]+)>(.*?)</a>""", html, re.IGNORECASE | re.DOTALL):
            attrs = _parse_attrs(am.group(1))
            href = attrs.get("href") or ""
            u = _clean_escaped_url(href, base_url)
            if not u:
                continue
            resolved = resolve_file_host(u)
            if resolved:
                link_title = html_lib.unescape(re.sub(r"<[^>]+>", " ", am.group(2))).strip()
                add_item(resolved, "file", link_title)
                continue
            ext = _ext_of(u)
            if ext in (_VIDEO_EXTS | _AUDIO_EXTS | _STREAM_EXTS | _FILE_EXTS) or "download" in attrs:
                link_title = html_lib.unescape(re.sub(r"<[^>]+>", " ", am.group(2))).strip()
                if ext in _STREAM_EXTS:
                    kind = "stream"
                elif ext in _VIDEO_EXTS:
                    kind = "video"
                elif ext in _AUDIO_EXTS:
                    kind = "audio"
                else:
                    kind = "file"
                add_item(u, kind, link_title)

    # Prefer streaming manifests or video/audio over generic fallback items when ordered
    priority = {"stream": 0, "video": 1, "audio": 2, "file": 3, "image": 4}
    res.items.sort(key=lambda it: priority.get(it.kind, 9))
    return res, iframes, has_scripts or bool(iframes)


def _hls_child_urls(manifest_url: str, body: str) -> set[str]:
    """Return variant playlist and rendition URIs referenced inside an HLS master manifest."""
    children: set[str] = set()
    lines = body.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                children.add(urllib.parse.urljoin(manifest_url, lines[j].strip()))
        elif line.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")):
            m = re.search(r'URI="([^"]+)"', line, re.IGNORECASE)
            if m:
                children.add(urllib.parse.urljoin(manifest_url, m.group(1)))
    return children


def _merge_sniff_result(
    res: ExtractionResult,
    sniff: SniffResult,
    url: str,
    threads_mode: bool = False,
    target_code: str = "",
) -> ExtractionResult:
    """Populate *res* from headless browser capture (manifests, GraphQL JSON, progressive media, DOM)."""
    if sniff.cookies_text:
        res.cookies_text = sniff.cookies_text
    if sniff.live:
        res.live = True

    if threads_mode:
        payloads = list(_iter_json_payloads(sniff.html)) if sniff.html else []
        for jp in sniff.json_payloads:
            try:
                payloads.append(json.loads(jp["body"]))
            except Exception:  # noqa: BLE001
                pass
        t_res = parse_threads_payloads(payloads, sniff.url or url, target_code=target_code)
        if t_res.items:
            res.site = "Threads"
            res.title = res.title or t_res.title or sniff.title
            res.items.extend(t_res.items)
            return res

    # Hide sub-playlists (e.g. 360p/index.m3u8) that belong to a master HLS manifest
    hidden_variants: set[str] = set()
    for man in sniff.manifests:
        if man["kind"] == "hls":
            hidden_variants.update(_hls_child_urls(man["url"], man.get("body") or ""))

    seen = {it.url for it in res.items} | hidden_variants
    # 1. Non-DRM HLS / DASH manifests captured over CDP network
    for man in sniff.manifests:
        if man["url"] not in seen:
            seen.add(man["url"])
            res.items.append(ExtractedItem(
                url=man["url"],
                kind="stream",
                title=sniff.title or res.title,
                ext="m3u8" if man["kind"] == "hls" else "mpd",
                headers=man.get("headers") or {},
            ))

    # 2. Direct media responses captured over CDP
    for m in sniff.media_urls:
        m_url = m["url"]
        if "bytestart=" in m_url.lower() or "byteend=" in m_url.lower():
            m_url = strip_byte_range(m_url)
        if m_url in seen or _is_page_asset(m_url):
            continue
        seen.add(m_url)
        res.items.append(ExtractedItem(
            url=m_url,
            kind=m["kind"],
            title=sniff.title or res.title,
            ext=_ext_of(m_url),
            headers=m.get("headers") or {},
            size=int(m.get("size") or 0),
        ))

    # 3. Rendered DOM & GraphQL JSON bodies
    if sniff.html:
        dom_res, _iframes, _dyn = extract_from_html(sniff.url or url, sniff.html, title_hint=sniff.title)
        if not res.title:
            res.title = dom_res.title
        # If CDP already captured live manifests/media, only supplement with other streams (avoid passive fallbacks)
        have_network = bool(res.items)
        for it in dom_res.items:
            if have_network and it.kind != "stream":
                continue
            if it.url not in seen:
                seen.add(it.url)
                res.items.append(it)

    for jp in sniff.json_payloads:
        body = jp.get("body") or ""
        for um in _PLAYER_KEY_RE.finditer(body):
            u = _clean_escaped_url(um.group(1), sniff.url or url)
            if u and u not in seen and not _is_page_asset(u):
                seen.add(u)
                res.items.append(ExtractedItem(
                    url=u,
                    kind="stream" if _ext_of(u) in _STREAM_EXTS else "video",
                    title=sniff.title or res.title,
                    ext=_ext_of(u),
                ))
        for um in _MEDIA_URL_RE.finditer(body):
            u = _clean_escaped_url(um.group(1), sniff.url or url)
            if u and u not in seen and not _is_page_asset(u):
                seen.add(u)
                ext = _ext_of(u)
                res.items.append(ExtractedItem(
                    url=u,
                    kind="stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video"),
                    title=sniff.title or res.title,
                    ext=ext,
                ))

    if sniff.drm and not res.items:
        res.drm = sniff.drm
    return res


def _filter_by_mode(items: list[ExtractedItem], mode: str) -> list[ExtractedItem]:
    if mode == "video":
        return [it for it in items if it.kind in ("stream", "video")]
    if mode == "audio":
        return [it for it in items if it.kind in ("stream", "audio", "video")]
    return items


def _direct_result(final_url: str, status: int, r_headers: dict, cookies_text: str) -> ExtractionResult:
    cd_name = _filename_from_cd(r_headers.get("content-disposition", ""))
    ext = _ext_of(final_url) or (cd_name.rsplit(".", 1)[-1].lower() if "." in cd_name else "")
    if not ext:
        ctype = (r_headers.get("content-type") or "").split(";")[0].strip().lower()
        import mimetypes
        guessed = mimetypes.guess_extension(ctype) or ""
        ext = guessed.lstrip(".").lower()
    kind = "audio" if ext in _AUDIO_EXTS else ("video" if ext in _VIDEO_EXTS else ("image" if ext in _IMAGE_EXTS else "file"))
    size = 0
    try:
        size = int(r_headers.get("content-length") or 0)
    except ValueError:
        size = 0
    return ExtractionResult(
        url=final_url,
        title=cd_name,
        items=[ExtractedItem(url=final_url, kind=kind, title=cd_name, ext=ext, size=size)],
        cookies_text=cookies_text,
        is_direct_content=True,
        status=status,
    )


def _manifest_item_if_any(final_url: str, r_headers: dict, text: str) -> ExtractedItem | None:
    """Detect direct HLS/DASH manifests (by Content-Type, URL extension, or body signature)."""
    ctype = (r_headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = _ext_of(final_url)
    head = (text or "").lstrip()[:512]
    is_hls = "mpegurl" in ctype or ext == "m3u8" or head.startswith("#EXTM3U")
    is_dash = "dash+xml" in ctype or ext == "mpd" or ("<MPD" in head or "<mpd" in head)
    if is_hls:
        return ExtractedItem(url=final_url, kind="stream", ext="m3u8")
    if is_dash:
        return ExtractedItem(url=final_url, kind="stream", ext="mpd", dash_xml=text if "<MPD" in head or "<mpd" in head else "")
    return None


def _ytdlp_known_extractor(url: str) -> str | None:
    """Return yt-dlp extractor key if a non-Generic extractor handles *url*."""
    from yt_dlp.extractor import gen_extractor_classes
    if not hasattr(_ytdlp_known_extractor, "ies"):
        _ytdlp_known_extractor.ies = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]
    for ie in _ytdlp_known_extractor.ies:
        try:
            if ie.suitable(url) and ie.working():
                return ie.ie_key().replace("Tab", "")
        except Exception:  # noqa: BLE001
            continue
    return None


def _meta_refresh_url(html: str, base_url: str) -> str | None:
    m = re.search(
        r"""<meta\b[^>]*http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["']([^"']+)["']""",
        html,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"""<meta\b[^>]*content\s*=\s*["']([^"']+)["'][^>]*http-equiv\s*=\s*["']?refresh["']?""",
            html,
            re.IGNORECASE,
        )
    if not m:
        return None
    um = re.search(r"""^\s*(\d+)?\s*[;,]\s*url\s*=\s*['"]?([^'">\s]+)""", m.group(1), re.IGNORECASE)
    if not um:
        return None
    delay = int(um.group(1) or 0)
    if delay > 5:
        return None
    target = urllib.parse.urljoin(base_url, html_lib.unescape(um.group(2).strip()))
    return target if target != base_url and target.startswith(("http://", "https://")) else None


def _resolve_host_or_redirect(
    orig_url: str,
    final_url: str,
    html: str,
    cookie_file: str | Path | None,
) -> ExtractionResult | None:
    """Resolve Google Drive confirm pages, MediaFire, Yandex Disk, Pixeldrain albums, Archive.org, and meta-refresh."""
    host = _host_of(final_url)
    orig_host = _host_of(orig_url)
    path = urllib.parse.urlsplit(final_url).path

    # 1. Google Drive virus-scan interstitial form
    if "drive.google.com" in host or "drive.usercontent.google.com" in host:
        confirmed = extract_google_drive_confirm(html, final_url)
        if confirmed:
            return ExtractionResult(
                url=confirmed,
                site="Google Drive",
                items=[ExtractedItem(url=confirmed, kind="file")],
                is_direct_content=True,
                status=200,
            )

    # 2. MediaFire direct download button
    if host.endswith("mediafire.com") and html:
        m = re.search(r"""<a\b[^>]*id=["']downloadButton["'][^>]*>""", html, re.IGNORECASE)
        if m:
            attrs = _parse_attrs(m.group(0))
            dl = ""
            if attrs.get("data-scrambled-url"):
                try:
                    dl = base64.b64decode(attrs["data-scrambled-url"]).decode("utf-8", "ignore").strip()
                except Exception:  # noqa: BLE001
                    dl = ""
            if not dl and (attrs.get("href") or "").startswith(("http://", "https://")):
                dl = attrs["href"]
            if dl:
                return ExtractionResult(
                    url=dl,
                    site="MediaFire",
                    items=[ExtractedItem(url=dl, kind="file", ext=_ext_of(dl))],
                    is_direct_content=True,
                    status=200,
                )

    # 3. Yandex Disk public share API
    if orig_host in ("yadi.sk", "disk.yandex.ru", "disk.yandex.com") or host in ("yadi.sk", "disk.yandex.ru", "disk.yandex.com"):
        api_url = f"https://cloud-api.yandex.net/v1/disk/public/resources/download?public_key={urllib.parse.quote(orig_url, safe='')}"
        _u, st, _h, body, _d, _c = fetch_page_curl(api_url, cookie_file=cookie_file, timeout=10.0)
        if st == 200 and body:
            try:
                href = json.loads(body).get("href")
                if href:
                    return ExtractionResult(
                        url=href,
                        site="Yandex Disk",
                        items=[ExtractedItem(url=href, kind="file")],
                        is_direct_content=True,
                        status=200,
                    )
            except Exception:  # noqa: BLE001
                pass

    # 4. Pixeldrain album /l/<id>
    if host == "pixeldrain.com" and path.startswith("/l/"):
        lid = path[3:].strip("/").split("/")[0]
        if lid:
            _u, st, _h, body, _d, _c = fetch_page_curl(f"https://pixeldrain.com/api/list/{lid}", cookie_file=cookie_file, timeout=10.0)
            if st == 200 and body:
                try:
                    data = json.loads(body)
                    files = data.get("files") or []
                    items = [
                        ExtractedItem(
                            url=f"https://pixeldrain.com/api/file/{f['id']}?download",
                            kind="file",
                            title=str(f.get("name") or f["id"]),
                            ext=_ext_of(str(f.get("name") or "")),
                            size=int(f.get("size") or 0),
                        )
                        for f in files if isinstance(f, dict) and f.get("id")
                    ]
                    if items:
                        return ExtractionResult(url=final_url, site="Pixeldrain", title=str(data.get("title") or ""), items=items, status=200)
                except Exception:  # noqa: BLE001
                    pass

    # 5. Internet Archive /details/<id> downloadable files metadata API
    if host == "archive.org" and path.startswith("/details/"):
        item_id = path[len("/details/"):].strip("/").split("/")[0]
        if item_id:
            _u, st, _h, body, _d, _c = fetch_page_curl(f"https://archive.org/metadata/{urllib.parse.quote(item_id)}", timeout=12.0)
            if st == 200 and body:
                try:
                    meta = json.loads(body)
                    title = str(((meta.get("metadata") or {}) if isinstance(meta.get("metadata"), dict) else {}).get("title") or item_id)
                    items: list[ExtractedItem] = []
                    for f in meta.get("files") or []:
                        if not isinstance(f, dict):
                            continue
                        name = str(f.get("name") or "")
                        if not name or name.endswith(("_meta.xml", "_files.xml", "_meta.sqlite", ".torrent", "__ia_thumb.jpg")):
                            continue
                        if f.get("source") == "metadata":
                            continue
                        ext = _ext_of(name)
                        if ext in (_VIDEO_EXTS | _AUDIO_EXTS | _FILE_EXTS | _IMAGE_EXTS):
                            kind = "video" if ext in _VIDEO_EXTS else ("audio" if ext in _AUDIO_EXTS else ("image" if ext in _IMAGE_EXTS else "file"))
                            items.append(ExtractedItem(
                                url=f"https://archive.org/download/{urllib.parse.quote(item_id)}/{urllib.parse.quote(name)}",
                                kind=kind,
                                title=name,
                                ext=ext,
                                size=int(f.get("size") or 0),
                            ))
                    if items:
                        return ExtractionResult(url=final_url, site="Internet Archive", title=title, items=items, status=200)
                except Exception:  # noqa: BLE001
                    pass

    # 6. HTML <meta http-equiv="refresh"> redirect (SourceForge mirrors, download gates, etc.)
    if html:
        redir = _meta_refresh_url(html, final_url)
        if redir:
            r_url, r_st, r_hdrs, _r_html, r_direct, r_cookies = fetch_page_curl(redir, cookie_file=cookie_file, referer=final_url)
            if r_direct:
                return _direct_result(r_url, r_st, r_hdrs, r_cookies)
    return None


_THREADS_RESTRICTED_RE = re.compile(r"isn.{1,8}t available to everyone", re.IGNORECASE)


def extract_threads(
    url: str,
    cookie_file: str | Path | None = None,
    cancel_event: threading.Event | None = None,
    allow_browser: bool = True,
    note=None,
) -> ExtractionResult:
    """Extract video/DASH/carousel/photo/audio items from a Threads post, profile, or embed page."""
    code = threads_post_code(url)
    final_url, status, r_hdrs, html, is_direct, cookies_text = fetch_page_curl(url, cookie_file=cookie_file)
    if is_direct:
        res = _direct_result(final_url, status, r_hdrs, cookies_text)
        res.site = "Threads"
        return res

    payloads = _iter_json_payloads(html) if html else []
    res = parse_threads_payloads(payloads, final_url, target_code=code)
    res.status = status
    res.cookies_text = cookies_text

    # Try lightweight /embed endpoint before spawning headless Chromium
    if not res.items and code and "/embed" not in urllib.parse.urlsplit(final_url).path:
        base_path = urllib.parse.urlsplit(final_url)._replace(query="", fragment="")
        embed_url = urllib.parse.urlunsplit(base_path._replace(path=base_path.path.rstrip("/") + "/embed"))
        _, _st, _h, emb_html, _, emb_cookies = fetch_page_curl(embed_url, cookie_file=cookie_file, referer=final_url)
        if emb_html:
            emb_res = parse_threads_payloads(_iter_json_payloads(emb_html), final_url, target_code=code)
            if emb_res.items:
                res.items = emb_res.items
                res.title = res.title or emb_res.title
            else:
                html_res, _iframes, _dyn = extract_from_html(final_url, emb_html)
                if html_res.items:
                    res.items = html_res.items
                    res.title = res.title or html_res.title
            if emb_cookies:
                res.cookies_text = emb_cookies

    # Check OpenGraph tags on main HTML if no JSON media existed
    if not res.items and html:
        html_res, _iframes, _dyn = extract_from_html(final_url, html)
        if html_res.items:
            res.items = html_res.items
            res.title = res.title or html_res.title

    if not res.items and allow_browser and status not in (404, 410):
        sniff = sniff_page(url, cookie_file=cookie_file, cancel_event=cancel_event, note=note)
        res = _merge_sniff_result(res, sniff, url, threads_mode=True, target_code=code)
        if not res.items and _THREADS_RESTRICTED_RE.search(sniff.html or ""):
            res.error = "Threads says this post isn't available to everyone (it may be private, or limited by age or region)."

    return res


def extract_media(
    url: str,
    mode: str = "auto",
    cookie_file: str | Path | None = None,
    referer: str = "",
    cancel_event: threading.Event | None = None,
    allow_browser: bool = True,
    note=None,
) -> ExtractionResult:
    """Universal extractor combining curl_cffi TLS, file hosts, static HTML/JS mining, iframes, and headless CDP sniffing."""
    if is_threads_url(url):
        return extract_threads(url, cookie_file=cookie_file, cancel_event=cancel_event, allow_browser=allow_browser, note=note)

    rewritten = resolve_file_host(url)
    target_url = rewritten or url

    final_url, status, r_hdrs, html, is_direct, cookies_text = fetch_page_curl(
        target_url, cookie_file=cookie_file, referer=referer
    )
    if is_direct:
        return _direct_result(final_url, status, r_hdrs, cookies_text)

    if status in (404, 410):
        return ExtractionResult(url=final_url, status=status, error=f"HTTP Error {status}")

    # Direct HLS / DASH manifest responses (even when served as text/plain without extension)
    man_item = _manifest_item_if_any(final_url, r_hdrs, html) if status < 400 else None
    if man_item:
        return ExtractionResult(url=final_url, items=[man_item], cookies_text=cookies_text, status=status)

    res = ExtractionResult(url=final_url, cookies_text=cookies_text, status=status)
    iframes: list[str] = []
    has_dynamic = False

    # JSON API endpoint directly returned
    ctype = (r_hdrs.get("content-type") or "").split(";")[0].strip().lower()
    if "json" in ctype and html:
        norm = html.replace("\\/", "/")
        seen_j: set[str] = set()
        for pat in (_PLAYER_KEY_RE, _MEDIA_URL_RE):
            for um in pat.finditer(norm):
                u = _clean_escaped_url(um.group(1), final_url)
                if u and u not in seen_j:
                    seen_j.add(u)
                    ext = _ext_of(u)
                    res.items.append(ExtractedItem(
                        url=u,
                        kind="stream" if ext in _STREAM_EXTS else ("audio" if ext in _AUDIO_EXTS else "video"),
                        ext=ext,
                    ))
        res.items = _filter_by_mode(res.items, mode)
        if res.items:
            return res

    if html:
        res, iframes, has_dynamic = extract_from_html(final_url, html)
        res.cookies_text = cookies_text
        res.status = status
        res.items = _filter_by_mode(res.items, mode)

    # Host-specific HTML/API resolvers (Google Drive virus-scan confirm, MediaFire, Yandex, Pixeldrain album, Archive.org, meta-refresh)
    if (not res.items or "drive.usercontent.google.com" in final_url) and status < 400:
        host_res = _resolve_host_or_redirect(url, final_url, html, cookie_file)
        if host_res and host_res.items:
            host_res.cookies_text = host_res.cookies_text or cookies_text
            if not host_res.is_direct_content:
                host_res.items = _filter_by_mode(host_res.items, mode)
            if host_res.items:
                return host_res

    # Inspect embedded player iframes one hop deep when no media found on outer page
    if not res.items and iframes:
        for frame_url in iframes[:4]:
            if is_threads_url(frame_url):
                f_res = extract_threads(frame_url, cookie_file=cookie_file, cancel_event=cancel_event, allow_browser=False)
                if f_res.items:
                    res.items.extend(f_res.items)
                    res.title = res.title or f_res.title
                    res.site = res.site or f_res.site
                    break
            known_site = _ytdlp_known_extractor(frame_url)
            if known_site and mode != "images":
                res.items.append(ExtractedItem(
                    url=frame_url,
                    kind="video",
                    title=res.title,
                    headers={"Referer": final_url},
                ))
                break
            f_url, _f_st, fh, f_html, f_direct, f_cookies = fetch_page_curl(
                frame_url, cookie_file=cookie_file, referer=final_url
            )
            if f_direct:
                d_res = _direct_result(f_url, _f_st, fh, f_cookies)
                for it in d_res.items:
                    it.title = res.title or it.title
                res.items.extend(d_res.items)
                res.cookies_text = res.cookies_text or f_cookies
                break
            f_man = _manifest_item_if_any(f_url, fh, f_html)
            if f_man:
                f_man.title = res.title
                f_man.headers = {"Referer": final_url}
                res.items.append(f_man)
                break
            if f_html:
                f_res, _fi, f_dyn = extract_from_html(f_url, f_html, title_hint=res.title)
                if f_dyn:
                    has_dynamic = True
                if f_res.items:
                    for it in f_res.items:
                        if res.title and (not it.title or it.title == f_res.title):
                            it.title = res.title
                    res.items.extend(_filter_by_mode(f_res.items, mode))
                    break

    # Escalate to headless Chromium only when the page has JS/iframes or bot-blocked status
    need_browser = (
        allow_browser
        and not res.items
        and (has_dynamic or status in (0, 403, 429, 503))
    )
    if need_browser:
        hdrs = {"Referer": referer} if referer else None
        sniff = sniff_page(url, cookie_file=cookie_file, headers=hdrs, cancel_event=cancel_event, note=note)
        res = _merge_sniff_result(res, sniff, url)
        res.items = _filter_by_mode(res.items, mode)

    return res

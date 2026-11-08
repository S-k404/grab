// Decides which network requests are downloadable media. Pure functions: no browser APIs,
// so tests/test_extension.mjs can run them under Node.

const MANIFEST_TYPES = [
  [/mpegurl/i, "HLS"], [/dash\+xml/i, "DASH"], [/vnd\.ms-sstr\+xml/i, "Smooth"], [/f4m/i, "HDS"],
];
const MANIFEST_EXT = { m3u8: "HLS", mpd: "DASH", f4m: "HDS", ism: "Smooth" };
const MEDIA_EXT = { mp4: "MP4", m4v: "MP4", webm: "WebM", mov: "MOV", mkv: "MKV", flv: "FLV", ogv: "OGG",
                    mp3: "MP3", m4a: "M4A", aac: "AAC", ogg: "OGG", opus: "Opus", wav: "WAV", flac: "FLAC" };
const AUDIO_EXT = new Set(["mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"]);
// pieces of a stream, not something to download on its own
const SEGMENT_RE = /\.(ts|m4s|cmfv|cmfa|m4f|vtt|webvtt|key)(\?|$)|[/_-](seg|segment|chunk|frag|fragment|part)[-_]?\d+|init\.(mp4|m4s)|\/range\/\d+-\d+|[?&](range|bytestart|byterange)=|\/QualityLevels\(/i;
// sites Grab already understands from the page link (better titles, all qualities, audio merged)
const NATIVE_HOSTS = /(^|\.)(googlevideo\.com|youtube\.com|ytimg\.com|ggpht\.com)$/i;
// ad / tracking players whose clips nobody wants
const AD_HOSTS = /(doubleclick\.net|googlesyndication\.com|imasdk\.googleapis\.com|adnxs\.com|moatads\.com|spotxchange\.com|springserve\.com|innovid\.com|teads\.tv|2mdn\.net)/i;

const MIN_PROGRESSIVE = 400 * 1024;  // smaller video/audio files are previews, sprites or UI sounds

export function header(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value : "";
}

export function totalSize(headers) {
  const range = header(headers, "content-range");            // "bytes 0-1023/987654"
  const m = range.match(/\/(\d+)\s*$/);
  if (m) return Number(m[1]);
  const len = Number(header(headers, "content-length"));
  return Number.isFinite(len) && len > 0 ? len : null;
}

function extOf(url) {
  try {
    const path = new URL(url).pathname.toLowerCase();
    const last = path.split("/").pop();
    return last.includes(".") ? last.split(".").pop() : "";
  } catch { return ""; }
}

/**
 * Look at one finished request and say whether it's media worth offering.
 * Returns null, or {kind: "HLS"|"DASH"|"MP4"|..., manifest: bool, size, audio: bool}.
 */
export function classify({ url, type, statusCode, responseHeaders }) {
  if (!/^https?:/i.test(url) || (statusCode && statusCode >= 400)) return null;
  let host = "";
  try { host = new URL(url).hostname; } catch { return null; }
  if (NATIVE_HOSTS.test(host) || AD_HOSTS.test(host)) return null;
  const ctype = header(responseHeaders, "content-type").toLowerCase();
  const ext = extOf(url);
  const size = totalSize(responseHeaders);

  for (const [re, kind] of MANIFEST_TYPES) if (re.test(ctype)) return { kind, manifest: true, size, audio: false };
  if (MANIFEST_EXT[ext] && !/text\/html/.test(ctype)) return { kind: MANIFEST_EXT[ext], manifest: true, size, audio: false };
  if (/\/manifest(\(format=[^)]*\))?$/i.test(new URL(url).pathname) && /\.ism/i.test(url)) return { kind: "Smooth", manifest: true, size, audio: false };

  if (SEGMENT_RE.test(url)) return null;
  const isMediaType = /^(video|audio)\//.test(ctype) && !/mp2t/.test(ctype);
  const kind = MEDIA_EXT[ext] || (isMediaType ? ctype.split("/")[1].split(";")[0].toUpperCase().replace("MPEG", "MP3") : null);
  if (!kind || (!isMediaType && !MEDIA_EXT[ext])) return null;
  if (ctype && !isMediaType && !/octet-stream|binary/.test(ctype)) return null;   // e.g. an .mp4 URL that returned HTML
  if (size !== null && size < MIN_PROGRESSIVE) return null;
  // <video src> requests are type "media"; players that fetch() whole files are fine too if big enough
  if (type !== "media" && (size === null || size < 2 * 1024 * 1024)) return null;
  const audio = ctype.startsWith("audio/") || (!ctype.startsWith("video/") && AUDIO_EXT.has(ext));
  return { kind, manifest: false, size, audio };
}

/** Parse an HLS playlist: master (variants with resolutions) or media (duration). */
export function parseHls(text, baseUrl) {
  if (!text || !text.trimStart().startsWith("#EXTM3U")) return null;
  const lines = text.split(/\r?\n/);
  const variants = [], renditions = [];
  let duration = 0, segments = 0, encrypted = false, ended = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (line.startsWith("#EXT-X-STREAM-INF")) {
      const res = /RESOLUTION=(\d+)x(\d+)/i.exec(line);
      const bw = /BANDWIDTH=(\d+)/i.exec(line);
      let j = i + 1;
      while (j < lines.length && (!lines[j].trim() || lines[j].startsWith("#"))) j++;
      if (j < lines.length) {
        variants.push({ url: new URL(lines[j].trim(), baseUrl).href, height: res ? Number(res[2]) : null,
                        bandwidth: bw ? Number(bw[1]) : null });
      }
    } else if (/^#EXT-X-(MEDIA|I-FRAME-STREAM-INF):/i.test(line)) {
      const uri = /URI="([^"]+)"/i.exec(line);  // separate audio track, subtitles or trick-play: part of this stream
      if (uri) renditions.push(new URL(uri[1], baseUrl).href);
    } else if (/^#EXT-X-(ENDLIST|PLAYLIST-TYPE:VOD)/i.test(line)) {
      ended = true;
    } else if (line.startsWith("#EXTINF")) {
      duration += parseFloat(line.slice(8)) || 0;
      segments++;
    } else if (/^#EXT-X-KEY:.*METHOD=(SAMPLE-AES|SAMPLE-AES-CTR)/i.test(line) || /KEYFORMAT="com\.(apple\.streamingkeydelivery|widevine|microsoft\.playready)/i.test(line)) {
      encrypted = true;  // DRM (FairPlay/Widevine/PlayReady): can't be downloaded
    }
  }
  if (variants.length) {
    const heights = variants.map((v) => v.height).filter(Boolean);
    return { master: true, variants, renditions, maxHeight: heights.length ? Math.max(...heights) : null, drm: encrypted };
  }
  return { master: false, duration: segments ? duration : null, segments, drm: encrypted, live: segments > 0 && !ended };
}

/** Is this DASH manifest DRM-protected? */
export function dashHasDrm(text) {
  return /<ContentProtection[^>]+(edef8ba9|9a04f079|94ce86fb|urn:uuid)/i.test(text || "");
}

/** Is this DASH manifest a live stream (it never ends)? */
export function dashIsLive(text) {
  return /<MPD[^>]*\stype=["']dynamic["']/i.test(text || "");
}

// ---------------------------------------------------------------- auto-download

/** Did this request show the page is playing a video? (Even pieces count: YouTube, Instagram etc. only load pieces.) */
export function loadsVideo({ url, type, statusCode, responseHeaders }) {
  if (statusCode && statusCode >= 400) return false;
  let u;
  try { u = new URL(url); } catch { return false; }
  if (!/^https?:$/.test(u.protocol) || AD_HOSTS.test(u.hostname)) return false;
  if (/(^|\.)googlevideo\.com$/i.test(u.hostname) && u.pathname.startsWith("/videoplayback")) return true;
  const ctype = header(responseHeaders, "content-type").toLowerCase();
  if (ctype.startsWith("video/") || /mpegurl|dash\+xml/.test(ctype)) return true;
  return type === "media" && !ctype.startsWith("audio/");
}

// Pages that are one video on a site Grab knows: downloading the page link beats the raw stream
// (best quality, audio merged, real title). Anything else (feeds, profiles, channels) is not one video.
const VIDEO_PATHS = [
  ["Instagram", /^instagram\.com$/, /^\/([\w.]+\/)?(p|reels?|tv)\/[\w-]+/],
  ["TikTok", /^tiktok\.com$/, /^\/@[^/]+\/video\/\d+/],
  ["X", /^(x|twitter)\.com$/, /^\/\w+\/status\/\d+/],
  ["Reddit", /^reddit\.com$/, /^\/r\/\w+\/comments\/\w+/],
  ["Threads", /^threads\.(net|com)$/, /^\/@[\w.]+\/post\/[\w-]+/],
  ["Facebook", /^facebook\.com$/, /^\/(reel\/\d+|[\w.]+\/videos\/[\w.-]+)/],
  ["Vimeo", /^vimeo\.com$/, /^\/\d+/],
  ["Dailymotion", /^dailymotion\.com$/, /^\/video\/\w+/],
  ["Twitch", /^twitch\.tv$/, /^\/(videos\/\d+|\w+\/clip\/[\w-]+)/],
  ["Twitch", /^clips\.twitch\.tv$/, /^\/[\w-]+/],
];

/** The site's name and a clean link to the one video, if this page is a single video (else null). */
export function videoPage(url) {
  let u;
  try { u = new URL(url); } catch { return null; }
  if (!/^https?:$/.test(u.protocol)) return null;
  const host = siteOf(url);
  if (host === "youtube.com") {  // never the playlist (&list=) or a start time: just the video
    const v = u.pathname === "/watch" && u.searchParams.get("v");
    if (v && /^[\w-]{11}$/.test(v)) return { site: "YouTube", url: `https://www.youtube.com/watch?v=${v}` };
    const s = /^\/shorts\/([\w-]{11})/.exec(u.pathname);
    return s ? { site: "YouTube", url: `https://www.youtube.com/shorts/${s[1]}` } : null;
  }
  if (host === "youtu.be") {
    const v = /^\/([\w-]{11})/.exec(u.pathname);
    return v ? { site: "YouTube", url: `https://www.youtube.com/watch?v=${v[1]}` } : null;
  }
  if (host === "facebook.com" && /^\/watch\/?$/.test(u.pathname)) {
    const v = u.searchParams.get("v");
    return v && /^\d+$/.test(v) ? { site: "Facebook", url: `https://www.facebook.com/watch/?v=${v}` } : null;
  }
  for (const [site, hosts, path] of VIDEO_PATHS) {
    const m = hosts.test(host) && path.exec(u.pathname);
    if (m) return { site, url: u.origin + m[0] };
  }
  return null;
}

/** Host name as the user thinks of the site: "www.x.com" and "m.youtube.com" are "x.com" and "youtube.com". */
export function siteOf(url) {
  try { return new URL(url).hostname.toLowerCase().replace(/^(www|m|mobile|old|new)\./, ""); } catch { return ""; }
}

/** Is auto-download on for this page? settings: {all, sites: ["x.com", …]} (a site covers its subdomains). */
export function autoOn(page, { all = false, sites = [] } = {}) {
  const host = siteOf(page);
  if (!host || !/^https?:/i.test(page)) return false;
  return all || sites.some((s) => host === s || host.endsWith("." + s));
}

export const MIN_AUTO_SECONDS = 4;

/** Should a stream the page loaded be auto-downloaded? Not DRM, live, audio-only, tiny, or a part of another stream. */
export function autoWorthy(item) {
  if (item.hidden || item.drm || item.audio || item.live) return false;
  if (item.manifest && !item.inspected) return false;  // wait until we know it isn't DRM or live
  return !(item.duration != null && item.duration < MIN_AUTO_SECONDS);
}

// query parameters that change on every load (signatures, expiry times, tracking) and so say nothing about which video it is
const VOLATILE = /^(utm_[\w-]+|si|feature|pp|t|start|list|index|igsh|igshid|ref|ref_src|ref_url|s|fbclid|gclid|token|sig|signature|expires?|exp|e|hdnts|hdntl|hdnea|policy|key-pair-id|st|md5|hash|auth[\w-]*|x-amz-[\w-]+|_|ts|time|nonce|validfrom|validto|acl|hmac|cb|rnd|rand)$/i;

/** Same video, same key: used so each video is auto-downloaded only once, even when its link is re-signed. */
export function autoKey(url) {
  let u;
  try { u = new URL(url); } catch { return url; }
  const keep = [...u.searchParams].filter(([k]) => !VOLATILE.test(k)).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return siteOf(url) + u.pathname.replace(/\/+$/, "") + (keep.length ? "?" + new URLSearchParams(keep) : "");
}

export function label(item) {
  const parts = [item.kind];
  if (item.maxHeight) parts.push(`up to ${item.maxHeight}p`);
  if (item.duration) {
    const s = Math.round(item.duration), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    parts.push(h ? `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${m}:${String(s % 60).padStart(2, "0")}`);
  }
  if (item.size && !item.manifest) parts.push(item.size >= 1 << 30 ? `${(item.size / (1 << 30)).toFixed(1)} GB` : `${(item.size / (1 << 20)).toFixed(item.size < 10 << 20 ? 1 : 0)} MB`);
  return parts.join(" · ");
}

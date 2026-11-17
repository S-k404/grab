// "Everything on this page": every picture and video file a page shows or links to.
// scanPage() runs inside the page itself (chrome.scripting copies its source there), so it must not use
// anything from outside its own body. pickItems() is the popup's filter; both run under Node for the tests.

export function scanPage() {
  const IMG = /\.(jpe?g|png|gif|webp|avif|bmp|tiff?|heic)(\?|#|$)/i;
  const VID = /\.(mp4|m4v|webm|mov|mkv|ogv)(\?|#|$)/i;
  const found = new Map();
  const abs = (u) => {
    try {
      const x = new URL(String(u).trim(), document.baseURI);
      return /^https?:$/.test(x.protocol) && !/\.svg(\?|#|$)/i.test(x.pathname) ? x.href : null;  // SVGs are icons and logos
    } catch { return null; }
  };
  const add = (raw, kind, w, h, name) => {
    const url = raw && abs(raw);
    if (!url) return;
    const old = found.get(url);
    if (old) {
      old.w = Math.max(old.w, w || 0);
      old.h = Math.max(old.h, h || 0);
      return;
    }
    found.set(url, { url, kind, w: w || 0, h: h || 0, name: String(name || "").replace(/\s+/g, " ").trim().slice(0, 100) });
  };
  // the biggest picture a srcset offers ("a.jpg 320w, b.jpg 1280w"), and its width when it says
  const largest = (srcset) => {
    let best = null;
    for (const part of String(srcset || "").split(/,\s+/)) {
      const [u, d = "1x"] = part.trim().split(/\s+/);
      const n = parseFloat(d) || 1;
      if (u && (!best || n > best.n)) best = { u, n, w: d.endsWith("w") ? n : 0 };
    }
    return best;
  };

  const roots = [document];
  const allEls = [];
  for (let ri = 0; ri < roots.length; ri++) {
    for (const el of roots[ri].querySelectorAll("*")) {
      allEls.push(el);
      if (el.shadowRoot && roots.length < 200) roots.push(el.shadowRoot);
      if (allEls.length >= 4000) break;
    }
    if (allEls.length >= 4000) break;
  }
  const qAll = (sel) => roots.flatMap((r) => [...r.querySelectorAll(sel)]);

  for (const img of qAll("img")) {
    const name = img.alt || img.title;
    const link = img.closest("a[href]");
    if (link && IMG.test(link.href)) {  // a thumbnail linking to the full-size picture: take the full one
      add(link.href, "image", 0, 0, name);
      continue;
    }
    const big = largest(img.getAttribute("srcset")) || largest(img.getAttribute("data-srcset"));
    const lazy = img.getAttribute("data-src") || img.getAttribute("data-original") || img.getAttribute("data-lazy-src");
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (big && big.w) add(big.u, "image", big.w, nw ? Math.round(big.w * nh / nw) : 0, name);
    else if (big) add(big.u, "image", nw * big.n, nh * big.n, name);
    else if (lazy && (!img.currentSrc || img.currentSrc.startsWith("data:") || nw < 50)) add(lazy, "image", 0, 0, name);
    else add(img.currentSrc || img.src, "image", nw, nh, name);
  }
  for (const s of qAll("picture source[srcset]")) {
    const big = largest(s.getAttribute("srcset"));
    if (big) add(big.u, "image", big.w, 0);
  }
  for (const v of qAll("video")) {
    const urls = [
      v.currentSrc, v.getAttribute("src"), v.getAttribute("data-src"), v.getAttribute("data-hls"),
      v.getAttribute("data-hls-url"), v.getAttribute("data-mp4"), v.getAttribute("data-video"),
      v.getAttribute("data-stream"), v.getAttribute("data-fallback-url"),
      ...[...v.querySelectorAll("source")].map((s) => s.getAttribute("src") || s.getAttribute("data-src")),
    ];
    for (const u of urls) if (u && !u.startsWith("blob:")) add(u, "video", v.videoWidth, v.videoHeight);
  }
  for (const a of qAll("audio")) {
    const urls = [a.currentSrc, a.getAttribute("src"), a.getAttribute("data-src"),
                  ...[...a.querySelectorAll("source")].map((s) => s.getAttribute("src") || s.getAttribute("data-src"))];
    for (const u of urls) if (u && !u.startsWith("blob:")) add(u, "audio", 0, 0, a.title);
  }
  for (const a of qAll("a[href]")) {
    if (VID.test(a.href)) add(a.href, "video", 0, 0, a.textContent || a.title);
    else if (IMG.test(a.href)) add(a.href, "image", 0, 0, a.textContent || a.title);
  }
  for (const m of document.querySelectorAll('meta[property^="og:image"], meta[property^="og:video"], meta[name="twitter:image"]')) {
    const prop = m.getAttribute("property") || "";
    if (/^og:(image|video)(:url|:secure_url)?$/.test(prop) || m.getAttribute("name")) add(m.content, prop.startsWith("og:video") ? "video" : "image");
  }
  // pictures set as CSS backgrounds (galleries and hero images often are)
  for (const el of allEls) {
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg !== "none") for (const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) add(m[1], "image", el.clientWidth, el.clientHeight);
  }
  return [...found.values()];
}

/** The popup's filter: pictures, videos or both, and a minimum size (things whose size isn't known always pass). */
export function pickItems(items, { kind = "all", min = 0 } = {}) {
  return items.filter((i) => (kind === "all" || i.kind === kind) && (!min || !(i.w || i.h) || Math.max(i.w, i.h) >= min));
}

/** Lightweight self-contained DOM probe for unplayed/paused streams on the page. */
export function scanStreams() {
  const out = new Map();
  const abs = (u) => {
    try {
      const x = new URL(String(u).trim(), document.baseURI);
      return /^https?:$/.test(x.protocol) ? x.href : null;
    } catch { return null; }
  };
  const kindOf = (u, fallback = "MP4") => (/\.m3u8(\?|#|$)/i.test(u) ? "HLS" : /\.mpd(\?|#|$)/i.test(u) ? "DASH" : fallback);
  const add = (raw, kind, labelText) => {
    if (!raw || /^(blob|data):/i.test(raw)) return;
    const url = abs(raw);
    if (!url || out.has(url)) return;
    out.set(url, { url, kind: kind || kindOf(url), label: labelText || `${kind || kindOf(url)} · on page` });
  };
  const roots = [document];
  let visited = 0;
  for (let ri = 0; ri < roots.length && visited < 4000; ri++) {
    for (const el of roots[ri].querySelectorAll("*")) {
      visited++;
      if (el.shadowRoot && roots.length < 200) roots.push(el.shadowRoot);
      if (visited >= 4000) break;
    }
  }
  const qAll = (sel) => roots.flatMap((r) => [...r.querySelectorAll(sel)]);

  for (const v of qAll("video, audio")) {
    const fb = v.tagName === "AUDIO" ? "Audio" : "MP4";
    for (const attr of ["currentSrc", "src", "data-src", "data-hls", "data-hls-url", "data-mp4", "data-video", "data-stream", "data-fallback-url"]) {
      const val = attr === "currentSrc" ? v.currentSrc : v.getAttribute(attr);
      if (val) add(val, kindOf(val, fb));
    }
    for (const s of v.querySelectorAll("source")) {
      const val = s.getAttribute("src") || s.getAttribute("data-src");
      if (val) add(val, kindOf(val, fb));
    }
  }
  for (const m of document.querySelectorAll('meta[property^="og:video"], meta[property^="og:audio"], meta[name="twitter:player:stream"]')) {
    const val = m.getAttribute("content");
    if (val) add(val, kindOf(val, m.getAttribute("property")?.includes("audio") ? "Audio" : "MP4"));
  }
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const stack = [JSON.parse(s.textContent || "{}")];
      while (stack.length) {
        const n = stack.pop();
        if (Array.isArray(n)) stack.push(...n);
        else if (n && typeof n === "object") {
          if (typeof n.contentUrl === "string") add(n.contentUrl, kindOf(n.contentUrl, "Video"));
          for (const v of Object.values(n)) if (v && typeof v === "object") stack.push(v);
        }
      }
    } catch { /* ignore invalid json-ld */ }
  }
  for (const s of qAll("script:not([src])")) {
    const txt = (s.textContent || "").replace(/\\\//g, "/").replace(/\\u0026/gi, "&");
    for (const m of txt.matchAll(/(?:https?:)?\/\/[^"'\s<>\\]+?\.(?:m3u8|mpd)(?:\?[^"'\s<>\\]*)?/gi)) {
      add(m[0], kindOf(m[0]), `${kindOf(m[0])} · in script`);
    }
    for (const m of txt.matchAll(/["'](\/[^"'\s<>\\]+?\.(?:m3u8|mpd)(?:\?[^"'\s<>\\]*)?)["']/gi)) {
      add(m[1], kindOf(m[1]), `${kindOf(m[1])} · in script`);
    }
    for (const m of txt.matchAll(/(?:hls|dash|stream|playlist|mediaUrl|media_url|videoUrl|video_url|mp4Url|mp4_url|src|file)\s*[:=]\s*["']([^"']+\.(?:m3u8|mpd|mp4|webm|m4v|mov|mp3|m4a)(?:\?[^"']*)?)["']/gi)) {
      add(m[1], kindOf(m[1]), `${kindOf(m[1])} · in script`);
    }
  }
  return [...out.values()];
}

/** Results from every frame of the page in one list (the same file once, with the biggest size seen). */
export function mergeScans(lists) {
  const out = new Map();
  for (const i of lists.flat()) {
    const old = out.get(i.url);
    if (!old) out.set(i.url, { ...i });
    else Object.assign(old, { w: Math.max(old.w, i.w), h: Math.max(old.h, i.h), name: old.name || i.name });
  }
  return [...out.values()];
}

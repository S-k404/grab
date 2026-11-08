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

  for (const img of document.querySelectorAll("img")) {
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
  for (const s of document.querySelectorAll("picture source[srcset]")) {
    const big = largest(s.getAttribute("srcset"));
    if (big) add(big.u, "image", big.w, 0);
  }
  for (const v of document.querySelectorAll("video")) {
    const urls = [v.currentSrc, v.getAttribute("src"), ...[...v.querySelectorAll("source")].map((s) => s.getAttribute("src"))];
    for (const u of urls) if (u && !u.startsWith("blob:")) add(u, "video", v.videoWidth, v.videoHeight);
  }
  for (const a of document.querySelectorAll("a[href]")) {
    if (VID.test(a.href)) add(a.href, "video", 0, 0, a.textContent || a.title);
    else if (IMG.test(a.href)) add(a.href, "image", 0, 0, a.textContent || a.title);
  }
  for (const m of document.querySelectorAll('meta[property^="og:image"], meta[property^="og:video"], meta[name="twitter:image"]')) {
    const prop = m.getAttribute("property") || "";
    if (/^og:(image|video)(:url|:secure_url)?$/.test(prop) || m.getAttribute("name")) add(m.content, prop.startsWith("og:video") ? "video" : "image");
  }
  // pictures set as CSS backgrounds (galleries and hero images often are)
  let n = 0;
  for (const el of document.querySelectorAll("body *")) {
    if (++n > 4000) break;
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg !== "none") for (const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) add(m[1], "image", el.clientWidth, el.clientHeight);
  }
  return [...found.values()];
}

/** The popup's filter: pictures, videos or both, and a minimum size (things whose size isn't known always pass). */
export function pickItems(items, { kind = "all", min = 0 } = {}) {
  return items.filter((i) => (kind === "all" || i.kind === kind) && (!min || !(i.w || i.h) || Math.max(i.w, i.h) >= min));
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

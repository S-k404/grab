// Grab Stream Catcher - background service worker.
// Watches each tab's network traffic for media (detect.js decides what counts), remembers what it
// found per tab, shows a count on the toolbar icon, and sends a chosen stream to Grab - or, on sites
// where you turned auto-download on, every video the page plays.

import { classify, parseHls, dashHasDrm, dashIsLive, label, loadsVideo, videoPage, siteOf, autoOn, autoWorthy, autoKey } from "./detect.js";
import { scanPage, mergeScans } from "./scan.js";

const GRAB_PORTS = [8765, 8766, 8767, 8768, 8769, 8770];
const MAX_ITEMS = 40;

// tabId -> {page, title, played, autoSent, items: [{url, kind, manifest, size, audio, maxHeight, duration, drm, live, hidden, inspected, time}]}
// Kept in memory and mirrored to session storage (the worker can be stopped and restarted any time).
const tabs = new Map();
const ready = chrome.storage.session.get(null).then((all) => {
  for (const [k, v] of Object.entries(all)) if (k.startsWith("tab:")) tabs.set(Number(k.slice(4)), v);
});
const saveTimers = new Map();

function save(tabId) {
  clearTimeout(saveTimers.get(tabId));
  saveTimers.set(tabId, setTimeout(() => {
    const t = tabs.get(tabId);
    t ? chrome.storage.session.set({ [`tab:${tabId}`]: t }) : chrome.storage.session.remove(`tab:${tabId}`);
  }, 300));
  badge(tabId);
}

function visible(t) {
  return (t?.items || []).filter((i) => !i.hidden);
}

function badge(tabId) {
  const n = visible(tabs.get(tabId)).filter((i) => !i.drm).length;
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#3b6ff5" }).catch(() => {});
}

function tabState(tabId) {
  if (!tabs.has(tabId)) tabs.set(tabId, { page: "", title: "", items: [] });
  return tabs.get(tabId);
}

// ---------------------------------------------------------------- detection

chrome.webRequest.onResponseStarted.addListener(
  (d) => { onResponse(d).catch(() => {}); },
  { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object", "sub_frame"] },
  ["responseHeaders"],
);

async function onResponse(d) {
  if (d.tabId < 0) return;
  const hit = classify(d);
  const playing = loadsVideo(d);
  if (!hit && !playing) return;
  await ready;
  const t = tabState(d.tabId);
  if (playing && !t.played) {  // the page is playing a video: on a video page that's what auto-download waits for
    t.played = true;
    save(d.tabId);
    scheduleAuto(d.tabId);
  }
  if (!hit) return;
  if (t.items.some((i) => i.url === d.url)) return;
  if (t.items.some((i) => i.variants?.includes(d.url))) return;  // a quality level of a stream we already have
  const item = { url: d.url, kind: hit.kind, manifest: hit.manifest, size: hit.size, audio: hit.audio, time: Date.now(), hidden: false };
  t.items.push(item);
  if (t.items.length > MAX_ITEMS) t.items.splice(0, t.items.length - MAX_ITEMS);
  save(d.tabId);
  if (hit.manifest) await inspectManifest(d.tabId, item);
  scheduleAuto(d.tabId);
}

// Read HLS/DASH manifests: best resolution, duration, DRM, live, and hide the per-quality sub-playlists.
async function inspectManifest(tabId, item) {
  try {
    await readManifest(tabId, item);
  } finally {
    item.inspected = true;  // even if it couldn't be read: Grab sends the page as Referer and may manage
    save(tabId);
  }
}

async function readManifest(tabId, item) {
  let text = "";
  try {
    const r = await fetch(item.url, { credentials: "include", cache: "force-cache", signal: AbortSignal.timeout(8000) });
    if (!r.ok) return;
    text = (await r.text()).slice(0, 2_000_000);
  } catch { return; }  // some servers insist on the page as Referer; the download in Grab sends it
  const t = tabs.get(tabId);
  if (!t) return;
  if (item.kind === "HLS") {
    const p = parseHls(text, item.url);
    if (!p) return;
    item.drm = p.drm;
    if (p.master) {
      item.maxHeight = p.maxHeight;
      // its quality levels, plus its separate audio/subtitle tracks: all part of this one stream
      item.variants = [...p.variants.map((v) => v.url), ...p.renditions];
      for (const other of t.items) if (item.variants.includes(other.url)) other.hidden = true;
      // length of the video: read one quality level's playlist
      try {
        const r = await fetch(item.variants[0], { credentials: "include", cache: "force-cache", signal: AbortSignal.timeout(8000) });
        const v = r.ok ? parseHls(await r.text(), item.variants[0]) : null;
        if (v && !v.master) { item.duration = v.duration; item.drm = item.drm || v.drm; item.live = v.live; }
      } catch { /* fine without */ }
    } else {
      item.duration = p.duration;
      item.live = p.live;
      // a media playlist that belongs to a master we already listed
      if (t.items.some((o) => o.variants?.includes(item.url))) item.hidden = true;
    }
  } else if (item.kind === "DASH") {
    item.drm = dashHasDrm(text);
    item.live = dashIsLive(text);
  }
}

// New page in the tab: forget what the old page played.
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  await ready;
  if (info.url) {
    const t = tabState(tabId);
    if (t.page && t.page !== info.url) Object.assign(t, { items: [], played: false, autoSent: 0 });
    t.page = info.url;
    save(tabId);
  }
  if (info.title || tab.title) tabState(tabId).title = tab.title || info.title;
});
chrome.tabs.onRemoved.addListener((tabId) => { tabs.delete(tabId); chrome.storage.session.remove(`tab:${tabId}`); });

// ---------------------------------------------------------------- sharing logins with Grab
// Only these sites' cookies are ever read, and only sent to Grab on this Mac. That lets Grab download
// things that need you logged in (and gets past YouTube's bot check) without access to Brave's files.
const SHARE_SITES = {
  YouTube: ["youtube.com"], Instagram: ["instagram.com"], Reddit: ["reddit.com"], X: ["x.com", "twitter.com"],
  TikTok: ["tiktok.com"], Facebook: ["facebook.com"], Threads: ["threads.net", "threads.com"],
};
const LOGIN_COOKIES = {  // the cookie that shows you're logged in on each site
  YouTube: ["SID", "__Secure-3PSID", "LOGIN_INFO"], Instagram: ["sessionid"], Reddit: ["reddit_session", "token_v2"],
  X: ["auth_token"], TikTok: ["sessionid"], Facebook: ["c_user"], Threads: ["sessionid"],
};
let lastShare = 0;

async function sharingOn() {
  const { shareLogins } = await chrome.storage.local.get("shareLogins");
  return shareLogins !== false;
}

function browserName() {
  const b = (navigator.userAgentData?.brands || []).map((x) => x.brand).find((x) => !/chromium|not/i.test(x));
  return b || "Chromium browser";
}

async function shareLogins(port, force = false) {
  if (!port || !(await sharingOn())) return null;
  if (!force && Date.now() - lastShare < 10 * 60 * 1000) return null;
  lastShare = Date.now();
  const cookies = [], loggedIn = [];
  for (const [site, domains] of Object.entries(SHARE_SITES)) {
    const found = (await Promise.all(domains.map((domain) => chrome.cookies.getAll({ domain })))).flat();
    if (found.some((c) => LOGIN_COOKIES[site].includes(c.name))) loggedIn.push(site);
    cookies.push(...found.map((c) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path, secure: c.secure,
                                         httpOnly: c.httpOnly, hostOnly: c.hostOnly, expirationDate: c.expirationDate })));
  }
  try {
    const r = await fetch(`http://127.0.0.1:${port}/api/ext/cookies`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookies, sites: loggedIn, browser: browserName() }),
    });
    if (!r.ok) throw new Error(`Grab answered ${r.status}`);
  } catch (e) {
    lastShare = 0;  // try again next time
    await chrome.storage.local.set({ shareError: String(e.message || e) });
    return null;
  }
  await chrome.storage.local.set({ sharedSites: loggedIn, sharedAt: Date.now(), shareError: "" });
  return loggedIn;
}

async function stopSharing() {
  await chrome.storage.local.set({ shareLogins: false, sharedSites: [], sharedAt: 0 });
  const port = await findGrab(true);
  if (port) await fetch(`http://127.0.0.1:${port}/api/ext/cookies`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clear: true }) });
}

// Share by itself: when the browser starts, and (at most every 2 min) as pages load, whenever Grab is running.
// findGrab() also refreshes Grab's key and shares logins (that part at most every 10 min).
let lastAttempt = 0;
const bootTime = Date.now();
async function maybeConnect() {
  const sinceBoot = Date.now() - bootTime;
  if (sinceBoot < 5000) {  // let the browser finish starting (and settings load) first
    setTimeout(maybeConnect, 5000 - sinceBoot + 100);
    return;
  }
  if (Date.now() - lastAttempt < 2 * 60 * 1000) return;
  lastAttempt = Date.now();
  await findGrab().catch(() => {});
}
chrome.runtime.onStartup.addListener(() => { maybeConnect(); });
chrome.runtime.onInstalled.addListener(() => { maybeConnect(); });
chrome.tabs.onUpdated.addListener((_id, info) => { if (info.status === "complete") maybeConnect(); });

// logged in or out of one of those sites: tell Grab a few seconds later
let shareTimer;
chrome.cookies.onChanged.addListener(({ cookie }) => {
  const site = Object.keys(SHARE_SITES).find((s) => SHARE_SITES[s].some((d) => cookie.domain.replace(/^\./, "").endsWith(d)));
  if (!site || !LOGIN_COOKIES[site].includes(cookie.name)) return;
  clearTimeout(shareTimer);
  shareTimer = setTimeout(async () => shareLogins(await findGrab(true), true).catch(() => {}), 4000);
});

// ---------------------------------------------------------------- talking to Grab

async function findGrab(quiet = false) {
  const { ports } = await chrome.storage.local.get("ports");  // override (used by the automated tests)
  for (const port of ports?.length ? ports : GRAB_PORTS) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/ext/ping`, { signal: AbortSignal.timeout(700) });
      if (r.ok && (await r.json()).app === "grab") {
        // remember Grab's private key, so even the grab:// fallback (Grab closed) downloads right away
        if (!quiet) {
          fetch(`http://127.0.0.1:${port}/api/ext/hello`, { method: "POST" }).then((h) => h.json())
            .then((j) => j.key && chrome.storage.local.set({ grabKey: j.key })).catch(() => {});
          shareLogins(port).catch(() => {});  // keep Grab's copy of your logins fresh (at most every 10 min)
        }
        flushPending(port).catch(() => {});
        return port;
      }
    } catch { /* not this port */ }
  }
  return null;
}

async function addToGrab(port, { url, page, title, now, auto }) {
  const r = await fetch(`http://127.0.0.1:${port}/api/ext/add`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, page, title, now: !!now, auto: !!auto, user_agent: navigator.userAgent }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, error: j.error || `Grab said ${r.status}` };
  return j.duplicate ? { ok: true, via: "grab", duplicate: true } : { ok: true, via: "grab" };
}

async function send({ url, page, title, now }) {
  const port = await findGrab();
  if (port) {
    await shareLogins(port).catch(() => {});
    return addToGrab(port, { url, page, title, now });
  }
  // Grab isn't running: open it through its grab:// link (it starts and shows the link to confirm)
  const { grabKey } = await chrome.storage.local.get("grabKey");
  const q = new URLSearchParams({ url, ...(grabKey ? { key: grabKey } : {}) });
  if (page && page !== url) { q.set("referer", page); if (title) q.set("title", title); }
  const opener = await chrome.tabs.create({ url: `grab://add?${q}`, active: true });
  setTimeout(() => chrome.tabs.remove(opener.id).catch(() => {}), 4000);
  return { ok: true, via: "app-link" };
}

// ---------------------------------------------------------------- auto-download
// Off until you turn it on for a site (or every site) in the popup. On a page that is one video on a
// site Grab knows (a YouTube video, an Instagram reel, an X post…) the page link goes to Grab once the
// page plays something; elsewhere each stream the page loads goes (not DRM, live, audio-only or tiny).
// Each video goes once. While Grab isn't running they wait here, and go when it's back.

const AUTO_MAX_PER_PAGE = 15;   // a page that loads more than this is cycling previews, not showing videos
const AUTO_REMEMBER = 2000;     // videos remembered as already sent
const PENDING_MAX = 50, PENDING_HOURS = 12;  // older stream links have usually expired
const autoTimers = new Map();
let autoChain = Promise.resolve();  // one auto-send at a time, so two tabs can't send the same video

async function autoSettings() {
  const { auto } = await chrome.storage.local.get("auto");
  return { all: !!auto?.all, sites: auto?.sites || [] };
}

function scheduleAuto(tabId, delay = 2500) {  // wait for the page to settle (master playlist read, title set)
  clearTimeout(autoTimers.get(tabId));
  autoTimers.set(tabId, setTimeout(() => {
    autoTimers.delete(tabId);
    autoChain = autoChain.then(() => autoCheck(tabId)).catch(() => {});
  }, delay));
}

async function autoCheck(tabId) {
  await ready;
  const t = tabs.get(tabId);
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!t || !tab) return;
  t.page ||= tab.url;  // a tab that was already open when the extension started
  if (tab.url !== t.page || !autoOn(t.page, await autoSettings())) return;  // the tab has moved on, or auto is off here
  const vp = videoPage(t.page);
  const picks = vp
    ? (t.played ? [{ url: vp.url, page: vp.url, title: "" }] : [])   // Grab names it from the site itself
    : visible(t).filter(autoWorthy).map((i) => ({ url: i.url, page: t.page, title: tab.title || t.title || "" }));
  for (const pick of picks) {
    if ((t.autoSent || 0) >= AUTO_MAX_PER_PAGE) break;
    const r = await autoSend(pick);
    if (!r) continue;
    t.autoSent = (t.autoSent || 0) + 1;
    save(tabId);
    chrome.action.setBadgeText({ tabId, text: r === "waiting" ? "…" : "✓" }).catch(() => {});
    setTimeout(() => badge(tabId), 2500);
  }
}

/** Send one video unless it was sent before. Returns "sent", "waiting" (Grab not running) or null. */
async function autoSend(pick) {
  const key = autoKey(pick.url);
  const { autoSeen = {} } = await chrome.storage.local.get("autoSeen");
  if (autoSeen[key]) return null;
  autoSeen[key] = Date.now();
  const keys = Object.keys(autoSeen);
  if (keys.length > AUTO_REMEMBER) keys.sort((a, b) => autoSeen[a] - autoSeen[b]).slice(0, keys.length - AUTO_REMEMBER).forEach((k) => delete autoSeen[k]);
  await chrome.storage.local.set({ autoSeen });

  const port = await findGrab(true);
  if (!port) {
    const { autoPending = [] } = await chrome.storage.local.get("autoPending");
    autoPending.push({ ...pick, time: Date.now() });
    await chrome.storage.local.set({ autoPending: autoPending.slice(-PENDING_MAX) });
    return "waiting";
  }
  const r = await addToGrab(port, { ...pick, auto: true }).catch((e) => ({ ok: false, error: String(e) }));
  if (!r.ok) {  // try again next time the page plays it
    delete autoSeen[key];
    await chrome.storage.local.set({ autoSeen, autoError: r.error });
    return null;
  }
  await chrome.storage.local.set({ autoError: "" });
  return r.duplicate ? null : "sent";
}

let flushing = false;
async function flushPending(port) {
  if (flushing) return;
  flushing = true;
  try {
    const { autoPending = [] } = await chrome.storage.local.get("autoPending");
    if (!autoPending.length) return;
    await chrome.storage.local.set({ autoPending: [] });
    const fresh = autoPending.filter((p) => Date.now() - p.time < PENDING_HOURS * 3600 * 1000);
    for (const p of fresh) await addToGrab(port, { ...p, auto: true }).catch(() => {});
  } finally {
    flushing = false;
  }
}

async function autoStatus(tabId, page) {
  const settings = await autoSettings();
  const { autoPending = [], autoError = "" } = await chrome.storage.local.get(["autoPending", "autoError"]);
  const vp = videoPage(page);
  return { ...settings, site: siteOf(page), on: autoOn(page, settings), sentHere: tabs.get(tabId)?.autoSent || 0,
           waiting: autoPending.length, error: autoError, videoPage: vp?.site || "" };
}

async function setAuto({ tabId, page, site, all }) {
  const s = await autoSettings();
  if (all !== undefined) s.all = !!all;
  if (site !== undefined) {
    const host = siteOf(page);
    s.sites = s.sites.filter((x) => x !== host);
    if (site && host) s.sites.push(host);
  }
  await chrome.storage.local.set({ auto: s });
  // turning it on also catches the video this page already played
  if (tabId != null && autoOn(page, s)) scheduleAuto(tabId, 0);
  return autoStatus(tabId, page);
}

// ---------------------------------------------------------------- everything on this page

async function scanTab(tabId) {
  let frames;
  try {
    frames = await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, func: scanPage });
  } catch {
    return { items: [], error: "This page can't be looked into (browser pages and the extension store are off-limits)." };
  }
  const items = mergeScans(frames.map((f) => f.result || []));
  // plain video files the page played that aren't in its HTML (players that fetch() them)
  for (const s of visible(tabs.get(tabId))) {
    if (!s.manifest && !s.audio && !s.drm && !items.some((i) => i.url === s.url)) items.push({ url: s.url, kind: "video", w: 0, h: 0, name: "" });
  }
  return { items };
}

async function sendMany({ page, title, items }) {
  const port = await findGrab();
  if (!port) return { ok: false, error: "Grab isn't running. Open it, then try again." };
  await shareLogins(port).catch(() => {});
  const r = await fetch(`http://127.0.0.1:${port}/api/ext/add_many`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ page, title, items: items.map(({ url, kind }) => ({ url, kind })), user_agent: navigator.userAgent }),
  });
  const j = await r.json().catch(() => ({}));
  return r.ok ? { ok: true, added: j.added } : { ok: false, error: j.error || `Grab said ${r.status}` };
}

// ---------------------------------------------------------------- popup + right-click menu

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    await ready;
    if (msg.type === "list") {
      const t = tabs.get(msg.tabId);
      const items = visible(t).map((i) => ({ ...i, label: label(i) }))
        .sort((a, b) => (b.manifest - a.manifest) || ((b.maxHeight || 0) - (a.maxHeight || 0)) || ((b.size || 0) - (a.size || 0)));
      return { items };
    }
    if (msg.type === "status") {
      const port = await findGrab();
      let site = null;
      if (port && msg.page?.startsWith("http")) {
        try {
          const r = await fetch(`http://127.0.0.1:${port}/api/ext/check?url=${encodeURIComponent(msg.page)}`, { signal: AbortSignal.timeout(4000) });
          site = (await r.json()).site;
        } catch { /* ignore */ }
      }
      return { running: !!port, site };
    }
    if (msg.type === "send") return send(msg);
    if (msg.type === "sharing") {
      const { sharedSites = [], sharedAt = 0, shareError = "" } = await chrome.storage.local.get(["sharedSites", "sharedAt", "shareError"]);
      return { on: await sharingOn(), sites: sharedSites, at: sharedAt, error: shareError, all: Object.keys(SHARE_SITES) };
    }
    if (msg.type === "setSharing") {
      if (msg.on) {
        await chrome.storage.local.set({ shareLogins: true });
        return { sites: await shareLogins(await findGrab(true), true) };
      }
      await stopSharing();
      return { sites: [] };
    }
    if (msg.type === "clear") { tabs.set(msg.tabId, { ...tabState(msg.tabId), items: [] }); save(msg.tabId); return {}; }
    if (msg.type === "auto") return autoStatus(msg.tabId, msg.page);
    if (msg.type === "scan") return scanTab(msg.tabId);
    if (msg.type === "sendMany") return sendMany(msg);
    if (msg.type === "setAuto") return setAuto(msg);
    return {};
  })().then(reply, (e) => reply({ ok: false, error: String(e) }));
  return true;  // async reply
});

// handles for the end-to-end test (tests/test_extension_e2e.py), harmless otherwise
globalThis.grabCatcher = { tabs, send, findGrab, shareLogins, stopSharing, maybeConnect, setAuto, autoStatus, flushPending, scanTab, sendMany,
                           resetThrottle: () => { lastAttempt = 0; lastShare = 0; } };

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "grab-link", title: "Download link with Grab", contexts: ["link", "image"] });
    chrome.contextMenus.create({ id: "grab-media", title: "Download this video with Grab", contexts: ["video", "audio"] });
    chrome.contextMenus.create({ id: "grab-page", title: "Download this page with Grab", contexts: ["page"] });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  await ready;
  const page = info.pageUrl || tab?.url || "";
  let url = info.menuItemId === "grab-page" ? page : (info.srcUrl || info.linkUrl || page);
  if (url.startsWith("blob:") || url.startsWith("data:")) {
    // the player feeds the video in pieces (no real URL): use the best stream we saw, or the page itself
    const best = visible(tabs.get(tab.id)).filter((i) => !i.drm)
      .sort((a, b) => (b.manifest - a.manifest) || ((b.maxHeight || 0) - (a.maxHeight || 0)))[0];
    url = best ? best.url : page;
  }
  const r = await send({ url, page, title: tab?.title || "" });
  chrome.action.setBadgeText({ tabId: tab.id, text: r.ok ? "✓" : "!" }).catch(() => {});
  setTimeout(() => badge(tab.id), 2500);
});

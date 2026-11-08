import { videoPage } from "./detect.js";
import { pickItems } from "./scan.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ask = (msg) => chrome.runtime.sendMessage(msg);

// popup.html?tab=<id> opens it for a specific tab (handy for debugging and the automated test)
const forced = Number(new URLSearchParams(location.search).get("tab"));
const [tab] = forced ? [await chrome.tabs.get(forced)] : await chrome.tabs.query({ active: true, currentWindow: true });
const page = tab?.url || "";
const host = (() => { try { return new URL(page).hostname.replace(/^www\./, ""); } catch { return ""; } })();
$("#ptitle").textContent = tab?.title || page;
$("#psub").textContent = host;
// One video on a site Grab knows (a YouTube video, an X post…): Grab gets it from the page link, so its
// streams are never listed. The popup shows the video itself instead of "nothing found".
const vp = videoPage(page);
let knownSite = "";  // set when Grab says it supports this site

async function sendIt(btn, payload) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "Sending…";
  const r = await ask({ type: "send", page, title: tab?.title || "", ...payload });
  if (r?.ok) {
    btn.textContent = r.via === "grab" ? "Added ✓" : "Opening Grab…";
    btn.classList.add("done");
  } else {
    btn.textContent = "Failed";
    btn.classList.add("fail");
    btn.title = r?.error || "";
    setTimeout(() => { btn.textContent = old; btn.classList.remove("fail"); btn.disabled = false; }, 2500);
  }
}

$("#sendPage").onclick = (e) => sendIt(e.target, { url: page });
$("#sendPage").disabled = !page.startsWith("http");

// connection + "does Grab know this site?"
ask({ type: "status", page }).then((s) => {
  setTimeout(() => showSharing(), 1200);  // connecting just shared the logins: show the result
  const c = $("#conn");
  c.textContent = s.running ? "● connected" : "○ Grab not running";
  c.className = "conn " + (s.running ? "ok" : "off");
  c.title = s.running ? "" : "Grab will be opened when you download something";
  if (s.site) {
    $("#psub").innerHTML = `${esc(host)} · <span class="good">Grab supports ${esc(s.site)}: best option, all qualities</span>`;
    knownSite = s.site;
    if (!vp) render();  // a video page doesn't need it (and re-drawing would reset its Download button)
  }
});

async function render() {
  const { items } = await ask({ type: "list", tabId: tab.id });
  $("#clear").hidden = !items.length;
  $("#listHead").textContent = vp && !items.length ? "Video on this page" : "Streams found on this page";
  if (!items.length && vp) {
    $("#list").innerHTML = `
      <div class="item">
        <div class="k">${esc(vp.site)} video · best quality</div>
        <div class="u">Found. Grab downloads it from the page link, with sound and the real title.</div>
        <div class="acts"><button class="go" id="sendVideo">Download</button></div>
      </div>`;
    $("#sendVideo").onclick = (e) => sendIt(e.target, { url: vp.url, page: vp.url });
  } else if (!items.length) {
    $("#list").innerHTML = knownSite
      ? `<div class="empty">Open a video and press <b>Download this page</b>. Grab gets ${esc(knownSite)} videos from the
         page link (best quality and title), so their streams aren't listed here.</div>`
      : `<div class="empty">Nothing yet. Start playing the video, then open this again.<br>
         For YouTube, Reddit, X and other known sites, <b>Download this page</b> gives the best result.</div>`;
  } else {
    $("#list").innerHTML = items.map((i, n) => `
      <div class="item ${i.drm ? "drm" : ""}">
        <div class="k">${esc(i.label)}</div>
        <div class="u" title="${esc(i.url)}">${esc(new URL(i.url).hostname)} · ${esc(new URL(i.url).pathname.split("/").pop() || "/")}</div>
        <div class="acts">
          <button data-copy="${n}" title="Copy stream link">Copy</button>
          <button class="go" data-go="${n}" ${i.drm ? "disabled" : ""}>Download</button>
        </div>
      </div>`).join("");
    $("#list").onclick = (e) => {
      const go = e.target.closest("[data-go]"), cp = e.target.closest("[data-copy]");
      if (go) sendIt(go, { url: items[Number(go.dataset.go)].url });
      if (cp) navigator.clipboard.writeText(items[Number(cp.dataset.copy)].url).then(() => { cp.textContent = "Copied"; });
    };
  }
  $("#foot").innerHTML = items.length
    ? "Tip: right-click any video, link or image → <b>Download with Grab</b>."
    : "Grab Stream Catcher watches pages for HLS, DASH and MP4 streams.";
}
$("#clear").onclick = async () => { await ask({ type: "clear", tabId: tab.id }); render(); };

// everything on this page: every picture and video, to tick and download together
let scanned = [], chosen = new Set(), kind = "all";
const shown = () => pickItems(scanned, { kind, min: Number($("#minSize").value) });
const px = (i) => (i.w && i.h ? `${i.w}×${i.h}` : i.w || i.h ? `${i.w || i.h}px` : "");

$("#scanBtn").disabled = !page.startsWith("http");
$("#scanBtn").onclick = async () => {
  document.querySelectorAll(".main, .page").forEach((el) => { el.hidden = true; });
  $("#everything").hidden = false;
  $("#grid").innerHTML = `<div class="empty small" style="grid-column: 1 / -1">Looking through the page…</div>`;
  const r = await ask({ type: "scan", tabId: tab.id });
  scanned = r.items || [];
  chosen = new Set(scanned.map((i) => i.url));  // all ticked: untick what you don't want
  drawGrid(r.error);
};
$("#back").onclick = () => {
  $("#everything").hidden = true;
  document.querySelectorAll(".main, .page").forEach((el) => { el.hidden = false; });
};
$("#kind").onclick = (e) => {
  const b = e.target.closest("[data-k]");
  if (!b) return;
  kind = b.dataset.k;
  $("#kind").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  drawGrid();
};
$("#minSize").onchange = () => drawGrid();
$("#selAll").onclick = () => { shown().forEach((i) => chosen.add(i.url)); drawGrid(); };
$("#selNone").onclick = () => { shown().forEach((i) => chosen.delete(i.url)); drawGrid(); };

function drawGrid(error) {
  const list = shown();
  const pics = scanned.filter((i) => i.kind === "image").length, vids = scanned.length - pics;
  $("#ecount").textContent = `${pics} picture${pics === 1 ? "" : "s"} · ${vids} video${vids === 1 ? "" : "s"}`;
  $("#grid").innerHTML = list.length ? list.map((i, n) => `
    <button class="tile ${chosen.has(i.url) ? "on" : ""}" data-n="${n}" data-url="${esc(i.url)}" title="${esc(i.name ? `${i.name}\n${i.url}` : i.url)}">
      ${i.kind === "image"
        ? `<img src="${esc(i.url)}" loading="lazy" referrerpolicy="no-referrer" alt="">`
        : `<span class="vid"><b>▶</b>${esc(new URL(i.url).pathname.split("/").pop().slice(0, 40))}</span>`}
      ${px(i) ? `<span class="dim">${px(i)}</span>` : ""}
      <span class="tick">✓</span>
    </button>`).join("")
    : `<div class="empty small" style="grid-column: 1 / -1">${esc(error || (scanned.length
        ? "Nothing that big here. Try a smaller size."
        : "No pictures or videos found on this page."))}</div>`;
  $("#grid").onclick = (e) => {
    const t = e.target.closest(".tile");
    if (!t) return;
    const url = list[Number(t.dataset.n)].url;
    chosen.has(url) ? chosen.delete(url) : chosen.add(url);
    t.classList.toggle("on", chosen.has(url));
    countSel();
  };
  countSel();
}
function countSel() {
  const n = shown().filter((i) => chosen.has(i.url)).length, b = $("#getSel");
  b.textContent = n ? `Download ${n}` : "Download";
  b.disabled = !n;
  b.classList.remove("done", "fail");
}
$("#getSel").onclick = async () => {
  const b = $("#getSel"), items = shown().filter((i) => chosen.has(i.url));
  b.disabled = true;
  b.textContent = "Sending…";
  const r = await ask({ type: "sendMany", page, title: tab?.title || "", items });
  if (r?.ok) {
    b.textContent = `Added ${r.added} ✓`;
    b.classList.add("done");
  } else {
    b.textContent = "Failed";
    b.classList.add("fail");
    b.title = r?.error || "";
    $("#ecount").textContent = r?.error || "Couldn't send to Grab";
    setTimeout(countSel, 3000);
  }
};

// auto-download: videos this site plays go to Grab by themselves
async function showAuto(s) {
  s = s || await ask({ type: "auto", tabId: tab.id, page });
  const web = page.startsWith("http"), n = (k) => `${k} video${k === 1 ? "" : "s"}`;
  $("#autoHost").textContent = s.site || "this site";
  $("#autoSite").checked = s.on;
  $("#autoSite").disabled = !web || s.all;
  $("#autoAll").checked = s.all;
  $("#autoInfo").textContent = !web ? "Works on web pages only."
    : s.on && s.error ? `Couldn't send to Grab (${s.error}).`
    : s.waiting ? `Grab isn't running: ${n(s.waiting)} waiting, sent when it opens.`
    : !s.on ? "Off. When on, every video the site plays goes straight to Grab, once each."
    : s.sentHere ? `On. ${n(s.sentHere)} from this page sent to Grab.`
    : s.videoPage ? `On. ${s.videoPage} videos download as soon as they start playing.`
    : "On. Videos this site plays go to Grab (not ads, live streams or DRM).";
}
async function setAuto(change) {
  showAuto(await ask({ type: "setAuto", tabId: tab.id, page, ...change }));
  setTimeout(() => showAuto(), 2000);  // turning it on sends what this page already played
}
$("#autoSite").onchange = (e) => setAuto({ site: e.target.checked });
$("#autoAll").onchange = (e) => setAuto({ all: e.target.checked });
showAuto();

// logins shared with Grab
async function showSharing(s) {
  s = s || await ask({ type: "sharing" });
  $("#shareOn").checked = s.on;
  $("#shareInfo").textContent = s.on && s.error ? `Couldn't share with Grab (${s.error}). Is Grab open?`
    : !s.on
    ? "Off. Grab can't download things that need you logged in (YouTube may ask it to prove it's not a bot)."
    : s.sites?.length ? `Shared: ${s.sites.join(", ")}. Only these sites, only with Grab on this Mac.`
    : `On, but you're not logged in to ${s.all.join(", ")} in this browser yet.`;
}
$("#shareOn").onchange = async (e) => {
  $("#shareInfo").textContent = e.target.checked ? "Sharing…" : "Stopping and deleting Grab's copy…";
  await ask({ type: "setSharing", on: e.target.checked });
  showSharing();
};
showSharing();
render();

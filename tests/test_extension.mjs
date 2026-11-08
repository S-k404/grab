// Unit tests for the browser extension's media detection.  Run: node --test tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import { pickItems, mergeScans } from "../browser-extension/scan.js";
import { classify, parseHls, dashHasDrm, dashIsLive, label, totalSize, loadsVideo, videoPage, siteOf, autoOn, autoWorthy, autoKey }
  from "../browser-extension/detect.js";

const H = (o) => Object.entries(o).map(([name, value]) => ({ name, value: String(value) }));
const req = (url, headers = {}, type = "xmlhttprequest", statusCode = 200) => ({ url, type, statusCode, responseHeaders: H(headers) });

test("HLS and DASH manifests are found by content type or extension", () => {
  assert.equal(classify(req("https://cdn.site.com/v/master.m3u8"))?.kind, "HLS");
  assert.equal(classify(req("https://cdn.site.com/play?id=1", { "Content-Type": "application/vnd.apple.mpegurl" }))?.kind, "HLS");
  assert.equal(classify(req("https://cdn.site.com/play?id=1", { "Content-Type": "application/x-mpegURL" }))?.kind, "HLS");
  assert.equal(classify(req("https://cdn.site.com/v/stream.mpd"))?.kind, "DASH");
  assert.equal(classify(req("https://cdn.site.com/m", { "Content-Type": "application/dash+xml" }))?.kind, "DASH");
  assert.ok(classify(req("https://cdn.site.com/v/master.m3u8")).manifest);
});

test("progressive video played by a <video> tag is found, with its real size", () => {
  const r = classify(req("https://media.site.com/clip.mp4", { "Content-Type": "video/mp4", "Content-Range": "bytes 0-1048575/52428800" }, "media", 206));
  assert.deepEqual(r, { kind: "MP4", manifest: false, size: 52428800, audio: false });
  assert.equal(classify(req("https://m.site.com/a.mp3", { "Content-Type": "audio/mpeg", "Content-Length": 5e6 }, "media"))?.kind, "MP3");
  assert.equal(classify(req("https://m.site.com/a.mp3", { "Content-Type": "audio/mpeg", "Content-Length": 5e6 }, "media"))?.audio, true);
  assert.equal(classify(req("https://m.site.com/song.m4a", { "Content-Length": 5e6 }, "media"))?.audio, true);
  assert.equal(classify(req("https://m.site.com/stream?id=9", { "Content-Type": "video/webm", "Content-Length": 9e6 }, "media"))?.kind, "WEBM");
});

test("stream pieces, previews, ads and errors are ignored", () => {
  assert.equal(classify(req("https://cdn.site.com/v/seg-12.ts", { "Content-Type": "video/mp2t" })), null);
  assert.equal(classify(req("https://cdn.site.com/v/chunk_00042.m4s", { "Content-Type": "video/iso.segment" })), null);
  assert.equal(classify(req("https://cdn.site.com/v/init.mp4", { "Content-Type": "video/mp4", "Content-Length": 9e6 }, "media")), null);
  assert.equal(classify(req("https://cdn.site.com/hover-preview.mp4", { "Content-Type": "video/mp4", "Content-Length": 120000 }, "media")), null);
  assert.equal(classify(req("https://pubads.g.doubleclick.net/ad.mp4", { "Content-Type": "video/mp4", "Content-Length": 9e6 }, "media")), null);
  assert.equal(classify(req("https://cdn.site.com/missing.m3u8", {}, "xmlhttprequest", 404)), null);
  assert.equal(classify(req("https://site.com/watch.mp4", { "Content-Type": "text/html" }, "main_frame")), null);
  // a small XHR'd mp4 is most likely a player fragment, not a whole video
  assert.equal(classify(req("https://cdn.site.com/a.mp4", { "Content-Type": "video/mp4", "Content-Length": 800000 })), null);
});

test("YouTube's internal streams are skipped (Grab downloads YouTube from the page link instead)", () => {
  assert.equal(classify(req("https://rr3---sn-abc.googlevideo.com/videoplayback?itag=137", { "Content-Type": "video/mp4", "Content-Length": 9e7 }, "media")), null);
});

test("HLS master playlist: variants and best resolution", () => {
  const master = `#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
https://cdn.site.com/1080p/index.m3u8`;
  const p = parseHls(master, "https://cdn.site.com/v/master.m3u8");
  assert.equal(p.master, true);
  assert.equal(p.maxHeight, 1080);
  assert.deepEqual(p.variants.map((v) => v.url), ["https://cdn.site.com/v/360p/index.m3u8", "https://cdn.site.com/1080p/index.m3u8"]);
});

test("HLS media playlist: duration, and DRM detection", () => {
  const media = "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXTINF:6.0,\nb.ts\n#EXTINF:3.5,\nc.ts\n#EXT-X-ENDLIST";
  assert.deepEqual(parseHls(media, "https://x/y.m3u8"), { master: false, duration: 15.5, segments: 3, drm: false, live: false });
  const fairplay = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:6,\na.ts';
  assert.equal(parseHls(fairplay, "https://x/y.m3u8").drm, true);
  const aes = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:6,\na.ts';  // plain AES-128 is fine (yt-dlp decrypts it)
  assert.equal(parseHls(aes, "https://x/y.m3u8").drm, false);
  assert.equal(parseHls("<html>nope</html>", "https://x/"), null);
});

test("DASH DRM detection", () => {
  assert.ok(dashHasDrm('<ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>'));
  assert.ok(!dashHasDrm('<MPD><Period><AdaptationSet mimeType="video/mp4"/></Period></MPD>'));
});

test("labels and sizes", () => {
  assert.equal(label({ kind: "HLS", maxHeight: 1080, duration: 3725 }), "HLS · up to 1080p · 1:02:05");
  assert.equal(label({ kind: "MP4", size: 52428800 }), "MP4 · 50 MB");
  assert.equal(label({ kind: "HLS", manifest: true, size: 312, maxHeight: 720 }), "HLS · up to 720p");  // playlist size isn't the video size
  assert.equal(totalSize(H({ "Content-Length": 1234 })), 1234);
  assert.equal(totalSize([]), null);
});

test("HLS: separate audio tracks belong to the master, and live playlists are spotted", () => {
  const master = `#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",URI="audio/en.m3u8"
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=90000,URI="iframes.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="aud"
1080p.m3u8`;
  const p = parseHls(master, "https://cdn.site.com/v/master.m3u8");
  assert.deepEqual(p.renditions, ["https://cdn.site.com/v/audio/en.m3u8", "https://cdn.site.com/v/iframes.m3u8"]);
  assert.deepEqual(p.variants.map((v) => v.url), ["https://cdn.site.com/v/1080p.m3u8"]);
  assert.equal(parseHls("#EXTM3U\n#EXTINF:6,\na.ts\n#EXTINF:6,\nb.ts", "https://x/").live, true);  // no end: live
  assert.equal(parseHls("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:6,\na.ts", "https://x/").live, false);
  assert.ok(dashIsLive('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" minimumUpdatePeriod="PT2S">'));
  assert.ok(!dashIsLive('<MPD type="static" mediaPresentationDuration="PT1M">'));
});

test("auto-download: what counts as a page playing a video", () => {
  assert.ok(loadsVideo(req("https://rr3---sn-abc.googlevideo.com/videoplayback?itag=137&range=0-9999", { "Content-Type": "application/vnd.yt-ump" })));
  assert.ok(loadsVideo(req("https://scontent.cdninstagram.com/v/t50/clip.mp4?bytestart=0&byteend=9999", { "Content-Type": "video/mp4" })));
  assert.ok(loadsVideo(req("https://video.twimg.com/ext_tw_video/1/pu/pl/abc.m3u8", { "Content-Type": "application/x-mpegURL" })));
  assert.ok(loadsVideo(req("https://site.com/v", {}, "media")));
  assert.ok(!loadsVideo(req("https://site.com/song", { "Content-Type": "audio/mpeg" }, "media")));     // music isn't a video
  assert.ok(!loadsVideo(req("https://site.com/api/feed", { "Content-Type": "application/json" })));
  assert.ok(!loadsVideo(req("https://pubads.g.doubleclick.net/ad.mp4", { "Content-Type": "video/mp4" }, "media")));
  assert.ok(!loadsVideo(req("https://site.com/gone.mp4", { "Content-Type": "video/mp4" }, "media", 404)));
});

test("auto-download: single-video pages give a clean link; feeds, channels and profiles don't count", () => {
  const vp = (u) => videoPage(u)?.url ?? null;
  assert.equal(vp("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&t=42s"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(vp("https://m.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(vp("https://youtu.be/dQw4w9WgXcQ?si=abc"), "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(vp("https://www.youtube.com/shorts/abcdefghijk"), "https://www.youtube.com/shorts/abcdefghijk");
  assert.equal(vp("https://www.instagram.com/reel/C9xYz12AbCd/?igsh=xyz"), "https://www.instagram.com/reel/C9xYz12AbCd");
  assert.equal(vp("https://x.com/someone/status/1850000000000000000?s=20"), "https://x.com/someone/status/1850000000000000000");
  assert.equal(vp("https://www.reddit.com/r/videos/comments/abc123/a_title/"), "https://www.reddit.com/r/videos/comments/abc123");
  assert.equal(vp("https://www.tiktok.com/@user/video/7300000000000000000"), "https://www.tiktok.com/@user/video/7300000000000000000");
  assert.equal(vp("https://www.facebook.com/watch/?v=123456"), "https://www.facebook.com/watch/?v=123456");
  assert.equal(videoPage("https://vimeo.com/76979871").site, "Vimeo");
  for (const u of ["https://www.youtube.com/", "https://www.youtube.com/@channel/videos", "https://www.youtube.com/playlist?list=PL1",
                   "https://www.youtube.com/live/abcdefghijk", "https://x.com/home", "https://x.com/someone",
                   "https://www.instagram.com/someone/", "https://www.instagram.com/someone/reels/", "https://www.reddit.com/r/videos/",
                   "https://www.twitch.tv/somechannel", "https://example.com/watch?v=dQw4w9WgXcQ", "chrome://newtab/"]) {
    assert.equal(videoPage(u), null, u);
  }
});

test("auto-download: on for chosen sites (and their subdomains) or every site", () => {
  assert.equal(siteOf("https://www.x.com/home"), "x.com");
  assert.ok(autoOn("https://x.com/home", { sites: ["x.com"] }));
  assert.ok(autoOn("https://video.example.org/v/1", { sites: ["example.org"] }));
  assert.ok(!autoOn("https://notx.com/", { sites: ["x.com"] }));
  assert.ok(!autoOn("https://x.com/home", { sites: [] }));
  assert.ok(autoOn("https://anything.net/", { all: true }));
  assert.ok(!autoOn("chrome://extensions/", { all: true }));
});

test("auto-download: which found streams go (not DRM, live, audio, tiny, unread or part of another)", () => {
  const ok = { url: "u", manifest: true, inspected: true, duration: 120 };
  assert.ok(autoWorthy(ok));
  assert.ok(autoWorthy({ url: "u", manifest: false, size: 5e7 }));                 // a plain MP4: no duration known, fine
  assert.ok(autoWorthy({ ...ok, duration: undefined }));                           // couldn't read its length
  for (const bad of [{ drm: true }, { live: true }, { audio: true }, { hidden: true }, { inspected: false }, { duration: 2 }]) {
    assert.ok(!autoWorthy({ ...ok, ...bad }), JSON.stringify(bad));
  }
});

test("auto-download: the same video keeps its key when the link is re-signed", () => {
  const a = "https://cdn.site.com/v/abc/master.m3u8?token=111&expires=1700000000&quality=hd";
  const b = "https://cdn.site.com/v/abc/master.m3u8?expires=1800000000&quality=hd&token=999";
  assert.equal(autoKey(a), autoKey(b));
  assert.equal(autoKey(a), "cdn.site.com/v/abc/master.m3u8?quality=hd");
  assert.notEqual(autoKey("https://s.com/play?id=1"), autoKey("https://s.com/play?id=2"));
  assert.equal(autoKey("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), autoKey("https://youtube.com/watch?v=dQw4w9WgXcQ&t=5"));
});

test("everything on the page: filter by kind and size, unknown sizes always shown", () => {
  const items = [{ url: "a", kind: "image", w: 1200, h: 800 }, { url: "b", kind: "image", w: 16, h: 16 },
                 { url: "c", kind: "image", w: 0, h: 0 }, { url: "d", kind: "video", w: 320, h: 240 }];
  assert.deepEqual(pickItems(items, { min: 200 }).map((i) => i.url), ["a", "c", "d"]);
  assert.deepEqual(pickItems(items, { min: 1000 }).map((i) => i.url), ["a", "c"]);
  assert.deepEqual(pickItems(items, { kind: "video" }).map((i) => i.url), ["d"]);
  assert.equal(pickItems(items).length, 4);
  // the same picture found in two frames: listed once, with the bigger size
  const merged = mergeScans([[{ url: "a", kind: "image", w: 0, h: 0, name: "" }], [{ url: "a", kind: "image", w: 640, h: 480, name: "Cat" }]]);
  assert.deepEqual(merged, [{ url: "a", kind: "image", w: 640, h: 480, name: "Cat" }]);
});

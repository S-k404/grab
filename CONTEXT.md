# Grab: full project context (hand-off notes)

Written 2026-10-27 so work can continue in a new session without the old conversation.
Full transcript of the session that built all this:
`~/.claude/projects/-Users-me-Documents-site-download/d18e2e71-6e71-4b58-aaf2-53db857bd4b8.jsonl`

---

## 1. What Grab is

A batch downloader for videos, image posts and files from YouTube, Reddit, X/Twitter, Instagram, Threads, TikTok and about 1800 other sites, plus raw HLS/DASH/MP4 streams. It replaces Video DownloadHelper, JDownloader and image-downloader extensions. It has five parts:

| Part | Where | What |
|---|---|---|
| Engine | `grab/core.py` (+ helpers) | Python. yt-dlp as a library, gallery-dl as a subprocess, aria2c for multi-connection, ffmpeg/ffprobe |
| CLI | `grab/cli.py` → `grab` | Batch downloads from the terminal |
| Web GUI | `grab/web.py` + `grab/ui.html` → `grab-gui` | Local server on `127.0.0.1:8765` with a one-page UI |
| Mac app | `app/main.swift` → `Grab.app` | WKWebView window around the GUI, a menu-bar icon, the `grab://` URL scheme, notifications |
| Browser extension | `browser-extension/` | "Grab Stream Catcher" for Brave (Chromium MV3): catches streams, one-click send, shares logins |

---

## 2. How to run, build and test

```zsh
cd ~/Documents/site-download
./setup.sh                        # first time: venv + deps + ffmpeg/aria2c/deno checks
.venv/bin/pytest -q               # tests (138 pass, 10 skipped as of 2026-10-27)
.venv/bin/pytest -q --online      # also the real-site tests (YouTube etc.)
./build_app.sh                    # build ./Grab.app
./build_app.sh --install          # build and copy to ~/Applications, refresh the runtime venv and the extension copy
./build_app.sh --update-runtime   # only refresh the app's engine after editing grab/
grab -f links.txt -j 8            # CLI batch
grab --doctor                     # self-check (also a button in Settings)
```

- **Restarting the installed app without stealing focus:** `osascript -e 'quit app "Grab"'; open -g ~/Applications/Grab.app`.
- **Tests:** conftest serves a local test site, including `/protected`, `/feed.xml`, and VP9 and HDR AV1 sample files. A guard fixture (`_never_touch_the_real_grab_folder`) fails any test that writes to the real `~/Library/Application Support/Grab`.
- **Extension end-to-end tests:** `tests/test_extension_e2e.py` drives headless Helium over CDP, and `tests/test_extension.mjs` covers the same flow.

---

## 3. Paths on this Mac

| Path | What |
|---|---|
| `~/Documents/site-download` | Source (this folder) |
| `~/Applications/Grab.app` | Installed app, bundle id `local.grab.downloader` |
| `~/Library/Application Support/Grab/` | Grab's state dir. Override it with `--state-dir` or the `GRAB_STATE_DIR` env var |
| `…/Grab/runtime/` | The app's own venv, so the app never needs access to Documents |
| `…/Grab/prefs.json` | Saved settings (preset, out_dir, cookies_browser=`extension`, workers, folders=`site`, schedule, limits, `send_key`, …) |
| `…/Grab/history.json` | Job list + `learned` per-site strategies |
| `…/Grab/update.json` | Auto-update state |
| `…/Grab/logins/extension-cookies.txt` (+`.json`) | Logins shared by the Brave extension (Netscape cookie file, mode 600) |
| `…/Grab/browser-extension/` | Copy of the extension. Not used now; Brave loads the one in Documents |
| `~/Downloads/grab` | Default download folder. Sub-folders per site (`Youtube/`, `Reddit/`, `direct/`, …) unless "No sub-folders" is on |

**Extension ID:** `hejmghgkkgadcieojoganellldeehhog`. The `key` in manifest.json pins it. Brave loads the extension unpacked from `~/Documents/site-download/browser-extension`.

---

## 4. Engine (`grab/core.py`, ~1800 lines)

- **`Options`**
  - Fields: out_dir, mode (auto/video/audio/images/direct), quality, container, audio_format, subtitles, cookies_browser, cookies_file, referer, headers, fragments, aria2, aria2_streams, aria2_conns, archive, expand_playlists, items, smart, folders (`site`/`none`), title, media, scan, watch, compat, clip, sponsorblock, chapters, thumbnail, sub_langs, polite, rate_limit, subfolder.
  - Conversions: `Options.from_dict` / `to_dict`.
- **`Job`**: the per-job fields used by the queue are strategy, force, auto_paused, proxy and min_height.
- **`Manager`** (the job queue):
  - Worker threads.
  - Methods: add(titles=), pause(auto=), start_now, retry, remove, `clear_finished()` (removes only done, skipped and cancelled jobs), learn, release_hold, save/_load.
  - `_next()` honours the download window, polite groups and holds.
- **Routing** in `run_job`: `unwrap_url` → `prefers_gallery` → `run_ytdlp_smart` → `run_gallery_smart` → `run_direct`.
- **Smart mode:**
  - Errors are classified, then a ladder of `STRATEGIES` is tried: other player clients, cookies, a referer, etc.
  - A strategy that works is learned per site.
  - A quality guard (`min_height` 720) stops a fallback from quietly giving 360p.
  - The "bot" error class has an empty ladder and sets `needs_login`.
- **Compatible formats:**
  - Best quality is kept in HEVC/H.264.
  - AV1 and VP9 are converted with `hevc_videotoolbox` by `make_compatible`, keeping HDR.
  - Opus audio inside MP4 becomes AAC.
- **Logins:**
  - `resolve_login` picks one, in this order: your cookies.txt → a browser Grab can read → logins shared by the extension.
  - `login_for()` is a context manager that makes a private temp copy of the cookie file.
  - Helpers: `shared_cookies_path` / `save_shared_cookies` / `clear_shared_cookies`, `browser_data_blocked`, `cookie_problem`, `needs_login`, `installed_browsers`, `cookie_source`.
  - `BROWSERS` includes `"extension"`.
- **Folders and names:** `site_folder`, `target_dir`, `name_pattern`, `parse_clip` (for `--clip FROM-TO`).
- **Polite mode:** `POLITE_SITES` and `polite_group` space out requests to strict sites (Instagram etc.).
- **Other modules:**
  - `limiter.py`: an asyncio local proxy with one shared token bucket, giving a total bandwidth cap across all jobs.
  - `schedule.py`: download window (e.g. 01:00–07:00), pause outside it, keep awake.
  - `watch.py`: watching channels, subreddits and profiles. A baseline keeps the newest N; only new posts download. CLI: `grab watch`.
  - `analyze.py`: qualities, sizes and conversion notes before downloading.
  - `doctor.py`: self-check of ffmpeg, aria2c, gallery-dl, deno, the folder, versions, and YouTube with the current login.
  - `updater.py`: auto-updates yt-dlp and gallery-dl in the runtime venv (stable or nightly).

---

## 5. Web server (`grab/web.py`)

- **Server:** `ThreadingHTTPServer` on 127.0.0.1:8765.
- **Security:**
  - Host header check.
  - Origin check: only the extension's `chrome-extension://hejmghgkkgadcieojoganellldeehhog` origin may use `/api/ext/*`.
  - `send_key` (in prefs) authorizes the instant `/add?key=…` links that `grab://` uses.
- **Endpoints:**
  - Pages: `/` and `/add`.
  - Data: `/api/jobs` (also returns update, schedule, watch, learned), `/api/config`, `/api/thumb/…`.
  - Extension: `/api/ext/ping|check|add|hello|cookies`.
  - Downloads: `/api/add`, `/api/quick_add` (the menu-bar paste, using saved prefs and preset, no window needed), `/api/retry_login`, cancel, pause, resume, retry, start_now, remove, `clear` (→ `clear_finished`), reveal, open.
  - Settings and tools: `/api/prefs`, `/api/workers`, `/api/forget`, `/api/analyze`, `/api/doctor`, `/api/update/check`, `/api/watch/*`.
- **`PRESETS`:** best, 2160, 1440, 1080, 720, 480, audio, etc.
- **`main()`:**
  - Sets `GRAB_STATE_DIR`.
  - Flags: `--exit-with-parent` (no orphaned engines), `--no-auto-update`, `--no-browser`.
  - Restarts itself with `os.execv` after updates.

### UI (`grab/ui.html`, redesigned 2026-10-27)

- **Header:** Downloads / Watching tabs, stats pills, Settings.
- **Composer:**
  - An auto-growing link box.
  - Kind buttons Video / Audio / Images / File, plus a quality select.
  - Analyze and Download (⌘↩).
  - A meta row with the folder chip, the login chip ("Logins: Instagram, Reddit, Threads +2") and "More options".
  - Advanced options in groups: Video, Extras, Playlists & profiles, Connection, Raw stream links.
- **List:**
  - All / Active / Done / Failed filters, search (⌘F), Open folder.
  - **"Clear all"** (`#clearAll`) removes done, skipped, cancelled and failed jobs from the list and saves history at once; files are never touched. **"Clear finished"** (`#clear`) keeps failed jobs and only shows when both kinds exist. Both use `/api/clear` (`{"all": true}` for Clear all), which calls `Manager.clear_finished(failed=…)` and returns `cleared`. Playlist parents with children still active are kept.
  - Empty state with four tips.
- **Settings dialog tabs:** General, Speed, Logins, Browser, Updates, Engine.
- **Other:** drag-and-drop anywhere. `window.grabPasteAndGo(text)` is used by the app.
- **Backup:** the pre-redesign file is in the old session's scratchpad (`ui.before-redesign.html`). It may be gone.

---

## 6. Mac app (`app/main.swift`, ~650 lines)

- **Engine:** starts `runtime/bin/grab-gui --no-browser --port 8765 --exit-with-parent`, reads its stdout for the URL, then loads it in the WKWebView. Engine output stays in memory (`outputBuffer`); there is no log file.
- **Menu-bar icon:**
  - Custom-drawn (`MenuIcon`), made bolder at the user's request.
  - Polls `/api/jobs` every 1 s and shows progress.
  - Menu: active jobs, "Paste Link and Download" (→ `/api/quick_add`), "Open Grab", Pause All / Resume All, Quit.
- **Dock and activation:**
  - Closing the window switches to `.accessory` (no Dock icon); showing it switches back to `.regular`.
  - Cold launch from a `grab://` link is guarded so the window exists before it's used.
- **`grab://add?url=…&key=…`** downloads instantly when the key matches.
- **Background page fixes (2026-10-27):**
  - Before: WebKit suspended the hidden page, which then stopped polling and could show blank.
  - `config.preferences.inactiveSchedulingPolicy = .throttle` (macOS 14+).
  - `ensurePageAlive()` runs on `windowDidBecomeKey` and when the window becomes visible (occlusion). It evaluates `document.querySelector('#urls')`; on an error, a wrong answer or no answer within 3 s, it reloads.
  - Verified: after 3 min hidden the page still polled about once a second (38 requests in 20 s vs ~20 from the menu bar alone).
- **Notifications:** `UNUserNotificationCenter` when downloads finish or fail.

---

## 7. Browser extension (`browser-extension/`, v1.1.0)

- **Permissions:** webRequest, storage, contextMenus, tabs, cookies.
- **`detect.js` / `background.js`:** detect HLS (.m3u8), DASH (.mpd) and MP4 streams per tab, and flag DRM.
- **Popup:**
  - "Download this page" (best for YouTube, Reddit, X etc.).
  - A list of streams found on the page, each with a Download button.
  - A "Share my logins with Grab" switch.
- **Right-click menu:** "Download with Grab".
- **`send()`:**
  - Finds Grab with `/api/ext/ping` on known ports and POSTs `/api/ext/add`.
  - If Grab isn't running, it opens `grab://add?url=…&key=…` in a tab (closed after 4 s).
  - The key comes from `/api/ext/hello`.
- **Login sharing** (the fix for YouTube's "confirm you're not a bot"):
  - Shares cookies for SHARE_SITES only (YouTube, Instagram, Reddit, X, Threads), via `/api/ext/cookies`.
  - Re-shares when cookies change, at most every 10 min, and on browser start.
  - Waits 5 s after start before connecting, so the test's port override can't leak to the real Grab.
  - Turning sharing off deletes Grab's copy.
- **Test hooks:** a `ports` override in `chrome.storage.local`; `globalThis.grabCatcher`.
- **Auto-download** (added 2026-10-27, off by default):
  - Popup switches "Auto-download videos on <site>" and "On every site", stored as `auto: {all, sites: [host…]}` in `chrome.storage.local`. A site covers its subdomains, and `www.`, `m.`, `mobile.`, `old.` and `new.` are ignored.
  - **Video pages** (`videoPage()` in detect.js: YouTube watch/shorts/youtu.be, Instagram p/reel/tv, TikTok video, X status, Reddit comments, Threads post, Facebook watch/reel/videos, Vimeo, Dailymotion, Twitch VOD/clip) send a clean page link (no `&list=` or `t=`) once the tab has `played`, meaning `loadsVideo()` saw googlevideo `/videoplayback`, a `video/*` or manifest response, or a `media` request. Raw streams on those pages are not sent.
  - **Other pages** send each `autoWorthy()` stream: not hidden, DRM, live, audio-only, under 4 s, or a manifest that hasn't been inspected yet.
  - Sending runs 2.5 s after the last detection (`scheduleAuto`), one at a time (`autoChain`), at most 15 per page.
  - **Dedupe:** `autoKey()` ignores signature, expiry and tracking query params. Sent keys are kept in `autoSeen` (2000 max). The server also skips `auto: true` adds whose URL is already queued, running or done (`Manager.has_url`).
  - **Grab not running:** sends queue in `autoPending` (50 max, 12 h) and flush on the next successful `findGrab()`. No grab:// window is opened.
  - HLS `#EXT-X-MEDIA` and I-frame playlists are now part of the master (`renditions`), so separate audio tracks aren't listed or sent. Live is detected for HLS (no ENDLIST/VOD) and DASH (`type="dynamic"`).
- **Popup on video pages:** instead of "Nothing yet", it shows "<Site> video · best quality" with a Download button that sends the clean page link. On other pages of a site Grab supports, it explains that streams aren't listed there.

---

## 8. User preferences and decisions

- **Browser:** Brave only. Zen is not needed. Brave is the login browser; YouTube etc. use the extension's shared logins.
- **Full Disk Access:** stays off. The user chose extension-based login sharing instead of letting Grab read Brave's cookie database.
- **Not wanted for now:**
  - Music mode
  - A library view
  - iPhone sharing
  - Torrents
- **Wanted and built:**
  - Best quality in a compatible format (HEVC/H.264), and Analyze
  - Instant download from the extension button, and clip downloads
  - YouTube extras: SponsorBlock, chapters, subtitles, thumbnail
  - Polite mode
  - Scheduling and a total bandwidth cap
  - Site sub-folders, with a "No sub-folders" option
  - Watching channels, profiles and subreddits
  - Profile and batch downloads (Instagram, Reddit, …)
  - Smart error recovery and auto-update
  - The menu-bar mini app (made more visible)
- **A file under `~/Downloads/grab/direct`:** the user said "Leave it where it is."
- **Working style:** the user writes short, informal messages and prefers that things are done in the background without taking over the screen.

### Rules followed (keep following them)

- Never read browser cookie stores directly; that was blocked as credential access. Logins reach Grab only through the extension, or a browser the user picks.
- Brave and other browsers are view-only for computer use; never click in them. Don't operate the user's menu-bar manager (Vorssaint).
- Tests must never touch the real `~/Library/Application Support/Grab` (the guard fixture enforces this).
- Never delete or change the user's files without asking.

---

## 9. Bugs fixed along the way (so they don't come back)

**YouTube and logins**
- The YouTube bot check fell back to other players, got 360p, and learned it. Fixed with the quality guard; the "bot" class needs a login.
- Logins appeared not to be shared because Brave wasn't running, and the user may have been in Chrome.

**Leaks into the real Grab folder**
- Tests wrote to the real state folder twice:
  - `state_dir` ignored `--state-dir`; fixed with the env var.
  - The extension connected to the real Grab before the test port override; fixed with the 5 s grace.
- The guard fixture was added after these.

**Mac app**
- Blank window: WebKit suspended the page in the background (see §6).
- Cold launch from a `grab://` link crashed because the window was nil.
- Orphaned engine processes after the app quit; fixed with `--exit-with-parent`.

**Downloads**
- "Latest N" downloaded older posts.
- The media filter's range didn't match.
- Watch counted posts twice.
- A bad clip fell back to downloading the full file.
- Opus audio inside MP4.
- The auto-pause flag.
- Squashed time inputs in the UI.
- File names lost their `[id]` in deep folders: yt-dlp's `trim_file_name: 200` counts the whole path. Removed it; titles are capped at 150 **bytes** (`%(title).150B`, and `_hint_name` for page titles, which also escapes `%`). Tests: `test_deep_folders_keep_the_whole_file_name`, `test_page_title_names_fit_and_stay_literal`.

---

## 10. State right now (2026-10-27 ~14:20 IST)

- Installed and running: Grab.app (engine 1.0.0, yt-dlp 2026.10.26), with the redesigned UI and the background-page fix.
- Login sharing works. Brave shared YouTube, Instagram, Reddit, X and Threads; the popup confirms it.
- CLI test with the user's logins: `Me at the zoo` downloaded fine in 13 s. A 1080p best-quality test (`dQw4w9WgXcQ`) was **stopped before finishing** because the user said "wait". Test files are in the old session's scratchpad `dl-test/`, not the user's folder.
- `~/Downloads/grab` is currently **empty**, and Grab's history has 0 jobs. Nothing in Grab's code deletes finished downloads (only its own temp conversion files). The folder's 13:38 timestamp matches a self-check probe file. Most likely the user cleared it; unconfirmed.

## 10b. Second session (2026-10-27 afternoon)

- Built auto-download in the extension (see §7), the Clear all button (see §5 UI), and the popup's video-page display.
- **"NOPE doesnt woek":** the user's screenshot showed the popup on a playing YouTube video saying "Nothing yet. Start playing the video". YouTube streams are skipped on purpose, so the popup looked broken. Grab's history was empty, so "Download this page" had apparently never been used. Fixed by the video-page display (§7).

- **Feature: Everything on page** (extension 1.2.0, new `scripting` permission):
  - `scan.js`: `scanPage()` runs in every frame via `chrome.scripting.executeScript` and must stay self-contained. `mergeScans()` and `pickItems()` are pure and tested in Node.
  - Background messages: `scan` and `sendMany` → `POST /api/ext/add_many` (extension origin only).
  - Server: makes one `Manager.add_group` parent ("N files from <title>") with direct-mode children. `aria2` is off so `batch_names()` names are used. Files go to `<site>/<page title>/`; `run_direct` now honours `opts.subfolder`.
  - Test page: `/gallery.html` in conftest.
- **Feature: failure log:**
  - `core.log` (logger "grab") writes to `<state>/logs/grab.log` (1 MB × 3) through `start_log_file()`, called in `web.main`. `Job.note` lines, plus start, end and errors with tracebacks.
  - Failed jobs keep their `log` in history.json.
  - `GET /api/details?id=` returns `job_details()` text (Cookie and Authorization headers hidden). The UI has a "Copy details" button on failed rows (ClipboardItem with a promise, for WebKit) and Settings → Engine → "Show log file" (`/api/reveal_log`).
  - **To diagnose a user's failure, read `~/Library/Application Support/Grab/logs/grab.log`.**

## 11. Open items (where to pick up)

1. **"NOPE doesnt woek"**: most likely the popup saying "Nothing yet" on YouTube (now fixed, see §10b). Confirm with the user after they reload the extension.
   - Still worth doing: a real 1080p+ YouTube download through the popup → `/api/ext/add` → job → file. The HEVC conversion and SABR/PO-token formats are the likely failure points.
   - Possible hardening: write the engine's output to a log file (e.g. `…/Grab/engine.log`, rotated) so failures can be diagnosed later; today it lives only in memory.
2. **Clear all:** done (§5 UI). Optional extra if the user wants it: a setting to not keep history between launches.
3. **Brave must reload the extension** (brave://extensions → Grab Stream Catcher → reload) to get auto-download and the new popup. It loads unpacked from `browser-extension/`. Never click in Brave yourself.
- After changing anything: run `.venv/bin/pytest -q`, then `./build_app.sh --update-runtime` (or `--install` if `app/main.swift` changed), then relaunch Grab in the background.

"""Command-line front end:  grab URL [URL ...] [-f urls.txt] [options]"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .core import BROWSERS, MODES, QUALITIES, Manager, Options, human_bytes, human_eta, parse_urls, tool_status

COLORS = {"done": "\033[32m", "skipped": "\033[36m", "error": "\033[31m", "cancelled": "\033[33m",
          "running": "\033[1m", "queued": "\033[2m"}
RESET = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    d = Options()
    p = argparse.ArgumentParser(
        prog="grab",
        description="Fast batch downloader for videos (YouTube & ~1800 sites, HLS/DASH), "
                    "image posts (Reddit, Twitter/X, Imgur, Instagram...) and direct files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("urls", nargs="*", help="URLs to download (playlists/channels/profiles work too)")
    p.add_argument("-f", "--file", action="append", default=[], help="text file with one URL per line ('-' = stdin)")
    p.add_argument("-o", "--out", default=d.out_dir, help="output folder")
    p.add_argument("--flat", action="store_true", help="save everything directly in the output folder (no Youtube/, Reddit/ ... sub-folders)")
    p.add_argument("-j", "--jobs", type=int, default=4, help="downloads running at the same time")
    p.add_argument("-m", "--mode", choices=MODES, default="auto",
                   help="auto: video, falling back to images then direct file")
    p.add_argument("-a", "--audio", action="store_const", const="audio", dest="mode", help="shortcut for --mode audio")
    p.add_argument("-q", "--quality", choices=QUALITIES, default="best", help="max video height")
    p.add_argument("--container", choices=("mp4", "mkv", "webm"), default="mp4")
    p.add_argument("--audio-format", choices=("m4a", "mp3", "opus", "flac", "wav"), default="m4a")
    p.add_argument("--subs", action="store_true", help="download + embed English subtitles")
    p.add_argument("--sub-langs", default="", help="subtitle languages to embed, e.g. en,es or all")
    p.add_argument("--clip", default="", metavar="FROM-TO", help="only this part, e.g. 1:20-2:45 (or 90- for 'to the end')")
    p.add_argument("--original", action="store_true", help="keep AV1/VP9 instead of converting to compatible HEVC/H.264 MP4")
    p.add_argument("--no-sponsorblock", action="store_true", help="don't cut YouTube sponsor segments")
    p.add_argument("-c", "--cookies", choices=[b for b in BROWSERS if b], help="use your login cookies from this browser")
    p.add_argument("--cookies-file", help="Netscape cookies.txt file")
    p.add_argument("-r", "--referer", help="Referer header (needed for many raw .m3u8 links)")
    p.add_argument("-H", "--header", action="append", default=[], help="extra header 'Name: value' (repeatable)")
    p.add_argument("--fragments", type=int, default=d.fragments, help="parallel HLS/DASH fragments per video")
    p.add_argument("--conns", type=int, default=d.aria2_conns, help="aria2c connections per file")
    p.add_argument("--no-aria2", action="store_true", help="don't use aria2c")
    p.add_argument("--no-archive", action="store_true", help="re-download things already downloaded")
    p.add_argument("--no-smart", action="store_true", help="don't switch download strategy automatically on errors")
    p.add_argument("--no-expand", action="store_true", help="download playlists inside one job instead of fanning out")
    p.add_argument("-i", "--items", default="", help="only these playlist/gallery items, e.g. 1-20 or 1,5,10-")
    p.add_argument("--aria2-streams", action="store_true", help="also use aria2c for plain-HTTP video streams (no live %%)")
    p.add_argument("--limit-rate", default="", help="per-download speed cap, e.g. 5M")
    p.add_argument("--total-limit", default="", help="speed cap for all downloads together, e.g. 5M")
    p.add_argument("--window", default="", metavar="HH:MM-HH:MM", help="only download inside this time window, e.g. 01:00-07:00 (waits until then)")
    p.add_argument("--gui", action="store_true", help="open the browser GUI instead")
    p.add_argument("--doctor", action="store_true", help="check that everything is installed and working, then exit")
    p.add_argument("--update", action="store_true", help="update yt-dlp and gallery-dl now, then exit")
    p.add_argument("--nightly", action="store_true", help="with --update: use yt-dlp nightly builds (newest fixes)")
    p.add_argument("--version", action="version", version=f"grab {__version__}")
    return p


def collect_urls(args) -> list[str]:
    lines = list(args.urls)
    for f in args.file:
        if f == "-":
            lines += sys.stdin.read().splitlines()
            continue
        p = Path(f).expanduser()
        if not p.is_file():
            sys.exit(f"grab: can't find the links file '{f}' (looked in {p.resolve().parent}).\n"
                     f"Create it with one URL per line, or pass the full path, e.g. -f ~/Desktop/links.txt")
        lines += p.read_text(errors="replace").splitlines()
    return parse_urls(lines)


def app_prefs() -> dict:
    """The Grab app's settings (so the command line uses the same login etc.)."""
    import json
    from .web import state_dir
    try:
        return json.loads((state_dir() / "prefs.json").read_text())
    except (OSError, ValueError):
        return {}


def options_from_args(args) -> Options:
    headers = {}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    return Options(
        out_dir=str(Path(args.out).expanduser()), mode=args.mode, quality=args.quality,
        container=args.container, audio_format=args.audio_format, subtitles=args.subs,
        cookies_browser=args.cookies or app_prefs().get("cookies_browser", ""), cookies_file=args.cookies_file or "",
        referer=args.referer or "", headers=headers, fragments=args.fragments,
        aria2=not args.no_aria2, aria2_conns=args.conns, archive=not args.no_archive,
        expand_playlists=not args.no_expand, rate_limit=args.limit_rate, items=args.items,
        aria2_streams=args.aria2_streams, smart=not args.no_smart, folders="none" if args.flat else "site",
        clip=args.clip, compat=not args.original, sponsorblock=not args.no_sponsorblock, sub_langs=args.sub_langs,
    )


class Display:
    """Live multi-line progress for a TTY, plain status lines otherwise."""

    def __init__(self, mgr: Manager):
        self.mgr = mgr
        self.tty = sys.stdout.isatty()
        self.drawn = 0
        self.reported: dict[int, str] = {}

    def line(self, j: dict, width: int) -> str:
        st = j["status"]
        title = j["title"]
        if st == "running":
            pct = j["progress"]
            bar_w = 20
            if pct is not None:
                fill = int(bar_w * pct / 100)
                bar = "█" * fill + "░" * (bar_w - fill) + f" {pct:5.1f}%"
            else:
                bar = "·" * bar_w + "   ...  "
            spd = f"{human_bytes(j['speed'])}/s" if j["speed"] else ""
            info = f"{bar} {spd:>11} {human_eta(j['eta']):>7}  {j['stage']}"
        elif st == "error":
            info = f"ERROR {j['error']}"
        elif st in ("done", "skipped"):
            info = f"{st}  {len(j['files'])} file(s)" + (f"  {j['stage']}" if j["stage"] else "")
        else:
            info = st
        head = f"{COLORS.get(st, '')}[{j['id']:>3}] {st:<9}{RESET} "
        room = max(10, width - 16 - len(info) - 2)
        t = title if len(title) <= room else title[: room - 1] + "…"
        return (head + f"{t:<{room}}  {info}")[: width + len(COLORS.get(st, "")) + len(RESET)]

    def tick(self):
        jobs = self.mgr.snapshot()
        if not self.tty:
            for j in jobs:
                if j["status"] in ("done", "skipped", "error", "cancelled") and self.reported.get(j["id"]) != j["status"]:
                    self.reported[j["id"]] = j["status"]
                    extra = j["error"] or ", ".join(j["files"][:3]) or j["stage"]
                    print(f"[{j['id']}] {j['status']}: {j['title']}  {extra}", flush=True)
            return
        width = shutil.get_terminal_size((120, 20)).columns
        rows = self.mgr.workers + 6
        active = [j for j in jobs if j["status"] == "running"]
        finished = [j for j in jobs if j["status"] in ("done", "skipped", "error", "cancelled")]
        # print finished jobs permanently above the live area
        new_final = [j for j in finished if j["id"] not in self.reported]
        out = []
        if self.drawn:
            out.append(f"\033[{self.drawn}F\033[J")
        for j in new_final:
            self.reported[j["id"]] = j["status"]
            out.append(self.line(j, width) + "\n")
        live = [self.line(j, width) for j in active[:rows]]
        queued = sum(1 for j in jobs if j["status"] == "queued")
        speed = sum(j["speed"] or 0 for j in active)
        counts = {s: sum(1 for j in jobs if j["status"] == s) for s in ("done", "skipped", "error")}
        live.append(f"\033[2m── {len(active)} running · {queued} queued · {counts['done']} done · "
                    f"{counts['skipped']} skipped · {counts['error']} failed · total {human_bytes(speed)}/s{RESET}")
        out.append("\n".join(live) + "\n")
        self.drawn = len(live)
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["watch"]:
        from .watch import cli as watch_cli
        return watch_cli(argv[1:])
    args = build_parser().parse_args(argv)
    if args.doctor:
        from .doctor import main as doctor_main
        return doctor_main(args.out, args.cookies or app_prefs().get("cookies_browser", ""))
    if args.update:
        from .updater import available_updates, install
        try:
            ups = available_updates(args.nightly)
        except OSError as e:
            print(f"grab: couldn't reach PyPI: {e}")
            return 1
        if not ups:
            print("Everything is up to date.")
            return 0
        print("Updating " + ", ".join(f"{p} {old or '-'} → {new}" for p, (old, new) in ups.items()) + " …")
        ok, log = install(ups, args.nightly)
        print("Done." if ok else f"Update failed:\n{log}")
        return 0 if ok else 1
    if args.gui:
        from .web import main as gui_main
        return gui_main(["--jobs", str(args.jobs), "--out", args.out])

    urls = collect_urls(args)
    if not urls and args.file:
        print(f"grab: no links found in {', '.join(args.file)} - add one URL per line (lines starting with # are ignored)")
        return 2
    if not urls:
        build_parser().print_usage()
        print("grab: give at least one URL or -f urls.txt   (or run `grab --gui`)")
        return 2

    tools = tool_status()
    missing = [k for k in ("aria2c", "ffmpeg", "gallery-dl") if not tools[k]]
    if missing:
        print(f"note: not found: {', '.join(missing)} (some features disabled)", file=sys.stderr)

    opts = options_from_args(args)
    mgr = Manager(args.jobs)
    sched_cfg = {"keep_awake": True}
    if args.total_limit:
        from yt_dlp.utils import parse_bytes
        sched_cfg["speed_limit"] = (parse_bytes(args.total_limit) or 0) / (1024 * 1024)
    if args.window:
        start, _, end = args.window.partition("-")
        sched_cfg.update(window_on=True, window_start=start.strip(), window_end=end.strip())
    from .schedule import Scheduler
    sched = Scheduler(mgr, lambda: sched_cfg).start()
    if not sched.status["window_open"]:
        print(f"Waiting for the download window ({sched.status['window']}); starts at {sched.status['next_change']}. Ctrl-C to cancel.")
    mgr.add(urls, opts)
    print(f"grab: {len(urls)} URL(s) → {opts.out_dir}   ({args.jobs} parallel, yt-dlp {tools['yt-dlp']})")

    disp = Display(mgr)
    interrupted = {"n": 0}

    def on_sigint(*_):
        interrupted["n"] += 1
        if interrupted["n"] > 1:
            raise SystemExit(130)
        print("\nCancelling... (Ctrl-C again to force quit)")
        mgr.cancel()

    signal.signal(signal.SIGINT, on_sigint)
    t0 = time.time()
    mgr.wait(poll=0.4, on_tick=disp.tick)

    jobs = mgr.snapshot()
    failed = [j for j in jobs if j["status"] == "error"]
    files = sum(len(j["files"]) for j in jobs)
    print(f"\nFinished in {human_eta(time.time() - t0)}: {files} file(s) saved, {len(failed)} failed → {opts.out_dir}")
    for j in failed:
        print(f"  ✗ {j['url']}\n    {j['error']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""Download window + total speed cap + keep-awake, re-evaluated every few seconds.

Settings (from the GUI's prefs, or the CLI):
  speed_limit          total MB/s for all downloads together (0 = unlimited)
  limit_only_window    apply that cap only between limit_start and limit_end (e.g. daytime)
  window_on            only start downloads between window_start and window_end (e.g. 01:00-07:00)
  pause_outside        pause running downloads when the window closes, resume when it opens
  keep_awake           stop the Mac from idle-sleeping while there is work to do
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import threading
import time

from .limiter import SpeedLimiter

MB = 1024 * 1024
DEFAULTS = {"speed_limit": 0, "limit_only_window": False, "limit_start": "09:00", "limit_end": "23:00",
            "window_on": False, "window_start": "01:00", "window_end": "07:00", "pause_outside": True,
            "keep_awake": True}


def _hm(s: str) -> dt.time:
    h, m = (s or "0:0").split(":")[:2]
    return dt.time(int(h) % 24, int(m) % 60)


def in_window(now: dt.datetime, start: str, end: str) -> bool:
    """True if now is inside [start, end); windows may cross midnight (e.g. 23:00-07:00)."""
    s, e, t = _hm(start), _hm(end), now.time()
    if s == e:
        return True
    return s <= t < e if s < e else (t >= s or t < e)


def next_change(now: dt.datetime, start: str, end: str) -> dt.datetime:
    """When the window next opens or closes."""
    inside = in_window(now, start, end)
    target = _hm(end if inside else start)
    cand = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    return cand if cand > now else cand + dt.timedelta(days=1)


def _when(t: dt.datetime, now: dt.datetime) -> str:
    return t.strftime("%H:%M") + ("" if t.date() == now.date() else " tomorrow")


class Scheduler:
    def __init__(self, mgr, config, clock=dt.datetime.now):
        self.mgr, self.config, self.clock = mgr, config, clock
        self.limiter: SpeedLimiter | None = None
        self._was_open: bool | None = None
        self._awake: subprocess.Popen | None = None
        self.status = {"window_open": True, "cap": None, "awake": False, "note": ""}

    def start(self):
        self.tick()
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()
        return self

    def _loop(self):
        while True:
            time.sleep(5)
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - never let a bad setting kill the scheduler
                self.status["note"] = f"schedule error: {e}"

    def tick(self):
        c = {**DEFAULTS, **(self.config() or {})}
        now = self.clock()

        # ---- total speed cap
        limit = float(c["speed_limit"] or 0) * MB
        cap_now = limit if limit and (not c["limit_only_window"] or in_window(now, c["limit_start"], c["limit_end"])) else None
        if limit and self.limiter is None:
            self.limiter = SpeedLimiter().start()
        if self.limiter:
            self.limiter.set_rate(cap_now)
        # downloads go through the limiter whenever a cap is configured, so a cap that starts
        # later (e.g. at 09:00) also slows downloads that began at night
        self.mgr.proxy = self.limiter.url if (limit and self.limiter) else None

        # ---- download window
        open_ = (not c["window_on"]) or in_window(now, c["window_start"], c["window_end"])
        with self.mgr.cv:
            self.mgr.window_open = open_
            if open_ != self._was_open:
                self.mgr.cv.notify_all()
            jobs = list(self.mgr.jobs.values())
        nxt = _when(next_change(now, c["window_start"], c["window_end"]), now) if c["window_on"] else ""
        if not open_:
            for j in jobs:
                if j.status == "queued" and not j.force:
                    j.stage = f"waits for {c['window_start']}"
                elif j.status == "running" and not j.force and c["pause_outside"]:
                    self.mgr.pause(j.id, auto=True)
                elif j.status == "paused" and j.auto_paused:
                    j.stage = f"paused until {c['window_start']}"
        else:
            for j in jobs:
                if j.status == "queued" and j.stage.startswith("waits for"):
                    j.stage = ""
                elif j.status == "paused" and j.auto_paused:
                    j.auto_paused = False
                    self.mgr.resume(j.id)
        self._was_open = open_

        # ---- keep the Mac awake while there's work
        with self.mgr.cv:
            busy = bool(self.mgr.running) or (open_ and bool(self.mgr.queue))
        self._keep_awake(busy and c["keep_awake"])

        waiting = sum(1 for j in jobs if (j.status == "queued" and not j.force) or (j.status == "paused" and j.auto_paused))
        self.status = {
            "window_on": c["window_on"], "window_open": open_, "next_change": nxt,
            "window": f"{c['window_start']}–{c['window_end']}", "waiting": waiting if not open_ else 0,
            "cap": cap_now, "limit": limit or None, "awake": self._awake is not None,
            "note": "",
        }

    def _keep_awake(self, on: bool):
        if on and self._awake is None and sys.platform == "darwin" and shutil.which("caffeinate"):
            # -i: no idle sleep; -w: stop automatically if we exit
            self._awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif not on and self._awake is not None:
            self._awake.terminate()
            self._awake = None

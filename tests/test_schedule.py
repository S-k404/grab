"""Total speed cap and download window, against the local test site."""

from __future__ import annotations

import datetime as dt
import time

import pytest

import grab.core as core
from grab.core import Manager, Options
from grab.schedule import Scheduler, in_window, next_change

from conftest import run, sha

NOON = dt.datetime(2026, 10, 26, 12, 0)
NIGHT = dt.datetime(2026, 10, 26, 2, 0)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def sched(mgr, clock=None, **cfg):
    config = {"keep_awake": False, **cfg}
    return Scheduler(mgr, lambda: config, clock=clock or dt.datetime.now), config


def timed(fn):
    t0 = time.time()
    result = fn()
    return result, time.time() - t0


# ---------------------------------------------------------------- time windows

@pytest.mark.parametrize("hhmm,start,end,inside", [
    ("12:00", "09:00", "23:00", True),
    ("08:59", "09:00", "23:00", False),
    ("23:00", "09:00", "23:00", False),  # end is exclusive
    ("02:00", "01:00", "07:00", True),
    ("23:30", "23:00", "07:00", True),   # crosses midnight
    ("06:59", "23:00", "07:00", True),
    ("12:00", "23:00", "07:00", False),
    ("12:00", "00:00", "00:00", True),   # same start/end = all day
])
def test_in_window(hhmm, start, end, inside):
    h, m = map(int, hhmm.split(":"))
    assert in_window(NOON.replace(hour=h, minute=m), start, end) is inside


def test_next_change():
    assert next_change(NOON, "01:00", "07:00") == dt.datetime(2026, 10, 27, 1, 0)   # opens tomorrow 01:00
    assert next_change(NIGHT, "01:00", "07:00") == dt.datetime(2026, 10, 26, 7, 0)  # closes today 07:00


# ---------------------------------------------------------------- total speed cap

needs_aria2 = pytest.mark.skipif(not core.ARIA2C, reason="aria2c not installed")


@needs_aria2
def test_cap_limits_a_multi_connection_download(site, media, out):
    mgr = Manager(2)
    s, _ = sched(mgr, speed_limit=2)          # 2 MB/s
    s.tick()
    (job,), secs = timed(lambda: run([f"{site}/files/big.bin"], out, mgr=mgr, aria2_conns=8))  # 6 MB
    assert job.status == "done", job.error
    assert sha(job.files[-1]) == sha(media / "files/big.bin")
    assert 2.4 < secs < 4.8, f"6 MB at 2 MB/s took {secs:.1f}s"


@needs_aria2
def test_cap_is_shared_by_parallel_downloads(site, out):
    mgr = Manager(4)
    s, _ = sched(mgr, speed_limit=3)          # 3 MB/s for everything
    s.tick()
    jobs, secs = timed(lambda: run([f"{site}/files/big.bin", f"{site}/files/data.zip"], out, mgr=mgr))  # 9 MB total
    assert [j.status for j in jobs] == ["done", "done"]
    assert 2.4 < secs < 4.8, f"9 MB at 3 MB/s took {secs:.1f}s"


def test_cap_applies_to_yt_dlp_and_hls(site, out):
    mgr = Manager(2)
    s, _ = sched(mgr, speed_limit=8)          # 8 MB/s; clip is ~13 MB
    s.tick()
    (job,), secs = timed(lambda: run([f"{site}/files/clip.mp4"], out, mgr=mgr, archive=False))
    assert job.status == "done", job.error
    assert secs > 1.3, f"13 MB at 8 MB/s finished in {secs:.1f}s - cap not applied"


@needs_aria2
def test_cap_change_applies_to_running_download(site, out):
    mgr = Manager(1)
    s, cfg = sched(mgr, speed_limit=1)        # starts at 1 MB/s (6 MB would take 6 s)
    s.tick()
    (job,) = mgr.add([f"{site}/files/big.bin"], Options(out_dir=str(out)))
    time.sleep(1)
    cfg["speed_limit"] = 12                   # raise it mid-download
    s.tick()
    t0 = time.time()
    while job.status != "done":
        assert job.status in ("queued", "running"), job.error
        assert time.time() - t0 < 4, "new limit didn't take effect"
        time.sleep(0.1)


def test_cap_only_during_daytime(site, out):
    mgr = Manager(1)
    clock = Clock(NIGHT)
    s, _ = sched(mgr, clock, speed_limit=1, limit_only_window=True, limit_start="09:00", limit_end="23:00")
    s.tick()
    assert s.status["cap"] is None and mgr.proxy  # proxy stays in place, but no cap at night
    clock.t = NOON
    s.tick()
    assert s.status["cap"] == 1024 * 1024


def test_no_cap_means_no_proxy(site, out):
    mgr = Manager(1)
    s, _ = sched(mgr)
    s.tick()
    assert mgr.proxy is None and s.status["cap"] is None


# ---------------------------------------------------------------- download window

def test_downloads_wait_for_window_and_start_now_overrides(site, out):
    mgr = Manager(2)
    s, _ = sched(mgr, Clock(NOON), window_on=True, window_start="01:00", window_end="07:00")
    s.tick()
    a, b = mgr.add([f"{site}/files/data.zip", f"{site}/files/big.bin"], Options(out_dir=str(out)))
    time.sleep(2.5)
    s.tick()
    assert a.status == b.status == "queued"
    assert a.stage == "waits for 01:00" and s.status["waiting"] == 2
    mgr.start_now(a.id)                       # user presses "Start now" on one
    deadline = time.time() + 20
    while a.status != "done":
        assert time.time() < deadline
        time.sleep(0.1)
    assert b.status == "queued"               # the other keeps waiting


def test_window_opening_starts_waiting_downloads(site, out):
    mgr = Manager(2)
    clock = Clock(NOON)
    s, _ = sched(mgr, clock, window_on=True, window_start="01:00", window_end="07:00")
    s.tick()
    (job,) = mgr.add([f"{site}/files/data.zip"], Options(out_dir=str(out)))
    time.sleep(1)
    assert job.status == "queued"
    clock.t = NIGHT                           # 02:00 - window is open
    s.tick()
    deadline = time.time() + 20
    while job.status != "done":
        assert time.time() < deadline, job.status
        time.sleep(0.1)


@needs_aria2
def test_window_closing_pauses_and_reopening_resumes(site, media, out):
    mgr = Manager(1)
    clock = Clock(NIGHT)
    s, _ = sched(mgr, clock, window_on=True, window_start="01:00", window_end="07:00", pause_outside=True)
    s.tick()
    (job,) = mgr.add([f"{site}/slow/big.bin"], Options(out_dir=str(out), aria2_conns=2))
    deadline = time.time() + 20
    while (job.progress or 0) < 10:
        assert time.time() < deadline
        time.sleep(0.1)
    clock.t = NOON                            # 07:00 passed - window closes
    s.tick()
    while job.status == "running":
        time.sleep(0.1)
    assert job.status == "paused" and job.auto_paused
    assert not mgr.idle()                     # the CLI keeps waiting for it
    clock.t = NIGHT                           # next night
    s.tick()
    deadline = time.time() + 60
    while job.status != "done":
        assert job.status in ("queued", "running"), job.error
        assert time.time() < deadline
        time.sleep(0.2)
    assert sha(job.files[-1]) == sha(media / "files/big.bin")

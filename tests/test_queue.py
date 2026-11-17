"""Queue order, "don't keep history" and exported link lists. The queue is held, so nothing really downloads."""

from __future__ import annotations

import json

from grab.core import Manager, Options


def held(state_file=None, **kw) -> Manager:
    mgr = Manager(1, state_file=str(state_file) if state_file else None, **kw)
    mgr.hold = True  # nothing starts, so jobs stay exactly where the test puts them
    return mgr


def tail(rows):
    return [r["url"].rsplit("/", 1)[-1] for r in rows]


def test_move_reorders_the_line():
    mgr = held()
    a, b, c = mgr.add(["http://x.test/a", "http://x.test/b", "http://x.test/c"], Options())

    def line():
        return tail(sorted((j for j in mgr.snapshot() if j["queue_pos"]), key=lambda j: j["queue_pos"]))

    assert line() == ["a", "b", "c"]
    assert mgr.move(c.id, "top") and line() == ["c", "a", "b"]
    assert mgr.move(c.id, "bottom") and line() == ["a", "b", "c"]
    assert mgr.move(b.id, "top") and list(mgr.queue) == [b.id, a.id, c.id]


def test_only_waiting_downloads_can_move():
    mgr = held()
    a, b = mgr.add(["http://x.test/a", "http://x.test/b"], Options())
    mgr.pause(b.id)
    assert not mgr.move(b.id, "top")           # paused: no place in the line
    assert not mgr.move(a.id, "sideways")      # not a direction
    assert not mgr.move(9999, "top")           # not a download
    a.status = "done"
    assert not mgr.move(a.id, "bottom")        # already over
    assert [j["queue_pos"] for j in mgr.snapshot()] == [None, None]


def test_history_off_saves_only_what_is_still_waiting(tmp_path):
    f = tmp_path / "history.json"
    mgr = held(f, keep_history=False)
    done, failed, _waiting = mgr.add(["http://x.test/done", "http://x.test/failed", "http://x.test/waiting"], Options())
    done.status, failed.status = "done", "error"
    mgr.learn("x.test", "other client")  # a learned fix is not history
    mgr.save()
    saved = json.loads(f.read_text())
    assert tail(saved["jobs"]) == ["waiting"] and saved["learned"] == {"x.test": "other client"}
    assert len(mgr.jobs) == 3              # the list on screen stays until Grab quits


def test_history_off_keeps_a_playlist_while_its_items_go(tmp_path):
    f = tmp_path / "history.json"
    mgr = held(f, keep_history=False)
    (playlist,) = mgr.add(["http://x.test/list"], Options())
    playlist.status, playlist.children = "done", 2
    over, _going = mgr.add(["http://x.test/v1", "http://x.test/v2"], Options(), parent=playlist.id)
    over.status = "done"
    mgr.save()
    assert tail(json.loads(f.read_text())["jobs"]) == ["list", "v2"]


def test_history_switch_applies_at_once_and_on_the_next_launch(tmp_path):
    f = tmp_path / "history.json"
    mgr = held(f)
    (job,) = mgr.add(["http://x.test/done"], Options())
    job.status = "done"
    mgr.save()
    assert len(json.loads(f.read_text())["jobs"]) == 1
    mgr.set_keep_history(False)
    assert json.loads(f.read_text())["jobs"] == []     # cleaned right away...
    assert len(mgr.jobs) == 1                          # ...but not off the screen
    mgr.set_keep_history(True)
    assert len(json.loads(f.read_text())["jobs"]) == 1
    assert len(Manager(1, state_file=str(f)).jobs) == 1
    assert Manager(1, state_file=str(f), keep_history=False).jobs == {}   # an old history file is dropped on launch


def test_export_lists_the_links_to_do_again():
    mgr = held()
    ok, bad, cancelled, _live = mgr.add([f"http://x.test/{n}" for n in ("ok", "bad", "cancelled", "live")], Options())
    ok.status, bad.status, cancelled.status = "done", "error", "cancelled"
    (again,) = mgr.add(["http://x.test/ok"], Options())  # the same link added twice
    again.status = "done"
    (playlist,) = mgr.add(["http://x.test/list"], Options())
    playlist.status, playlist.children = "done", 2
    v1, v2 = mgr.add(["http://x.test/v1", "http://x.test/v2"], Options(), parent=playlist.id)
    v1.status, v2.status = "error", "done"

    assert tail(mgr.export_rows("failed")) == ["bad", "cancelled", "v1"]   # each failed item, not the playlist around it
    assert tail(mgr.export_rows("done")) == ["ok", "list"]                 # a playlist counts once
    assert tail(mgr.export_rows("active")) == ["live"]
    assert tail(mgr.export_rows("all")) == ["ok", "bad", "cancelled", "live", "list"]
    assert mgr.export_rows("failed")[0] == {"url": "http://x.test/bad", "title": "", "status": "error", "site": "x.test"}

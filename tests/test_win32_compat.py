"""Cross-platform Windows 11 compatibility unit tests for Grab."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from grab import core, doctor, updater, web


def test_win32_state_dir_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("GRAB_STATE_DIR", raising=False)
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppDataLocal"))
    assert core.state_dir() == Path(str(tmp_path / "AppDataLocal")) / "Grab"


def test_win32_bin_finds_exe_beside_python(monkeypatch, tmp_path):
    fake_py_dir = tmp_path / "Scripts"
    fake_py_dir.mkdir()
    fake_py = fake_py_dir / "python.exe"
    fake_ffmpeg = fake_py_dir / "ffmpeg.exe"
    fake_py.write_text("")
    fake_ffmpeg.write_text("")
    monkeypatch.setattr(core.sys, "platform", "win32")
    monkeypatch.setattr(core.sys, "executable", str(fake_py))
    assert core._bin("ffmpeg") == str(fake_ffmpeg)


def test_win32_cookie_problem_mentions_dpapi_and_extension(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    msg = core.cookie_problem("Failed to decrypt Chrome cookies via App-Bound DPAPI")
    assert "App-Bound Encryption" in msg or "Share logins" in msg
    assert core.browser_data_blocked("chrome") is False


def test_win32_doctor_and_updater_hints(monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    res = doctor._tool(None, "ffmpeg", "needed for mux")()
    assert res[0] is False and "setup.ps1" in res[1]

    with mock.patch("grab.doctor.shutil.which", return_value=None), mock.patch("grab.doctor.os.path.exists", return_value=False):
        ok, detail = doctor._deno()
        assert ok is False and "winget install DenoLand.Deno" in detail

    monkeypatch.setattr(updater.sys, "platform", "win32")
    with mock.patch("grab.updater.shutil.which", return_value="C:\\uv\\uv.exe"), mock.patch("grab.updater.os.access", return_value=True):
        assert updater.find_uv() == "C:\\uv\\uv.exe"


def test_win32_open_uses_explorer(monkeypatch):
    monkeypatch.setattr(web.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(web.subprocess, "Popen", lambda cmd: calls.append(cmd))
    web._open("C:\\Users\\test\\Downloads\\video.mp4", reveal=True)
    assert calls == [["explorer", "/select,", "C:\\Users\\test\\Downloads\\video.mp4"]]

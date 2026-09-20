"""Hardcoded, OS-standard config/log directories (issue #17) — config.json
and series.json always live in one per-user config directory, console
logs in a separate per-user logs directory, on both the GUI (gui.py) and
CLI (service_video.py) sides, no longer configurable by the user. Pure
logic tests — no GUI/display needed."""

import sys
from pathlib import Path, PureWindowsPath

import pytest

import gui
import service_video as sv


# -- gui.py ---------------------------------------------------------------

def test_gui_config_dir_on_windows_uses_appdata(monkeypatch):
    # Python 3.14+ refuses to instantiate a real WindowsPath on a non-
    # Windows host even with os.name faked — swap in PureWindowsPath
    # (which never touches the OS) so _config_dir()'s own Path(...) calls
    # can still run, to actually exercise the "nt" branch's logic here.
    monkeypatch.setattr(gui, "Path", PureWindowsPath)
    monkeypatch.setattr(gui.os, "name", "nt")
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    assert gui._config_dir() == PureWindowsPath(r"C:\Users\test\AppData\Roaming") / "subsplash-generator"


def test_gui_config_dir_on_mac_linux_uses_xdg_style_path(monkeypatch):
    monkeypatch.setattr(gui.os, "name", "posix")
    assert gui._config_dir() == Path.home() / ".config" / "subsplash-generator"


def test_gui_logs_dir_on_windows_uses_localappdata(monkeypatch):
    monkeypatch.setattr(gui, "Path", PureWindowsPath)
    monkeypatch.setattr(gui.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert gui._logs_dir() == PureWindowsPath(r"C:\Users\test\AppData\Local") / "subsplash-generator"


def test_gui_logs_dir_on_mac_linux_uses_xdg_style_path(monkeypatch):
    monkeypatch.setattr(gui.os, "name", "posix")
    assert gui._logs_dir() == Path.home() / ".local" / "share" / "subsplash-generator"


def test_gui_config_path_and_series_path_share_the_config_dir():
    assert gui.CONFIG_PATH.parent == gui.CONFIG_DIR
    assert gui.SERIES_PATH.parent == gui.CONFIG_DIR
    assert gui.CONFIG_PATH.name == "config.json"
    assert gui.SERIES_PATH.name == "series.json"


def test_gui_log_path_pattern_uses_the_new_filename_format():
    # No "console_" prefix, no underscore between date and time — see
    # issue #17's exact requested format.
    assert Path(gui.LOG_PATH_PATTERN).parent == gui.LOGS_DIR
    assert Path(gui.LOG_PATH_PATTERN).name == "%Y%m%d%H%M%S.log"


# -- service_video.py -------------------------------------------------------

def test_sv_config_dir_on_windows_uses_appdata(monkeypatch):
    monkeypatch.setattr(sv, "Path", PureWindowsPath)
    monkeypatch.setattr(sv.os, "name", "nt")
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    assert sv._config_dir() == PureWindowsPath(r"C:\Users\test\AppData\Roaming") / "subsplash-generator"


def test_sv_config_dir_on_mac_linux_uses_xdg_style_path(monkeypatch):
    monkeypatch.setattr(sv.os, "name", "posix")
    assert sv._config_dir() == Path.home() / ".config" / "subsplash-generator"


def test_sv_config_path_and_series_path_share_the_config_dir():
    assert sv.CONFIG_PATH.parent == sv.CONFIG_DIR
    assert sv.SERIES_PATH.parent == sv.CONFIG_DIR


def test_watch_and_learn_config_flag_default_to_the_hardcoded_config_path(monkeypatch, tmp_path):
    # Both CONFIG_DIR and CONFIG_PATH monkeypatched into tmp_path — main()
    # unconditionally mkdir()s CONFIG_DIR at startup, and the real
    # ~/.config/subsplash-generator must never be touched by a test run.
    monkeypatch.setattr(sv, "CONFIG_DIR", tmp_path)
    fake_config_path = tmp_path / "config.json"
    monkeypatch.setattr(sv, "CONFIG_PATH", fake_config_path)

    for command in ("watch", "learn"):
        monkeypatch.setattr(sys, "argv", ["service_video.py", command])
        with pytest.raises(SystemExit) as exc_info:
            sv.main()
        # No -c/--config given, and the file doesn't exist — the error
        # message names whatever path args.config actually defaulted to.
        assert str(fake_config_path) in str(exc_info.value.code)

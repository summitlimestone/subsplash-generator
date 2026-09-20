"""ConfigWindow's OK/Cancel/Apply bottom bar (issue #17) — replaces the
old top Config-file path bar (Browse…/Load/Save): OK writes and closes,
Apply writes and stays open, Cancel reverts any unsaved edits back to
what's on disk (confirming first if there actually are any), and the
window's own close button behaves exactly like Cancel. Needs a real (or
Xvfb) display — see conftest.py's `app` fixture, which already redirects
CONFIG_PATH/LOGS_DIR/LOG_PATH_PATTERN into a throwaway tmp_path so these
never touch a real contributor's own config.json or log files."""

import json
import re
from pathlib import Path

import gui


def test_config_is_not_dirty_right_after_startup(app):
    assert app._config_is_dirty() is False


def test_editing_a_field_makes_config_dirty(app):
    app.vars["obs_host"].set("changed-host")
    assert app._config_is_dirty() is True


def test_write_config_clears_dirty_and_writes_the_file(app):
    app.vars["obs_host"].set("changed-host")

    assert app._write_config() is True

    assert app._config_is_dirty() is False
    saved = json.loads(gui.CONFIG_PATH.read_text())
    assert saved["obs"]["host"] == "changed-host"


def test_ok_writes_and_closes_without_a_popup(app, monkeypatch):
    infos = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, msg: infos.append(msg))
    app.vars["obs_host"].set("changed-host")
    app.config_window.deiconify()
    app.update()

    app.config_window._on_ok()

    assert not infos
    assert app.config_window.state() == "withdrawn"
    saved = json.loads(gui.CONFIG_PATH.read_text())
    assert saved["obs"]["host"] == "changed-host"


def test_apply_writes_but_stays_open_without_a_popup(app, monkeypatch):
    infos = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, msg: infos.append(msg))
    app.vars["obs_host"].set("changed-host")
    app.config_window.deiconify()
    app.update()

    app.config_window._on_apply()

    assert not infos
    assert app.config_window.state() != "withdrawn"
    saved = json.loads(gui.CONFIG_PATH.read_text())
    assert saved["obs"]["host"] == "changed-host"


def test_cancel_with_no_changes_closes_silently(app, monkeypatch):
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, msg: asked.append(msg) or True)
    app.config_window.deiconify()
    app.update()

    app.config_window._on_cancel()

    assert not asked
    assert app.config_window.state() == "withdrawn"


def test_cancel_with_changes_confirms_then_reverts_and_closes(app, monkeypatch):
    original_host = app.vars["obs_host"].get()
    app.vars["obs_host"].set("changed-host")
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, msg: asked.append(msg) or True)
    app.config_window.deiconify()
    app.update()

    app.config_window._on_cancel()

    assert asked == ["You have unsaved changes. Cancel anyways?"]
    assert app.vars["obs_host"].get() == original_host
    assert app.config_window.state() == "withdrawn"
    # Cancel never writes — the file on disk is untouched.
    assert "changed-host" not in gui.CONFIG_PATH.read_text()


def test_cancel_declined_leaves_the_window_open_and_the_edit_in_place(app, monkeypatch):
    app.vars["obs_host"].set("changed-host")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, msg: False)
    app.config_window.deiconify()
    app.update()

    app.config_window._on_cancel()

    assert app.vars["obs_host"].get() == "changed-host"
    assert app.config_window.state() != "withdrawn"


def test_window_close_protocol_behaves_like_cancel(app, monkeypatch):
    app.vars["obs_host"].set("changed-host")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, msg: True)
    app.config_window.deiconify()
    app.update()

    handler = app.config_window.protocol("WM_DELETE_WINDOW")
    assert handler  # a handler really is registered
    app.config_window.tk.call(handler)  # invokes it exactly as the OS [X] would

    assert app.config_window.state() == "withdrawn"
    original_host = json.loads(gui.CONFIG_PATH.read_text())["obs"]["host"]
    assert app.vars["obs_host"].get() == original_host


def test_log_file_opens_once_at_startup_with_the_new_filename_pattern(app):
    assert app._log_file is not None
    log_path = Path(app._log_file.name)
    assert log_path.parent == gui.LOGS_DIR
    assert re.fullmatch(r"\d{14}\.log", log_path.name)


def test_default_config_has_no_general_section():
    assert "general" not in gui.default_config()


def test_collect_config_has_no_general_section(app):
    assert "general" not in app.collect_config()

"""Series Manager (issue #12): named intro/outro bundles, selectable from
the Live/Offline tabs instead of typing intro/outro paths by hand every
run. Needs a real (or Xvfb) display — see conftest.py's `app` fixture,
which already redirects SERIES_PATH into a throwaway tmp_path so these
never touch a real contributor's own series.json."""

import json

import gui


def _make_series(app, name, intro="/a_intro.mp4", outro="/a_outro.mp4", intro_duration=5.0, outro_duration=5.0):
    win = gui.SeriesEditWindow(app, series=None)
    win.name_var.set(name)
    win.intro_var.set(intro)
    win.intro_duration_var.set(str(intro_duration))
    win.outro_var.set(outro)
    win.outro_duration_var.set(str(outro_duration))
    win._save()
    return win


def test_starts_empty(app):
    assert app.series == []
    assert app._series_names() == []


def test_new_series_persists_to_disk(app):
    _make_series(app, "Fall 2026", intro="/videos/fall_intro.mp4", outro="/videos/fall_outro.mp4")
    assert app.series == [{
        "name": "Fall 2026", "intro": "/videos/fall_intro.mp4", "intro_duration": 5.0,
        "outro": "/videos/fall_outro.mp4", "outro_duration": 5.0,
    }]
    assert gui.SERIES_PATH.is_file()
    assert json.loads(gui.SERIES_PATH.read_text()) == app.series


def test_selecting_series_populates_live_tab_vars(app):
    _make_series(app, "Fall 2026", intro="/i.mp4", outro="/o.mp4", intro_duration=3.0, outro_duration=4.0)
    app.vars["stitch_series"].set("Fall 2026")
    assert app.vars["stitch_intro"].get() == "/i.mp4"
    assert app.vars["stitch_intro_duration"].get() == "3.0"
    assert app.vars["stitch_outro"].get() == "/o.mp4"
    assert app.vars["stitch_outro_duration"].get() == "4.0"


def test_selecting_series_populates_offline_tab_vars(app):
    _make_series(app, "Fall 2026", intro="/i.mp4", outro="/o.mp4")
    app.vars["st_series"].set("Fall 2026")
    assert app.vars["st_intro"].get() == "/i.mp4"
    assert app.vars["st_outro"].get() == "/o.mp4"


def test_editing_selected_series_propagates_live(app):
    _make_series(app, "Fall 2026", intro="/old_intro.mp4")
    app.vars["stitch_series"].set("Fall 2026")
    assert app.vars["stitch_intro"].get() == "/old_intro.mp4"

    edit = gui.SeriesEditWindow(app, series=app._find_series("Fall 2026"))
    edit.intro_var.set("/new_intro.mp4")
    edit._save()

    assert app.vars["stitch_intro"].get() == "/new_intro.mp4"


def test_renaming_selected_series_carries_the_selection_forward(app):
    _make_series(app, "Fall 2026")
    app.vars["stitch_series"].set("Fall 2026")
    app.vars["st_series"].set("Fall 2026")

    edit = gui.SeriesEditWindow(app, series=app._find_series("Fall 2026"))
    edit.name_var.set("Fall 2026 Series")
    edit._save()

    assert app.vars["stitch_series"].get() == "Fall 2026 Series"
    assert app.vars["st_series"].get() == "Fall 2026 Series"
    assert app._series_names() == ["Fall 2026 Series"]


def test_duplicate_creates_distinctly_named_copy(app):
    _make_series(app, "Fall 2026", intro="/i.mp4")
    app.series_tree.selection_set("Fall 2026")
    app._duplicate_series()
    # _duplicate_series() opens an edit dialog on the copy — close it
    # without changing anything, same as a user just hitting Cancel.
    dup_windows = [w for w in app.winfo_children() if isinstance(w, gui.SeriesEditWindow)]
    assert len(dup_windows) == 1
    dup_windows[0].destroy()

    assert app._series_names() == ["Fall 2026", "Fall 2026 (copy)"]
    assert app._find_series("Fall 2026 (copy)")["intro"] == "/i.mp4"


def test_delete_removes_only_the_targeted_series(app):
    _make_series(app, "A")
    _make_series(app, "B")
    app.series_tree.selection_set("A")
    _confirm_yes(app)
    assert app._series_names() == ["B"]


def test_deleting_the_selected_series_clears_dropdown_and_vars(app):
    _make_series(app, "A", intro="/i.mp4")
    app.vars["stitch_series"].set("A")
    app.vars["st_series"].set("A")
    assert app.vars["stitch_intro"].get() == "/i.mp4"

    app.series_tree.selection_set("A")
    _confirm_yes(app)

    assert app._series_names() == []
    assert app.vars["stitch_series"].get() == ""
    assert app.vars["stitch_intro"].get() == ""
    assert app.vars["st_series"].get() == ""
    assert app.vars["st_intro"].get() == ""


def test_deleting_an_unselected_series_leaves_other_selections_alone(app):
    _make_series(app, "A", intro="/a.mp4")
    _make_series(app, "B", intro="/b.mp4")
    app.vars["stitch_series"].set("A")
    app.series_tree.selection_set("B")
    _confirm_yes(app)
    assert app._series_names() == ["A"]
    assert app.vars["stitch_series"].get() == "A"
    assert app.vars["stitch_intro"].get() == "/a.mp4"


def _confirm_yes(app):
    """Drives App._delete_series() through a real messagebox.askyesno
    confirmation — monkeypatched to "yes" here since there's no user to
    click it in a headless test."""
    import tkinter.messagebox as messagebox

    original = messagebox.askyesno
    messagebox.askyesno = lambda *a, **k: True
    try:
        app._delete_series()
    finally:
        messagebox.askyesno = original


# -- Validation -------------------------------------------------------------

def _expect_error(app, build):
    import tkinter.messagebox as messagebox

    errors = []
    original = messagebox.showerror
    messagebox.showerror = lambda title, msg: errors.append(msg)
    try:
        win = gui.SeriesEditWindow(app, series=None)
        build(win)
        win._save()
    finally:
        messagebox.showerror = original
    return errors


def test_duplicate_name_rejected(app):
    _make_series(app, "A")
    errors = _expect_error(app, lambda w: (w.name_var.set("A"), w.intro_var.set("/x"), w.outro_var.set("/y")))
    assert errors and "already exists" in errors[0]
    assert app._series_names() == ["A"]


def test_blank_name_rejected(app):
    errors = _expect_error(app, lambda w: None)
    assert errors and "Name is required" in errors[0]


def test_blank_intro_rejected(app):
    errors = _expect_error(app, lambda w: (w.name_var.set("C"), w.outro_var.set("/y")))
    assert errors and "required" in errors[0]
    assert app._series_names() == []


# -- config.json / render-state round-tripping ------------------------------

def test_config_json_round_trip_restores_series_selection(app, tmp_path):
    _make_series(app, "B", intro="/b_intro.mp4")
    app.vars["stitch_series"].set("B")

    cfg = app.collect_config()
    assert cfg["stitch"]["series"] == "B"
    assert cfg["stitch"]["intro"] == "/b_intro.mp4"

    config_path = tmp_path / "roundtrip_config.json"
    config_path.write_text(json.dumps(cfg))

    app.vars["stitch_series"].set("")
    app.vars["stitch_intro"].set("")
    app.load_config(str(config_path))

    assert app.vars["stitch_series"].get() == "B"
    assert app.vars["stitch_intro"].get() == "/b_intro.mp4"


def test_config_json_falls_back_when_saved_series_no_longer_exists(app, tmp_path):
    _make_series(app, "B", intro="/b_intro.mp4")
    app.vars["stitch_series"].set("B")
    cfg = app.collect_config()
    config_path = tmp_path / "roundtrip_config.json"
    config_path.write_text(json.dumps(cfg))

    app.series_tree.selection_set("B")
    _confirm_yes(app)
    app.load_config(str(config_path))

    assert app.vars["stitch_series"].get() == ""
    assert app.vars["stitch_intro"].get() == "/b_intro.mp4", (
        "should fall back to the config's own raw intro value once the named series is gone"
    )


def test_render_state_json_round_trip_restores_offline_series_selection(app, tmp_path):
    _make_series(app, "A", intro="/a_intro.mp4")
    app.vars["st_series"].set("A")
    app.vars["st_main"].set("/recording.mp4")
    app.vars["st_start"].set("00:00:01.000")
    app.vars["st_end"].set("00:00:05.000")

    f = app._collect_offline_fields(error_title="test")
    assert f is not None and f["series"] == "A"
    render_state = app._build_render_state(f, "trimmed.mp4", "state.json")
    assert render_state["stitch"]["series"] == "A"
    assert render_state["stitch"]["intro"] == "/a_intro.mp4"

    state_path = tmp_path / "roundtrip_state.json"
    state_path.write_text(json.dumps(render_state))

    app.vars["st_series"].set("")
    app.vars["st_intro"].set("")
    app._load_render_state_json(str(state_path))

    assert app.vars["st_series"].get() == "A"
    assert app.vars["st_intro"].get() == "/a_intro.mp4"


# -- Actually used by Stitch --------------------------------------------

def test_run_stitch_uses_the_selected_series(app, monkeypatch):
    _make_series(app, "Fall 2026 Series", intro="/videos/fall_intro.mp4", intro_duration=3.0,
                 outro="/videos/fall_outro.mp4", outro_duration=4.0)
    app.vars["st_series"].set("Fall 2026 Series")
    app.vars["st_main"].set("/videos/body_trimmed.mp4")
    app.vars["st_output"].set("final.mp4")

    captured = {}
    monkeypatch.setattr(app, "_start", lambda command_name, args: captured.setdefault("args", args))
    app._run_stitch()

    args = captured["args"]
    assert args[0:4] == ["stitch", "/videos/fall_intro.mp4", "/videos/body_trimmed.mp4", "/videos/fall_outro.mp4"]
    assert args[args.index("--intro-duration") + 1] == "3.0"
    assert args[args.index("--outro-duration") + 1] == "4.0"

"""Series Manager (issue #12): named intro/outro/transition bundles,
selectable from the Live/Offline tabs instead of typing intro/outro
paths by hand every run. Needs a real (or Xvfb) display — see
conftest.py's `app` fixture, which already redirects SERIES_PATH into a
throwaway tmp_path so these never touch a real contributor's own
series.json.

series/intro/outro/intro_duration/outro_duration/transition/
transition_duration no longer exist in config.json or render-state files
— only a series *name* does (resolved server-side by
service_video.py's resolve_series()) — so these tests check the GUI's
own vars/config/render-state shapes reflect that, not literal
intro/outro values living in config.json or a render-state file."""

import json

import gui


def _make_series(app, name, intro="/a_intro.mp4", outro="/a_outro.mp4", intro_duration=5.0, outro_duration=5.0,
                  transition="fade", transition_duration=1.0, hidden=False):
    win = gui.SeriesEditWindow(app, series=None)
    win.name_var.set(name)
    win.intro_var.set(intro)
    win.intro_duration_var.set(str(intro_duration))
    win.outro_var.set(outro)
    win.outro_duration_var.set(str(outro_duration))
    win.transition_var.set(transition)
    win.transition_duration_var.set(str(transition_duration))
    win.hidden_var.set(hidden)
    win._save()
    return win


def test_starts_empty(app):
    assert app.series == []
    assert app._series_names() == []


def test_new_series_persists_to_disk(app):
    _make_series(app, "Fall 2026", intro="/videos/fall_intro.mp4", outro="/videos/fall_outro.mp4",
                 transition="wipeleft", transition_duration=2.0)
    assert app.series == [{
        "name": "Fall 2026", "intro": "/videos/fall_intro.mp4", "intro_duration": 5.0,
        "outro": "/videos/fall_outro.mp4", "outro_duration": 5.0,
        "transition": "wipeleft", "transition_duration": 2.0, "hidden": False,
    }]
    assert gui.SERIES_PATH.is_file()
    assert json.loads(gui.SERIES_PATH.read_text()) == app.series


def test_selecting_series_on_live_tab_only_tracks_last_valid(app):
    # The Live tab's own Series dropdown wires no intro/outro/transition
    # target vars any more (see _build_live_tab()) — resolving those now
    # happens server-side (service_video.py's resolve_series()), not in
    # the GUI. Selecting a series here should still be tracked (for
    # delete-clearing/rename-carry-forward — see _wire_series_selector())
    # without creating any stitch_intro-style var.
    _make_series(app, "Fall 2026", intro="/i.mp4", outro="/o.mp4")
    app.vars["stitch_series"].set("Fall 2026")
    assert app._series_last_valid["stitch_series"] == "Fall 2026"
    assert "stitch_intro" not in app.vars
    assert "stitch_outro" not in app.vars


def test_selecting_series_populates_offline_tab_vars(app):
    _make_series(app, "Fall 2026", intro="/i.mp4", outro="/o.mp4", transition="dissolve", transition_duration=2.5)
    app.vars["st_series"].set("Fall 2026")
    assert app.vars["st_intro"].get() == "/i.mp4"
    assert app.vars["st_outro"].get() == "/o.mp4"
    assert app.vars["st_transition"].get() == "dissolve"
    assert app.vars["st_duration"].get() == "2.5"


def test_editing_selected_series_propagates_on_offline_tab(app):
    _make_series(app, "Fall 2026", intro="/old_intro.mp4")
    app.vars["st_series"].set("Fall 2026")
    assert app.vars["st_intro"].get() == "/old_intro.mp4"

    edit = gui.SeriesEditWindow(app, series=app._find_series("Fall 2026"))
    edit.intro_var.set("/new_intro.mp4")
    edit._save()

    assert app.vars["st_intro"].get() == "/new_intro.mp4"


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


def test_deleting_the_selected_series_clears_dropdowns(app):
    _make_series(app, "A", intro="/i.mp4")
    app.vars["stitch_series"].set("A")
    app.vars["st_series"].set("A")
    assert app.vars["st_intro"].get() == "/i.mp4"

    app.series_tree.selection_set("A")
    _confirm_yes(app)

    assert app._series_names() == []
    assert app.vars["stitch_series"].get() == ""
    assert app.vars["st_series"].get() == ""
    assert app.vars["st_intro"].get() == ""


def test_deleting_an_unselected_series_leaves_other_selections_alone(app):
    _make_series(app, "A", intro="/a.mp4")
    _make_series(app, "B", intro="/b.mp4")
    app.vars["st_series"].set("A")
    app.series_tree.selection_set("B")
    _confirm_yes(app)
    assert app._series_names() == ["A"]
    assert app.vars["st_series"].get() == "A"
    assert app.vars["st_intro"].get() == "/a.mp4"


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


# -- config.json: no series/intro/outro/transition data at all --------------

def test_collect_config_carries_no_series_or_literal_fields(app):
    _make_series(app, "B", intro="/b_intro.mp4")
    app.vars["st_series"].set("B")  # only the Offline tab has intro/outro vars to leak from
    cfg = app.collect_config()
    for key in ("series", "intro", "outro", "intro_duration", "outro_duration", "transition", "transition_duration"):
        assert key not in cfg["stitch"], f"config.json's stitch section must not carry {key!r}"


def test_load_config_does_not_touch_series_selection(app):
    _make_series(app, "B")
    app.vars["stitch_series"].set("B")
    cfg = app.collect_config()
    gui.CONFIG_PATH.write_text(json.dumps(cfg))

    app.vars["stitch_series"].set("")
    app.load_config()

    # Nothing to restore — config.json never carried it — so the
    # dropdown simply stays however it already was (blank here).
    assert app.vars["stitch_series"].get() == ""


# -- render-state files: series name only, never literal fields -------------

def test_render_state_json_only_carries_the_series_name(app, tmp_path):
    _make_series(app, "A", intro="/a_intro.mp4")
    app.vars["st_series"].set("A")
    app.vars["st_main"].set("/recording.mp4")
    app.vars["st_start"].set("00:00:01.000")
    app.vars["st_end"].set("00:00:05.000")

    f = app._collect_offline_fields(error_title="test")
    assert f is not None and f["series"] == "A"
    render_state = app._build_render_state(f, "trimmed.mp4", "state.json")
    assert render_state["stitch"]["series"] == "A"
    for key in ("intro", "outro", "intro_duration", "outro_duration", "transition", "transition_duration"):
        assert key not in render_state["stitch"], f"render-state's stitch section must not carry {key!r}"

    state_path = tmp_path / "roundtrip_state.json"
    state_path.write_text(json.dumps(render_state))

    app.vars["st_series"].set("")
    app.vars["st_intro"].set("")
    app._load_render_state_json(str(state_path))

    assert app.vars["st_series"].get() == "A"
    assert app.vars["st_intro"].get() == "/a_intro.mp4"


def test_load_render_state_json_clears_offline_fields_when_series_gone(app, tmp_path):
    _make_series(app, "A", intro="/a_intro.mp4")
    app.vars["st_series"].set("A")
    app.vars["st_main"].set("/recording.mp4")
    app.vars["st_start"].set("00:00:01.000")
    app.vars["st_end"].set("00:00:05.000")
    f = app._collect_offline_fields(error_title="test")
    render_state = app._build_render_state(f, "trimmed.mp4", "state.json")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(render_state))

    app.series_tree.selection_set("A")
    _confirm_yes(app)
    app._load_render_state_json(str(state_path))

    # The saved series name no longer resolves to anything — there's no
    # literal intro/outro to fall back to any more (unlike before this
    # change), so the Offline tab's fields are cleared instead.
    assert app.vars["st_series"].get() == ""
    assert app.vars["st_intro"].get() == ""
    assert app.vars["st_outro"].get() == ""


# -- Actually used by Stitch --------------------------------------------

def test_run_stitch_uses_the_selected_series(app, monkeypatch):
    _make_series(app, "Fall 2026 Series", intro="/videos/fall_intro.mp4", intro_duration=3.0,
                 outro="/videos/fall_outro.mp4", outro_duration=4.0,
                 transition="wipeleft", transition_duration=2.0)
    app.vars["st_series"].set("Fall 2026 Series")
    # Stitch always reads Trimmed clip (see #8), never Main clip directly.
    app.vars["st_trimmed"].set("/videos/body_trimmed.mp4")
    app.vars["st_output"].set("final.mp4")

    captured = {}
    monkeypatch.setattr(app, "_start", lambda command_name, args: captured.setdefault("args", args))
    app._run_stitch()

    args = captured["args"]
    assert args[0:4] == ["stitch", "/videos/fall_intro.mp4", "/videos/body_trimmed.mp4", "/videos/fall_outro.mp4"]
    assert args[args.index("-t") + 1] == "wipeleft"
    assert args[args.index("-d") + 1] == "2.0"
    assert args[args.index("--intro-duration") + 1] == "3.0"
    assert args[args.index("--outro-duration") + 1] == "4.0"


def test_run_stitch_fails_with_no_series_selected(app, monkeypatch):
    app.vars["st_series"].set("")
    app.vars["st_trimmed"].set("/videos/body_trimmed.mp4")

    started = []
    monkeypatch.setattr(app, "_start", lambda *a: started.append(a))
    errors = []
    import tkinter.messagebox as messagebox
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg: errors.append(msg))

    app._run_stitch()

    assert not started
    assert errors and "series" in errors[0].lower()


def test_stitch_live_fails_with_no_series_selected(app, monkeypatch):
    app.vars["stitch_series"].set("")
    sent = []
    monkeypatch.setattr(app.runner, "send_line", lambda line: sent.append(line))
    monkeypatch.setattr(app.runner, "running", lambda: True)
    errors = []
    import tkinter.messagebox as messagebox
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg: errors.append(msg))

    app._stitch_live()

    assert not sent
    assert errors and "series" in errors[0].lower()


def test_stitch_live_sends_the_currently_selected_series(app, monkeypatch):
    _make_series(app, "Fall 2026 Series")
    app.vars["stitch_series"].set("Fall 2026 Series")
    sent = []
    monkeypatch.setattr(app.runner, "send_line", lambda line: sent.append(line))
    monkeypatch.setattr(app.runner, "running", lambda: True)

    app._stitch_live()

    assert sent == ["stitch Fall 2026 Series"]


def test_stitch_live_post_exit_uses_the_live_tabs_own_current_selection(app, monkeypatch, tmp_path):
    """Regression: watch() exits after a live Trim, with no series ever
    selected live — the render-state file's stitch.series (and so
    Offline's own st_series, restored from it — see
    _load_render_state_json()) stays blank. Selecting a series *only* on
    the Live tab's own dropdown and clicking Stitch used to still fail
    (post-exit Stitch called _run_stitch(), which checked Offline's own
    st_series — never touched — instead of the Live tab's stitch_series
    the user actually just set)."""
    _make_series(app, "Fall Series", intro="/videos/intro.mp4", outro="/videos/outro.mp4")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "recording_path": "/rec.mp4", "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trimmed_path": "/videos/body_trimmed.mp4",
        "trim": {"output": "/videos/body_trimmed.mp4"},
        "stitch": {"auto": True, "series": "", "output": "final.mp4"},
    }))
    app.render_state_var.set(str(state_path))
    app._load_render_state_json(str(state_path))
    assert app.vars["st_series"].get() == ""  # confirms the bug's precondition

    app.vars["stitch_series"].set("Fall Series")
    app.vars["stitch_output"].set("live_final.mp4")
    monkeypatch.setattr(app.runner, "running", lambda: False)
    captured = {}
    monkeypatch.setattr(app, "_start", lambda name, args: captured.setdefault("args", args))
    errors = []
    import tkinter.messagebox as messagebox
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg: errors.append(msg))

    app._stitch_live()

    assert errors == []
    args = captured["args"]
    assert args[0:4] == ["stitch", "/videos/intro.mp4", "/videos/body_trimmed.mp4", "/videos/outro.mp4"]
    assert args[args.index("-o") + 1] == "live_final.mp4"
    assert app.vars["st_series"].get() == "Fall Series"
    saved = json.loads(state_path.read_text())
    assert saved["stitch"]["series"] == "Fall Series"
    assert saved["stitch"]["output"] == "live_final.mp4"


# -- Fuzzy search -------------------------------------------------------

def test_fuzzy_match_is_subsequence_not_substring():
    names = ["Fall 2026 Series", "Winter Youth Camp", "Spring Retreat"]
    # "f26s" only appears as an in-order subsequence of "Fall 2026 Series",
    # never as one contiguous substring of any of them.
    assert gui.fuzzy_match_series("f26s", names) == ["Fall 2026 Series"]


def test_fuzzy_match_ranks_tighter_earlier_matches_first():
    names = ["Winter Youth Camp", "Fall Youth Camp"]
    # Both contain "youth camp" as a contiguous run, but "Fall Youth Camp"'s
    # match starts earlier in the string, so it should sort first.
    assert gui.fuzzy_match_series("youth camp", names) == ["Fall Youth Camp", "Winter Youth Camp"]


def test_fuzzy_match_blank_query_returns_everything_unfiltered():
    names = ["B", "A", "C"]
    assert gui.fuzzy_match_series("", names) == names
    assert gui.fuzzy_match_series("   ", names) == names


def test_fuzzy_match_excludes_names_missing_a_query_character():
    assert gui.fuzzy_match_series("xyz", ["Fall 2026 Series"]) == []


def test_fuzzy_match_is_case_insensitive():
    assert gui.fuzzy_match_series("FALL", ["Fall 2026 Series"]) == ["Fall 2026 Series"]


def _focus_and_type(combo, text):
    """Synthetic KeyRelease events only actually dispatch to a widget's
    instance bindings once its toplevel is deiconified (the `app` fixture
    withdraws it) and real-focused, with the event loop run at least
    once (confirmed directly: without all three, event_generate silently
    reaches nothing) — so every test that drives the searchable
    combobox through simulated typing needs this, not just insert()."""
    combo.winfo_toplevel().deiconify()
    combo.focus_force()
    combo.update()
    combo.delete(0, "end")
    combo.insert(0, text)
    combo.event_generate("<KeyRelease>", keysym=text[-1] if text else "BackSpace")
    combo.update()


def test_typing_in_series_combobox_filters_its_values(app):
    _make_series(app, "Fall 2026 Series")
    _make_series(app, "Winter Youth Camp")
    combo = app._series_comboboxes[0]  # Live tab's "stitch_series" combobox
    _focus_and_type(combo, "f26s")
    assert combo.cget("values") == ("Fall 2026 Series",)


def test_committing_unmatched_text_snaps_back_to_last_valid_selection(app):
    _make_series(app, "Fall 2026 Series")
    app.vars["stitch_series"].set("Fall 2026 Series")
    combo = app._series_comboboxes[0]
    _focus_and_type(combo, "not a real series")
    combo.event_generate("<FocusOut>")
    combo.update()
    assert app.vars["stitch_series"].get() == "Fall 2026 Series"


def test_committing_text_narrowed_to_one_match_selects_it(app):
    _make_series(app, "Fall 2026 Series")
    _make_series(app, "Winter Youth Camp")
    combo = app._series_comboboxes[0]
    _focus_and_type(combo, "f26s")
    combo.event_generate("<FocusOut>")
    combo.update()
    assert app.vars["stitch_series"].get() == "Fall 2026 Series"


# -- Hide from dropdowns -------------------------------------------------

def test_hidden_series_excluded_from_series_names_by_default(app):
    _make_series(app, "Visible")
    _make_series(app, "Hidden One", hidden=True)
    assert app._series_names() == ["Visible"]
    assert app._series_names(include_hidden=True) == ["Visible", "Hidden One"]


def test_hidden_series_excluded_from_combobox_values(app):
    _make_series(app, "Visible")
    _make_series(app, "Hidden One", hidden=True)
    combo = app._series_comboboxes[0]
    assert combo.cget("values") == ("Visible",)


def test_hidden_series_still_resolvable_and_editable(app):
    _make_series(app, "Hidden One", intro="/h.mp4", hidden=True)
    assert app._find_series("Hidden One")["intro"] == "/h.mp4"
    edit = gui.SeriesEditWindow(app, series=app._find_series("Hidden One"))
    assert edit.hidden_var.get() is True
    edit.intro_var.set("/h2.mp4")
    edit._save()
    assert app._find_series("Hidden One")["intro"] == "/h2.mp4"
    assert app._find_series("Hidden One")["hidden"] is True


def test_toggle_series_hidden(app):
    _make_series(app, "A")
    app.series_tree.selection_set("A")
    app._toggle_series_hidden()
    assert app._find_series("A")["hidden"] is True
    assert app._series_names() == []
    app.series_tree.selection_set("A")
    app._toggle_series_hidden()
    assert app._find_series("A")["hidden"] is False
    assert app._series_names() == ["A"]


def test_hiding_the_currently_selected_series_does_not_clear_its_selection(app):
    """Hiding only affects what future dropdown picks *offer* — a tab
    that already has the now-hidden series selected should keep using it
    (e.g. an old render-state file that named it), not silently lose its
    resolved intro/outro."""
    _make_series(app, "A", intro="/a.mp4")
    app.vars["st_series"].set("A")
    app.series_tree.selection_set("A")
    app._toggle_series_hidden()
    assert app.vars["st_series"].get() == "A"
    assert app.vars["st_intro"].get() == "/a.mp4"


def test_series_tree_sorts_visible_first_then_alphabetically(app):
    _make_series(app, "Zebra")
    _make_series(app, "Archived Old One", hidden=True)
    _make_series(app, "Apple")
    _make_series(app, "Archived Older Two", hidden=True)
    assert list(app.series_tree.get_children()) == [
        "Apple", "Zebra", "Archived Old One", "Archived Older Two",
    ]

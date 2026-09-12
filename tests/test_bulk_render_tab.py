"""The Bulk Render tab (gui.py): loads a JSON array of render-state
dicts into a Treeview and runs service_video.py's 'bulk-render'
subcommand against it (Trim/Stitch/Full Render). GUI-side wiring only —
see tests/test_bulk_render.py for the actual trim/stitch behavior these
buttons kick off."""

import json

import gui


def _write_states(tmp_path, n=2, trimmed=None):
    states = [
        {
            "recording_path": f"/rec{i}.mp4", "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
            "trimmed_path": trimmed,
            "trim": {"output": f"/trim{i}.mp4"},
            "stitch": {"auto": True, "intro": "/intro.mp4", "outro": "/outro.mp4", "output": f"/final{i}.mp4"},
        }
        for i in range(n)
    ]
    path = tmp_path / "states.json"
    path.write_text(json.dumps(states))
    return path


def test_buttons_start_disabled_with_no_file_loaded(app):
    assert str(app.bulk_trim_btn["state"]) == "disabled"
    assert str(app.bulk_stitch_btn["state"]) == "disabled"
    assert str(app.bulk_full_btn["state"]) == "disabled"


def test_loading_a_valid_file_populates_the_tree_and_enables_buttons(app, tmp_path):
    path = _write_states(tmp_path, n=2)
    app.vars["bulk_states_path"].set(str(path))

    assert len(app.bulk_states) == 2
    rows = [app.bulk_render_tree.item(iid, "values") for iid in app.bulk_render_tree.get_children()]
    assert rows == [
        ("1", "/rec0.mp4", "", "/final0.mp4"),
        ("2", "/rec1.mp4", "", "/final1.mp4"),
    ]
    assert str(app.bulk_trim_btn["state"]) == "normal"
    assert str(app.bulk_stitch_btn["state"]) == "normal"
    assert str(app.bulk_full_btn["state"]) == "normal"


def test_loading_shows_trimmed_path_when_present(app, tmp_path):
    path = _write_states(tmp_path, n=1, trimmed="/already_trimmed.mp4")
    app.vars["bulk_states_path"].set(str(path))
    rows = [app.bulk_render_tree.item(iid, "values") for iid in app.bulk_render_tree.get_children()]
    assert rows == [("1", "/rec0.mp4", "/already_trimmed.mp4", "/final0.mp4")]


def test_loading_a_non_array_json_leaves_the_tree_empty(app, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}))
    app.vars["bulk_states_path"].set(str(path))

    assert app.bulk_states == []
    assert app.bulk_render_tree.get_children() == ()
    assert str(app.bulk_trim_btn["state"]) == "disabled"


def test_loading_malformed_json_leaves_the_tree_empty(app, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    app.vars["bulk_states_path"].set(str(path))

    assert app.bulk_states == []
    assert str(app.bulk_trim_btn["state"]) == "disabled"


def test_clearing_the_path_clears_the_tree(app, tmp_path):
    path = _write_states(tmp_path, n=1)
    app.vars["bulk_states_path"].set(str(path))
    assert app.bulk_states

    app.vars["bulk_states_path"].set("")
    assert app.bulk_states == []
    assert app.bulk_render_tree.get_children() == ()
    assert str(app.bulk_trim_btn["state"]) == "disabled"


def test_run_bulk_render_invokes_the_cli_with_the_right_mode(app, tmp_path, monkeypatch):
    path = _write_states(tmp_path, n=1)
    app.vars["bulk_states_path"].set(str(path))

    captured = {}
    monkeypatch.setattr(app, "_start", lambda name, args: captured.setdefault("call", (name, args)))

    app.bulk_full_btn.invoke()

    name, args = captured["call"]
    assert name == "bulk_full"
    assert args == ["bulk-render", str(path), "--mode", "full"]


def test_run_bulk_render_refuses_with_nothing_loaded(app, monkeypatch):
    started = []
    monkeypatch.setattr(app, "_start", lambda name, args: started.append((name, args)))
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))
    # Buttons are disabled with nothing loaded, but the handler itself
    # (invoked directly here, bypassing the disabled button) should still
    # refuse rather than launch a subprocess against an empty/blank path.
    app._run_bulk_render("trim")
    assert started == []
    assert errors

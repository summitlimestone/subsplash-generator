"""The Bulk Render tab (gui.py): an in-GUI-editable list of render-state
dicts (Import/Export, double-click to edit via BulkEntryEditWindow,
drag-to-reorder, "+ Add entry…") that runs service_video.py's
'bulk-render' subcommand against a temp snapshot of itself
(Trim/Stitch/Full Render). GUI-side wiring only — see
tests/test_bulk_render.py for the actual trim/stitch behavior these
buttons kick off, and for bulk_render()'s own status-line output."""

import json
from pathlib import Path

import gui


class FakeEvent:
    """A minimal stand-in for a real Tk event — every handler under test
    here only ever reads .y (to find which Treeview row it landed on)."""
    def __init__(self, y):
        self.y = y


def _make_states(n=2, trimmed=None):
    return [
        {
            "recording_path": f"/rec{i}.mp4", "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
            "trimmed_path": trimmed,
            "trim": {"output": f"/trim{i}.mp4"},
            "stitch": {"auto": True, "series": "Some Series", "output": f"/final{i}.mp4"},
        }
        for i in range(n)
    ]


def _load(app, states):
    app.bulk_states = states
    app._refresh_bulk_render_tree()


def _row_y(app, iid):
    bbox = app.bulk_render_tree.bbox(iid)
    assert bbox, f"row {iid!r} isn't visible/laid out yet — call app.update() first"
    return bbox[1] + bbox[3] // 2


# -- Basic tree/button state ----------------------------------------------

def test_buttons_start_disabled_with_nothing_loaded(app):
    assert str(app.bulk_trim_btn["state"]) == "disabled"
    assert str(app.bulk_stitch_btn["state"]) == "disabled"
    assert str(app.bulk_full_btn["state"]) == "disabled"


def test_loading_states_populates_the_tree_and_enables_buttons(app):
    _load(app, _make_states(n=2))

    rows = [app.bulk_render_tree.item(iid, "values") for iid in app.bulk_render_tree.get_children()]
    assert rows == [
        ("1", "idle", "/rec0.mp4", "/final0.mp4"),
        ("2", "idle", "/rec1.mp4", "/final1.mp4"),
    ]
    assert str(app.bulk_trim_btn["state"]) == "normal"
    assert str(app.bulk_stitch_btn["state"]) == "normal"
    assert str(app.bulk_full_btn["state"]) == "normal"


def test_tree_height_matches_row_count(app):
    _load(app, _make_states(n=5))
    assert int(app.bulk_render_tree.cget("height")) == 5
    _load(app, _make_states(n=1))
    assert int(app.bulk_render_tree.cget("height")) == 1


def test_tree_has_no_trimmed_clip_column(app):
    assert app.bulk_render_tree.cget("columns") == ("index", "status", "recording", "output")


def test_headings_are_left_aligned(app):
    for col in app.bulk_render_tree.cget("columns"):
        assert str(app.bulk_render_tree.heading(col, "anchor")) == "w"


# -- Import/Export ----------------------------------------------------------

def test_import_populates_the_list(app, tmp_path, monkeypatch):
    path = tmp_path / "states.json"
    path.write_text(json.dumps(_make_states(n=2)))
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **_k: str(path))

    app._import_bulk_states()

    assert len(app.bulk_states) == 2
    assert app._bulk_states_path == str(path)


def test_import_cancelled_dialog_leaves_state_alone(app, monkeypatch):
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **_k: "")
    app._import_bulk_states()
    assert app.bulk_states == []


def test_import_rejects_non_array_json(app, tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}))
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **_k: str(path))
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))

    app._import_bulk_states()

    assert app.bulk_states == []
    assert errors


def test_export_writes_the_current_in_memory_list(app, tmp_path, monkeypatch):
    _load(app, _make_states(n=2))
    out_path = tmp_path / "exported.json"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **_k: str(out_path))

    app._export_bulk_states()

    saved = json.loads(out_path.read_text())
    assert len(saved) == 2
    assert app._bulk_states_path == str(out_path)


def test_export_refuses_with_nothing_to_export(app, monkeypatch):
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))
    saved_dialog_called = []
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **_k: saved_dialog_called.append(1))

    app._export_bulk_states()

    assert errors
    assert not saved_dialog_called


# -- Add / double-click edit -------------------------------------------------

def test_add_entry_opens_a_blank_editor_and_appends_on_save(app):
    app._add_bulk_entry()
    editors = [w for w in app.winfo_children() if isinstance(w, gui.BulkEntryEditWindow)]
    assert len(editors) == 1
    editor = editors[0]
    assert editor.index is None

    editor.recording_var.set("/new_rec.mp4")
    editor.output_var.set("/new_final.mp4")
    editor._save()

    assert len(app.bulk_states) == 1
    assert app.bulk_states[0]["recording_path"] == "/new_rec.mp4"
    assert app.bulk_states[0]["stitch"]["output"] == "/new_final.mp4"


def test_double_click_opens_editor_for_the_right_row(app):
    _load(app, _make_states(n=3))
    app.update()

    app._on_bulk_render_double_click(FakeEvent(_row_y(app, "1")))

    editors = [w for w in app.winfo_children() if isinstance(w, gui.BulkEntryEditWindow)]
    assert len(editors) == 1
    assert editors[0].index == 1
    assert editors[0].recording_var.get() == "/rec1.mp4"


def test_editing_and_saving_updates_the_entry_and_tree(app):
    _load(app, _make_states(n=2))
    app.update()
    app._on_bulk_render_double_click(FakeEvent(_row_y(app, "0")))
    editor = next(w for w in app.winfo_children() if isinstance(w, gui.BulkEntryEditWindow))

    editor.output_var.set("/edited_final.mp4")
    editor._save()

    assert app.bulk_states[0]["stitch"]["output"] == "/edited_final.mp4"
    assert app.bulk_render_tree.item("0", "values")[3] == "/edited_final.mp4"


def test_double_click_outside_any_row_does_nothing(app):
    _load(app, _make_states(n=1))
    app.update()
    app._on_bulk_render_double_click(FakeEvent(-100))
    assert not [w for w in app.winfo_children() if isinstance(w, gui.BulkEntryEditWindow)]


# -- Drag-to-reorder ----------------------------------------------------------

def test_dragging_a_row_reorders_bulk_states(app):
    _load(app, _make_states(n=3))
    app.update()

    app._on_bulk_render_drag_start(FakeEvent(_row_y(app, "0")))
    app._on_bulk_render_drag_motion(FakeEvent(_row_y(app, "2")))
    app._on_bulk_render_drag_end(FakeEvent(_row_y(app, "2")))

    assert [s["recording_path"] for s in app.bulk_states] == ["/rec1.mp4", "/rec2.mp4", "/rec0.mp4"]
    # iids realign with the new list positions after a drag.
    assert list(app.bulk_render_tree.get_children()) == ["0", "1", "2"]
    assert app.bulk_render_tree.item("0", "values")[2] == "/rec1.mp4"


def test_a_plain_click_with_no_motion_does_not_reorder(app):
    _load(app, _make_states(n=2))
    app.update()
    original = [s["recording_path"] for s in app.bulk_states]

    app._on_bulk_render_drag_start(FakeEvent(_row_y(app, "0")))
    app._on_bulk_render_drag_end(FakeEvent(_row_y(app, "0")))

    assert [s["recording_path"] for s in app.bulk_states] == original


# -- Running --------------------------------------------------------------

def test_run_bulk_render_invokes_the_cli_against_a_temp_snapshot(app, monkeypatch):
    _load(app, _make_states(n=1))
    captured = {}
    monkeypatch.setattr(app, "_start", lambda name, args: captured.setdefault("call", (name, args)))

    app.bulk_full_btn.invoke()

    name, args = captured["call"]
    assert name == "bulk_full"
    assert args[0] == "bulk-render"
    assert args[2:] == ["--mode", "full"]
    temp_path = args[1]
    assert json.loads(Path(temp_path).read_text()) == app.bulk_states
    assert app._bulk_run_temp_path == temp_path


def test_run_bulk_render_resets_every_row_to_idle_first(app, monkeypatch):
    _load(app, _make_states(n=2))
    app._handle_bulk_render_line("[bulk-render] status entry=1 state=stitched")
    assert app.bulk_render_tree.item("0", "values")[1] == "stitched"
    monkeypatch.setattr(app, "_start", lambda *a: None)

    app.bulk_full_btn.invoke()

    assert app.bulk_render_tree.item("0", "values")[1] == "idle"
    assert app.bulk_render_tree.item("1", "values")[1] == "idle"


def test_run_bulk_render_refuses_with_nothing_loaded(app, monkeypatch):
    started = []
    monkeypatch.setattr(app, "_start", lambda name, args: started.append((name, args)))
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))

    app._run_bulk_render("trim")

    assert started == []
    assert errors


# -- Live status updates -----------------------------------------------------

def test_handle_bulk_render_line_updates_the_right_row(app):
    _load(app, _make_states(n=2))

    app._handle_bulk_render_line("[bulk-render] status entry=2 state=trimming")

    assert app.bulk_render_tree.item("0", "values")[1] == "idle"
    values = app.bulk_render_tree.item("1", "values")
    assert values[1] == "trimming"
    assert app.bulk_render_tree.item("1", "tags") == ("bulk_status_trimming",)


def test_handle_bulk_render_line_ignores_unrelated_console_lines(app):
    _load(app, _make_states(n=1))
    app._handle_bulk_render_line("Running: ffmpeg -y -nostdin ...")
    assert app.bulk_render_tree.item("0", "values")[1] == "idle"


def test_status_survives_a_post_run_reload(app, tmp_path):
    """_on_process_exit()'s own bulk_ branch calls
    _refresh_bulk_render_tree(preserve_status=True) — simulated directly
    here rather than through a real subprocess (see test_bulk_render.py
    for the real end-to-end version)."""
    _load(app, _make_states(n=2))
    app._bulk_run_status = [("idle", "bulk_status_idle")] * 2
    app._handle_bulk_render_line("[bulk-render] status entry=1 state=stitched")

    app.bulk_states[0]["trimmed_path"] = "/now_trimmed.mp4"  # what a real run would have written
    app._refresh_bulk_render_tree(preserve_status=True)

    assert app.bulk_render_tree.item("0", "values")[1] == "stitched"
    assert app.bulk_render_tree.item("0", "tags") == ("bulk_status_stitched",)


def test_bulk_entry_status_re_matches_real_service_video_output():
    m = gui.BULK_ENTRY_STATUS_RE.match("[bulk-render] status entry=3 state=failed_stitch")
    assert m.groups() == ("3", "failed_stitch")


def test_every_status_key_has_a_display_entry():
    for state in ("idle", "trimming", "stitching", "trimmed", "stitched", "failed_trim", "failed_stitch"):
        assert state in gui.BULK_STATUS_DISPLAY


# -- _blank_bulk_entry() -----------------------------------------------------

def test_blank_bulk_entry_shape():
    entry = gui._blank_bulk_entry()
    assert entry["recording_path"] is None
    assert entry["stitch"]["series"] == ""
    assert "series" not in entry["trim"]

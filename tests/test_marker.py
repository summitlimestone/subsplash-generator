"""Sermon Marker (marker.py): the standalone backlog-marking utility.

MarkerStore tests are pure logic (no display). MarkerApp tests need a real
(or Xvfb) display, like the rest of the GUI tests, and drive the real
InteractiveTrimWindow over a real synthetic video."""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

import marker
from conftest import pump_until


def _touch(path: Path, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _store(base: Path) -> marker.MarkerStore:
    store = marker.MarkerStore(base)
    store.refresh()
    return store


def _entries(base: Path) -> list[dict]:
    return json.loads((base / marker.STATES_FILE_NAME).read_text())


# -- scanning ---------------------------------------------------------------

def test_scan_finds_videos_recursively_and_ignores_other_files(tmp_path):
    _touch(tmp_path / "input" / "b.mp4")
    _touch(tmp_path / "input" / "A.MKV")
    _touch(tmp_path / "input" / "2024" / "c.mov")
    _touch(tmp_path / "input" / "notes.txt")
    _touch(tmp_path / "elsewhere.mp4")

    store = _store(tmp_path)

    assert store.recordings == ["input/2024/c.mov", "input/A.MKV", "input/b.mp4"]


def test_missing_input_folder_just_means_no_recordings(tmp_path):
    assert _store(tmp_path).recordings == []


def test_no_states_file_means_nothing_marked(tmp_path):
    _touch(tmp_path / "input" / "a.mp4")
    store = _store(tmp_path)
    assert not store.is_marked("input/a.mp4")
    assert store.marked_count() == 0


# -- saving ------------------------------------------------------------------

def test_save_appends_an_entry_with_relative_paths_and_date_named_outputs(tmp_path):
    _touch(tmp_path / "input" / "2024" / "a.mkv")
    store = _store(tmp_path)

    store.save_mark("input/2024/a.mkv", 90.25, 3600.0, "2024-01-07", " Fall 2026 ")

    assert _entries(tmp_path) == [{
        "recording_path": "input/2024/a.mkv",
        "raw_begin_offset": "00:01:30.250",
        "raw_end_offset": "01:00:00.000",
        "trimmed_path": None,
        "trim": {"output": "output/2024-01-07_trimmed.mp4"},
        "stitch": {"series": "Fall 2026", "output": "output/2024-01-07.mp4"},
    }]


def test_marks_are_appended_in_order_and_survive_a_reopen(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        _touch(tmp_path / "input" / name)
    store = _store(tmp_path)
    store.save_mark("input/b.mp4", 1, 10, "2024-01-14", "S")
    store.save_mark("input/a.mp4", 1, 10, "2024-01-07", "S")

    reopened = _store(tmp_path)

    assert [e["recording_path"] for e in _entries(tmp_path)] == ["input/b.mp4", "input/a.mp4"]
    assert reopened.is_marked("input/a.mp4") and reopened.is_marked("input/b.mp4")
    assert not reopened.is_marked("input/c.mp4")
    assert reopened.marked_count() == 2
    assert reopened.start_end_for("input/a.mp4") == (1.0, 10.0)


def test_remarking_updates_the_entry_in_place(tmp_path):
    _touch(tmp_path / "input" / "a.mp4")
    (tmp_path / marker.STATES_FILE_NAME).write_text(json.dumps([{
        "recording_path": "input/a.mp4", "raw_begin_offset": "00:00:01.000",
        "raw_end_offset": "00:00:02.000", "trimmed_path": "output/old_trimmed.mp4",
        "trim": {"output": "output/2024-01-01_trimmed.mp4", "crf": 30},
        "stitch": {"series": "Old", "output": "output/2024-01-01.mp4", "encoder": "software"},
    }]))
    store = _store(tmp_path)

    store.save_mark("input/a.mp4", 5, 50, "2024-01-07", "New")

    [entry] = _entries(tmp_path)
    assert entry["raw_begin_offset"] == "00:00:05.000"
    assert entry["trimmed_path"] is None  # the old trim was of the old range
    assert entry["trim"] == {"output": "output/2024-01-07_trimmed.mp4", "crf": 30}
    assert entry["stitch"] == {"series": "New", "output": "output/2024-01-07.mp4", "encoder": "software"}


def test_entries_for_other_recordings_are_preserved(tmp_path):
    _touch(tmp_path / "input" / "a.mp4")
    foreign = {"recording_path": "input/gone.mp4", "raw_begin_offset": 1, "raw_end_offset": 2}
    (tmp_path / marker.STATES_FILE_NAME).write_text(json.dumps([foreign]))
    store = _store(tmp_path)

    store.save_mark("input/a.mp4", 1, 10, "2024-01-07", "")

    assert _entries(tmp_path)[0] == foreign


def test_saving_leaves_no_temp_files_behind(tmp_path):
    _touch(tmp_path / "input" / "a.mp4")
    _store(tmp_path).save_mark("input/a.mp4", 1, 10, "2024-01-07", "")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bulk_states.json", "input"]


def test_an_unreadable_states_file_is_never_overwritten(tmp_path):
    _touch(tmp_path / "input" / "a.mp4")
    states = tmp_path / marker.STATES_FILE_NAME
    states.write_text("{not json")
    with pytest.raises(marker.StatesFileError):
        marker.MarkerStore(tmp_path).refresh()
    states.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(marker.StatesFileError):
        marker.MarkerStore(tmp_path).refresh()


# -- matching entries written elsewhere -----------------------------------------

def test_an_absolute_path_inside_the_folder_matches(tmp_path):
    rec = _touch(tmp_path / "input" / "a.mp4")
    (tmp_path / marker.STATES_FILE_NAME).write_text(json.dumps([{"recording_path": str(rec)}]))
    assert _store(tmp_path).is_marked("input/a.mp4")


def test_a_windows_style_relative_path_matches(tmp_path):
    _touch(tmp_path / "input" / "2024" / "a.mp4")
    (tmp_path / marker.STATES_FILE_NAME).write_text(json.dumps([{"recording_path": "input\\2024\\a.mp4"}]))
    assert _store(tmp_path).is_marked("input/2024/a.mp4")


def test_a_path_from_another_machine_matches_by_filename(tmp_path):
    _touch(tmp_path / "input" / "a.mp4")
    (tmp_path / marker.STATES_FILE_NAME).write_text(
        json.dumps([{"recording_path": "D:\\Streams\\input\\a.mp4"}])
    )
    assert _store(tmp_path).is_marked("input/a.mp4")


def test_filename_fallback_skips_ambiguous_names(tmp_path):
    _touch(tmp_path / "input" / "x" / "a.mp4")
    _touch(tmp_path / "input" / "y" / "a.mp4")
    (tmp_path / marker.STATES_FILE_NAME).write_text(json.dumps([{"recording_path": "/elsewhere/a.mp4"}]))
    store = _store(tmp_path)
    assert store.marked_count() == 0


# -- navigation, series, dates -----------------------------------------------------

def test_next_unmarked_starts_after_the_given_recording_and_wraps(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        _touch(tmp_path / "input" / name)
    store = _store(tmp_path)
    store.save_mark("input/b.mp4", 1, 10, "2024-01-14", "")

    assert store.next_unmarked() == "input/a.mp4"
    assert store.next_unmarked("input/a.mp4") == "input/c.mp4"
    assert store.next_unmarked("input/c.mp4") == "input/a.mp4"
    store.save_mark("input/a.mp4", 1, 10, "2024-01-07", "")
    store.save_mark("input/c.mp4", 1, 10, "2024-01-21", "")
    assert store.next_unmarked() is None


def test_series_suggestions_and_last_used_series(tmp_path):
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        _touch(tmp_path / "input" / name)
    store = _store(tmp_path)
    store.save_mark("input/a.mp4", 1, 10, "2024-01-07", "Romans")
    store.save_mark("input/b.mp4", 1, 10, "2024-01-14", "Advent")
    store.save_mark("input/c.mp4", 1, 10, "2024-01-21", "")

    reopened = _store(tmp_path)

    assert reopened.series_suggestions() == ["Advent", "Romans"]
    assert reopened.last_series == "Advent"


def test_date_is_guessed_from_the_filename(tmp_path):
    _touch(tmp_path / "input" / "2024-01-07 10-30-12.mkv")
    _touch(tmp_path / "input" / "service_20240114.mp4")
    store = _store(tmp_path)
    assert store.date_for("input/2024-01-07 10-30-12.mkv") == "2024-01-07"
    assert store.date_for("input/service_20240114.mp4") == "2024-01-14"


def test_date_falls_back_to_the_files_modified_date(tmp_path):
    mtime = time.mktime((2023, 12, 31, 12, 0, 0, 0, 0, -1))
    _touch(tmp_path / "input" / "livestream.mp4", mtime=mtime)
    assert _store(tmp_path).date_for("input/livestream.mp4") == "2023-12-31"


def test_a_marked_recordings_date_comes_from_its_saved_output(tmp_path):
    _touch(tmp_path / "input" / "2024-01-07 10-30-12.mkv")
    store = _store(tmp_path)
    store.save_mark("input/2024-01-07 10-30-12.mkv", 1, 10, "2024-01-06", "")
    assert _store(tmp_path).date_for("input/2024-01-07 10-30-12.mkv") == "2024-01-06"


@pytest.mark.parametrize("text,ok", [
    ("2024-01-07", True), (" 2024-01-07 ", True), ("2024-02-30", False),
    ("2024-1-7", False), ("01/07/2024", False), ("", False),
])
def test_parse_date(text, ok):
    assert (marker.parse_date(text) is not None) == ok


def test_date_conflict_finds_another_recording_with_the_same_date(tmp_path):
    for name in ("a.mp4", "b.mp4"):
        _touch(tmp_path / "input" / name)
    store = _store(tmp_path)
    store.save_mark("input/a.mp4", 1, 10, "2024-01-07", "")

    assert store.date_conflict("input/b.mp4", "2024-01-07") == "input/a.mp4"
    assert store.date_conflict("input/a.mp4", "2024-01-07") is None  # re-marking itself is fine
    assert store.date_conflict("input/b.mp4", "2024-01-14") is None


# -- the app --------------------------------------------------------------------

@pytest.fixture
def marker_app(tmp_path, monkeypatch):
    shown = []
    for kind in ("showinfo", "showerror"):
        monkeypatch.setattr(marker.messagebox, kind, lambda *a, _k=kind, **kw: shown.append((_k, a)))
    app = marker.MarkerApp(tmp_path)
    app.withdraw()
    app.shown = shown
    yield app
    app.destroy()


def _with_recordings(app, sample_video, *names):
    for name in names:
        dest = app.store.input_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(sample_video, dest)
    app.refresh()


def _open_loaded(app, key):
    app.open_mark(key)
    win = app._trim_win
    pump_until(app, lambda: win.duration is not None, timeout=30)
    return win


def test_app_creates_the_input_folder(tmp_path, marker_app):
    assert (tmp_path / "input").is_dir()
    assert marker_app.progress_var.get() == "0 of 0 marked"
    assert str(marker_app.mark_next_btn["state"]) == "disabled"


def test_marking_saves_then_moves_on_to_the_next_recording(marker_app, sample_video):
    _with_recordings(marker_app, sample_video, "2024-01-07 service.mp4", "2024-01-14 service.mp4")
    win = _open_loaded(marker_app, "input/2024-01-07 service.mp4")
    assert win.playhead == 0.0  # nothing marked yet: starts at the beginning
    assert marker_app.date_var.get() == "2024-01-07"

    win.playhead = 3.0
    win._set_handle_at_playhead("start")
    win.playhead = 20.0
    win._set_handle_at_playhead("end")
    marker_app.series_var.set("Romans")
    win._apply()

    assert win._closed
    [entry] = _entries(marker_app.store.base)
    assert entry["raw_begin_offset"] == "00:00:03.000"
    assert entry["raw_end_offset"] == "00:00:20.000"
    assert entry["stitch"] == {"series": "Romans", "output": "output/2024-01-07.mp4"}
    assert marker_app.progress_var.get() == "1 of 2 marked"
    assert marker_app.tree.item("input/2024-01-07 service.mp4", "values")[0] == "✓"

    pump_until(marker_app, lambda: marker_app._trim_win is not win and marker_app._trim_win is not None)
    next_win = marker_app._trim_win
    assert next_win.source_path.endswith("2024-01-14 service.mp4")
    assert marker_app.series_var.get() == "Romans"  # last used carries over
    next_win._close()


def test_an_invalid_date_keeps_the_window_open(marker_app, sample_video):
    _with_recordings(marker_app, sample_video, "a.mp4")
    win = _open_loaded(marker_app, "input/a.mp4")
    marker_app.date_var.set("Jan 7")

    win._apply()

    assert not win._closed
    assert marker_app.shown and marker_app.shown[-1][0] == "showerror"
    assert not (marker_app.store.base / marker.STATES_FILE_NAME).exists()
    win._close()


def test_a_declined_date_conflict_keeps_the_window_open(marker_app, sample_video, monkeypatch):
    _with_recordings(marker_app, sample_video, "a.mp4", "b.mp4")
    marker_app.store.save_mark("input/a.mp4", 1, 10, "2024-01-07", "")
    marker_app.refresh()
    asked = []
    monkeypatch.setattr(marker.messagebox, "askyesno", lambda *a, **k: asked.append(a) or False)
    win = _open_loaded(marker_app, "input/b.mp4")
    marker_app.date_var.set("2024-01-07")

    win._apply()

    assert asked
    assert not win._closed
    assert len(_entries(marker_app.store.base)) == 1
    win._close()


def test_remarking_opens_at_the_saved_range_and_does_not_advance(marker_app, sample_video):
    _with_recordings(marker_app, sample_video, "a.mp4", "b.mp4")
    marker_app.store.save_mark("input/a.mp4", 2, 12, "2024-01-07", "Romans")
    marker_app.refresh()
    marker_app.tree.selection_set("input/a.mp4")
    marker_app.mark_selected()
    win = marker_app._trim_win
    pump_until(marker_app, lambda: win.duration is not None, timeout=30)
    assert (win.start, win.end) == (2.0, 12.0)
    assert marker_app.series_var.get() == "Romans"

    win._apply()
    for _ in range(20):
        marker_app.update()
        time.sleep(0.02)

    assert marker_app._trim_win is win  # no new window opened
    assert len(_entries(marker_app.store.base)) == 1


def test_an_unreadable_states_file_turns_marking_off(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(marker.messagebox, "showerror", lambda *a, **k: shown.append(a))
    _touch(tmp_path / "input" / "a.mp4")
    (tmp_path / marker.STATES_FILE_NAME).write_text("{not json")
    app = marker.MarkerApp(tmp_path)
    app.withdraw()
    try:
        assert shown
        assert str(app.mark_next_btn["state"]) == "disabled"
        assert app.tree.get_children() == ("input/a.mp4",)
        assert (tmp_path / marker.STATES_FILE_NAME).read_text() == "{not json"
    finally:
        app.destroy()

"""Core Offline-tab "Trim visually…" functional flow. Needs a real (or
Xvfb) display — see conftest.py's `app`/`trim_window`/`pump` fixtures."""

from pathlib import Path

import gui


def test_loads_duration_and_full_filmstrip(trim_window):
    assert trim_window.duration is not None
    assert len(trim_window._thumb_images) == gui.TRIM_THUMBS


def test_default_selection_is_full_clip(trim_window):
    assert trim_window.start == 0.0
    assert abs(trim_window.end - trim_window.duration) < 0.01


def test_dragging_end_handle_updates_selection(trim_window):
    class FakeEvent:
        pass

    trim_window._begin_drag("end")
    event = FakeEvent()
    event.x = trim_window.strip_w // 2  # live width, not a hardcoded constant — the strip is resizable
    trim_window._on_drag(event)
    assert abs(trim_window.end - trim_window.duration / 2) < 1.0


def test_nudge_moves_active_handle_by_expected_step(trim_window):
    trim_window.end = trim_window.duration / 2  # away from either edge, so both nudge directions have room
    trim_window._begin_drag("end")  # sets active_handle = "end"
    before = trim_window.end
    trim_window._nudge(1, fine=False)
    assert abs(trim_window.end - (before + 0.5)) < 1e-6

    before = trim_window.end
    trim_window._nudge(-1, fine=True)
    assert abs(trim_window.end - (before - 0.05)) < 1e-6


def test_nudge_does_not_cross_the_other_handle(trim_window):
    trim_window.start, trim_window.end = 5.0, 5.2
    trim_window.active_handle = "start"
    trim_window._nudge(1, fine=False)  # +0.5s would push start past end
    assert trim_window.start <= trim_window.end


def test_apply_writes_back_to_offline_fields_and_closes(app, trim_window):
    trim_window.start, trim_window.end = 2.0, 10.0
    trim_window._apply()
    assert app.vars["st_start"].get() == gui.format_timestamp(2.0)
    assert app.vars["st_end"].get() == gui.format_timestamp(10.0)
    assert trim_window.winfo_exists() == 0


def test_cancel_leaves_offline_fields_untouched(app, trim_window):
    app.vars["st_start"].set("00:00:05.000")
    app.vars["st_end"].set("00:00:10.000")
    trim_window.start, trim_window.end = 1.0, 2.0  # would-be changes if Apply were clicked instead
    trim_window._cancel()
    assert app.vars["st_start"].get() == "00:00:05.000"
    assert app.vars["st_end"].get() == "00:00:10.000"


def test_close_cleans_up_temp_directory(trim_window):
    tmpdir = Path(trim_window._tmpdir)
    assert tmpdir.exists()
    trim_window._close()
    assert not tmpdir.exists()

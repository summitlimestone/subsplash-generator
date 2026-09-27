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


def test_apply_calls_a_custom_on_apply_callback(app, sample_video, pump):
    """InteractiveTrimWindow no longer hardcodes writing to the Offline
    tab's own st_start/st_end (see the default callback
    App._open_interactive_trim() passes, covered by
    test_apply_writes_back_to_offline_fields_and_closes above) — this is
    what lets BulkEntryEditWindow reuse the same window for its own,
    unrelated local start/end vars."""
    captured = {}
    win = gui.InteractiveTrimWindow(
        app, sample_video, 0.0, 0.0,
        on_apply=lambda s, e: captured.update(start=s, end=e),
    )
    pump(lambda: win.duration is not None and len(win._thumb_images) == gui.TRIM_THUMBS, timeout=30)
    win.start, win.end = 3.0, 7.0

    win._apply()

    assert captured == {"start": 3.0, "end": 7.0}
    assert win.winfo_exists() == 0
    # The default (Offline tab) behavior is a completely separate
    # callback — this custom one must not also touch st_start/st_end,
    # which should still show their untouched default.
    assert app.vars["st_start"].get() == "00:00:00.000"


def test_full_clip_fallback_starts_the_playhead_at_the_beginning(trim_window):
    assert trim_window.playhead == 0.0


def test_set_start_and_end_here_use_the_playhead(trim_window):
    trim_window.playhead = 5.0
    trim_window._set_handle_at_playhead("start")
    trim_window.playhead = 12.5
    trim_window._set_handle_at_playhead("end")
    assert (trim_window.start, trim_window.end) == (5.0, 12.5)
    assert trim_window.active_handle == "end"


def test_set_start_after_the_end_moves_the_end_out_of_the_way(trim_window):
    trim_window.start, trim_window.end = 2.0, 6.0
    trim_window.playhead = 10.0
    trim_window._set_handle_at_playhead("start")
    assert trim_window.start == 10.0
    assert abs(trim_window.end - trim_window.duration) < 1e-6


def test_set_end_before_the_start_moves_the_start_out_of_the_way(trim_window):
    trim_window.start, trim_window.end = 8.0, 20.0
    trim_window.playhead = 4.0
    trim_window._set_handle_at_playhead("end")
    assert (trim_window.start, trim_window.end) == (0.0, 4.0)


def _custom_window(app, sample_video, pump, **kwargs):
    win = gui.InteractiveTrimWindow(app, sample_video, 0.0, 0.0, **kwargs)
    pump(lambda: win.duration is not None, timeout=30)
    return win


def test_apply_text_and_build_extra(app, sample_video, pump):
    built = []

    def build_extra(frame):
        built.append(gui.ttk.Label(frame, text="extra field"))

    win = _custom_window(
        app, sample_video, pump, on_apply=lambda s, e: None, apply_text="Save", build_extra=build_extra,
    )
    try:
        assert built and built[0].winfo_exists()
        texts = [str(w.cget("text")) for w in win.winfo_children()[0].winfo_children()[0].winfo_children()
                 if isinstance(w, gui.ttk.Button)]
        assert "Save" in texts and "Apply" not in texts
    finally:
        win._close()


def test_on_apply_returning_false_keeps_the_window_open(app, sample_video, pump):
    results = [False, None]
    win = _custom_window(app, sample_video, pump, on_apply=lambda s, e: results.pop(0))
    win._apply()
    assert not win._closed
    win._apply()
    assert win._closed

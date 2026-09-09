"""Preview pane / filmstrip resize behavior, including a regression test
for a real bug this project hit: the window growing/shrinking on its own
with no further user input. Root cause: the preview Label had no pinned
width/height, so its requested size followed whatever image happened to
be displayed on it — swapping in a freshly-generated frame (even one
sized to match the very Configure event that triggered it) could still
nudge the label's geometry a hair, firing another Configure, requesting
another frame, forever. Fixed by pinning the label's width/height
explicitly on every real Configure event (see
InteractiveTrimWindow._on_preview_configure())."""

import time

import gui


def _resize_and_wait(trim_window, pump, geometry: str, target_w: int):
    """Requests a new geometry and waits for it to actually take — not
    just for *any* Configure event to fire (one always does almost
    immediately, well before the window has actually reached the
    requested size), and not just for the filmstrip to finish
    regenerating (which needs the real size settled first anyway)."""
    trim_window.geometry(geometry)
    pump(lambda: abs(trim_window.winfo_width() - target_w) < 20, timeout=5)
    pump(lambda: len(trim_window._thumb_images) == gui.TRIM_THUMBS, timeout=15)


def test_growing_window_regenerates_preview_and_filmstrip_at_new_size(trim_window, app, pump):
    initial_strip_w = trim_window.strip_w
    initial_preview_size = (trim_window.preview_w, trim_window.preview_h)

    _resize_and_wait(trim_window, pump, "900x700", 900)

    assert trim_window.strip_w > initial_strip_w + 80
    assert (trim_window.preview_w, trim_window.preview_h) != initial_preview_size


def test_shrinking_window_also_regenerates_down(trim_window, app, pump):
    _resize_and_wait(trim_window, pump, "900x700", 900)
    grown_strip_w = trim_window.strip_w

    min_w, min_h = trim_window.minsize()
    _resize_and_wait(trim_window, pump, f"{min_w}x{min_h}", min_w)

    assert trim_window.strip_w < grown_strip_w - 50


def test_shrinking_cannot_go_below_minsize(trim_window):
    min_w, min_h = trim_window.minsize()
    trim_window.geometry(f"{min_w // 2}x{min_h // 2}")
    trim_window.update()
    assert trim_window.winfo_width() >= min_w
    assert trim_window.winfo_height() >= min_h


def test_window_does_not_grow_on_its_own(trim_window, app, pump):
    """Once resized and settled, the window must not keep changing size
    with no further real input — not while idle, not from repeated
    preview-frame swaps (e.g. dragging trim handles back and forth), and
    not during active playback."""
    _resize_and_wait(trim_window, pump, "820x620", 820)
    settled = (trim_window.winfo_width(), trim_window.winfo_height())

    def spin(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.update()
            time.sleep(0.02)

    spin(3)
    assert (trim_window.winfo_width(), trim_window.winfo_height()) == settled, "drifted while idle"

    class FakeEvent:
        pass

    for i in range(20):
        trim_window._begin_drag("end")
        event = FakeEvent()
        event.x = 50 + (i % 10) * 30
        trim_window._on_drag(event)
        spin(0.15)  # let each debounced preview request land before the next drag
    assert (trim_window.winfo_width(), trim_window.winfo_height()) == settled, (
        "drifted from repeated preview-frame swaps alone"
    )

    trim_window.playhead = 2.0
    trim_window.seekbar.set(2.0)
    trim_window._toggle_play()
    pump(lambda: trim_window._video_proc is not None, timeout=5)
    spin(3)
    assert (trim_window.winfo_width(), trim_window.winfo_height()) == settled, "drifted during active playback"
    trim_window._stop_playback()

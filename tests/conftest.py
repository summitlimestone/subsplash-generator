"""Shared fixtures for gui.py's tests.

GUI tests here create real Tk windows (App, InteractiveTrimWindow) — they
need a real or virtual (Xvfb) X11 display. Run under
`xvfb-run -a pytest tests/` in CI or any other headless environment; on a
normal desktop with a display already available, no wrapper is needed.
Plain `import tkinter`/`import gui` don't need a display themselves (only
actually creating a widget does), so a test that never instantiates one —
see test_accurate_seek.py — runs fine either way.

Also needs ffmpeg/ffprobe on PATH: used both by gui.py itself and, here,
to generate small synthetic test videos on the fly rather than committing
binary video fixtures to the repo.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import gui  # noqa: E402


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def pump_until(app, condition, timeout: float = 20.0) -> None:
    """Drives the Tk event loop (app.update()) until `condition()` is
    true — the standard way these tests wait for background-thread work
    (ffmpeg extraction, playback) to land back on the main thread via
    InteractiveTrimWindow's own after()-based queue drain."""
    deadline = time.monotonic() + timeout
    while not condition():
        app.update()
        time.sleep(0.02)
        if time.monotonic() > deadline:
            raise TimeoutError(f"condition not met within {timeout}s")


@pytest.fixture(scope="session")
def color_blocks_video(tmp_path_factory) -> tuple[str, list[str]]:
    """A 20s synthetic video made of ten distinct 2s solid-color blocks —
    lets a test assert *exactly* which frame a seek landed on by checking
    pixel color, not just trust a timestamp label. Session-scoped: built
    once, reused read-only by every test that needs it."""
    out_dir = tmp_path_factory.mktemp("color_blocks")
    colors = [
        "red", "0x00FF00", "blue", "yellow", "0xFF00FF",
        "cyan", "0xFFA500", "0x800080", "0x008080", "white",
    ]
    concat_list = out_dir / "concat.txt"
    lines = []
    for i, color in enumerate(colors):
        seg = out_dir / f"seg_{i}.mp4"
        _run_ffmpeg([
            "-f", "lavfi", "-i", f"color=c={color}:size=64x64:duration=2:rate=10",
            "-pix_fmt", "yuv420p", str(seg),
        ])
        lines.append(f"file '{seg}'")
    concat_list.write_text("\n".join(lines))
    out = out_dir / "color_blocks.mp4"
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(out)])
    return str(out), colors


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> str:
    """A short (~24s) synthetic video with real video+audio streams, for
    tests exercising the general GUI flow (filmstrip, drag, playback)
    rather than needing exact per-second color content. Deliberately much
    shorter than the multi-minute clips used for manual/interactive
    testing during development — long enough to exercise real seeking,
    short enough to keep CI fast."""
    out_dir = tmp_path_factory.mktemp("sample_video")
    out = out_dir / "sample.mp4"
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=24:size=320x180:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=24",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(out),
    ])
    return str(out)


@pytest.fixture
def app(monkeypatch, tmp_path):
    """A real App() instance, withdrawn (never shown). Redirects
    DEFAULT_CONFIG_PATH/SERIES_PATH into a throwaway tmp_path first —
    App() writes a starter config.json/series.json next to gui.py on
    first run if neither exists, and tests have no business touching (or
    depending on) whatever a real contributor's own actual config.json/
    series.json contains."""
    monkeypatch.setattr(gui, "DEFAULT_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(gui, "SERIES_PATH", tmp_path / "series.json")
    application = gui.App()
    application.withdraw()
    yield application
    application.destroy()


@pytest.fixture
def pump(app):
    """A test-local shortcut for pump_until() bound to this test's app."""
    def _pump(condition, timeout: float = 20.0) -> None:
        pump_until(app, condition, timeout)
    return _pump


@pytest.fixture
def trim_window(app, sample_video, pump):
    """A real InteractiveTrimWindow over sample_video, loaded and ready
    (duration known, filmstrip fully populated) before the test body runs."""
    app.vars["st_main"].set(sample_video)
    app._open_interactive_trim()
    win = next(w for w in app.winfo_children() if isinstance(w, gui.InteractiveTrimWindow))
    pump(lambda: win.duration is not None and len(win._thumb_images) == gui.TRIM_THUMBS, timeout=30)
    yield win
    if not win._closed:
        win._close()

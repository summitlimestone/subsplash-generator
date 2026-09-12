"""gui.py's accurate_seek_input_args()/probe_duration() — no display
needed, just ffmpeg (see module docstring in conftest.py).

Checks actual pixel content, not just a returned timestamp — the bug this
guards against (a plain -ss before -i landing on the nearest keyframe
*before* the target, sometimes many seconds early) would happily report
"success" while quietly returning the wrong frame."""

import subprocess

import gui

REFERENCE_COLORS = [
    (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255),
    (0, 255, 255), (255, 165, 0), (128, 0, 128), (0, 128, 128), (255, 255, 255),
]


def _closest_color_index(rgb: tuple[int, int, int]) -> int:
    return min(
        range(len(REFERENCE_COLORS)),
        key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, REFERENCE_COLORS[i])),
    )


def _first_frame_center_pixel(path: str, timestamp: float, size: int = 64) -> tuple[int, int, int]:
    """Seeks to `timestamp` via gui.accurate_seek_input_args() and reads
    the center pixel of the first decoded frame, via a raw rgb24 pipe —
    the same technique gui.py's own playback pipe uses (see
    InteractiveTrimWindow._video_playback_worker()), so this needs no PNG
    decoding (and thus no Tk/Pillow) just to check one pixel."""
    cmd = [
        "ffmpeg", *gui.accurate_seek_input_args(path, timestamp),
        "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={size}:{size}",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-loglevel", "error", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    raw = result.stdout
    assert len(raw) == size * size * 3, f"expected {size * size * 3} bytes, got {len(raw)}"
    idx = ((size // 2) * size + (size // 2)) * 3
    return raw[idx], raw[idx + 1], raw[idx + 2]


def test_accurate_seek_lands_in_correct_color_block(color_blocks_video):
    path, colors = color_blocks_video
    for i in range(len(colors)):
        target = i * 2.0 + 1.0  # 1s into each 2s block, safely away from the boundary
        rgb = _first_frame_center_pixel(path, target)
        got = _closest_color_index(rgb)
        assert got == i, f"seeking to block {i} (t={target}s) landed in block {got} instead (rgb={rgb})"


def test_accurate_seek_deep_into_the_file(color_blocks_video):
    """The whole point of the two-`-ss` trick is staying accurate without
    paying to decode from the start of the file — this doesn't measure
    speed, but at least confirms correctness holds all the way to the end,
    not just near the start where a naive implementation might coast by."""
    path, colors = color_blocks_video
    rgb = _first_frame_center_pixel(path, 19.0)
    assert _closest_color_index(rgb) == 9


def test_probe_duration(color_blocks_video):
    path, _ = color_blocks_video
    duration = gui.probe_duration(path)
    assert duration is not None
    assert abs(duration - 20.0) < 0.5


def test_probe_duration_missing_file():
    assert gui.probe_duration("/nonexistent/path/does-not-exist.mp4") is None

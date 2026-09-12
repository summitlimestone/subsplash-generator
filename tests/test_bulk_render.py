"""bulk_render() (service_video.py, backing the GUI's Bulk Render tab):
trim and/or stitch every entry in a JSON array of render-state dicts in
one pass, tolerating one entry's failure rather than aborting the whole
batch. Pure CLI-module tests — no GUI/display needed — using real
ffmpeg-generated clips, the same "verify against real behavior, not just
the code" discipline the rest of this test suite already applies to its
own ffmpeg-facing logic (see conftest.py)."""

import json
import subprocess
from pathlib import Path

import pytest

import service_video as sv


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-y", *args], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.fixture(scope="session")
def bulk_clips(tmp_path_factory) -> dict[str, str]:
    """Small synthetic intro/outro/recording clips, real content (not just
    distinct paths) since bulk_render() actually runs ffmpeg over these —
    built once and reused read-only across every test in this file, same
    convention as conftest.py's own video fixtures."""
    out_dir = tmp_path_factory.mktemp("bulk_clips")
    intro, outro = out_dir / "intro.mp4", out_dir / "outro.mp4"
    rec1, rec2 = out_dir / "rec1.mp4", out_dir / "rec2.mp4"
    _run_ffmpeg(["-f", "lavfi", "-i", "color=c=red:size=64x64:duration=1:rate=10", "-pix_fmt", "yuv420p", str(intro)])
    _run_ffmpeg(["-f", "lavfi", "-i", "color=c=green:size=64x64:duration=1:rate=10", "-pix_fmt", "yuv420p", str(outro)])
    for path, color in ((rec1, "blue"), (rec2, "purple")):
        _run_ffmpeg([
            "-f", "lavfi", "-i", f"color=c={color}:size=64x64:duration=6:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ])
    return {"intro": str(intro), "outro": str(outro), "rec1": str(rec1), "rec2": str(rec2)}


def _make_state(bulk_clips: dict, out_dir: Path, idx: int, recording_path: str | None) -> dict:
    return {
        "recording_path": recording_path,
        "raw_begin_offset": 1.0,
        "raw_end_offset": 4.0,
        "trimmed_path": None,
        "trim": {
            "output": str(out_dir / f"trim{idx}.mp4"), "pad_start_seconds": 0, "pad_end_seconds": 0,
            "crf": 30, "fast_copy": False, "normalize_audio": False, "normalize_target_lufs": -16.0,
            "encoder": "software", "encoder_preset": "ultrafast",
        },
        "stitch": {
            "auto": True, "intro": bulk_clips["intro"], "outro": bulk_clips["outro"],
            "intro_duration": 1.0, "outro_duration": 1.0, "output": str(out_dir / f"final{idx}.mp4"),
            "transition_duration": 0.2, "transition": "fade", "crf": 30, "fast_copy": False,
            "encoder": "software", "encoder_preset": "ultrafast",
        },
    }


def test_bulk_render_full_trims_and_stitches_every_entry(bulk_clips, tmp_path):
    states_path = tmp_path / "states.json"
    states = [
        _make_state(bulk_clips, tmp_path, 0, bulk_clips["rec1"]),
        _make_state(bulk_clips, tmp_path, 1, bulk_clips["rec2"]),
    ]
    states_path.write_text(json.dumps(states))

    result = sv.bulk_render(str(states_path), "full")

    assert result == 0
    assert (tmp_path / "trim0.mp4").is_file()
    assert (tmp_path / "trim1.mp4").is_file()
    assert (tmp_path / "final0.mp4").is_file()
    assert (tmp_path / "final1.mp4").is_file()
    saved = json.loads(states_path.read_text())
    assert saved[0]["trimmed_path"] == str(tmp_path / "trim0.mp4")
    assert saved[1]["trimmed_path"] == str(tmp_path / "trim1.mp4")


def test_bulk_render_trim_only_does_not_stitch(bulk_clips, tmp_path):
    states_path = tmp_path / "states.json"
    states_path.write_text(json.dumps([_make_state(bulk_clips, tmp_path, 0, bulk_clips["rec1"])]))

    result = sv.bulk_render(str(states_path), "trim")

    assert result == 0
    assert (tmp_path / "trim0.mp4").is_file()
    assert not (tmp_path / "final0.mp4").exists()
    saved = json.loads(states_path.read_text())
    assert saved[0]["trimmed_path"] == str(tmp_path / "trim0.mp4")


def test_bulk_render_stitch_only_uses_existing_trimmed_path(bulk_clips, tmp_path):
    states_path = tmp_path / "states.json"
    state = _make_state(bulk_clips, tmp_path, 0, bulk_clips["rec1"])
    # An entry that was already trimmed by some earlier pass (or hand-
    # edited) — mode="stitch" should use it directly, not re-trim.
    trimmed = tmp_path / "already_trimmed.mp4"
    sv.trim_clip(
        bulk_clips["rec1"], str(trimmed), 1.0, 4.0, crf=30, fast_copy=False,
        encoder="software", encoder_preset="ultrafast", normalize_audio=False,
    )
    state["trimmed_path"] = str(trimmed)
    states_path.write_text(json.dumps([state]))

    result = sv.bulk_render(str(states_path), "stitch")

    assert result == 0
    assert (tmp_path / "final0.mp4").is_file()


def test_bulk_render_stitch_only_skips_entry_with_no_trimmed_path(bulk_clips, tmp_path):
    states_path = tmp_path / "states.json"
    states_path.write_text(json.dumps([_make_state(bulk_clips, tmp_path, 0, bulk_clips["rec1"])]))

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "stitch")

    assert "trimmed_path" in str(exc_info.value.code)
    assert not (tmp_path / "final0.mp4").exists()


def test_bulk_render_tolerates_one_bad_entry_and_keeps_going(bulk_clips, tmp_path):
    states_path = tmp_path / "states.json"
    states = [
        _make_state(bulk_clips, tmp_path, 0, bulk_clips["rec1"]),
        _make_state(bulk_clips, tmp_path, 1, str(tmp_path / "does_not_exist.mp4")),
        _make_state(bulk_clips, tmp_path, 2, bulk_clips["rec2"]),
    ]
    states_path.write_text(json.dumps(states))

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "full")

    assert exc_info.value.code != 0
    assert "#2" in str(exc_info.value.code)
    assert (tmp_path / "final0.mp4").is_file()
    assert (tmp_path / "final2.mp4").is_file()
    assert not (tmp_path / "final1.mp4").exists()
    saved = json.loads(states_path.read_text())
    assert saved[0]["trimmed_path"] is not None
    assert saved[1]["trimmed_path"] is None
    assert saved[2]["trimmed_path"] is not None


def test_bulk_render_rejects_non_array_json(tmp_path):
    states_path = tmp_path / "states.json"
    states_path.write_text(json.dumps({"not": "a list"}))

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "trim")

    assert "array" in str(exc_info.value.code)


def test_bulk_render_rejects_empty_array(tmp_path):
    states_path = tmp_path / "states.json"
    states_path.write_text("[]")

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "trim")

    assert "empty" in str(exc_info.value.code)


def test_bulk_render_rejects_missing_file(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(tmp_path / "nope.json"), "trim")

    assert "Could not read" in str(exc_info.value.code)

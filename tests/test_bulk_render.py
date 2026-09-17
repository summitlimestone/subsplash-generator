"""bulk_render() (service_video.py, backing the GUI's Bulk Render tab):
trim and/or stitch every entry in a JSON array of render-state dicts in
one pass, tolerating one entry's failure rather than aborting the whole
batch. Pure CLI-module tests — no GUI/display needed — using real
ffmpeg-generated clips, the same "verify against real behavior, not just
the code" discipline the rest of this test suite already applies to its
own ffmpeg-facing logic (see conftest.py).

A render-state's own "stitch" section only ever carries a series *name*
now (never literal intro/outro) — resolved via resolve_series(), which
reads SERIES_PATH (series.json) — so every test here points that at a
real series.json via the `series_name` fixture rather than embedding
intro/outro directly in each state dict."""

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


@pytest.fixture
def series_name(bulk_clips, tmp_path, monkeypatch) -> str:
    """Points sv.SERIES_PATH at a real series.json (one series, "Test
    Series", resolving to bulk_clips' intro/outro) for the duration of
    one test — bulk_render() resolves every entry's stitch.series
    through resolve_series(), which reads this file. Returns the name
    to put in a state's stitch.series."""
    series_path = tmp_path / "series.json"
    series_path.write_text(json.dumps([{
        "name": "Test Series", "intro": bulk_clips["intro"], "intro_duration": 1.0,
        "outro": bulk_clips["outro"], "outro_duration": 1.0,
        "transition": "fade", "transition_duration": 0.2, "hidden": False,
    }]))
    monkeypatch.setattr(sv, "SERIES_PATH", series_path)
    return "Test Series"


def _make_state(series_name: str, out_dir: Path, idx: int, recording_path: str | None) -> dict:
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
            "auto": True, "series": series_name, "output": str(out_dir / f"final{idx}.mp4"),
            "crf": 30, "fast_copy": False, "encoder": "software", "encoder_preset": "ultrafast",
        },
    }


def test_bulk_render_full_trims_and_stitches_every_entry(bulk_clips, series_name, tmp_path):
    states_path = tmp_path / "states.json"
    states = [
        _make_state(series_name, tmp_path, 0, bulk_clips["rec1"]),
        _make_state(series_name, tmp_path, 1, bulk_clips["rec2"]),
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


def test_bulk_render_trim_only_does_not_stitch(bulk_clips, series_name, tmp_path):
    states_path = tmp_path / "states.json"
    states_path.write_text(json.dumps([_make_state(series_name, tmp_path, 0, bulk_clips["rec1"])]))

    result = sv.bulk_render(str(states_path), "trim")

    assert result == 0
    assert (tmp_path / "trim0.mp4").is_file()
    assert not (tmp_path / "final0.mp4").exists()
    saved = json.loads(states_path.read_text())
    assert saved[0]["trimmed_path"] == str(tmp_path / "trim0.mp4")


def test_bulk_render_stitch_only_uses_existing_trimmed_path(bulk_clips, series_name, tmp_path):
    states_path = tmp_path / "states.json"
    state = _make_state(series_name, tmp_path, 0, bulk_clips["rec1"])
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


def test_bulk_render_stitch_only_skips_entry_with_no_trimmed_path(bulk_clips, series_name, tmp_path):
    states_path = tmp_path / "states.json"
    states_path.write_text(json.dumps([_make_state(series_name, tmp_path, 0, bulk_clips["rec1"])]))

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "stitch")

    # Caught by the upfront validation pass now (see
    # test_bulk_render_validates_every_entry_before_starting_anything
    # below) rather than mid-run, but the entry is still never stitched.
    assert "Trimmed clip" in str(exc_info.value.code)
    assert not (tmp_path / "final0.mp4").exists()


def test_bulk_render_stitch_only_skips_entry_with_no_series(bulk_clips, series_name, tmp_path):
    states_path = tmp_path / "states.json"
    state = _make_state(series_name, tmp_path, 0, bulk_clips["rec1"])
    trimmed = tmp_path / "already_trimmed.mp4"
    sv.trim_clip(
        bulk_clips["rec1"], str(trimmed), 1.0, 4.0, crf=30, fast_copy=False,
        encoder="software", encoder_preset="ultrafast", normalize_audio=False,
    )
    state["trimmed_path"] = str(trimmed)
    state["stitch"]["series"] = ""
    states_path.write_text(json.dumps([state]))

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "stitch")

    assert "series" in str(exc_info.value.code).lower()
    assert not (tmp_path / "final0.mp4").exists()


def _add_series_with_missing_intro(bulk_clips) -> str:
    """Appends a second series to sv.SERIES_PATH (already pointed at a
    real file by the series_name fixture) whose intro file doesn't
    exist — resolve_series() only checks the *name* against series.json
    (see _check_series()), so this resolves fine at validation time, but
    fails for real once stitch() itself tries to open that intro file.
    A genuine mid-run-only failure — unlike a bad *input* (missing
    recording, unresolvable series name, etc. — see
    test_bulk_render_validates_every_entry_before_starting_anything),
    which the upfront validation pass now catches before anything
    starts. Returns the new series' name."""
    series = json.loads(sv.SERIES_PATH.read_text())
    series.append({
        "name": "Bad Series", "intro": "/does/not/exist/intro.mp4", "intro_duration": 1.0,
        "outro": bulk_clips["outro"], "outro_duration": 1.0,
        "transition": "fade", "transition_duration": 0.2, "hidden": False,
    })
    sv.SERIES_PATH.write_text(json.dumps(series))
    return "Bad Series"


def test_bulk_render_tolerates_one_bad_entry_and_keeps_going(bulk_clips, series_name, tmp_path):
    bad_series = _add_series_with_missing_intro(bulk_clips)
    states_path = tmp_path / "states.json"
    states = [
        _make_state(series_name, tmp_path, 0, bulk_clips["rec1"]),
        _make_state(bad_series, tmp_path, 1, bulk_clips["rec1"]),
        _make_state(series_name, tmp_path, 2, bulk_clips["rec2"]),
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
    # Entry #2's trim succeeds fine — only its stitch (the missing intro
    # file) fails, so unlike a trim-phase failure its trimmed_path *is*
    # still recorded.
    assert saved[0]["trimmed_path"] is not None
    assert saved[1]["trimmed_path"] is not None
    assert saved[2]["trimmed_path"] is not None


def test_bulk_render_prints_per_entry_status_lines(bulk_clips, series_name, tmp_path, capsys):
    """The GUI's Bulk Render tab parses these lines live (see
    BULK_ENTRY_STATUS_RE/_handle_bulk_render_line() in gui.py) to drive
    its own per-row Status column — this is the wire format contract
    between the two, checked directly against real output here."""
    bad_series = _add_series_with_missing_intro(bulk_clips)
    states_path = tmp_path / "states.json"
    states = [
        _make_state(series_name, tmp_path, 0, bulk_clips["rec1"]),
        _make_state(bad_series, tmp_path, 1, bulk_clips["rec1"]),
    ]
    states_path.write_text(json.dumps(states))

    with pytest.raises(SystemExit):
        sv.bulk_render(str(states_path), "full")

    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("[bulk-render] status ")
    ]
    assert lines == [
        # The upfront validation pass, across every entry, before any of
        # them starts running (see
        # test_bulk_render_validates_every_entry_before_starting_anything)
        # — both entries pass it (a missing *intro* file isn't checked
        # there), so both go straight to "ready".
        "[bulk-render] status entry=1 state=verifying",
        "[bulk-render] status entry=1 state=ready",
        "[bulk-render] status entry=2 state=verifying",
        "[bulk-render] status entry=2 state=ready",
        # The real trim/stitch loop.
        "[bulk-render] status entry=1 state=trimming",
        "[bulk-render] status entry=1 state=trimmed",
        "[bulk-render] status entry=1 state=stitching",
        "[bulk-render] status entry=1 state=stitched",
        "[bulk-render] status entry=2 state=trimming",
        "[bulk-render] status entry=2 state=trimmed",
        "[bulk-render] status entry=2 state=stitching",
        "[bulk-render] status entry=2 state=failed_stitch",
    ]


def test_bulk_render_status_line_reports_failed_stitch_not_failed_trim(
    bulk_clips, series_name, tmp_path, capsys,
):
    # An entry whose trimmed_path exists (so it passes the upfront
    # validation pass — see
    # test_bulk_render_validates_every_entry_before_starting_anything)
    # but isn't a real video — stitch() itself fails once it actually
    # tries to process it (a genuine mid-run failure, not a bad input),
    # which should report failed_stitch, not failed_trim (trim never
    # even runs in mode="stitch").
    states_path = tmp_path / "states.json"
    state = _make_state(series_name, tmp_path, 0, bulk_clips["rec1"])
    corrupt_trimmed = tmp_path / "corrupt_trimmed.mp4"
    corrupt_trimmed.write_bytes(b"not a real video file" * 20)
    state["trimmed_path"] = str(corrupt_trimmed)
    states_path.write_text(json.dumps([state]))

    with pytest.raises(SystemExit):
        sv.bulk_render(str(states_path), "stitch")

    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("[bulk-render] status ")
    ]
    assert lines == [
        "[bulk-render] status entry=1 state=verifying",
        "[bulk-render] status entry=1 state=ready",
        "[bulk-render] status entry=1 state=stitching",
        "[bulk-render] status entry=1 state=failed_stitch",
    ]


def test_bulk_render_validates_every_entry_before_starting_anything(bulk_clips, series_name, tmp_path):
    """A bad *input* (as opposed to a runtime ffmpeg failure — see
    test_bulk_render_tolerates_one_bad_entry_and_keeps_going) aborts the
    whole batch before entry #1 even starts, not just the bad entry —
    the whole point of validating every entry up front."""
    states_path = tmp_path / "states.json"
    states = [
        _make_state(series_name, tmp_path, 0, bulk_clips["rec1"]),
        _make_state(series_name, tmp_path, 1, str(tmp_path / "does_not_exist.mp4")),
    ]
    states_path.write_text(json.dumps(states))

    with pytest.raises(SystemExit) as exc_info:
        sv.bulk_render(str(states_path), "full")

    message = str(exc_info.value.code)
    assert "failed validation" in message
    assert "#2" in message
    assert "Main clip not found" in message
    # Nothing ran at all — not even the first, perfectly valid entry.
    assert not (tmp_path / "final0.mp4").exists()
    assert not (tmp_path / "trim0.mp4").exists()
    saved = json.loads(states_path.read_text())
    assert saved[0]["trimmed_path"] is None


# -- _validate_bulk_entry() ---------------------------------------------------

def test_validate_bulk_entry_trim_mode_catches_missing_main_clip(tmp_path):
    state = {
        "recording_path": str(tmp_path / "missing.mp4"), "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trim": {"output": str(tmp_path / "trim.mp4")}, "stitch": {},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("Main clip not found" in p for p in problems)


def test_validate_bulk_entry_trim_mode_catches_unset_main_clip(tmp_path):
    state = {
        "recording_path": None, "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trim": {"output": str(tmp_path / "trim.mp4")}, "stitch": {},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("Main clip is not set" in p for p in problems)


def test_validate_bulk_entry_trim_mode_catches_missing_sermon_bounds(bulk_clips):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": None, "raw_end_offset": None,
        "trim": {}, "stitch": {},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("Sermon start" in p for p in problems)
    assert any("Sermon end" in p for p in problems)


def test_validate_bulk_entry_trim_mode_catches_blank_output_path(bulk_clips):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trim": {"output": ""}, "stitch": {},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("Trimmed clip output path" in p and "blank" in p for p in problems)


def test_validate_bulk_entry_trim_mode_passes_a_good_entry(bulk_clips, tmp_path):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trim": {"output": str(tmp_path / "trim.mp4")}, "stitch": {},
    }
    assert sv._validate_bulk_entry(state, "trim") == []


def test_validate_bulk_entry_trim_mode_catches_end_past_the_main_clips_length(bulk_clips, tmp_path):
    # bulk_clips["rec1"] is 6s long.
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 100.0,
        "trim": {"output": str(tmp_path / "trim.mp4")}, "stitch": {},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("outside the main clip's own length" in p for p in problems)


def test_validate_bulk_entry_trim_mode_catches_start_after_end(bulk_clips, tmp_path):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 4.0, "raw_end_offset": 1.0,
        "trim": {"output": str(tmp_path / "trim.mp4")}, "stitch": {},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("past itself" in p for p in problems)


def test_validate_bulk_entry_trim_mode_catches_range_too_short_for_the_transition(
    bulk_clips, series_name, tmp_path,
):
    # series_name's own transition_duration (see the fixture) is 0.2s;
    # a 0.1s trim range is too short for it to crossfade.
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 1.1,
        "trim": {"output": str(tmp_path / "trim.mp4")},
        "stitch": {"series": series_name, "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "trim")
    assert any("too short for" in p and "transition" in p for p in problems)


def test_validate_bulk_entry_trim_mode_skips_the_transition_check_with_no_series_set(
    bulk_clips, tmp_path,
):
    # Same too-short range as above, but no series set — trim mode
    # doesn't otherwise require one (only stitch/full do), so this can't
    # be checked and isn't treated as a failure.
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 1.1,
        "trim": {"output": str(tmp_path / "trim.mp4")}, "stitch": {},
    }
    assert sv._validate_bulk_entry(state, "trim") == []


def test_validate_bulk_entry_trim_mode_skips_the_transition_check_with_an_unresolvable_series(
    bulk_clips, tmp_path,
):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 1.1,
        "trim": {"output": str(tmp_path / "trim.mp4")},
        "stitch": {"series": "Not A Real Series", "output": str(tmp_path / "final.mp4")},
    }
    # mode="trim" alone never requires a series to resolve — only that
    # this one extra check gets silently skipped when it doesn't.
    assert sv._validate_bulk_entry(state, "trim") == []


def test_validate_bulk_entry_full_mode_catches_range_too_short_for_the_transition(
    bulk_clips, series_name, tmp_path,
):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 1.1,
        "trimmed_path": None,
        "trim": {"output": str(tmp_path / "trim.mp4")},
        "stitch": {"series": series_name, "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "full")
    assert any("too short for" in p and "transition" in p for p in problems)


def test_validate_bulk_entry_stitch_mode_catches_missing_trimmed_clip(series_name, tmp_path):
    state = {
        "trimmed_path": str(tmp_path / "missing_trim.mp4"),
        "trim": {}, "stitch": {"series": series_name, "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "stitch")
    assert any("Trimmed clip not found" in p for p in problems)


def test_validate_bulk_entry_stitch_mode_catches_unset_trimmed_clip(series_name, tmp_path):
    state = {
        "trimmed_path": None,
        "trim": {}, "stitch": {"series": series_name, "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "stitch")
    assert any("Trimmed clip is not set" in p for p in problems)


def test_validate_bulk_entry_stitch_mode_catches_unknown_series(series_name, tmp_path):
    # series_name (not just bulk_clips) so a real series.json exists —
    # otherwise this would hit resolve_series()'s "no series.json found"
    # branch instead of its "name not found in it" one.
    trimmed = tmp_path / "trim.mp4"
    trimmed.write_bytes(b"x")
    state = {
        "trimmed_path": str(trimmed),
        "trim": {}, "stitch": {"series": "Not A Real Series", "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "stitch")
    assert any("Not A Real Series" in p for p in problems)


def test_validate_bulk_entry_stitch_mode_catches_blank_series(tmp_path):
    trimmed = tmp_path / "trim.mp4"
    trimmed.write_bytes(b"x")
    state = {
        "trimmed_path": str(trimmed),
        "trim": {}, "stitch": {"series": "", "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "stitch")
    assert any("series" in p.lower() for p in problems)


def test_validate_bulk_entry_stitch_mode_passes_a_good_entry(series_name, tmp_path):
    trimmed = tmp_path / "trim.mp4"
    trimmed.write_bytes(b"x")
    state = {
        "trimmed_path": str(trimmed),
        "trim": {}, "stitch": {"series": series_name, "output": str(tmp_path / "final.mp4")},
    }
    assert sv._validate_bulk_entry(state, "stitch") == []


def test_validate_bulk_entry_full_mode_does_not_require_trimmed_clip_to_already_exist(
    bulk_clips, series_name, tmp_path,
):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trimmed_path": None,
        "trim": {"output": str(tmp_path / "trim.mp4")},
        "stitch": {"series": series_name, "output": str(tmp_path / "final.mp4")},
    }
    assert sv._validate_bulk_entry(state, "full") == []


def test_validate_bulk_entry_full_mode_still_checks_series_and_output(bulk_clips, tmp_path):
    state = {
        "recording_path": bulk_clips["rec1"], "raw_begin_offset": 1.0, "raw_end_offset": 4.0,
        "trimmed_path": None,
        "trim": {"output": str(tmp_path / "trim.mp4")},
        "stitch": {"series": "", "output": str(tmp_path / "final.mp4")},
    }
    problems = sv._validate_bulk_entry(state, "full")
    assert any("series" in p.lower() for p in problems)


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


# -- resolve_series() ---------------------------------------------------

def test_resolve_series_rejects_blank_name():
    with pytest.raises(SystemExit) as exc_info:
        sv.resolve_series("")
    assert "series" in str(exc_info.value.code).lower()


def test_resolve_series_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "SERIES_PATH", tmp_path / "nope.json")
    with pytest.raises(SystemExit) as exc_info:
        sv.resolve_series("Anything")
    assert "series.json" in str(exc_info.value.code)


def test_resolve_series_rejects_unknown_name(series_name):
    with pytest.raises(SystemExit) as exc_info:
        sv.resolve_series("Not A Real Series")
    assert "Not A Real Series" in str(exc_info.value.code)


def test_resolve_series_returns_the_matching_record(series_name, bulk_clips):
    series = sv.resolve_series(series_name)
    assert series["intro"] == bulk_clips["intro"]
    assert series["outro"] == bulk_clips["outro"]
    assert series["transition"] == "fade"
    assert series["transition_duration"] == 0.2


def test_resolve_series_defaults_transition_for_older_series_json(tmp_path, monkeypatch):
    # A series.json saved before transition/transition_duration existed
    # on the record — resolve_series() should still work, defaulting
    # both rather than raising a KeyError.
    series_path = tmp_path / "series.json"
    series_path.write_text(json.dumps([{
        "name": "Old Series", "intro": "/i.mp4", "intro_duration": 5.0,
        "outro": "/o.mp4", "outro_duration": 5.0, "hidden": False,
    }]))
    monkeypatch.setattr(sv, "SERIES_PATH", series_path)
    series = sv.resolve_series("Old Series")
    assert series["intro"] == "/i.mp4"
    assert "transition" not in series  # not defaulted onto the record itself, only when read


# -- apply_stitch_command_series() (watch()'s live 'stitch <name>' parsing) -

def test_apply_stitch_command_series_records_the_name():
    stitch_cfg = {"series": "Old Series"}
    sv.apply_stitch_command_series("stitch New Series", stitch_cfg)
    assert stitch_cfg["series"] == "New Series"


def test_apply_stitch_command_series_handles_a_name_with_multiple_words():
    stitch_cfg = {}
    sv.apply_stitch_command_series("stitch Fall 2026 Sermon Series", stitch_cfg)
    assert stitch_cfg["series"] == "Fall 2026 Sermon Series"


def test_apply_stitch_command_series_bare_command_records_blank():
    stitch_cfg = {"series": "Old Series"}
    sv.apply_stitch_command_series("stitch", stitch_cfg)
    assert stitch_cfg["series"] == ""


def test_watch_live_stitch_command_updates_the_render_state_file(bulk_clips, series_name, tmp_path, monkeypatch):
    """End-to-end version of the two tests above: applying the parsed
    series the same way watch() does, then writing the render-state file
    the same way sync_state()/_write_render_state() does, produces a
    file whose stitch.series matches — without needing a live OBS/
    ProPresenter connection to exercise watch() itself."""
    stitch_cfg = {"auto": True, "series": "", "output": str(tmp_path / "final.mp4")}
    trim_cfg = {"output": str(tmp_path / "trim.mp4"), "state_output": str(tmp_path / "state.json")}
    state_path = tmp_path / "state.json"

    sv.apply_stitch_command_series(f"stitch {series_name}", stitch_cfg)
    sv._write_render_state(state_path, "/rec.mp4", 1.0, 4.0, trim_cfg, stitch_cfg, trimmed_path="/trimmed.mp4")

    saved = json.loads(state_path.read_text())
    assert saved["stitch"]["series"] == series_name

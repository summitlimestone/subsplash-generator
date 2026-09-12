#!/usr/bin/env python3
"""Tkinter GUI front-end for service_video.py.

Two windows:
  - Main window   the day-to-day view. Two modes in a Live/Offline
                   notebook (Live = the watch pipeline, with just the
                   fields that change week to week — intro/outro/output;
                   Offline = the standalone crossfade tool, which can
                   autofill from a saved render-state file), plus a
                   console pane at the bottom that both modes and the
                   Config window's Learn section stream output into.
  - Config window  everything that's set once and rarely touched again:
                   the config file path, and ProPresenter/OBS/Render tabs
                   (connection details, slide matching + Learn mode, and
                   the trim/auto-stitch settings used after a live Watch
                   run). Opened via the main window's "Config" button; hidden
                   rather than destroyed when closed, so it reopens instantly
                   with everything still in place.

Every operation is run as a real subprocess of service_video.py (the same
CLI documented in README.md) rather than calling its functions in-process,
because several of those functions call sys.exit() on error — fine for a
CLI, fatal for a long-lived GUI process. Subprocess output is streamed into
the main window's console pane.

Run:
    python gui.py

Requires the same things as service_video.py's 'watch'/'learn' commands
(ffmpeg on PATH, and 'pip install -r requirements.txt' for obsws-python /
websockets) plus a Tk-enabled Python. Tk ships with the standard Windows/Mac
installers; on Linux it's usually a separate package (e.g. `sudo pacman -S
tk` / `sudo apt install python3-tk`).
"""

import ast
import json
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
SERVICE_SCRIPT = SCRIPT_DIR / "service_video.py"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"
# Named intro/outro bundles ("series" — see issue #12) — a separate file
# from config.json since they're reusable across configs/instances, not
# connection settings for one. Purely a GUI convenience layer:
# service_video.py's CLI has no concept of a "series", only literal
# intro/outro paths — the GUI resolves a selected series to those before
# ever building a command line or render-state file (see
# App._wire_series_selector()).
SERIES_PATH = SCRIPT_DIR / "series.json"


def default_config() -> dict:
    """A starter config.json: real defaults where one genuinely exists
    (ports, CRF, transition, reconnect interval, auto-stitch), honest empty
    placeholders where it can't be guessed (host, password, slide UIDs,
    clip paths) — not example.json's fake example values, which could be
    mistaken for already being configured."""
    return {
        "general": {
            "log_path": DEFAULT_LOG_PATH,
        },
        "api": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8765,
            "password": "",
        },
        "propresenter": {
            "host": "",
            "port": 1025,
            "password": "",
            "reconnect_interval_seconds": 4,
            "begin_slide": {"uid": ""},
            "end_slide": {"uid": ""},
        },
        "obs": {
            "host": "localhost",
            "port": 4455,
            "password": "",
        },
        "trim": {
            "output": "body_trimmed.mp4",
            "state_output": DEFAULT_STATE_OUTPUT,
            "pad_start_seconds": 0.0,
            "pad_end_seconds": 0.0,
            "crf": 23,
            "fast_copy": True,
            "normalize_audio": True,
            "normalize_target_lufs": -16.0,
            "encoder": "nvenc",
            "encoder_preset": None,
        },
        "stitch": {
            "auto": True,
            # Which saved series (see SERIES_PATH) intro/outro below came
            # from, purely so the GUI's own Series dropdown can restore
            # its selection on load — service_video.py itself never reads
            # this key, only intro/outro/*_duration below.
            "series": "",
            "intro": "",
            "outro": "",
            "intro_duration": DEFAULT_IMAGE_DURATION,
            "outro_duration": DEFAULT_IMAGE_DURATION,
            "output": "final.mp4",
            "transition_duration": 1.0,
            "transition": "fade",
            # Off by default and not exposed in the GUI — see stitch()'s
            # own docstring in service_video.py: it's always safe to turn
            # on (verifies its own result before trusting it), just not
            # actually useful in practice. Still settable by hand (or via
            # the CLI's --fast-copy) if that ever changes.
            "fast_copy": False,
            "encoder": "nvenc",
            "encoder_preset": None,
        },
    }

STATE_RE = re.compile(r"state = (\w+)")
RENDER_STATE_PATH_RE = re.compile(r"wrote render state -> (.+)$")
SLIDE_RE = re.compile(r'^uid: "(.*)"\s+text: (.*)$')
# Matches service_video.py's "[watcher] trim status = ..."/"[watcher]
# stitch status = ..." lines (see _trim_worker()/_stitch_worker()).
TRIM_STITCH_STATUS_RE = re.compile(r"(trim|stitch) status = (\w+)")
# Matches render()'s own "\nTrimmed body clip -> ..." print line — used by
# the Offline tab's Trim button to auto-point Main clip at the result, so
# a follow-up Stitch click picks it up without the user re-Browsing.
TRIMMED_PATH_RE = re.compile(r"^Trimmed body clip -> (.+)$")
# Mirrors service_video.py's _print_step() output exactly (see
# _MACHINE_PROGRESS/--machine-progress there) — "[progress] step N/M
# duration=D.DDD: <description>".
PROGRESS_STEP_RE = re.compile(r"^\[progress\] step (\d+)/(\d+) duration=([\d.]+):")
# A bare ffmpeg -progress field line, e.g. "out_time_us=1234567" — matched
# generically (not by an exhaustive list of known keys) so a future
# ffmpeg version adding new fields still gets swallowed into the progress
# readout instead of leaking into the log; only consulted while
# App._in_progress_step is true, i.e. right after a PROGRESS_STEP_RE line.
PROGRESS_FIELD_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)=(.*)$")

# service_video.py's internal state machine names, relabeled for display —
# names not listed here (WAIT_RECORD_START etc.) show as-is.
WATCH_STATE_LABELS = {
    "RECORDING_STOPPED": "Recording stopped",
}

JSON_FILETYPES = [("JSON files", "*.json"), ("All files", "*.*")]
LOG_FILETYPES = [("Log files", "*.log *.txt"), ("All files", "*.*")]
VIDEO_FILETYPES = [("Video files", "*.mp4 *.mov *.mkv *.m4v *.avi"), ("All files", "*.*")]
# Intro/outro can be either a video or a still image (see IMAGE_DURATION_HELP) —
# their Browse buttons use this instead of VIDEO_FILETYPES.
INTRO_OUTRO_FILETYPES = [
    ("Video/image files", "*.mp4 *.mov *.mkv *.m4v *.avi *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"),
    ("All files", "*.*"),
]
DEFAULT_IMAGE_DURATION = 5.0
# Mirrors service_video.py's DEFAULT_STATE_OUTPUT exactly — duplicated
# rather than imported, same as format_timestamp()/parse_timestamp().
DEFAULT_STATE_OUTPUT = "render_state_%Y%m%d_%H%M%S.json"
# GUI-only (service_video.py has no console of its own to log) — see
# App._sync_log_file(). Same %Y%m%d_%H%M%S timestamp convention as
# DEFAULT_STATE_OUTPUT, expanded via expand_output_path() below.
DEFAULT_LOG_PATH = "console_%Y%m%d_%H%M%S.log"

# Matches the Summit Limestone brand palette used by the companion
# subsplash-form site (summitlimestone.github.io/subsplash-form) — its
# dark-mode variant specifically — applied on top of ttk's built-in 'clam'
# theme rather than pulling in a separate theming dependency. hover/active/
# disabled tints are derived from --accent/--error the same way the site's
# own CSS would via color-mix.
PALETTE = {
    "bg": "#201e1e",            # site dark --bg
    "surface": "#333132",       # site dark --surface (Base Grey)
    "border": "#46433f",        # site dark --border
    "text": "#e4e3df",          # site dark --text (Base Light)
    "muted": "#b6b4ad",         # site dark --muted
    "accent": "#78a22f",        # site --accent (Green, same in both modes)
    "accent_contrast": "#1c1c1e",  # site --accent-contrast (button text on accent)
    "accent_hover": "#90b354",
    "accent_active": "#a0be6d",
    "accent2": "#e0d6b4",       # site --accent-2 (Oatmeal, same in both modes)
    "danger": "#c0392b",        # site --error (same in both modes)
    "danger_hover": "#cb5d51",
    "success": "#78a22f",
    # Not from the site's own palette (it has no info/warning roles) —
    # picked to match its muted, earthy tone rather than a neon blue/gold.
    "info": "#3f7fb3",
    "warning": "#c9971f",
    # Shared across every button style's disabled state (plain, accent,
    # danger alike) — darker than --bg itself so a disabled button still
    # reads as a recessed element instead of blending into the page.
    "button_disabled_bg": "#121010",
    "console_bg": "#141313",    # a shade darker than --bg, for contrast
    "console_fg": "#e4e3df",
    "console_muted": "#b6b4ad",
}


def to_int(text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{field} must be a whole number (got {text!r})")


def to_float(text: str, field: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{field} must be a number (got {text!r})")


def _fuzzy_subsequence_score(query: str, text: str) -> float | None:
    """None if `query`'s characters don't all appear in `text`, in order
    (case already normalized by the caller) — not necessarily contiguous,
    same idea as fzf/VS Code's command-palette search, not spelling-
    correction. Otherwise a lower-is-better score: the length of the
    shortest span of `text` containing `query` as a subsequence, with how
    early that span starts as a tiebreak — so "fa26" scores better
    against "Fall 2026 Series" (tight match right at the start) than
    against "See Fall Foliage 2026" (same subsequence, but scattered)."""
    positions = []
    start = 0
    for ch in query:
        idx = text.find(ch, start)
        if idx == -1:
            return None
        positions.append(idx)
        start = idx + 1
    span = positions[-1] - positions[0] + 1
    return span + positions[0] / 1000


def fuzzy_match_series(query: str, names: list[str]) -> list[str]:
    """Fuzzy-filters `names` by `query` (see _fuzzy_subsequence_score()),
    best match first. A blank query matches everything, in its original
    order — what a Series combobox shows before you've typed anything to
    narrow it down."""
    query = query.strip().lower()
    if not query:
        return list(names)
    scored = []
    for name in names:
        score = _fuzzy_subsequence_score(query, name.lower())
        if score is not None:
            scored.append((score, name))
    scored.sort(key=lambda pair: pair[0])
    return [name for _score, name in scored]


# Mirrors service_video.py's format_timestamp()/parse_timestamp() exactly —
# duplicated rather than imported, since this GUI only ever talks to that
# script as a subprocess (see ProcessRunner), never as a library.
TIMESTAMP_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(\.\d+)?$")


def format_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    sign = "-" if total_ms < 0 else ""
    total_ms = abs(total_ms)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def parse_timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("-")
    body = text[1:] if negative else text
    m = TIMESTAMP_RE.match(body)
    if m:
        hours, minutes, secs, frac = m.groups()
        total = int(hours) * 3600 + int(minutes) * 60 + int(secs) + float(frac or 0.0)
        return -total if negative else total
    raise ValueError


def to_timestamp(text: str, field: str) -> float:
    try:
        return parse_timestamp(text)
    except ValueError:
        raise ValueError(f"{field} must be HH:MM:SS.mmm (got {text!r})")


def expand_output_path(path: str) -> str:
    """Duplicated from service_video.py's function of the same name
    (rather than imported — this script only ever runs service_video.py
    as a subprocess, never imports it) so App._sync_log_file() can expand
    general.log_path itself, the same way every other output-path field
    in this GUI is expanded by service_video.py once it's handed the raw
    string. Expands strftime placeholders anywhere in the path —
    filename and any directory components — and creates any directory
    component that doesn't exist yet; see service_video.py's own copy
    for the full reasoning. Unlike that copy, a failure creating the
    directory doesn't sys.exit() the whole GUI over a log file — it's
    left to raise a plain OSError, which _sync_log_file() already
    catches around its own open() call the same way."""
    expanded = datetime.now().strftime(path)
    Path(expanded).parent.mkdir(parents=True, exist_ok=True)
    return expanded


def format_elapsed(seconds: float) -> str:
    """Formats a wall-clock duration for the "took ..." summary shown in
    the console header's speed slot once a run finishes (see
    App._on_process_exit()) — h/m/s, omitting leading zero units (e.g.
    "45s", "2m 15s", "1h 03m 22s")."""
    total = round(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def probe_duration(path: str) -> float | None:
    """A file's duration in seconds via ffprobe, or None if it can't be
    read — used only by InteractiveTrimWindow to lay out its filmstrip.
    Called directly rather than through service_video.py: this is a
    quick, UI-only probe with nothing to trim/stitch, not worth spawning
    that whole script for."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError):
        return None


def accurate_seek_input_args(path: str, timestamp: float) -> list[str]:
    """ffmpeg input args that seek to `timestamp` both fast and frame-
    accurately — ffmpeg's own documented "fast + accurate seeking" recipe.
    A single -ss before -i is a fast demuxer-level seek that only lands
    at or near the nearest keyframe *before* the target, which is fine
    for a filmstrip thumbnail but not for something claiming to start
    "at the right time" (playback, or a precise single-frame preview): a
    keyframe interval of a few seconds means being off by up to that much.

    Splitting it into a coarse -ss before -i (fast, jumps to just shy of
    the target) plus a small residual -ss right after -i (which then
    decodes and discards only that short remaining gap to reach the exact
    frame) gets both — the "discard" phase stays bounded to a couple of
    seconds no matter how far into a two-hour recording the target is,
    unlike putting the whole seek after -i, which would decode from the
    very start of the file instead."""
    timestamp = max(timestamp, 0.0)
    margin = min(timestamp, 5.0)
    return ["-ss", f"{timestamp - margin:.3f}", "-i", path, "-ss", f"{margin:.3f}"]


def extract_frame_png(
    path: str, timestamp: float, out_path: Path, width: int, height: int | None = None,
    accurate: bool = False, letterbox: bool = False,
) -> bool:
    """Grabs a single frame at `timestamp` as a PNG — Tk's own PhotoImage
    loads PNG natively (Tk 8.6+), so no Pillow dependency is needed to
    show it.

    With `height` given and `letterbox=False` (filmstrip thumbnails),
    scales to fill and center-crops to an exact width x height tile so the
    filmstrip lines up evenly. With `height` and `letterbox=True` (the
    large preview, which — unlike a filmstrip tile — shouldn't crop any of
    the frame away), scales to fit *within* width x height and pads the
    rest with black, matching the letterbox filter chain the video
    playback pipe itself uses (see InteractiveTrimWindow._video_playback_worker())
    so a paused static frame and a playing streamed one line up the same
    way in the same box. With `height=None`, just scales to `width` wide,
    keeping the source aspect ratio (unused today, kept for callers that
    don't care what height they get).

    `accurate=False` (filmstrip thumbnails) uses a plain fast/approximate
    seek — fine for a coarse scrubber strip, and much quicker across 16 of
    them. `accurate=True` (the single large preview) uses
    accurate_seek_input_args() instead, since that image is what the
    handle-drag readout implicitly claims is "this exact point"."""
    if height and letterbox:
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    elif height:
        # Both dimensions have to be given explicitly here, not -2 for
        # width — force_original_aspect_ratio=increase needs an actual W:H
        # box to "increase" against to guarantee the scaled frame covers
        # it in both dimensions. scale=-2:{height} alone (auto width,
        # exact aspect match) makes "increase" a no-op — the scaled width
        # then only happens to be >= the requested crop width by luck of
        # the source's own aspect ratio, and crop hard-fails once it
        # isn't: a *real* bug this shipped with (a wide enough window/
        # filmstrip cell against a source aspect ratio that didn't happen
        # to cover it left every thumbnail blank above some window width,
        # confirmed by reproducing the exact ffmpeg crop failure directly).
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    else:
        vf = f"scale={width}:-2"
    input_args = accurate_seek_input_args(path, timestamp) if accurate else ["-ss", f"{max(timestamp, 0):.3f}", "-i", path]
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", *input_args,
                "-frames:v", "1", "-vf", vf, "-loglevel", "error", str(out_path),
            ],
            capture_output=True, timeout=20,
        )
        return result.returncode == 0 and out_path.exists()
    except (OSError, subprocess.TimeoutExpired):
        return False


# ffmpeg's xfade filter transition names (video-filters.html#xfade-1), for
# the Transition type dropdown. Editable, not readonly — these cover the
# built-in set, but xfade also accepts a custom expression, so free typing
# still needs to work.
XFADE_TRANSITIONS = [
    "fade", "fadeblack", "fadewhite", "fadegrays", "dissolve", "distance",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "wipetl", "wipetr", "wipebl", "wipebr",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circlecrop", "rectcrop", "circleopen", "circleclose", "radial",
    "vertopen", "vertclose", "horzopen", "horzclose",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur", "pixelize", "squeezeh", "squeezev", "zoomin",
    "fadefast", "fadeslow",
    "hlwind", "hrwind", "vuwind", "vdwind",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
]

# Mirrors service_video.py's ENCODER_PROFILES keys exactly — duplicated
# rather than imported, same as format_timestamp()/parse_timestamp(),
# since this GUI only ever talks to that script as a subprocess.
ENCODER_CHOICES = ["software", "nvenc", "qsv", "amf", "videotoolbox"]
ENCODER_HELP = (
    "Which encoder the full re-encode (not fast copy's own small "
    "re-encoded windows) uses. \"software\" is libx264, the same as "
    "always. A hardware choice (nvenc: NVIDIA, qsv: Intel Quick Sync, "
    "amf: AMD, videotoolbox: Apple/macOS) can be dramatically faster if "
    "the machine actually has one — if it fails to run at all (no such "
    "hardware, wrong driver, etc.) it automatically falls back to "
    "software and logs why, so it's safe to leave set either way. Quality/"
    "size at a given CRF isn't quite comparable across encoders, though —  "
    "expect to eyeball it against your own footage rather than assume "
    "parity with software."
)

# Mirrors service_video.py's ENCODER_PROFILES "presets"/"default_preset"
# per encoder exactly — duplicated rather than imported, same as
# ENCODER_CHOICES. Keyed the same as ENCODER_CHOICES; the Encoder preset
# dropdown's values/default are swapped to match whichever's selected
# (see _wire_encoder_preset_choices()) since preset names aren't shared
# across encoders.
ENCODER_PRESET_CHOICES = {
    "software": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
    "nvenc": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
    "qsv": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
    "amf": ["speed", "balanced", "quality", "high_quality"],
    "videotoolbox": [],
}
ENCODER_DEFAULT_PRESETS = {
    "software": "veryfast", "nvenc": "p4", "qsv": "veryfast", "amf": "speed", "videotoolbox": "",
}
ENCODER_PRESET_HELP = (
    "That encoder's own speed/quality preset — names and effect differ per "
    "encoder, so this follows whichever's picked above. A hardware "
    "encoder's fastest preset tends to give up noticeably more compression "
    "efficiency for its extra speed than a CPU preset does (confirmed with "
    "NVENC: its fastest, p1, produced files around 4x the size of software "
    "at the same CRF — p4, used here by default, is a much closer match "
    "without losing much of the speed advantage)."
)


CRF_HELP = (
    "Video quality (x264 CRF). Lower = better quality, larger file; higher = "
    "more compression, smaller file. 18 is visually lossless; 23 is x264's "
    "own default."
)

SUBSPLASH_PRESET_HELP = (
    "Matches Subsplash's own recommended On-Demand 1080p HandBrake preset "
    "instead: profile High, level 4.0, 1920x1080 at 30fps (upscaling/"
    "letterboxing as needed), ~2400kbps video (single-pass — not HandBrake's "
    "own more precise 2-pass), AAC 160kbps audio, no +faststart. Overrides "
    "CRF and the output resolution/framerate for Stitch only; Encoder still "
    "picks the actual encode backend."
)

OFFSET_HELP = (
    "Shifts this cut point from the ProPresenter-detected slide time. "
    "Positive pushes it later (further into the clip); negative pushes it "
    "earlier. Same convention for both offsets."
)

REGEX_HELP = (
    "Exact: the slide text must match Text exactly. Regex: Text is a "
    "Python regular expression matched anywhere in the slide text — a "
    "plain word or phrase with no special characters works like a "
    "substring search."
)

TIMESTAMP_HELP = (
    "Supports strftime date/time placeholders in the filename, filled in "
    "when the file is written. E.g. final_%Y-%m-%d_%H-%M-%S.mp4 → "
    "final_2026-09-01_14-30-05.mp4. Common codes: %Y year, %m month, %d "
    "day, %H hour (24h), %M minute, %S second. Only the filename itself "
    "is expanded, not any folder in the path."
)

LOG_PATH_HELP = (
    TIMESTAMP_HELP + " The timestamp (if any) is filled in once, the "
    "first time this app opens the file — not re-expanded on every line "
    "written — so a whole session's console output lands in one file. "
    "Leave blank to turn off file logging entirely; the console pane "
    "itself is unaffected either way."
)

BULK_STATES_HELP = (
    "A JSON file containing an array of render-state objects — the same "
    "self-contained shape a 'watch' run or the Offline tab's \"Export to "
    "JSON\" writes. Trim/Full Render rewrite this file's trimmed_path "
    "fields in place as each entry finishes."
)

API_HELP = (
    "Optional HTTP API for marking start/end remotely. Runs only during Watch; "
    "docs at /swagger. Requires pip install fastapi uvicorn."
)

API_PASSWORD_HELP = (
    "HTTP Basic Auth password required on every request — any username is "
    "accepted, only the password is checked (there's no user management "
    "here). Leave blank to run with no authentication at all; anyone who "
    "can reach host:port could mark start/end — a clear warning is logged "
    "each time Watch starts with this blank."
)

LIVE_TRIM_HELP = (
    "Trims the recording down to the marked start/end. Works before the "
    "recording actually stops too — it reads the in-progress file, and if "
    "that doesn't work yet (the encoder hasn't flushed that far), it "
    "automatically waits for the recording to finish and tries again once "
    "more, rather than failing outright. Nothing renders on its own — this "
    "is the only thing that ever trims."
)

LIVE_STITCH_HELP = (
    "Crossfades the just-trimmed clip with the intro/outro above. Needs a "
    "successful Trim first this run."
)

IMAGE_DURATION_HELP = (
    "Only used if the clip above is a still image (jpg/png/etc.) rather "
    f"than a video — how long to show it for. Ignored for a video clip. "
    f"Defaults to {DEFAULT_IMAGE_DURATION}s if left blank."
)

FAST_COPY_HELP = (
    "Re-encodes only a short sliver at the very start (up to the nearest "
    "keyframe — a cut can only start there) and stream-copies the rest, "
    "instead of re-encoding the whole trimmed clip. Much faster; needs an "
    "h264 or hevc recording, and always verifies its own result actually "
    "decodes cleanly before trusting it, falling back to a full re-encode "
    "automatically otherwise. (Stitching the intro/outro on afterward "
    "always fully re-encodes regardless — crossfading forces a real "
    "decode+re-encode of those clips, which in testing never came out "
    "compatible enough with the trimmed clip's own encoding to stream-copy "
    "the rest, so there's no equivalent toggle for it. It uses a fast x264 "
    "preset instead.)"
)

NORMALIZE_AUDIO_HELP = (
    "Loudness-normalizes the trimmed clip's audio to Target LUFS via "
    "ffmpeg's loudnorm filter — useful since a live recording's levels can "
    "vary service to service (mic gain, distance from the mic, etc.) in a "
    "way a fixed CRF/encoder choice has no bearing on. Measures the whole "
    "trimmed range once, up front, so a fast-copy trim's separately "
    "re-encoded sliver and tail both get the exact same correction rather "
    "than risking an audible jump where they join. Video is untouched "
    "either way; a failed measurement just skips normalization for that "
    "render rather than failing it outright."
)
NORMALIZE_TARGET_HELP = (
    "Integrated loudness target, in LUFS (lower = quieter). -16 is a "
    "common streaming/YouTube target and a good match for spoken-word "
    "content; -23 is the EBU R128 broadcast standard, quieter with more "
    "headroom."
)


class Tooltip:
    """A small hover tooltip for a single widget, shown after a short delay
    in a borderless Toplevel styled to match PALETTE. Plain Tk, not ttk —
    there's no ttk tooltip widget, and this only ever needs one look."""

    def __init__(self, widget, text: str, font=("TkDefaultFont", 9), delay=400):
        self.widget = widget
        self.text = text
        self.font = font
        self.delay = delay
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        p = PALETTE
        tk.Label(
            self._tip, text=self.text, justify="left", background=p["surface"],
            foreground=p["text"], relief="solid", borderwidth=1,
            font=self.font, wraplength=280, padx=8, pady=5,
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class ProcessRunner:
    """Runs one service_video.py subcommand at a time, streaming its stdout
    (stderr merged in) line-by-line to a callback. Callbacks fire from a
    background thread — callers must hop back to the Tk main thread
    themselves (e.g. via a queue drained with `after()`)."""

    def __init__(self, on_line, on_exit):
        self.proc: subprocess.Popen | None = None
        self._on_line = on_line
        self._on_exit = on_exit

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str]):
        if self.running():
            raise RuntimeError("A process is already running.")
        # -u forces the child to run fully unbuffered instead of Python's
        # default full block-buffering whenever stdout isn't a real
        # terminal (as it isn't here — it's a pipe). Without this, a
        # long-running command like 'watch' can sit for a very long time
        # (effectively the whole service) with everything it prints —
        # including connection status — stuck in an internal buffer never
        # flushed to this console, looking exactly like it's silently
        # broken even though it's working fine underneath. bufsize=1
        # below only affects how *this* process reads the pipe; it can't
        # do anything about how the child buffers its own writes.
        #
        # --machine-progress on every subcommand that can run ffmpeg
        # (learn never does, so it's left off there) — switches
        # service_video.py's ffmpeg calls to machine-readable progress
        # output instead of the normal human stats line, which App parses
        # into the progress bar/step/speed readout next to the console
        # status instead of logging it (see App._handle_progress_line()).
        if args and args[0] in ("watch", "render", "stitch"):
            args = [*args, "--machine-progress"]
        cmd = [sys.executable, "-u", str(SERVICE_SCRIPT), *args]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self._on_line(line.rstrip("\n"))
        returncode = proc.wait()
        self._on_exit(returncode)

    def stop(self):
        if self.running():
            self.proc.terminate()

    def send_line(self, text: str):
        """Writes one line to the running subprocess's stdin — 'watch'
        reads these as manual override commands (see start_stdin_thread in
        service_video.py). No-op if nothing's running or the pipe's
        already gone (e.g. the process just exited)."""
        if self.running() and self.proc.stdin is not None:
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass


def _build_api_app(app: "App"):
    """Builds the FastAPI app backing the control API — see App's own
    _start_api()/_stop_api()/_sync_api_to_config(). Runs all the time
    the "Enabled" checkbox (Config > API) is on, independent of any one
    `watch` subprocess: POST /mark/start, POST /mark/end, and GET /state
    proxy to whichever `watch` subprocess (if any) is currently running,
    the same way the GUI's own Mark Sermon Start/End buttons already do.
    Interactive Swagger docs are served at /swagger.

    Marks are synchronous: each is validated against the GUI's own live-
    tracked state (App._live_mark_applicable(), the same conditions that
    already enable/disable the Mark Sermon Start/End buttons themselves)
    before being sent, and the HTTP response reflects the real outcome —
    200 once actually applied, 409 if there's no active watch session or
    the mark doesn't apply in the current state (e.g. end before start)
    — rather than always succeeding and leaving the caller to separately
    poll GET /state to find out.

    HTTP Basic Auth, gated on api_password alone (no separate username —
    only a single shared password was asked for, not real user
    management): every route requires it via the app-level
    `dependencies` UNLESS the password is empty, in which case nothing
    here is protected at all — logged loudly once, since that's a real
    thing to know about an API with a network listener.

    FastAPI's own auto-generated docs/OpenAPI-schema routes are exempt
    from app-level `dependencies` (a known FastAPI quirk, confirmed by
    direct testing — /swagger came back 200 with no credentials even
    with app-level dependencies set), so both are disabled here
    (docs_url=None, openapi_url=None) and reimplemented as plain routes
    of our own below, which aren't exempt."""
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.openapi.utils import get_openapi
    from fastapi.security import HTTPBasic, HTTPBasicCredentials

    password = app.vars["api_password"].get()
    security = HTTPBasic()

    def require_password(credentials: HTTPBasicCredentials = Depends(security)):
        # secrets.compare_digest(), not ==, so a wrong guess can't be
        # narrowed down by how long the comparison took to fail.
        if not secrets.compare_digest(credentials.password, password):
            raise HTTPException(status_code=401, detail="Incorrect password", headers={"WWW-Authenticate": "Basic"})

    if password:
        app_dependencies = [Depends(require_password)]
    else:
        app_dependencies = []
        app._log(
            "[gui] api_password is empty — the control API is running with NO "
            "authentication; anyone who can reach it can mark start/end. Set "
            "Config > API > Password to require one."
        )

    fastapi_app = FastAPI(
        title="sclc-subsplash-generator control API",
        description="Mark the sermon's start/end and check the live watch state.",
        docs_url=None, openapi_url=None, redoc_url=None,
        dependencies=app_dependencies,
    )

    @fastapi_app.get("/openapi.json", include_in_schema=False)
    def openapi_schema():
        return get_openapi(
            title=fastapi_app.title, version="1.0.0", description=fastapi_app.description,
            routes=fastapi_app.routes,
        )

    @fastapi_app.get("/swagger", include_in_schema=False)
    def swagger_ui():
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{fastapi_app.title} — Swagger UI")

    def do_mark(which: str) -> dict:
        """Runs on Tk's main thread (see App._call_on_main_thread()):
        decides whether this mark applies right now and, if so, actually
        sends it — the exact same decision _update_live_buttons() makes
        for the Mark Sermon Start/End buttons themselves."""
        if not app.runner.running():
            return {"ok": False, "reason": "no active watch session"}
        if not app._live_mark_applicable(which):
            return {"ok": False, "reason": f"mark not valid in the current state ({app._live_raw_state})"}
        if which == "start":
            app._mark_sermon_start()
        else:
            app._mark_sermon_end()
        return {"ok": True}

    def handle_mark(which: str):
        result = app._call_on_main_thread(lambda: do_mark(which))
        if result is None:
            raise HTTPException(status_code=503, detail="could not confirm — try GET /state")
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["reason"])
        return {"status": "applied"}

    @fastapi_app.post("/mark/start", summary="Mark sermon start")
    def mark_start():
        """Equivalent to clicking the GUI's "Mark Sermon Start" button.
        200 once actually sent; 409 if there's no active watch session,
        or a start mark doesn't apply right now (e.g. the state has
        moved past where a re-mark is accepted)."""
        return handle_mark("start")

    @fastapi_app.post("/mark/end", summary="Mark sermon end")
    def mark_end():
        """Equivalent to clicking the GUI's "Mark Sermon End" button —
        same semantics as /mark/start, including 409 for marking end
        before a start has ever been marked."""
        return handle_mark("end")

    @fastapi_app.get("/state", summary="Current watch state")
    def get_state():
        """A snapshot of what the GUI currently knows: the state-machine
        state ("idle" if no watch run is active), whether OBS is
        actively recording, whether begin/end have been marked, the
        latest Trim/Stitch status (idle/running/done/failed), and the
        render-state file's path."""
        def snapshot():
            raw_state = app._live_raw_state
            return {
                "state": raw_state or "idle",
                "recording": raw_state not in (None, "WAIT_RECORD_START", "RECORDING_STOPPED"),
                "begin_marked": app._live_begin_marked,
                "end_marked": app._live_end_marked,
                "trim_status": app._live_trim_status,
                "stitch_status": app._live_stitch_status,
                "render_state_path": app.render_state_var.get(),
            }
        return app._call_on_main_thread(snapshot) or {}

    @fastapi_app.get("/brew/coffee", summary="Brew coffee")
    def brew_coffee():
        raise HTTPException(status_code=418, detail="I'm a teapot")

    return fastapi_app


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Service Video — Control Panel")
        self.geometry("820x680")
        self.minsize(640, 520)

        self.vars: dict[str, tk.Variable] = {}
        self._start_buttons: list[ttk.Button] = []
        self._pw_entries: list[ttk.Entry] = []
        self._current_command: str | None = None
        # Set by _load_render_state_json() when a loaded file has raw
        # recording/offset info; cleared (or left stale but unused) when
        # Main clip no longer matches — see _offline_use_raw_trim().
        self._offline_raw_state: dict | None = None
        # Live tab's Trim/Stitch button enablement — see
        # _update_live_buttons()/_handle_trim_stitch_status(). Both track
        # whether something has become true at any point *this run*
        # (an end has been marked; a trim has succeeded), not just
        # whether the most recent "state = ..." line implies it, since
        # neither should ever become un-true again mid-run once set.
        # Reset at the start of each watch run (_run_watch()).
        # _live_begin_marked follows the same "true at any point this
        # run" tracking, for GET /state's begin_marked (see
        # _build_api_app()) — Mark Start itself has no separate re-mark
        # state to track like Mark End's WAIT_RECORD_STOP does, so this
        # is simpler: true once past WAIT_BEGIN_SLIDE.
        self._live_begin_marked = False
        self._live_end_marked = False
        self._live_trimmed = False
        # Set once this run's live Trim has genuinely resolved (DONE or
        # FAILED, no retry still pending — see _handle_trim_stitch_status())
        # — watch() itself exits right after that (see its own docstring),
        # so _on_process_exit() uses this to know a just-finished "watch"
        # run should hand off to the Offline tab's own Trim/Stitch (below)
        # rather than just going idle. _live_trimmed_path is the trimmed
        # clip's actual path, captured the same way _handle_offline_trim_line()
        # already does for the Offline tab's own Trim — see _handle_watch_line().
        # Both reset at the start of each watch run (_run_watch()).
        self._live_trim_resolved = False
        self._live_trimmed_path: str | None = None
        # "trim" or "stitch" while a post-exit Live Trim/Stitch click (see
        # _trim_live()/_stitch_live()) is running its own freshly spawned
        # process — None otherwise, including for a genuine Offline-tab
        # Trim/Stitch click, which goes through the exact same _run_trim()/
        # _run_stitch() but shouldn't touch the Live tab's own status
        # readout. Lets _on_process_exit() know to update watch_state_var/
        # the Live buttons for this one, the same way _handle_trim_stitch_status()
        # did while watch() itself was still running.
        self._live_handoff_which: str | None = None
        # The most recently parsed live "state = ..." value (see
        # _update_live_buttons(), which sets this) — None whenever no
        # watch run is currently tracking anything (before Start Watch,
        # or after it's exited/stopped either way). This is the one
        # source of truth _live_mark_applicable() and the control API's
        # GET /state read, instead of each re-deriving it.
        self._live_raw_state: str | None = None
        # "idle"/"running"/"done"/"failed" — set at the same points
        # _handle_trim_stitch_status() (while watch() runs) or
        # _on_process_exit()'s post-exit handoff (once it's exited)
        # already update the Live buttons/status label, so GET /state has
        # a real value to report instead of a fourth copy of that logic.
        self._live_trim_status = "idle"
        self._live_stitch_status = "idle"
        # The running control API, if Config > API's "Enabled" is
        # checked — see _start_api()/_stop_api(). Independent of any
        # watch run's own lifetime; starts/stops with the checkbox
        # itself (or the GUI's own open/close), not with Start Watch/Stop.
        self._api_server = None
        self._api_thread: threading.Thread | None = None
        # See _handle_progress_line(): whether the most recently parsed
        # line was a "[progress] step N/M" marker (or a progress field
        # following one) — while true, key=value lines get swallowed
        # into the progress bar/step/speed readout instead of logged.
        # Reset at the start of every run and when one exits, same as
        # _live_end_marked/_live_trimmed above.
        self._in_progress_step = False
        self._progress_step_duration: float | None = None
        # When the current run started (time.monotonic(), so a system
        # clock change mid-run can't skew it) — set in _start(), read in
        # _on_process_exit() to show "took ..." in place of the last
        # speed reading once the run finishes; see _format_duration().
        self._run_started_at: float | None = None
        # Set by _run_trim() when it hands 'render' a
        # throwaway temp render-state file instead of overwriting the
        # loaded one — cleaned up in _on_process_exit() once that run
        # finishes, whichever way.
        self._temp_render_state_path: str | None = None
        # The open console-log file handle (see _sync_log_file()/_log()),
        # None while file logging is off (general.log_path blank, or it
        # failed to open). _log_path_raw is the unexpanded general.log_path
        # value the currently-open handle was last opened for, so
        # _sync_log_file() only actually reopens (and re-expands any
        # timestamp) when that setting has genuinely changed, rather than
        # on every config save/autosave.
        self._log_file = None
        self._log_path_raw: str | None = None
        self._queue: "queue.Queue" = queue.Queue()
        self.runner = ProcessRunner(
            on_line=lambda line: self._queue.put(("line", line)),
            on_exit=lambda code: self._queue.put(("exit", code)),
        )

        self._setup_style()

        if not SERVICE_SCRIPT.is_file():
            messagebox.showwarning(
                "service_video.py not found",
                f"Expected to find it at {SERVICE_SCRIPT}. Keep gui.py in the "
                "same folder as service_video.py.",
            )

        self.config_path_var = tk.StringVar(value=str(DEFAULT_CONFIG_PATH))

        # Series (see SERIES_PATH/issue #12) — loaded before _build_body()
        # since the Live/Offline tabs' own Series dropdowns need
        # self.series populated at construction time. _series_comboboxes
        # (every Series dropdown built so far) and _series_bindings (which
        # selector var feeds which intro/outro/duration vars — see
        # _wire_series_selector()) both get appended to as those tabs
        # build their own widgets, below.
        self.series: list[dict] = []
        self._series_comboboxes: list[ttk.Combobox] = []
        self._series_bindings: list[tuple[str, str, str, str, str]] = []
        # The last real series name each selector successfully resolved —
        # what a Series combobox's search text snaps back to if the user
        # types something that never matches a real, visible series (see
        # _make_series_combobox_searchable()). Keyed by selector_key.
        self._series_last_valid: dict[str, str] = {}
        self._load_series()

        self._build_header()
        self._build_body()

        # Built eagerly (but hidden) so every config field/var exists right
        # away — load_config() below, and autosave-before-run later, both
        # need the full field set regardless of whether the user has ever
        # opened the Config window.
        self.config_window = ConfigWindow(self)
        # Same reasoning — built eagerly (but hidden) so st_crf/
        # st_trim_fast_copy/st_encoder/st_encoder_preset exist regardless
        # of whether the user has ever opened it via the Offline tab's
        # "Advanced…" button.
        self.offline_advanced_window = OfflineAdvancedWindow(self)

        # Registered before load_config() below so loading a saved
        # api.enabled: true actually starts it — trace_add("write", ...)
        # fires on every .set(), including a programmatic one from
        # load_config(), not just the user clicking the checkbox by hand.
        self.vars["api_enabled"].trace_add("write", lambda *_args: self._sync_api_to_config())

        if not DEFAULT_CONFIG_PATH.is_file():
            DEFAULT_CONFIG_PATH.write_text(json.dumps(default_config(), indent=2))
            self._log(
                f"[gui] no config.json found next to this script — created a "
                f"starter one at {DEFAULT_CONFIG_PATH} with sensible defaults. "
                "Open Config and fill in the OBS host before running Watch — "
                "ProPresenter is optional (Config > ProPresenter) and only "
                "needed if you want slides to auto-mark start/end instead of "
                "the Mark Start/Mark End buttons."
            )
        self.load_config(str(DEFAULT_CONFIG_PATH))

        self.after(50, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Style
    # ------------------------------------------------------------------

    def _setup_style(self):
        """Flat, modern-ish look built entirely on ttk's stock 'clam' theme
        (no external theming package — keeps the GUI's only requirement a
        Tk-enabled Python, same as everything else in this project)."""
        import tkinter.font as tkfont

        families = set(tkfont.families())

        def pick(*names):
            for name in names:
                if name in families:
                    return name
            return "TkDefaultFont"

        # Mirrors the site's font-family stack (-apple-system, Segoe UI,
        # Roboto, Helvetica, Arial, sans-serif) as closely as a desktop Tk
        # app reasonably can.
        ui_family = pick(
            "Segoe UI", "SF Pro Text", "Helvetica Neue", "Roboto",
            "Helvetica", "Arial", "Cantarell", "DejaVu Sans",
        )
        mono_family = pick("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Courier New")
        self.ui_font = (ui_family, 10)
        self.ui_font_bold = (ui_family, 10, "bold")
        self.mono_font = (mono_family, 10)

        p = PALETTE
        self.configure(bg=p["bg"])
        self.option_add("*Font", self.ui_font)
        # ttk.Combobox's dropdown is a plain Tk Listbox under the hood, not
        # covered by ttk styling — set it via the option database or it'd
        # stay a stock white popup against the dark theme.
        self.option_add("*TCombobox*Listbox.background", p["surface"])
        self.option_add("*TCombobox*Listbox.foreground", p["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["accent_contrast"])

        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=p["bg"], foreground=p["text"], font=self.ui_font)
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        style.configure("Header.TLabel", background=p["bg"], foreground=p["text"], font=self.ui_font_bold)

        style.configure(
            "TLabelframe", background=p["bg"], bordercolor=p["border"],
            lightcolor=p["border"], darkcolor=p["border"],
            relief="solid", borderwidth=1,
        )
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["text"], font=self.ui_font_bold)

        style.configure(
            "TEntry", fieldbackground=p["surface"], foreground=p["text"],
            bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
            borderwidth=1, padding=6, insertcolor=p["text"],
        )
        style.map("TEntry", bordercolor=[("focus", p["accent"])])

        style.configure(
            "TCombobox", fieldbackground=p["surface"], background=p["surface"],
            foreground=p["text"], bordercolor=p["border"],
            lightcolor=p["border"], darkcolor=p["border"],
            arrowcolor=p["muted"], padding=5,
        )
        style.map("TCombobox", fieldbackground=[("readonly", p["surface"])])

        style.configure(
            "TSpinbox", fieldbackground=p["surface"], foreground=p["text"],
            bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
            arrowcolor=p["muted"], borderwidth=1, padding=6, insertcolor=p["text"],
        )
        style.map(
            "TSpinbox",
            bordercolor=[("focus", p["accent"])],
            arrowcolor=[("pressed", p["accent"])],
        )

        style.configure(
            "TButton", background=p["surface"], foreground=p["text"],
            bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
            borderwidth=1, padding=(8, 4), relief="flat",
        )
        style.map(
            "TButton",
            background=[("active", "#4b4a4b"), ("disabled", p["button_disabled_bg"])],
            foreground=[("disabled", p["muted"])],
        )

        style.configure(
            "Accent.TButton", background=p["accent"], foreground=p["accent_contrast"],
            bordercolor=p["accent"], lightcolor=p["accent"], darkcolor=p["accent"],
            borderwidth=0, padding=(10, 5), font=self.ui_font_bold,
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", p["button_disabled_bg"]),
                ("pressed", p["accent_active"]),
                ("active", p["accent_hover"]),
            ],
            bordercolor=[("disabled", p["button_disabled_bg"])],
            lightcolor=[("disabled", p["button_disabled_bg"])],
            darkcolor=[("disabled", p["button_disabled_bg"])],
            foreground=[("disabled", p["muted"])],
        )

        style.configure(
            "Danger.TButton", background=p["danger"], foreground="#ffffff",
            bordercolor=p["danger"], lightcolor=p["danger"], darkcolor=p["danger"],
            borderwidth=0, padding=(10, 5), font=self.ui_font_bold,
        )
        style.map(
            "Danger.TButton",
            background=[("disabled", p["button_disabled_bg"]), ("active", p["danger_hover"])],
            bordercolor=[("disabled", p["button_disabled_bg"])],
            lightcolor=[("disabled", p["button_disabled_bg"])],
            darkcolor=[("disabled", p["button_disabled_bg"])],
            foreground=[("disabled", p["muted"])],
        )

        style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        style.configure("TRadiobutton", background=p["bg"], foreground=p["text"])
        style.map("TRadiobutton", background=[("active", p["bg"])])

        style.configure(
            "TNotebook", background=p["bg"], bordercolor=p["bg"],
            lightcolor=p["bg"], darkcolor=p["bg"], borderwidth=0,
        )
        style.configure(
            "TNotebook.Tab", background=p["bg"], foreground=p["muted"],
            bordercolor=p["bg"], lightcolor=p["bg"], darkcolor=p["bg"],
            padding=(14, 5), borderwidth=0, font=self.ui_font,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", p["surface"])],
            foreground=[("selected", p["text"])],
            bordercolor=[("selected", p["surface"])],
            lightcolor=[("selected", p["surface"])],
            darkcolor=[("selected", p["surface"])],
            # clam's stock theme maps both extra padding AND expand onto
            # the selected tab by default, to make it grow into the pane
            # border. Both are per-state maps set up by the theme itself,
            # so pinning padding/expand to the same fixed value on every
            # state here overrides that and keeps all tabs identically
            # sized whether selected or not.
            padding=[("selected", (14, 5)), ("!selected", (14, 5))],
            expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
        )

        style.configure(
            "Treeview", background=p["surface"], fieldbackground=p["surface"],
            foreground=p["text"], bordercolor=p["border"],
            lightcolor=p["surface"], darkcolor=p["surface"], borderwidth=1, rowheight=26,
        )
        style.configure(
            "Treeview.Heading", background=p["bg"], foreground=p["text"],
            bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
            font=self.ui_font_bold, relief="flat", borderwidth=1,
        )
        style.map(
            "Treeview.Heading", background=[("active", p["bg"])],
        )
        style.map(
            "Treeview", background=[("selected", p["accent"])],
            foreground=[("selected", p["accent_contrast"])],
        )

        style.configure(
            "TScrollbar", background=p["border"], troughcolor=p["bg"],
            bordercolor=p["bg"], lightcolor=p["border"], darkcolor=p["border"],
            arrowcolor=p["muted"], relief="flat",
        )
        style.map("TScrollbar", background=[("active", p["muted"])])

        style.configure(
            "TScale", background=p["bg"], troughcolor=p["surface"],
            bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"],
        )
        style.map("TScale", background=[("active", p["bg"])])

        style.configure(
            "TPanedwindow", background=p["bg"], bordercolor=p["bg"],
            lightcolor=p["bg"], darkcolor=p["bg"],
        )
        style.configure(
            "Sash", sashthickness=6, gripcount=0,
            bordercolor=p["bg"], lightcolor=p["bg"], darkcolor=p["bg"],
        )
        style.configure("TSeparator", background=p["border"])

    # ------------------------------------------------------------------
    # Layout — main window
    # ------------------------------------------------------------------

    def _build_header(self):
        header = ttk.Frame(self, padding=8)
        header.pack(fill="x")
        ttk.Label(header, text="Service Video", style="Header.TLabel").pack(side="left")
        ttk.Button(header, text="Config", command=self._open_config_window).pack(side="right")

    def _open_config_window(self):
        self.config_window.deiconify()
        self.config_window.lift()
        self.config_window.focus_set()

    def _build_body(self):
        paned = ttk.Panedwindow(self, orient=tk.VERTICAL)
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.mode_notebook = ttk.Notebook(paned)
        paned.add(self.mode_notebook, weight=3)

        self._build_live_tab()
        self._build_offline_tab()
        self._build_bulk_render_tab()
        self._build_series_tab()

        console_frame = ttk.Frame(paned, padding=(0, 6, 0, 0))
        paned.add(console_frame, weight=2)
        self._build_console(console_frame)

    def _make_scrollable_tab(self, notebook, title, padding=10):
        """Adds a tab to notebook wrapped in a vertically-scrolling Canvas,
        so its fields stay reachable when the window's too short to show
        them all at once. Returns (outer, inner): outer is the actual tab
        widget notebook.add() saw — keep that around for anything needing
        to reference the tab itself (e.g. notebook.select()) — inner is a
        plain Frame to build the tab's content into exactly as if it were
        the tab."""
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=title)

        canvas = tk.Canvas(outer, highlightthickness=0, background=PALETTE["bg"])
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        # vscroll itself is packed/unpacked on demand below, only when
        # there's actually something to scroll — not unconditionally here.

        inner = ttk.Frame(canvas, padding=padding)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def update_scroll(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            bbox = canvas.bbox("all")
            content_height = (bbox[3] - bbox[1]) if bbox else 0
            needs_scroll = content_height > canvas.winfo_height()
            if needs_scroll and not vscroll.winfo_ismapped():
                vscroll.pack(side="right", fill="y")
            elif not needs_scroll and vscroll.winfo_ismapped():
                vscroll.pack_forget()

        inner.bind("<Configure>", update_scroll)

        def sync_inner_width(event):
            # Stretch content to the canvas's actual width instead of a
            # fixed narrow one, so the grid's own column weighting inside
            # still works as the window is resized.
            canvas.itemconfigure(window_id, width=event.width)
            update_scroll()

        canvas.bind("<Configure>", sync_inner_width)

        def on_mousewheel(event):
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def bind_wheel_tree(widget):
            # A plain Enter/Leave on the canvas isn't enough: any content
            # widget sitting on top of it (an Entry, Label, Combobox...)
            # captures Enter/Leave itself once the pointer is over it, so
            # the canvas's own binding never refires while hovering an
            # actual field — only over bare canvas background, which
            # content mostly covers. Binding directly on every widget in
            # the tree sidesteps that; add="+" so nothing already bound to
            # a widget (unlikely here, but safe) gets clobbered.
            widget.bind("<MouseWheel>", on_mousewheel, add="+")
            widget.bind("<Button-4>", on_mousewheel, add="+")
            widget.bind("<Button-5>", on_mousewheel, add="+")
            for child in widget.winfo_children():
                bind_wheel_tree(child)

        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", on_mousewheel)
        canvas.bind("<Button-5>", on_mousewheel)
        vscroll.bind("<MouseWheel>", on_mousewheel)
        # inner has no children yet — the caller adds this tab's fields
        # after this method returns. after_idle still fires before the
        # event loop actually starts taking user input: App()/
        # ConfigWindow() finish constructing every tab first; .mainloop()
        # is the first blocking call — so by the time this runs, every
        # widget in this tab is guaranteed to exist.
        inner.after_idle(lambda: bind_wheel_tree(inner))

        return outer, inner

    def _labeled_entry(self, parent, row, label, key, width=30, show=None, col=0, pad_left=0, colspan=1, help_text=None):
        # Bold field labels, matching the site's `label { font-weight: 600 }`.
        # pad_left adds breathing room before a label that sits right after
        # a previous column's entry (e.g. a second field sharing a row).
        # colspan lets a lone field (nothing sharing its row) absorb every
        # weighted column instead of just the one right after its label —
        # same idea as _crf_slider's colspan. help_text, if given, shows as
        # a hover tooltip on the label and the entry, same as _crf_slider.
        label_widget = ttk.Label(parent, text=label, style="Header.TLabel")
        label_widget.grid(row=row, column=col, sticky="w", padx=(pad_left, 6), pady=3)
        var = tk.StringVar()
        entry = ttk.Entry(parent, textvariable=var, width=width, show=show or "")
        entry.grid(row=row, column=col + 1, columnspan=colspan, sticky="ew", pady=3)
        self.vars[key] = var
        if help_text:
            Tooltip(label_widget, help_text, font=self.ui_font)
            Tooltip(entry, help_text, font=self.ui_font)
        return entry

    def _labeled_combobox(self, parent, row, label, key, values, width=30, col=0, pad_left=0, colspan=1):
        # Editable (not readonly) — values are suggestions, not the only
        # legal input (e.g. ffmpeg's xfade also takes a custom expression).
        ttk.Label(parent, text=label, style="Header.TLabel").grid(
            row=row, column=col, sticky="w", padx=(pad_left, 6), pady=3
        )
        var = tk.StringVar()
        combo = ttk.Combobox(parent, textvariable=var, values=values, width=width)
        combo.grid(row=row, column=col + 1, columnspan=colspan, sticky="ew", pady=3)
        self.vars[key] = var
        return combo

    def _wire_encoder_preset_choices(self, encoder_key: str, preset_combo, preset_key: str):
        """Keep an Encoder preset combobox's values (and current value, if
        it's not one of them) in sync with whichever Encoder is currently
        selected — preset names aren't shared across encoders (see
        ENCODER_PRESET_CHOICES), so the dropdown has to swap its whole
        option list rather than just filtering, and reset to the newly-
        selected encoder's own default if the old value doesn't carry over."""
        def refresh(*_):
            encoder = self.vars[encoder_key].get()
            choices = ENCODER_PRESET_CHOICES.get(encoder, [])
            preset_combo.configure(values=choices)
            if self.vars[preset_key].get() not in choices:
                self.vars[preset_key].set(ENCODER_DEFAULT_PRESETS.get(encoder, ""))
        self.vars[encoder_key].trace_add("write", refresh)
        refresh()

    def _labeled_spinbox(
        self, parent, row, label, key, from_=-30.0, to=30.0, increment=0.1,
        default="0.0", width=10, col=0, pad_left=0, colspan=1, help_text=None,
    ):
        # Still free-typeable (not just up/down-clickable) — a Spinbox is
        # an Entry with increment/decrement arrows attached, not a
        # restricted picker.
        label_widget = ttk.Label(parent, text=label, style="Header.TLabel")
        label_widget.grid(row=row, column=col, sticky="w", padx=(pad_left, 6), pady=3)
        var = tk.StringVar(value=default)
        spin = ttk.Spinbox(
            parent, textvariable=var, from_=from_, to=to, increment=increment, width=width,
        )
        spin.grid(row=row, column=col + 1, columnspan=colspan, sticky="ew", pady=3)
        self.vars[key] = var
        if help_text:
            Tooltip(label_widget, help_text, font=self.ui_font)
            Tooltip(spin, help_text, font=self.ui_font)
        return spin

    def _add_browse(self, parent, row, key, save=False, filetypes=None, col=2):
        filetypes = filetypes or [("All files", "*.*")]

        def do_browse():
            var = self.vars[key]
            if save:
                path = filedialog.asksaveasfilename(filetypes=filetypes, initialdir=str(SCRIPT_DIR))
            else:
                path = filedialog.askopenfilename(filetypes=filetypes, initialdir=str(SCRIPT_DIR))
            if path:
                var.set(path)

        ttk.Button(parent, text="Browse…", command=do_browse).grid(
            row=row, column=col + 1, sticky="w", padx=(4, 0), pady=3
        )

    def _crf_slider(self, parent, row, label, key, col=0, colspan=1, default=23):
        """A 0-51 CRF slider with a live numeric readout, replacing a plain
        text entry for this one field everywhere it appears — stored as an
        IntVar (not the StringVar _labeled_entry uses) so the slider can
        bind to it directly; every place that reads/writes self.vars[key]
        for a CRF field works with that int, not a string."""
        label_widget = ttk.Label(parent, text=label, style="Header.TLabel")
        label_widget.grid(row=row, column=col, sticky="w", padx=(0, 6), pady=3)

        var = tk.IntVar(value=default)
        self.vars[key] = var

        inner = ttk.Frame(parent)
        inner.grid(row=row, column=col + 1, columnspan=colspan, sticky="ew", pady=3)
        inner.columnconfigure(0, weight=1)

        def on_move(raw):
            # ttk.Scale has no integer "resolution" of its own — snap to a
            # whole number on every drag tick so the readout (bound to the
            # same var) never shows a fraction.
            var.set(round(float(raw)))

        scale = ttk.Scale(inner, from_=0, to=51, orient="horizontal", variable=var, command=on_move)
        scale.grid(row=0, column=0, sticky="ew")
        ttk.Label(inner, textvariable=var, style="Muted.TLabel", width=3).grid(
            row=0, column=1, padx=(6, 0)
        )

        Tooltip(label_widget, CRF_HELP, font=self.ui_font)
        Tooltip(scale, CRF_HELP, font=self.ui_font)
        return scale

    def _toggle_show_passwords(self):
        show = "" if self.show_pw_var.get() else "•"
        for entry in self._pw_entries:
            entry.configure(show=show)

    def _build_slide_picker(self, parent, row, prefix, label):
        ttk.Separator(parent, orient="horizontal").grid(
            row=row, column=0, columnspan=4, sticky="ew", pady=(8, 4)
        )
        ttk.Label(parent, text=label, style="Header.TLabel").grid(
            row=row + 1, column=0, columnspan=4, sticky="w"
        )

        mode_var = tk.StringVar(value="uid")
        self.vars[f"{prefix}_mode"] = mode_var
        uid_frame = ttk.Frame(parent)
        text_frame = ttk.Frame(parent)
        uid_frame.columnconfigure(1, weight=1)
        text_frame.columnconfigure(1, weight=1)

        def refresh(*_):
            if mode_var.get() == "uid":
                text_frame.grid_remove()
                uid_frame.grid(row=row + 3, column=0, columnspan=4, sticky="ew")
            else:
                uid_frame.grid_remove()
                text_frame.grid(row=row + 3, column=0, columnspan=4, sticky="ew")

        # Also react to programmatic changes (e.g. load_config(), or the
        # Learn section's "Use as Begin/End Slide" button), not just clicks.
        mode_var.trace_add("write", refresh)

        radio_row = ttk.Frame(parent)
        radio_row.grid(row=row + 2, column=0, columnspan=4, sticky="w")
        ttk.Radiobutton(
            radio_row, text="Match by UID (recommended — use Learn below)",
            variable=mode_var, value="uid", command=refresh,
        ).pack(side="left")
        ttk.Radiobutton(
            radio_row, text="Match by slide text", variable=mode_var, value="text", command=refresh,
        ).pack(side="left", padx=(10, 0))

        uid_var = tk.StringVar()
        self.vars[f"{prefix}_uid"] = uid_var
        ttk.Label(uid_frame, text="UID").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(uid_frame, textvariable=uid_var).grid(row=0, column=1, sticky="ew")

        text_var = tk.StringVar()
        match_mode_var = tk.StringVar(value="exact")
        case_var = tk.BooleanVar(value=False)
        self.vars[f"{prefix}_text"] = text_var
        self.vars[f"{prefix}_match_mode"] = match_mode_var
        self.vars[f"{prefix}_case_sensitive"] = case_var
        ttk.Label(text_frame, text="Text").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(text_frame, textvariable=text_var).grid(row=0, column=1, sticky="ew")
        opts = ttk.Frame(text_frame)
        opts.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        match_mode_combo = ttk.Combobox(
            opts, textvariable=match_mode_var, values=["exact", "regex"],
            width=10, state="readonly",
        )
        match_mode_combo.pack(side="left")
        Tooltip(match_mode_combo, REGEX_HELP, font=self.ui_font)
        ttk.Checkbutton(opts, text="Case sensitive", variable=case_var).pack(side="left", padx=(10, 0))

        refresh()

    # -- Series (named intro/outro bundles — see SERIES_PATH/issue #12) ----

    def _load_series(self):
        if not SERIES_PATH.is_file():
            SERIES_PATH.write_text(json.dumps([], indent=2))
        try:
            data = json.loads(SERIES_PATH.read_text())
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Series", f"Could not read {SERIES_PATH}: {e}")
            data = []
        self.series = data if isinstance(data, list) else []

    def _save_series(self):
        SERIES_PATH.write_text(json.dumps(self.series, indent=2))

    def _series_names(self, include_hidden: bool = False) -> list[str]:
        """Visible (non-hidden) series names by default — what a Series
        dropdown offers. Series Manager's own tree iterates self.series
        directly instead of calling this, since hiding a series from the
        *dropdowns* shouldn't also hide it from the one place that can
        un-hide it again."""
        return [s["name"] for s in self.series if include_hidden or not s.get("hidden", False)]

    def _find_series(self, name: str) -> dict | None:
        # Not filtered by hidden — resolving a name to its intro/outro (or
        # carrying a rename forward, or a Series Manager row edit) has to
        # keep working for a hidden series too, only *offering* it in a
        # dropdown's own list is what "hidden" actually means.
        for s in self.series:
            if s["name"] == name:
                return s
        return None

    def _apply_series_to_vars(
        self, selector_key: str, intro_key: str, intro_duration_key: str, outro_key: str, outro_duration_key: str,
    ):
        series = self._find_series(self.vars[selector_key].get())
        if series is None:
            return
        self._series_last_valid[selector_key] = series["name"]
        self.vars[intro_key].set(series["intro"])
        self.vars[intro_duration_key].set(str(series["intro_duration"]))
        self.vars[outro_key].set(series["outro"])
        self.vars[outro_duration_key].set(str(series["outro_duration"]))

    def _wire_series_selector(
        self, selector_key: str, intro_key: str, intro_duration_key: str, outro_key: str, outro_duration_key: str,
    ):
        """Selecting a series in `selector_key`'s combobox fills in the
        given intro/outro/duration var keys from the matching record (see
        self.series/_find_series()) — this (not a direct Entry) is now
        the normal way the Live/Offline tabs set intro/outro, per issue
        #12. Those 4 target vars are created here too (StringVars),
        since nothing else builds them anymore now that they're not
        directly-editable fields."""
        for key in (intro_key, intro_duration_key, outro_key, outro_duration_key):
            self.vars.setdefault(key, tk.StringVar())
        binding = (selector_key, intro_key, intro_duration_key, outro_key, outro_duration_key)
        self._series_bindings.append(binding)
        self.vars[selector_key].trace_add("write", lambda *_a, b=binding: self._apply_series_to_vars(*b))

    def _make_series_combobox_searchable(self, combo: ttk.Combobox, selector_key: str):
        """Turns a plain Series combobox into a fuzzy-searchable one:
        typing filters its dropdown list (subsequence fuzzy match — see
        fuzzy_match_series()) instead of ttk's own default combobox
        behavior (jumping to the first item starting with the typed
        letter). Selecting an item — click, Down-arrow then Enter (ttk's
        own normal way to post/navigate the dropdown, unaffected by any
        of this), or typing enough to narrow to one and pressing Enter —
        sets `selector_key`'s var exactly like before, so
        _wire_series_selector()'s own trace still does all the actual
        intro/outro resolution; this only changes how a name gets
        *picked*. Deliberately does NOT force the popdown open on every
        keystroke (e.g. via ttk::combobox::Post): that steals focus onto
        the popdown listbox, which fires a spurious <FocusOut> on the
        entry and commits the in-progress text early — confirmed by
        testing. Typing something that never resolves to a real, visible
        series snaps back to the last one that did once focus genuinely
        leaves the field, rather than leaving invalid text sitting there
        and silently going nowhere."""
        combo.configure(state="normal")  # not readonly — typing has to work
        # Keys that navigate/commit rather than edit the text — refiltering
        # on these would fight the very interaction they're performing
        # (e.g. re-opening the list out from under an Escape/Tab-out).
        non_editing_keys = {
            "Up", "Down", "Left", "Right", "Home", "End", "Return", "KP_Enter",
            "Escape", "Tab", "ISO_Left_Tab", "Shift_L", "Shift_R",
            "Control_L", "Control_R", "Alt_L", "Alt_R",
        }

        def on_keyrelease(event):
            if event.keysym in non_editing_keys:
                return
            query = combo.get()
            combo.configure(values=fuzzy_match_series(query, self._series_names()))

        def on_focus_in(_event):
            combo.configure(values=self._series_names())

        def commit(_event=None):
            current = combo.get()
            if self._find_series(current) is None:
                options = combo.cget("values")
                # Typed enough to narrow it to exactly one real match but
                # didn't type the full name — Enter still picks it, the
                # same fzf-style convenience a plain click would give.
                current = options[0] if len(options) == 1 else self._series_last_valid.get(selector_key, "")
            combo.set(current)
            combo.configure(values=self._series_names())

        def on_return(_event):
            commit()
            return "break"  # otherwise ttk's own default Return handling can re-fire selection oddly

        combo.bind("<KeyRelease>", on_keyrelease)
        combo.bind("<FocusIn>", on_focus_in)
        combo.bind("<FocusOut>", commit)
        combo.bind("<Return>", on_return)

    def _resync_series_selectors(self):
        """Re-applies whichever series is currently selected in each
        Series dropdown to its own intro/outro/duration vars — needed
        after saving an *edit* to an existing series: a plain reselect
        wouldn't otherwise notice, since the dropdown's own value didn't
        change, only what that series now points at."""
        for binding in self._series_bindings:
            self._apply_series_to_vars(*binding)

    def _refresh_series_choices(self):
        """Keeps every Series dropdown's option list, and the Series
        Manager tab's own list, in sync with self.series — call after
        any add/edit/duplicate/delete."""
        names = self._series_names()
        for combo in self._series_comboboxes:
            combo.configure(values=names)
        self._resync_series_selectors()
        if hasattr(self, "series_tree"):
            self._refresh_series_tree()

    # -- Series Manager tab --------------------------------------------------

    def _build_series_tab(self):
        self.series_tab, frame = self._make_scrollable_tab(self.mode_notebook, "Series Manager")
        frame.columnconfigure(0, weight=1)

        ttk.Label(
            frame,
            text="Named intro/outro bundles for the Series dropdowns on Live/Offline. "
            "Double-click a row to edit; Hide keeps one out of the dropdowns without "
            "deleting it.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.series_tree = ttk.Treeview(
            frame, columns=("name", "intro", "outro", "hidden"), show="headings", height=10, selectmode="browse",
        )
        self.series_tree.heading("name", text="Name")
        self.series_tree.heading("intro", text="Intro clip")
        self.series_tree.heading("outro", text="Outro clip")
        self.series_tree.heading("hidden", text="Hidden")
        self.series_tree.column("name", width=180, anchor="w")
        self.series_tree.column("intro", width=260, anchor="w")
        self.series_tree.column("outro", width=260, anchor="w")
        self.series_tree.column("hidden", width=60, anchor="center")
        self.series_tree.tag_configure("hidden", foreground=PALETTE["muted"])
        self.series_tree.grid(row=1, column=0, sticky="ew")
        self.series_tree.bind("<Double-1>", lambda _e: self._edit_series())

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Button(btn_row, text="New…", command=self._new_series).pack(side="left")
        ttk.Button(btn_row, text="Edit…", command=self._edit_series).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Duplicate", command=self._duplicate_series).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Hide/Show", command=self._toggle_series_hidden).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Delete", command=self._delete_series).pack(side="left", padx=(8, 0))

        self._refresh_series_tree()

    def _refresh_series_tree(self):
        self.series_tree.delete(*self.series_tree.get_children())
        # Visible series first (the ones actually usable from a dropdown
        # right now), then alphabetically within each group — not insertion
        # order, so the list stays predictable as entries pile up.
        ordered = sorted(self.series, key=lambda s: (bool(s.get("hidden", False)), s["name"].lower()))
        for s in ordered:
            hidden = bool(s.get("hidden", False))
            self.series_tree.insert(
                "", "end", iid=s["name"], values=(s["name"], s["intro"], s["outro"], "Yes" if hidden else ""),
                tags=("hidden",) if hidden else (),
            )

    def _selected_series_name(self) -> str | None:
        selection = self.series_tree.selection()
        return selection[0] if selection else None

    def _new_series(self):
        SeriesEditWindow(self, series=None)

    def _edit_series(self):
        name = self._selected_series_name()
        if not name:
            messagebox.showinfo("Edit series", "Select a series first.")
            return
        series = self._find_series(name)
        if series:
            SeriesEditWindow(self, series=series)

    def _duplicate_series(self):
        name = self._selected_series_name()
        if not name:
            messagebox.showinfo("Duplicate series", "Select a series first.")
            return
        series = self._find_series(name)
        if not series:
            return
        copy = dict(series)
        base = f"{series['name']} (copy)"
        new_name = base
        i = 2
        while self._find_series(new_name):
            new_name = f"{base} {i}"
            i += 1
        copy["name"] = new_name
        self.series.append(copy)
        self._save_series()
        self._refresh_series_choices()
        # Straight into editing the copy — duplicating with no other
        # change would be pointless, so this is almost always followed by
        # at least a rename.
        SeriesEditWindow(self, series=copy)

    def _toggle_series_hidden(self):
        name = self._selected_series_name()
        if not name:
            messagebox.showinfo("Hide/Show series", "Select a series first.")
            return
        series = self._find_series(name)
        if not series:
            return
        series["hidden"] = not series.get("hidden", False)
        self._save_series()
        self._refresh_series_choices()

    def _delete_series(self):
        name = self._selected_series_name()
        if not name:
            messagebox.showinfo("Delete series", "Select a series first.")
            return
        if not messagebox.askyesno("Delete series", f"Delete series {name!r}? This can't be undone."):
            return
        self.series = [s for s in self.series if s["name"] != name]
        self._save_series()
        # A dropdown that had the now-deleted series selected shouldn't
        # keep showing it (or keep whatever intro/outro it last resolved
        # to) — clear both back to blank rather than leave a dangling
        # reference nothing else knows is now stale.
        for selector_key, intro_key, intro_duration_key, outro_key, outro_duration_key in self._series_bindings:
            if self.vars[selector_key].get() == name:
                self.vars[selector_key].set("")
                self.vars[intro_key].set("")
                self.vars[intro_duration_key].set(str(DEFAULT_IMAGE_DURATION))
                self.vars[outro_key].set("")
                self.vars[outro_duration_key].set(str(DEFAULT_IMAGE_DURATION))
                self._series_last_valid.pop(selector_key, None)
        self._refresh_series_choices()

    # -- Live tab (the watch pipeline; only the fields that change week to
    #    week — everything else lives in the Config window) ----------------

    def _build_live_tab(self):
        _outer, frame = self._make_scrollable_tab(self.mode_notebook, "Live")
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Waits for OBS and the begin/end slides, then Trim and Stitch when "
            "you're ready. Connections and trim settings live in Config.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 10))

        series_combo = self._labeled_combobox(frame, 1, "Series", "stitch_series", self._series_names(), width=26)
        self._series_comboboxes.append(series_combo)
        self._wire_series_selector(
            "stitch_series", "stitch_intro", "stitch_intro_duration", "stitch_outro", "stitch_outro_duration",
        )
        self._make_series_combobox_searchable(series_combo, "stitch_series")

        self._labeled_entry(frame, 2, "Output path", "stitch_output", help_text=TIMESTAMP_HELP)
        self._add_browse(frame, 2, "stitch_output", save=True, filetypes=VIDEO_FILETYPES)

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=3, column=0, columnspan=6, sticky="w", pady=(10, 10))
        start_btn = ttk.Button(btn_row, text="Start Watch", style="Accent.TButton", command=self._run_watch)
        start_btn.pack(side="left")
        self._start_buttons.append(start_btn)
        self.mark_start_btn = ttk.Button(
            btn_row, text="Mark Sermon Start", command=self._mark_sermon_start, state="disabled",
        )
        self.mark_start_btn.pack(side="left", padx=(8, 0))
        self.mark_end_btn = ttk.Button(
            btn_row, text="Mark Sermon End", command=self._mark_sermon_end, state="disabled",
        )
        self.mark_end_btn.pack(side="left", padx=(8, 0))
        self.live_trim_btn = ttk.Button(
            btn_row, text="Trim", command=self._trim_live, state="disabled",
        )
        self.live_trim_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.live_trim_btn, LIVE_TRIM_HELP, font=self.ui_font)
        self.live_stitch_btn = ttk.Button(
            btn_row, text="Stitch", command=self._stitch_live, state="disabled",
        )
        self.live_stitch_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.live_stitch_btn, LIVE_STITCH_HELP, font=self.ui_font)
        self.watch_debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn_row, text="Debug", variable=self.watch_debug_var).pack(
            side="left", padx=(10, 0)
        )

        status_frame = ttk.LabelFrame(frame, text="Status", padding=10)
        status_frame.grid(row=4, column=0, columnspan=6, sticky="ew")
        self.watch_state_var = tk.StringVar(value="idle")
        self.watch_status_label = ttk.Label(
            status_frame, textvariable=self.watch_state_var,
            font=(self.ui_font[0], 18, "bold"), foreground=PALETTE["muted"],
        )
        self.watch_status_label.pack(side="left")

        state_path_frame = ttk.Frame(frame, padding=(0, 10, 0, 0))
        state_path_frame.grid(row=5, column=0, columnspan=6, sticky="ew")
        ttk.Label(state_path_frame, text="Last render-state file:").pack(side="left")
        self.render_state_var = tk.StringVar()
        ttk.Entry(state_path_frame, textvariable=self.render_state_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=(4, 4)
        )
        ttk.Button(
            state_path_frame, text="Open in Offline tab",
            command=self._open_last_state_in_offline_tab,
        ).pack(side="left")

    # -- Offline tab (crossfade intro/main/outro; can autofill from a saved
    #    render-state file, but always runs a plain stitch) -----------------

    def _build_offline_tab(self):
        self.offline_tab, frame = self._make_scrollable_tab(self.mode_notebook, "Offline")
        # One shared grid for the whole tab (not a nested sub-frame for the
        # paired fields) so every row's label column lines up at the same
        # width, path fields included. Column 4 is a dedicated, unweighted
        # slot for Browse buttons — path-field entries span columns 1-3
        # (colspan=3) to absorb all the weighted growth themselves, the
        # same trick _crf_slider already uses for its lone field.
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="Crossfades a Series' intro/outro with a trimmed clip. Pick a Series "
            "or fill in fields by hand, or \"Load from JSON\" a render-state file from "
            "a Watch run — Trim writes Main clip's result to Trimmed clip, which "
            "Stitch always reads from.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).grid(row=0, column=0, columnspan=7, sticky="w", pady=(0, 6))

        load_export_row = ttk.Frame(frame)
        load_export_row.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Button(load_export_row, text="Load from JSON…", command=self._browse_render_state).pack(side="left")
        ttk.Button(load_export_row, text="Export to JSON…", command=self._export_render_state).pack(
            side="left", padx=(8, 0)
        )

        series_combo = self._labeled_combobox(frame, 2, "Series", "st_series", self._series_names(), colspan=3)
        self._series_comboboxes.append(series_combo)
        self._wire_series_selector(
            "st_series", "st_intro", "st_intro_duration", "st_outro", "st_outro_duration",
        )
        self._make_series_combobox_searchable(series_combo, "st_series")

        self._labeled_entry(frame, 3, "Main clip", "st_main", colspan=3)
        self._add_browse(frame, 3, "st_main", filetypes=VIDEO_FILETYPES, col=3)
        self._labeled_entry(frame, 4, "Trimmed clip", "st_trimmed", colspan=3, help_text=TIMESTAMP_HELP)
        self._add_browse(frame, 4, "st_trimmed", save=True, filetypes=VIDEO_FILETYPES, col=3)
        self._labeled_entry(frame, 5, "Output path", "st_output", colspan=3, help_text=TIMESTAMP_HELP)
        self._add_browse(frame, 5, "st_output", save=True, filetypes=VIDEO_FILETYPES, col=3)
        self.vars["st_output"].set("output.mp4")

        self._labeled_entry(frame, 6, "Sermon start", "st_start", width=13, col=0)
        self.vars["st_start"].set("00:00:00.000")
        self._labeled_entry(frame, 6, "Sermon end", "st_end", width=13, col=2, pad_left=16)
        self.vars["st_end"].set("00:00:00.000")
        trim_visually_btn = ttk.Button(frame, text="Trim visually…", command=self._open_interactive_trim)
        trim_visually_btn.grid(row=6, column=4, sticky="w", padx=(16, 0))
        Tooltip(
            trim_visually_btn,
            "Pick Sermon start/end by dragging a filmstrip instead of typing "
            "timestamps — like a mobile photo app's trim tool. Needs Main clip "
            "set to a real file first.",
            font=self.ui_font,
        )

        self._labeled_combobox(
            frame, 7, "Transition type", "st_transition", XFADE_TRANSITIONS, width=12, col=0,
        )
        self.vars["st_transition"].set("fade")
        self._labeled_entry(frame, 7, "Transition duration (s)", "st_duration", width=8, col=2, pad_left=16)
        self.vars["st_duration"].set("1.0")

        # Trim/Stitch left-aligned, Advanced… right-aligned, all one row —
        # a single full-width frame (columnspan matching row 0's own
        # wraplength'd label above) with plain pack(side=...) inside it,
        # rather than grid columns, so the two sides can anchor
        # independently without needing to know how wide the row is.
        offline_btn_row = ttk.Frame(frame)
        offline_btn_row.grid(row=8, column=0, columnspan=7, sticky="ew", pady=(10, 0))
        trim_btn = ttk.Button(offline_btn_row, text="Trim", style="Accent.TButton", command=self._run_trim)
        trim_btn.pack(side="left")
        self._start_buttons.append(trim_btn)
        self.offline_stitch_btn = ttk.Button(
            offline_btn_row, text="Stitch", style="Accent.TButton", command=self._run_stitch,
        )
        self.offline_stitch_btn.pack(side="left", padx=(8, 0))
        self._start_buttons.append(self.offline_stitch_btn)
        # CRF/Fast copy/Encoder/Encoder preset live in their own "Advanced"
        # window (see OfflineAdvancedWindow) rather than inline here —
        # tuning knobs set once and rarely touched, unlike everything
        # above, which changes per run.
        ttk.Button(offline_btn_row, text="Advanced…", command=self._open_offline_advanced_window).pack(side="right")
        # Greyed out whenever Trimmed clip is empty — Stitch always reads
        # its main clip from there (never Main clip, which is Trim's own
        # raw source), so there's nothing to crossfade until either Trim
        # has actually produced one (see _handle_offline_trim_line()) or
        # it's filled in by hand/Load from JSON.
        self.vars["st_trimmed"].trace_add("write", self._update_offline_stitch_button)
        self._update_offline_stitch_button()

    def _update_offline_stitch_button(self, *_args):
        has_trimmed = bool(self.vars["st_trimmed"].get().strip())
        self.offline_stitch_btn.configure(state="normal" if has_trimmed else "disabled")

    def _open_offline_advanced_window(self):
        self.offline_advanced_window.deiconify()
        self.offline_advanced_window.lift()
        self.offline_advanced_window.focus_set()

    def _open_interactive_trim(self):
        main_clip = self.vars["st_main"].get().strip()
        if not main_clip:
            messagebox.showerror("Trim visually", "Set Main clip first.")
            return
        if not Path(main_clip).exists():
            messagebox.showerror("Trim visually", f"Main clip not found: {main_clip}")
            return
        try:
            start = to_timestamp(self.vars["st_start"].get().strip() or "00:00:00.000", "Sermon start")
        except ValueError:
            start = 0.0
        try:
            end = to_timestamp(self.vars["st_end"].get().strip() or "00:00:00.000", "Sermon end")
        except ValueError:
            end = 0.0
        # A fresh window every time (not built-once/withdrawn like
        # OfflineAdvancedWindow/ConfigWindow) since it's tied to whichever
        # file Main clip points at right now, which can change between opens.
        InteractiveTrimWindow(self, main_clip, start, end)

    def _browse_render_state(self):
        path = filedialog.askopenfilename(
            title="Select render-state JSON", filetypes=JSON_FILETYPES, initialdir=str(SCRIPT_DIR)
        )
        if path:
            self._load_render_state_json(path)

    def _load_render_state_json(self, path: str):
        try:
            state = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Load from JSON", f"Could not read {path}: {e}")
            return
        trim_cfg = state.get("trim", {})
        stitch_cfg = state.get("stitch", {})
        # Restore by series name if the one this file last saved still
        # exists — the normal case, and _wire_series_selector()'s own
        # trace (fired by the .set() below) fills in st_intro/outro/
        # durations from it automatically. Otherwise (no series key —
        # an older/foreign render-state file — or it's since been
        # renamed/deleted) fall straight back to setting intro/outro/
        # durations directly from whatever this file has saved, same as
        # before series existed.
        saved_series = stitch_cfg.get("series", "")
        if saved_series and self._find_series(saved_series):
            self.vars["st_series"].set(saved_series)
        else:
            self.vars["st_series"].set("")
            if "intro" in stitch_cfg:
                self.vars["st_intro"].set(stitch_cfg["intro"])
            if "outro" in stitch_cfg:
                self.vars["st_outro"].set(stitch_cfg["outro"])
            if "intro_duration" in stitch_cfg:
                self.vars["st_intro_duration"].set(str(stitch_cfg["intro_duration"]))
            if "outro_duration" in stitch_cfg:
                self.vars["st_outro_duration"].set(str(stitch_cfg["outro_duration"]))
        if "output" in stitch_cfg:
            self.vars["st_output"].set(stitch_cfg["output"])
        if "transition_duration" in stitch_cfg:
            self.vars["st_duration"].set(str(stitch_cfg["transition_duration"]))
        if "transition" in stitch_cfg:
            self.vars["st_transition"].set(stitch_cfg["transition"])
        # This one field drives both trim.crf and stitch.crf when Trim/
        # Stitch runs (see _run_trim()/_run_stitch()) — on load, prefer
        # stitch.crf (what actually determines the final video's visible
        # quality) and fall back to trim.crf so a file that only sets one
        # still reflects it.
        crf_val = stitch_cfg.get("crf", trim_cfg.get("crf"))
        if crf_val is not None:
            self.vars["st_crf"].set(max(0, min(51, round(crf_val))))
        if "fast_copy" in trim_cfg:
            self.vars["st_trim_fast_copy"].set(bool(trim_cfg["fast_copy"]))
        # Trim-only (no stitch equivalent — see _run_render()), so no
        # stitch_cfg fallback the way crf/encoder above have.
        if "normalize_audio" in trim_cfg:
            self.vars["st_normalize_audio"].set(bool(trim_cfg["normalize_audio"]))
        if "normalize_target_lufs" in trim_cfg:
            self.vars["st_normalize_target_lufs"].set(str(trim_cfg["normalize_target_lufs"]))
        if "encoder" in stitch_cfg or "encoder" in trim_cfg:
            # Set encoder_preset *after* encoder — see load_config()'s
            # identical comment on why the order matters here.
            self.vars["st_encoder"].set(stitch_cfg.get("encoder", trim_cfg.get("encoder", "nvenc")))
            saved_preset = stitch_cfg.get("encoder_preset") or trim_cfg.get("encoder_preset")
            if saved_preset:
                self.vars["st_encoder_preset"].set(saved_preset)

        # Not just key presence: a render-state file watch() is still
        # incrementally writing (see service_video.py's _write_render_state())
        # can have these keys present but null before that information is
        # actually known yet — treat that the same as missing entirely.
        has_raw = (
            state.get("recording_path") is not None
            and state.get("raw_begin_offset") is not None
            and state.get("raw_end_offset") is not None
        )
        if has_raw:
            # Point Main clip at the raw recording so the timestamp fields
            # below have something meaningful to trim from — see
            # _run_trim(). Shown as the actual computed trim points (raw
            # slide-detected offset + any padding that was applied live),
            # not as a raw/pad split — there's no slide detection here,
            # just a person looking at footage and picking exact
            # timestamps.
            self.vars["st_main"].set(state["recording_path"])
            try:
                start_ts = parse_timestamp(state["raw_begin_offset"]) + trim_cfg.get("pad_start_seconds", 0)
                end_ts = parse_timestamp(state["raw_end_offset"]) + trim_cfg.get("pad_end_seconds", 0)
            except ValueError:
                messagebox.showerror(
                    "Load from JSON",
                    f"Could not parse raw_begin_offset/raw_end_offset in {path} "
                    "(expected HH:MM:SS.mmm or a number of seconds).",
                )
                return
            self.vars["st_start"].set(format_timestamp(start_ts))
            self.vars["st_end"].set(format_timestamp(end_ts))
            self._offline_raw_state = {
                "recording_path": state["recording_path"],
                "state_output": trim_cfg.get("state_output", DEFAULT_STATE_OUTPUT),
                "state_path": path,
            }
            self._log(f"[gui] loaded render fields from {path} (timestamps active — Run will re-trim the raw recording)")
        else:
            # Older/hand-built state file missing the raw recording info —
            # Main clip has nothing meaningful to point at. Reset the
            # timestamp fields so they don't show stale numbers left over
            # from a previous load.
            self.vars["st_start"].set("00:00:00.000")
            self.vars["st_end"].set("00:00:00.000")
            self._offline_raw_state = None
            self._log(
                f"[gui] loaded render fields from {path} (no raw recording info in "
                "this file — timestamps won't apply; Trim needs Main clip and both "
                "set by hand or from a different file)"
            )
        # Trimmed clip: the real trimmed_path if this state already has
        # one (see _write_render_state()/service_video.py) — a trim
        # really happened for it — else blank, not a guess at
        # trim.output: Stitch's own greying (_update_offline_stitch_button())
        # keys off this field being non-empty, so pre-filling it with a
        # destination Trim hasn't actually written yet would make Stitch
        # look ready when it isn't. Trim falls back to trim.output's own
        # default on its own once clicked, blank field or not — see
        # _run_trim(). Independent of has_raw above, so this applies
        # either way.
        self.vars["st_trimmed"].set(state.get("trimmed_path") or "")
        # Covers the one case a plain Trimmed-clip trace can't: it may not
        # have changed value even though the render-state file (and so
        # what Stitch would actually use) has — see
        # _update_offline_stitch_button().
        self._update_offline_stitch_button()

    def _open_last_state_in_offline_tab(self):
        path = self.render_state_var.get().strip()
        if path:
            self._load_render_state_json(path)
        self.mode_notebook.select(self.offline_tab)

    # -- Bulk Render tab (trim/stitch many render-state files in one go —
    #    a JSON array of the same self-contained state dicts 'watch'/
    #    'render'/the Offline tab's own "Load from JSON" already use) -----

    def _build_bulk_render_tab(self):
        self.bulk_render_tab, frame = self._make_scrollable_tab(self.mode_notebook, "Bulk Render")
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Trim and/or stitch every render-state entry in a JSON array file, "
            "in one pass. Trim writes each entry's Trimmed clip back to the file, so "
            "a later Stitch (or Full Render, which does both per entry) picks it up.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        self._labeled_entry(frame, 1, "Render states JSON", "bulk_states_path", colspan=3, help_text=BULK_STATES_HELP)
        self._add_browse(frame, 1, "bulk_states_path", filetypes=JSON_FILETYPES, col=3)
        self.vars["bulk_states_path"].trace_add("write", lambda *_a: self._refresh_bulk_render_tree())

        self.bulk_render_tree = ttk.Treeview(
            frame, columns=("index", "recording", "trimmed", "output"), show="headings",
            height=10, selectmode="browse",
        )
        self.bulk_render_tree.heading("index", text="#")
        self.bulk_render_tree.heading("recording", text="Recording")
        self.bulk_render_tree.heading("trimmed", text="Trimmed clip")
        self.bulk_render_tree.heading("output", text="Output")
        self.bulk_render_tree.column("index", width=40, anchor="center")
        self.bulk_render_tree.column("recording", width=260, anchor="w")
        self.bulk_render_tree.column("trimmed", width=260, anchor="w")
        self.bulk_render_tree.column("output", width=200, anchor="w")
        self.bulk_render_tree.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.bulk_trim_btn = ttk.Button(
            btn_row, text="Trim", command=lambda: self._run_bulk_render("trim"), state="disabled",
        )
        self.bulk_trim_btn.pack(side="left")
        self.bulk_stitch_btn = ttk.Button(
            btn_row, text="Stitch", command=lambda: self._run_bulk_render("stitch"), state="disabled",
        )
        self.bulk_stitch_btn.pack(side="left", padx=(8, 0))
        self.bulk_full_btn = ttk.Button(
            btn_row, text="Full Render", style="Accent.TButton",
            command=lambda: self._run_bulk_render("full"), state="disabled",
        )
        self.bulk_full_btn.pack(side="left", padx=(8, 0))
        self._start_buttons.append(self.bulk_trim_btn)
        self._start_buttons.append(self.bulk_stitch_btn)
        self._start_buttons.append(self.bulk_full_btn)

        self.bulk_states: list[dict] = []
        self._refresh_bulk_render_tree()

    def _refresh_bulk_render_tree(self):
        """Reloads the Bulk Render tab's list from whatever's currently at
        `bulk_states_path` — called on every edit to that field (typing or
        Browse…) and again after a bulk run finishes (see
        _on_process_exit()), since Trim/Full Render rewrite the file's own
        trimmed_path fields as they go. A path that's blank, unreadable, or
        not a JSON array of objects just empties the list (with a console
        log line for the latter two, same as any other bad-input report in
        this app) rather than raising — the same tolerant, keep-going
        spirit bulk-render itself uses per-entry, applied here to the file
        as a whole."""
        self.bulk_render_tree.delete(*self.bulk_render_tree.get_children())
        self.bulk_states = []
        path = self.vars["bulk_states_path"].get().strip()
        if path:
            try:
                data = json.loads(Path(path).read_text())
            except (OSError, json.JSONDecodeError) as e:
                self._log(f"[gui] could not read {path!r} as JSON: {e}")
                data = None
            if data is not None and not (isinstance(data, list) and all(isinstance(s, dict) for s in data)):
                self._log(f"[gui] {path!r} must contain a JSON array of render-state objects.")
                data = None
            if data:
                self.bulk_states = data
        for i, state in enumerate(self.bulk_states):
            self.bulk_render_tree.insert(
                "", "end", iid=str(i),
                values=(
                    i + 1, state.get("recording_path") or "", state.get("trimmed_path") or "",
                    (state.get("stitch") or {}).get("output") or "",
                ),
            )
        self._update_bulk_render_buttons()

    def _update_bulk_render_buttons(self):
        state = "normal" if self.bulk_states else "disabled"
        for btn in (self.bulk_trim_btn, self.bulk_stitch_btn, self.bulk_full_btn):
            btn.configure(state=state)

    def _run_bulk_render(self, mode: str):
        path = self.vars["bulk_states_path"].get().strip()
        if not path or not self.bulk_states:
            messagebox.showerror("Bulk Render", "Load a valid render states JSON file first.")
            return
        self._start(f"bulk_{mode}", ["bulk-render", path, "--mode", mode])

    # -- Console ----------------------------------------------------------

    def _build_console(self, parent):
        p = PALETTE
        header = ttk.Frame(parent)
        header.pack(fill="x")
        ttk.Label(header, text="Console output", style="Header.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="idle")
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.pack(side="left", padx=(10, 0))

        # ffmpeg's own progress (see _handle_progress_line()) — step
        # count on the left, a determinate bar, render speed on the
        # right, replacing the wall of frame=.../time=.../speed=... lines
        # ffmpeg would otherwise print once per step. All start blank/at
        # 0 and stay that way outside of a step actually reporting
        # progress (most non-ffmpeg output, and 'learn', never touch
        # these at all).
        self.progress_step_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.progress_step_var, style="Muted.TLabel").pack(
            side="left", padx=(10, 4)
        )
        self.progress_bar = ttk.Progressbar(header, mode="determinate", maximum=100, length=140)
        self.progress_bar.pack(side="left")
        self.progress_speed_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.progress_speed_var, style="Muted.TLabel").pack(
            side="left", padx=(4, 0)
        )

        ttk.Button(header, text="Clear", command=self._clear_console).pack(side="right")
        self.stop_button = ttk.Button(
            header, text="Stop", style="Danger.TButton", command=self._stop, state="disabled"
        )
        self.stop_button.pack(side="right", padx=(0, 6))

        text_frame = ttk.Frame(parent)
        text_frame.pack(fill="both", expand=True, pady=(4, 0))
        self.console = tk.Text(
            text_frame, height=12, wrap="word", state="disabled",
            background=p["console_bg"], foreground=p["console_fg"],
            insertbackground=p["console_fg"], selectbackground=p["accent"],
            selectforeground=p["accent_contrast"], font=self.mono_font,
            borderwidth=0, highlightthickness=1, highlightbackground=p["border"],
            highlightcolor=p["accent"], padx=10, pady=8,
        )
        console_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=console_scroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        console_scroll.pack(side="left", fill="y")

    def _clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _log(self, line: str):
        self.console.configure(state="normal")
        self.console.insert("end", line + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")
        if self._log_file:
            try:
                self._log_file.write(line + "\n")
                self._log_file.flush()
            except OSError:
                # Don't let a mid-session write failure (e.g. the disk
                # filling up, or the file's been deleted/unmounted out from
                # under this) take the console pane down with it — just
                # stop trying to write to a file that's stopped working.
                self._log_file = None

    def _sync_log_file(self):
        """(Re)opens the console log file at general.log_path if that
        setting has actually changed since the last time this ran —
        called wherever a changed log_path takes effect: after loading a
        config, after Save Config, and after the autosave a run does
        before starting. A blank path turns file logging off. The path is
        expanded (any timestamp placeholder filled in) once, right here,
        rather than on every line _log() writes — so a whole session's
        console output lands in one file instead of a new one per line —
        which is also why this only reopens on an actual change rather
        than every time one of those callers runs, even with an unchanged
        path: reopening on every autosave (before every single run) would
        otherwise mean a session's output gets fragmented across a new
        timestamped file per run instead of staying in one place."""
        raw = self.vars["log_path"].get().strip()
        if raw == self._log_path_raw and (self._log_file or not raw):
            return
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
        self._log_path_raw = raw
        if not raw:
            return
        try:
            expanded = expand_output_path(raw)
            self._log_file = open(expanded, "a", encoding="utf-8")
        except OSError as e:
            self._log(f"[gui] couldn't open log file {raw!r}: {e}")

    # ------------------------------------------------------------------
    # Control API — hosted here in the GUI itself (see _build_api_app()),
    # not inside a `watch` subprocess: it needs to run all the time, not
    # just while one happens to be active, so it starts/stops with
    # Config > API's "Enabled" checkbox instead (see the trace_add() in
    # __init__) and proxies Mark Sermon Start/End to whichever `watch`
    # subprocess (if any) is currently running the same way its own
    # buttons already do — see _mark_sermon_start()/_mark_sermon_end().
    # ------------------------------------------------------------------

    def _sync_api_to_config(self):
        """Starts, stops, or restarts the control API so it matches
        Config > API's current fields — called by api_enabled's own
        trace (so ticking/unticking the checkbox takes effect
        immediately) and after every config save/autosave (so an edited
        host/port/password takes effect without needing an explicit
        untick-retick)."""
        if self.vars["api_enabled"].get():
            self._stop_api()  # restart-in-place if already running, picking up new host/port/password
            self._start_api()
        else:
            self._stop_api()

    def _start_api(self):
        try:
            import uvicorn
        except ImportError:
            self._log(
                "[gui] Config > API is enabled, but fastapi/uvicorn aren't installed — "
                "the control API won't start (pip install fastapi uvicorn)"
            )
            return
        app = _build_api_app(self)
        host = self.vars["api_host"].get().strip() or "127.0.0.1"
        port = to_int(self.vars["api_port"].get().strip() or "8765", "API port")
        self._api_server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        self._api_thread = threading.Thread(target=self._api_server.run, daemon=True)
        self._api_thread.start()
        self._log(f"[gui] control API listening on http://{host}:{port} — Swagger UI: http://{host}:{port}/swagger")

    def _stop_api(self):
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._api_thread is not None:
            self._api_thread.join(timeout=5)
        self._api_server = None
        self._api_thread = None

    def _call_on_main_thread(self, fn):
        """Runs `fn` (no args) on Tk's own main thread and returns its
        result — the control API's routes run on uvicorn's own thread(s),
        and Tk state (StringVars, widgets, ProcessRunner) isn't safe to
        touch from anywhere else. self.after(0, ...) is the standard safe
        way to hand work to Tk's event loop from another thread; the
        Event is just this call waiting for that handoff to actually
        run (near-instant in practice — Tk's next idle turn — the
        timeout below is only a safety net, not the expected path)."""
        result: dict = {}
        done = threading.Event()

        def run():
            result["value"] = fn()
            done.set()

        self.after(0, run)
        if not done.wait(timeout=5):
            return None
        return result.get("value")

    def _live_mark_applicable(self, which: str) -> bool:
        """Whether Mark Sermon Start (which="start") / Mark Sermon End
        (which="end") would currently be accepted if clicked right now —
        the exact conditions _update_live_buttons() already uses to
        enable those buttons, factored out here so the control API's
        mark validation (see _build_api_app()) shares this one source of
        truth instead of a second copy of these state sets."""
        if which == "start":
            return self._live_raw_state in ("WAIT_BEGIN_SLIDE", "WAIT_END_SLIDE")
        return self._live_raw_state in ("WAIT_END_SLIDE", "WAIT_RECORD_STOP")

    # ------------------------------------------------------------------
    # Config load/save (widgets live in the main window's Live tab and in
    # ConfigWindow; this state and the load/save logic live here on App).
    # ------------------------------------------------------------------

    def _browse_config(self):
        path = filedialog.askopenfilename(
            title="Select config JSON", filetypes=JSON_FILETYPES, initialdir=str(SCRIPT_DIR)
        )
        if path:
            self.config_path_var.set(path)
            self.load_config(path)

    def load_config(self, path=None):
        path = Path(path or self.config_path_var.get().strip())
        if not path.is_file():
            messagebox.showerror("Load config", f"File not found: {path}")
            return
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            messagebox.showerror("Load config", f"Invalid JSON: {e}")
            return

        general = cfg.get("general", {})
        api = cfg.get("api", {})
        pp = cfg.get("propresenter", {})
        obs = cfg.get("obs", {})
        trim = cfg.get("trim", {})
        stitch = cfg.get("stitch", {})

        self.vars["log_path"].set(general.get("log_path", DEFAULT_LOG_PATH))

        # host/port/password set *before* enabled: enabled has a trace
        # (see _sync_api_to_config()) that starts/restarts the API using
        # whatever these three currently hold, so they need to already be
        # this config's values by the time that fires, not the previous
        # config's (or a fresh window's defaults).
        self.vars["api_host"].set(api.get("host", "127.0.0.1"))
        self.vars["api_port"].set(str(api.get("port", 8765)))
        self.vars["api_password"].set(api.get("password", ""))
        self.vars["api_enabled"].set(bool(api.get("enabled", False)))

        self.vars["pp_host"].set(pp.get("host", ""))
        self.vars["pp_port"].set(str(pp.get("port", "")))
        self.vars["pp_password"].set(pp.get("password", ""))
        self.vars["pp_reconnect"].set(str(pp.get("reconnect_interval_seconds", 4)))
        self._load_slide(pp.get("begin_slide", {}), "begin")
        self._load_slide(pp.get("end_slide", {}), "end")

        self.vars["obs_host"].set(obs.get("host", ""))
        self.vars["obs_port"].set(str(obs.get("port", "")))
        self.vars["obs_password"].set(obs.get("password", ""))

        self.vars["trim_output"].set(trim.get("output", "body_trimmed.mp4"))
        self.vars["trim_state_output"].set(trim.get("state_output", DEFAULT_STATE_OUTPUT))
        self.vars["trim_pad_start"].set(str(trim.get("pad_start_seconds", 0)))
        self.vars["trim_pad_end"].set(str(trim.get("pad_end_seconds", 0)))
        self.vars["trim_crf"].set(max(0, min(51, round(trim.get("crf", 23)))))
        self.vars["trim_fast_copy"].set(bool(trim.get("fast_copy", True)))
        self.vars["trim_normalize_audio"].set(bool(trim.get("normalize_audio", True)))
        self.vars["trim_normalize_target_lufs"].set(str(trim.get("normalize_target_lufs", -16.0)))
        # One shared control for both trim.encoder and stitch.encoder (see
        # collect_config()) — on load, prefer stitch's (what determines
        # the final video's encoder, same as the Offline tab's CRF field
        # prefers stitch.crf for the same reason) and fall back to trim's.
        # Set encoder_preset *after* encoder — setting encoder resets it
        # to that encoder's own default (see _wire_encoder_preset_choices()),
        # which a saved preset value should override, not the other way
        # around.
        self.vars["encoder"].set(stitch.get("encoder", trim.get("encoder", "nvenc")))
        saved_preset = stitch.get("encoder_preset") or trim.get("encoder_preset")
        if saved_preset:
            self.vars["encoder_preset"].set(saved_preset)

        self.vars["stitch_auto"].set(bool(stitch.get("auto", True)))
        # Restore by series name if the one this config last saved still
        # exists — the normal case, and _wire_series_selector()'s own
        # trace (fired by the .set() below) fills in stitch_intro/outro/
        # durations from it automatically. Otherwise (no series key at
        # all — an older config.json — or it's since been renamed/
        # deleted) fall straight back to the raw values already saved
        # here, same as before series existed, rather than going blank.
        saved_series = stitch.get("series", "")
        if saved_series and self._find_series(saved_series):
            self.vars["stitch_series"].set(saved_series)
        else:
            self.vars["stitch_series"].set("")
            self.vars["stitch_intro"].set(stitch.get("intro", ""))
            self.vars["stitch_outro"].set(stitch.get("outro", ""))
            self.vars["stitch_intro_duration"].set(str(stitch.get("intro_duration", DEFAULT_IMAGE_DURATION)))
            self.vars["stitch_outro_duration"].set(str(stitch.get("outro_duration", DEFAULT_IMAGE_DURATION)))
        self.vars["stitch_output"].set(stitch.get("output", "final.mp4"))
        self.vars["stitch_transition_duration"].set(str(stitch.get("transition_duration", 1.0)))
        self.vars["stitch_transition"].set(stitch.get("transition", "fade"))
        self.vars["stitch_subsplash_preset"].set(bool(stitch.get("subsplash_preset", False)))

        self.config_path_var.set(str(path))
        self._sync_log_file()
        self._log(f"[gui] loaded config from {path}")

    def _load_slide(self, slide_cfg: dict, prefix: str):
        if slide_cfg.get("uid"):
            self.vars[f"{prefix}_mode"].set("uid")
            self.vars[f"{prefix}_uid"].set(slide_cfg.get("uid", ""))
        else:
            self.vars[f"{prefix}_mode"].set("text")
            self.vars[f"{prefix}_text"].set(slide_cfg.get("text", ""))
            # "exact" is the default for a slide config with no match_mode
            # key at all; a config saved before regex support existed may
            # still say "substring" — leave that as-is rather than
            # silently rewriting it out from under the user, since
            # service_video.py still honors it (a plain literal pattern
            # behaves the same under the new regex matching anyway).
            self.vars[f"{prefix}_match_mode"].set(slide_cfg.get("match_mode", "exact"))
            self.vars[f"{prefix}_case_sensitive"].set(bool(slide_cfg.get("case_sensitive", False)))

    def _collect_slide(self, prefix: str) -> dict:
        """A blank UID/text is allowed — ProPresenter is optional (Mark
        Start/Mark End can drive the whole run by hand), so leaving a slide
        unset just means that end never auto-matches rather than blocking
        Save/Start Watch; service_video.py's slide_matches() already treats
        an empty slide config as "never matches"."""
        mode = self.vars[f"{prefix}_mode"].get()
        if mode == "uid":
            uid = self.vars[f"{prefix}_uid"].get().strip()
            return {"uid": uid} if uid else {}
        text = self.vars[f"{prefix}_text"].get().strip()
        if not text:
            return {}
        match_mode = self.vars[f"{prefix}_match_mode"].get()
        if match_mode == "regex":
            try:
                re.compile(text)
            except re.error as e:
                raise ValueError(f"{prefix.capitalize()} slide text isn't a valid regex: {e}")
        return {
            "text": text,
            "match_mode": match_mode,
            "case_sensitive": bool(self.vars[f"{prefix}_case_sensitive"].get()),
        }

    def collect_config(self) -> dict:
        v = self.vars
        return {
            "general": {
                # Unlike the output-path fields below, blank here is a
                # real, meaningful value (file logging off — see
                # LOG_PATH_HELP) rather than "unset, use the default", so
                # this doesn't fall back to DEFAULT_LOG_PATH the way those
                # do.
                "log_path": v["log_path"].get().strip(),
            },
            "api": {
                "enabled": bool(v["api_enabled"].get()),
                "host": v["api_host"].get().strip() or "127.0.0.1",
                "port": to_int(v["api_port"].get().strip() or "8765", "API port"),
                "password": v["api_password"].get(),
            },
            "propresenter": {
                # Host (and everything else here) is optional — see
                # _collect_slide's docstring; a blank port/reconnect
                # interval falls back rather than blocking Save/Start Watch
                # just because ProPresenter isn't being used this run.
                "host": v["pp_host"].get().strip(),
                "port": to_int(v["pp_port"].get().strip() or "1025", "ProPresenter port"),
                "password": v["pp_password"].get(),
                "reconnect_interval_seconds": to_int(v["pp_reconnect"].get().strip() or "4", "Reconnect interval"),
                "begin_slide": self._collect_slide("begin"),
                "end_slide": self._collect_slide("end"),
            },
            "obs": {
                "host": v["obs_host"].get().strip(),
                "port": to_int(v["obs_port"].get().strip(), "OBS port"),
                "password": v["obs_password"].get(),
            },
            "trim": {
                "output": v["trim_output"].get().strip() or "body_trimmed.mp4",
                "state_output": v["trim_state_output"].get().strip() or DEFAULT_STATE_OUTPUT,
                "pad_start_seconds": to_float(v["trim_pad_start"].get().strip() or "0", "Pad start seconds"),
                "pad_end_seconds": to_float(v["trim_pad_end"].get().strip() or "0", "Pad end seconds"),
                "crf": v["trim_crf"].get(),
                "fast_copy": bool(v["trim_fast_copy"].get()),
                "normalize_audio": bool(v["trim_normalize_audio"].get()),
                "normalize_target_lufs": to_float(
                    v["trim_normalize_target_lufs"].get().strip() or "-16.0", "Normalize target LUFS"
                ),
                "encoder": v["encoder"].get(),
                "encoder_preset": v["encoder_preset"].get() or None,
            },
            "stitch": {
                "auto": bool(v["stitch_auto"].get()),
                # Purely for the GUI's own convenience restoring the
                # dropdown selection on load (see load_config()) —
                # service_video.py itself never reads this key.
                "series": v["stitch_series"].get().strip(),
                "intro": v["stitch_intro"].get().strip(),
                "outro": v["stitch_outro"].get().strip(),
                "intro_duration": to_float(
                    v["stitch_intro_duration"].get().strip() or str(DEFAULT_IMAGE_DURATION), "Intro duration"
                ),
                "outro_duration": to_float(
                    v["stitch_outro_duration"].get().strip() or str(DEFAULT_IMAGE_DURATION), "Outro duration"
                ),
                "output": v["stitch_output"].get().strip() or "final.mp4",
                "transition_duration": to_float(
                    v["stitch_transition_duration"].get().strip() or "1.0", "Transition duration"
                ),
                "transition": v["stitch_transition"].get().strip() or "fade",
                "subsplash_preset": bool(v["stitch_subsplash_preset"].get()),
                # Not GUI-exposed (see FAST_COPY_HELP / stitch()'s own
                # docstring in service_video.py for why) — always off here;
                # still settable by hand if that ever changes.
                "fast_copy": False,
                "encoder": v["encoder"].get(),
            },
        }

    def _save_config_clicked(self):
        try:
            cfg = self.collect_config()
        except ValueError as e:
            messagebox.showerror("Config error", str(e))
            return
        path = Path(self.config_path_var.get().strip() or str(DEFAULT_CONFIG_PATH))
        path.write_text(json.dumps(cfg, indent=2))
        self._sync_log_file()
        self._sync_api_to_config()
        self._log(f"[gui] saved config -> {path}")
        messagebox.showinfo("Config saved", f"Saved to {path}")

    def _autosave_for_run(self) -> bool:
        """Save the current form values (across both windows) to the config
        path before watch/learn, so the subprocess always sees what's on
        screen without requiring a separate manual Save click first."""
        try:
            cfg = self.collect_config()
        except ValueError as e:
            messagebox.showerror("Config error", str(e))
            return False
        path = Path(self.config_path_var.get().strip() or str(DEFAULT_CONFIG_PATH))
        path.write_text(json.dumps(cfg, indent=2))
        self.config_path_var.set(str(path))
        self._sync_log_file()
        self._sync_api_to_config()
        self._log(f"[gui] saved config -> {path}")
        return True

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool, command_name: str | None = None):
        for btn in self._start_buttons:
            btn.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.status_var.set(f"running: {command_name}" if busy else "idle")
        self.status_label.configure(foreground=PALETTE["accent"] if busy else PALETTE["muted"])

    def _start(self, command_name: str, args: list[str]):
        if self.runner.running():
            messagebox.showwarning("Busy", "Another operation is already running. Stop it first.")
            return
        self._current_command = command_name
        self._set_busy(True, command_name)
        self._reset_progress()
        self._run_started_at = time.monotonic()
        self._log(f"[gui] running: {' '.join([sys.executable, '-u', str(SERVICE_SCRIPT), *args])}")
        try:
            self.runner.start(args)
        except RuntimeError as e:
            self._log(f"[gui] {e}")
            self._set_busy(False)
            # The process never actually started, so _on_process_exit()
            # (which would otherwise clean this up) never fires either.
            if self._temp_render_state_path:
                Path(self._temp_render_state_path).unlink(missing_ok=True)
                self._temp_render_state_path = None

    def _stop(self):
        if self.runner.running():
            self._log("[gui] stop requested — terminating process (no graceful trim/exit message)...")
            self.runner.stop()

    def _reset_progress(self):
        """Full reset, for the start of a new run — blanks everything,
        including the speed/"took ..." slot. Contrast
        _clear_progress_parsing_state(), used at the end of one instead,
        which leaves the step/bar showing that run's final state."""
        self._clear_progress_parsing_state()
        self.progress_step_var.set("")
        self.progress_bar["value"] = 0
        self.progress_speed_var.set("")

    def _clear_progress_parsing_state(self):
        """Just the internal bookkeeping _handle_progress_line() uses,
        not what's on screen — see _reset_progress()."""
        self._in_progress_step = False
        self._progress_step_duration = None

    def _handle_progress_line(self, line: str) -> bool:
        """Parses one line of ffmpeg's machine-readable progress output
        (service_video.py's --machine-progress/_print_step(), started
        automatically by ProcessRunner) into the "[N/M]"/progress bar/
        speed readout next to the console status, and reports whether it
        did (True) so _drain_queue() skips logging it — meant to replace
        the wall of frame=.../time=.../speed=... lines ffmpeg would
        otherwise print once per step, not add to it."""
        m = PROGRESS_STEP_RE.match(line)
        if m:
            self.progress_step_var.set(f"[{m.group(1)}/{m.group(2)}]")
            self._progress_step_duration = float(m.group(3))
            self._in_progress_step = True
            self.progress_bar["value"] = 0
            self.progress_speed_var.set("")
            return True

        if not self._in_progress_step:
            return False
        fm = PROGRESS_FIELD_RE.match(line)
        if not fm:
            return False
        key, value = fm.group(1), fm.group(2).strip()
        if key == "out_time_us" and self._progress_step_duration:
            # -progress's own out_time_ms field is a long-documented
            # ffmpeg misnomer — it actually carries microseconds too
            # (identical to out_time_us), so out_time_us is used here
            # instead as the one whose name is actually accurate.
            try:
                seconds = int(value) / 1_000_000
                pct = max(0.0, min(100.0, seconds / self._progress_step_duration * 100))
                self.progress_bar["value"] = pct
            except ValueError:
                pass
        elif key == "speed":
            self.progress_speed_var.set(value)
        elif key == "progress" and value == "end":
            self.progress_bar["value"] = 100
        return True

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "line":
                    if not self._handle_progress_line(payload):
                        self._log(payload)
                    if self._current_command == "learn":
                        self._handle_learn_line(payload)
                    elif self._current_command == "watch":
                        self._handle_watch_line(payload)
                    elif self._current_command == "trim":
                        self._handle_offline_trim_line(payload)
                elif kind == "exit":
                    self._on_process_exit(payload)
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)

    def _on_process_exit(self, code: int):
        label = self._current_command or "process"
        # Leaves the step/bar showing this run's final state (rather than
        # blanking them, like _reset_progress() does for a new run) and
        # replaces the live speed reading with how long the whole thing
        # took — a still-useful summary, success or not.
        self._clear_progress_parsing_state()
        if self._run_started_at is not None:
            self.progress_speed_var.set(f"took {format_elapsed(time.monotonic() - self._run_started_at)}")
            self._run_started_at = None
        if self._temp_render_state_path:
            Path(self._temp_render_state_path).unlink(missing_ok=True)
            self._temp_render_state_path = None
        if code == 0:
            self._log(f"[gui] {label} finished successfully.\n")
        else:
            self._log(f"[gui] {label} exited with code {code}.\n")
        if label.startswith("bulk_"):
            # Trim/Full Render rewrite bulk_states_path's own trimmed_path
            # fields as they go (see bulk_render() in service_video.py) —
            # reload so the tab's list (and Stitch/Full Render's own
            # enabled state) reflects that, success or partial failure
            # either way.
            self._refresh_bulk_render_tree()
        if label == "watch":
            if self._live_trim_resolved and self.render_state_var.get().strip():
                # watch() itself now exits once a live Trim resolves (see
                # its own docstring) — from here on, Live Trim/Stitch work
                # the same way Offline's already do: prefill the Offline
                # tab from the render-state file this run wrote (the same
                # "Load from JSON" path, including this session's
                # Stitch-greying), then point Trimmed clip at the trim's
                # own output path if it actually succeeded (captured off
                # the console the same way _handle_offline_trim_line() does
                # for the Offline tab's own Trim — see _handle_watch_line())
                # — the render-state file's own trimmed_path should
                # already agree (service_video.py writes it too), but this
                # is the app's own real-time knowledge, so it wins if not.
                self._load_render_state_json(self.render_state_var.get().strip())
                if self._live_trimmed_path:
                    self.vars["st_trimmed"].set(self._live_trimmed_path)
                self.live_trim_btn.configure(state="normal")
                self.live_stitch_btn.configure(state=str(self.offline_stitch_btn["state"]))
                self.mark_start_btn.configure(state="disabled")
                self.mark_end_btn.configure(state="disabled")
                # No live state machine left to speak of — the control
                # API's mark validation (_live_mark_applicable()) and
                # GET /state should both now report "no active session".
                self._live_raw_state = None
            else:
                # watch() never got to a resolved live Trim this run (e.g.
                # Stop was clicked early) — a nonzero exit code here is the
                # normal case then, not a sign anything went wrong; show it
                # plainly rather than as an error.
                self.watch_state_var.set(f"stopped (exit {code})")
                self.watch_status_label.configure(foreground=PALETTE["muted"])
                self._update_live_buttons(None)
        elif label in ("trim", "stitch") and self._live_handoff_which == label:
            # A post-exit Live Trim/Stitch click (see _trim_live()/
            # _stitch_live()) — reflect its outcome in the Live tab's own
            # status readout the same way _handle_trim_stitch_status() did
            # while watch() was still running, even though this ran as a
            # plain Offline-style process instead. A genuine Offline-tab
            # click never sets _live_handoff_which, so this never fires for
            # those.
            btn = self.live_trim_btn if label == "trim" else self.live_stitch_btn
            if code == 0:
                self.watch_state_var.set({"trim": "Trimmed", "stitch": "Stitched"}[label])
                self.watch_status_label.configure(foreground=PALETTE["success"])
                if label == "trim":
                    self._live_trim_status = "done"
                    # Main clip (and therefore offline_stitch_btn's state)
                    # was already updated as the trim's own output line
                    # streamed in — see _handle_offline_trim_line() — so
                    # this is just mirroring the now-current state onto the
                    # Live tab's own Stitch button, same as right after the
                    # very first live Trim.
                    self.live_stitch_btn.configure(state=str(self.offline_stitch_btn["state"]))
                else:
                    self._live_stitch_status = "done"
            else:
                self.watch_state_var.set({"trim": "Trim failed", "stitch": "Stitch failed"}[label])
                self.watch_status_label.configure(foreground=PALETTE["danger"])
                if label == "trim":
                    self._live_trim_status = "failed"
                else:
                    self._live_stitch_status = "failed"
            btn.configure(state="normal")
            self._live_handoff_which = None
        self._current_command = None
        self._set_busy(False)

    def _handle_learn_line(self, line: str):
        m = SLIDE_RE.match(line)
        if not m:
            return
        uid, text_repr = m.group(1), m.group(2)
        try:
            text = ast.literal_eval(text_repr)
        except Exception:
            text = text_repr
        self.config_window.add_learned_slide(uid, text)

    def _handle_offline_trim_line(self, line: str):
        """Auto-points Trimmed clip at the Offline tab's Trim button's own
        output once it succeeds, so a follow-up Stitch click picks it up
        without the user having to re-Browse to it themselves."""
        m = TRIMMED_PATH_RE.match(line)
        if m:
            self.vars["st_trimmed"].set(m.group(1).strip())

    def _handle_watch_line(self, line: str):
        m = STATE_RE.search(line)
        if m:
            raw_state = m.group(1)
            self.watch_state_var.set(WATCH_STATE_LABELS.get(raw_state, raw_state))
            self.watch_status_label.configure(foreground=PALETTE["accent"])
            self._update_live_buttons(raw_state)
        m2 = RENDER_STATE_PATH_RE.search(line)
        if m2:
            path = m2.group(1).strip()
            self.render_state_var.set(path)
            self._log(f"[gui] captured render-state path for the Offline tab: {path}")
        m3 = TRIM_STITCH_STATUS_RE.search(line)
        if m3:
            self._handle_trim_stitch_status(m3.group(1), m3.group(2))
        # Same line, same regex _handle_offline_trim_line() already watches
        # for on the Offline tab's own Trim — service_video.py's live
        # _trim_worker() prints the identical wording on success now (see
        # its own comment), so this is "the same place the application
        # already derives a trim's output path," reused here rather than a
        # separate render-state JSON field. Consumed once watch() exits —
        # see _on_process_exit().
        m4 = TRIMMED_PATH_RE.match(line)
        if m4:
            self._live_trimmed_path = m4.group(1).strip()

    def _update_live_buttons(self, raw_state: str | None):
        """Mirrors service_video.py's own guard on manual mark_begin/
        mark_end/trim/stitch commands (see watch()'s "manual" event
        handling) so a click is never possible when the backend would
        just ignore it: Mark Start is live in WAIT_BEGIN_SLIDE (first
        mark) and WAIT_END_SLIDE (re-mark); Mark End is live in
        WAIT_END_SLIDE (first mark) and WAIT_RECORD_STOP (re-mark) — and
        once end is marked, start locks (WAIT_RECORD_STOP has Start
        disabled).

        Trim just needs an end to have been marked at some point this
        run — unlike Mark Start/End, this doesn't reset if a later state
        somehow doesn't imply it (there's no such state — once end is
        marked, every later state is "after that"), so _live_end_marked
        is tracked separately rather than computed fresh from raw_state
        each time, and stays enabled through the new RECORDING_STOPPED
        state and beyond. Stitch's own enablement is independent of
        raw_state entirely — see _handle_trim_stitch_status().

        Also the one place _live_raw_state is kept current (None means
        no watch run is currently tracking anything) — see
        _live_mark_applicable(), which the control API's mark validation
        reads instead of a second copy of these state sets."""
        self._live_raw_state = raw_state
        start_enabled = self._live_mark_applicable("start")
        end_enabled = self._live_mark_applicable("end")
        if raw_state in ("WAIT_END_SLIDE", "WAIT_RECORD_STOP", "RECORDING_STOPPED"):
            self._live_begin_marked = True
        if raw_state in ("WAIT_RECORD_STOP", "RECORDING_STOPPED"):
            self._live_end_marked = True
        self.mark_start_btn.configure(state="normal" if start_enabled else "disabled")
        self.mark_end_btn.configure(state="normal" if end_enabled else "disabled")
        self.live_trim_btn.configure(state="normal" if self._live_end_marked else "disabled")
        self.live_stitch_btn.configure(state="normal" if self._live_trimmed else "disabled")

    def _handle_trim_stitch_status(self, which: str, status: str):
        """which is "trim" or "stitch" (see TRIM_STITCH_STATUS_RE). Unlike
        the old Prerender/Skip Render, neither Trim nor Stitch is a
        one-shot terminal action — both stay re-clickable once done (or
        failed), so this never permanently locks anything the way
        _handle_prerender_status() used to; it just reflects whatever
        most recently happened.

        Also keeps _live_trim_status/_live_stitch_status current (see
        __init__) — the control API's GET /state reads those rather than
        a third copy of this RUNNING/DONE/FAILED tracking."""
        if which == "trim":
            self._live_trim_status = status.lower()
        else:
            self._live_stitch_status = status.lower()
        btn = self.live_trim_btn if which == "trim" else self.live_stitch_btn
        running_label = {"trim": "Trimming…", "stitch": "Stitching…"}[which]
        done_label = {"trim": "Trimmed", "stitch": "Stitched"}[which]
        failed_label = {"trim": "Trim failed", "stitch": "Stitch failed"}[which]
        if status == "RUNNING":
            self.watch_state_var.set(running_label)
            self.watch_status_label.configure(foreground=PALETTE["info"])
            btn.configure(state="disabled")
        elif status == "DONE":
            self.watch_state_var.set(done_label)
            self.watch_status_label.configure(foreground=PALETTE["success"])
            btn.configure(state="normal")
            if which == "trim":
                self._live_trimmed = True
                self._live_trim_resolved = True
                self.live_stitch_btn.configure(state="normal")
        elif status == "FAILED":
            self._log(f"[gui] {which} failed — see the console output above for why; safe to try again")
            self.watch_state_var.set(failed_label)
            self.watch_status_label.configure(foreground=PALETTE["danger"])
            btn.configure(state="normal")
            if which == "trim":
                self._live_trim_resolved = True

    def _mark_sermon_start(self):
        self.runner.send_line("mark_begin")
        self._log("[gui] sent: mark sermon start")

    def _mark_sermon_end(self):
        self.runner.send_line("mark_end")
        self._log("[gui] sent: mark sermon end")

    def _trim_live(self):
        # Once watch() has already exited (see its own docstring — it does
        # once a live Trim resolves), there's no process left to send
        # "trim" to; this button now works the same way Offline's Trim
        # does instead, straight off the Offline tab's own fields (already
        # prefilled from this run — see _on_process_exit()). The Live
        # tab's own status readout (watch_state_var) doesn't come along
        # for free once it's a plain Offline-style run — _live_handoff_which
        # plus the check below (self.runner.running(), true only if
        # _run_trim() actually started something rather than bailing on a
        # validation error) are what let _on_process_exit() keep updating
        # it here the same way _handle_trim_stitch_status() did while
        # watch() was still running.
        if self.runner.running():
            self.runner.send_line("trim")
            self._log("[gui] sent: trim")
        else:
            self._run_trim()
            if self.runner.running():
                self._live_handoff_which = "trim"
                self._live_trim_status = "running"
                self.watch_state_var.set("Trimming…")
                self.watch_status_label.configure(foreground=PALETTE["info"])
                self.live_trim_btn.configure(state="disabled")

    def _stitch_live(self):
        if self.runner.running():
            self.runner.send_line("stitch")
            self._log("[gui] sent: stitch")
        else:
            self._run_stitch()
            if self.runner.running():
                self._live_handoff_which = "stitch"
                self._live_stitch_status = "running"
                self.watch_state_var.set("Stitching…")
                self.watch_status_label.configure(foreground=PALETTE["info"])
                self.live_stitch_btn.configure(state="disabled")

    # -- per-mode run handlers ---------------------------------------------

    def _run_learn(self):
        if not self._autosave_for_run():
            return
        self._start("learn", ["learn", "-c", self.config_path_var.get().strip()])

    def _run_watch(self):
        if not self._autosave_for_run():
            return
        args = ["watch", "-c", self.config_path_var.get().strip()]
        if self.watch_debug_var.get():
            args.append("--debug")
        self._live_begin_marked = False
        self._live_end_marked = False
        self._live_trimmed = False
        self._live_trim_resolved = False
        self._live_trimmed_path = None
        self._live_handoff_which = None
        self._live_trim_status = "idle"
        self._live_stitch_status = "idle"
        self.watch_state_var.set("starting…")
        self.watch_status_label.configure(foreground=PALETTE["accent"])
        self._update_live_buttons(None)
        self._start("watch", args)

    def _collect_offline_fields(self, error_title: str = "Render") -> dict | None:
        """Validate and collect the Offline tab's fields as a plain dict —
        shared by _run_trim()/_run_stitch()/_export_render_state(), since
        all three need most of the same inputs (just doing different
        things with them afterward, and each requiring their own subset —
        e.g. Stitch needs Trimmed clip, not Main clip; see each caller's
        own check). Returns None (after showing an error dialog titled
        `error_title`) if something required is missing or invalid."""
        intro = self.vars["st_intro"].get().strip()
        main_clip = self.vars["st_main"].get().strip()
        trimmed_clip = self.vars["st_trimmed"].get().strip()
        outro = self.vars["st_outro"].get().strip()
        output = self.vars["st_output"].get().strip() or "output.mp4"
        if not intro or not outro:
            messagebox.showerror(error_title, "Intro and outro paths are both required.")
            return None
        try:
            duration = to_float(self.vars["st_duration"].get().strip() or "1.0", "Transition duration")
            intro_duration = to_float(
                self.vars["st_intro_duration"].get().strip() or str(DEFAULT_IMAGE_DURATION), "Intro duration"
            )
            outro_duration = to_float(
                self.vars["st_outro_duration"].get().strip() or str(DEFAULT_IMAGE_DURATION), "Outro duration"
            )
            crf = self.vars["st_crf"].get()
            subsplash_preset = bool(self.vars["st_subsplash_preset"].get())
            trim_fast_copy = bool(self.vars["st_trim_fast_copy"].get())
            normalize_audio = bool(self.vars["st_normalize_audio"].get())
            normalize_target_lufs = to_float(
                self.vars["st_normalize_target_lufs"].get().strip() or "-16.0", "Normalize target LUFS"
            )
            encoder = self.vars["st_encoder"].get()
            encoder_preset = self.vars["st_encoder_preset"].get() or None
            start_ts = to_timestamp(self.vars["st_start"].get().strip() or "00:00:00.000", "Sermon start")
            end_ts = to_timestamp(self.vars["st_end"].get().strip() or "00:00:00.000", "Sermon end")
        except ValueError as e:
            messagebox.showerror(error_title, str(e))
            return None
        transition = self.vars["st_transition"].get().strip() or "fade"
        series = self.vars["st_series"].get().strip()
        return {
            "intro": intro, "main_clip": main_clip, "trimmed_clip": trimmed_clip, "outro": outro, "output": output,
            "duration": duration, "intro_duration": intro_duration, "outro_duration": outro_duration,
            "crf": crf, "subsplash_preset": subsplash_preset, "trim_fast_copy": trim_fast_copy,
            "normalize_audio": normalize_audio, "normalize_target_lufs": normalize_target_lufs,
            "encoder": encoder, "encoder_preset": encoder_preset,
            "start_ts": start_ts, "end_ts": end_ts, "transition": transition, "series": series,
        }

    @staticmethod
    def _build_render_state(f: dict, trim_output: str, state_output: str, stitch_auto: bool = True) -> dict:
        """A render_state dict from _collect_offline_fields()'s result —
        i.e. everything 'watch' writes after a live run, but built from
        fields picked by hand instead. trim_output/state_output are
        threaded through separately since where they come from differs
        between callers: an already-loaded file's own values when
        re-trimming it (_run_trim), or generic defaults when there's no
        loaded file to inherit them from (_export_render_state).
        stitch_auto is False for _run_trim() (trim only, no stitch — see
        its docstring), True everywhere else. trimmed_path is whatever's
        currently in Trimmed clip, if anything — same field Stitch itself
        reads from (see _run_stitch()) — so a round trip through Export
        to JSON then Load from JSON preserves it."""
        return {
            "recording_path": f["main_clip"],
            "raw_begin_offset": format_timestamp(f["start_ts"]),
            "raw_end_offset": format_timestamp(f["end_ts"]),
            "trimmed_path": f.get("trimmed_clip") or None,
            "trim": {
                "output": trim_output,
                "state_output": state_output,
                "pad_start_seconds": 0,
                "pad_end_seconds": 0,
                "crf": f["crf"],
                "fast_copy": f["trim_fast_copy"],
                "normalize_audio": f["normalize_audio"],
                "normalize_target_lufs": f["normalize_target_lufs"],
                "encoder": f["encoder"],
                "encoder_preset": f["encoder_preset"],
            },
            "stitch": {
                "auto": stitch_auto,
                # Purely for the GUI's own convenience restoring the
                # Offline tab's Series dropdown on a later Load from
                # JSON (see _load_render_state_json()) — nothing else
                # reads this key.
                "series": f.get("series", ""),
                "intro": f["intro"],
                "outro": f["outro"],
                "intro_duration": f["intro_duration"],
                "outro_duration": f["outro_duration"],
                "output": f["output"],
                "transition_duration": f["duration"],
                "transition": f["transition"],
                "crf": f["crf"],
                "subsplash_preset": f["subsplash_preset"],
                # Not GUI-exposed (see FAST_COPY_HELP) — always off here.
                "fast_copy": False,
                "encoder": f["encoder"],
                "encoder_preset": f["encoder_preset"],
            },
        }

    def _offline_use_raw_trim(self, main_clip: str) -> bool:
        """Whether Main clip is still the raw recording a loaded render-
        state file named (see _load_render_state_json()) — the only case
        Trim (below) actually has a raw source to trim from."""
        raw = self._offline_raw_state
        return raw is not None and raw["recording_path"] == main_clip

    def _run_trim(self):
        """Re-trims the raw recording down to Sermon start/Sermon end —
        i.e. just the trim half of what a live Watch run does — writing
        the result to Trimmed clip (see _handle_offline_trim_line()/
        TRIMMED_PATH_RE for how the actual resolved path lands there) so
        a follow-up Stitch click picks it up. Only meaningful right after
        "Load from JSON", before Main clip is changed — Sermon start/end
        are absolute timestamps a person picked by eye, not a raw/pad
        split (there's no slide detection here), so they're passed
        straight through as the offsets with zero padding."""
        f = self._collect_offline_fields(error_title="Trim")
        if f is None:
            return
        raw = self._offline_raw_state
        if not self._offline_use_raw_trim(f["main_clip"]):
            messagebox.showerror(
                "Trim",
                "Main clip isn't the raw recording from a loaded render-state file — "
                "Trim only works right after \"Load from JSON\", before Main clip is "
                "changed. Use Stitch instead if Trimmed clip already points at an "
                "already-trimmed clip.",
            )
            return

        # 'render' needs a render-state *file* to read (not stdin), but
        # that doesn't have to be the loaded one — an offline trim is
        # exploratory (tweak timestamps/settings, see what comes out),
        # not something that should silently overwrite your saved record
        # of what actually happened live just because you clicked Trim.
        # So this writes to a throwaway temp file instead (cleaned up
        # once the process exits, see _on_process_exit()); use "Export to
        # JSON" if you actually want to keep these settings. Trimmed clip
        # itself is where Trim actually writes — whatever's currently in
        # that field, same as Stitch will read from once this succeeds.
        trim_output = f["trimmed_clip"] or "body_trimmed.mp4"
        render_state = self._build_render_state(f, trim_output, raw["state_output"], stitch_auto=False)
        temp_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="render_state_", delete=False
        )
        with temp_file:
            json.dump(render_state, temp_file, indent=2)
        self._temp_render_state_path = temp_file.name
        self._start("trim", ["render", temp_file.name])

    def _run_stitch(self):
        """Crossfades Intro/Trimmed clip/Outro as-is — Trimmed clip is
        assumed to already be trimmed (a prior Trim click already points
        it at the result — see _run_trim() — or it's some other already-
        trimmed file picked by hand); Main clip (the raw recording) is
        never read here."""
        f = self._collect_offline_fields(error_title="Stitch")
        if f is None:
            return
        if not f["trimmed_clip"]:
            messagebox.showerror(
                "Stitch", "Trimmed clip is required — run Trim first, or fill it in "
                "with an already-trimmed file.",
            )
            return
        args = [
            "stitch", f["intro"], f["trimmed_clip"], f["outro"],
            "-o", f["output"], "-d", str(f["duration"]), "-t", f["transition"], "--crf", str(f["crf"]),
            "--intro-duration", str(f["intro_duration"]), "--outro-duration", str(f["outro_duration"]),
            "--encoder", f["encoder"],
        ]
        if f["encoder_preset"]:
            args += ["--encoder-preset", f["encoder_preset"]]
        if f["subsplash_preset"]:
            args.append("--subsplash-preset")
        self._start("stitch", args)

    def _export_render_state(self):
        """Build a render_state.json from whatever's currently in the
        Offline tab's fields — Main clip is always treated as the raw
        recording here (unlike _run_trim(), which only does that when it
        happens to match a previously-loaded file), since the point of
        this button is authoring a render-state file from scratch rather
        than redoing an existing one. The result is exactly what a live
        Watch run would have written, and works the same afterward: open
        it with "Load from JSON" here, or run it directly with
        `render <file>`."""
        f = self._collect_offline_fields(error_title="Export to JSON")
        if f is None:
            return
        if not f["main_clip"]:
            messagebox.showerror("Export to JSON", "Main clip is required.")
            return
        if f["end_ts"] <= f["start_ts"]:
            messagebox.showerror("Export to JSON", "Sermon end must be after Sermon start.")
            return

        trim_output = f["trimmed_clip"] or "body_trimmed.mp4"
        render_state = self._build_render_state(f, trim_output, DEFAULT_STATE_OUTPUT)
        default_name = f"render_state_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = filedialog.asksaveasfilename(
            title="Export render-state JSON", defaultextension=".json",
            initialfile=default_name, initialdir=str(SCRIPT_DIR), filetypes=JSON_FILETYPES,
        )
        if not path:
            return
        Path(path).write_text(json.dumps(render_state, indent=2))
        self._log(f"[gui] exported render-state JSON -> {path}")

    def _on_close(self):
        if self.runner.running():
            if not messagebox.askyesno(
                "Quit", "A process is still running. Stop it and quit?"
            ):
                return
            self.runner.stop()
        self._stop_api()
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
        self.destroy()


class OfflineAdvancedWindow(tk.Toplevel):
    """The Offline tab's CRF/Fast copy/Encoder/Encoder preset settings —
    split out into their own window (opened via the Offline tab's
    "Advanced…" button) since they're tuning knobs set once and rarely
    touched, unlike Intro/Main/Outro/Output/Sermon start-end, which change
    every run. Built once at App startup and hidden with withdraw()/
    deiconify() rather than destroyed on close, so reopening is instant —
    same pattern as ConfigWindow. All actual state lives in app.vars; this
    window just hosts widgets bound to it (the same st_crf/
    st_trim_fast_copy/st_encoder/st_encoder_preset vars _run_trim()/
    _run_stitch()/_export_render_state() already read)."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Service Video — Advanced Render Settings")
        self.geometry("420x410")
        self.configure(bg=PALETTE["bg"])
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="Applies to Offline's Trim and Stitch.",
            style="Muted.TLabel", wraplength=380, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        crf_scale = app._crf_slider(frame, 1, "CRF (quality)", "st_crf", col=0, colspan=3)

        app.vars["st_subsplash_preset"] = tk.BooleanVar(value=False)
        subsplash_cb = ttk.Checkbutton(
            frame, text="Subsplash On-Demand (1080p) preset", variable=app.vars["st_subsplash_preset"],
        )
        subsplash_cb.grid(row=2, column=0, columnspan=3, sticky="w", pady=3)
        Tooltip(subsplash_cb, SUBSPLASH_PRESET_HELP, font=app.ui_font)
        # CRF is ignored once this is on (see stitch()'s subsplash_preset) —
        # grey the slider out so that's visible, not just documented in the
        # tooltip. Only touches Stitch's step; Trim's own CRF (a separate
        # field entirely) is unaffected.
        app.vars["st_subsplash_preset"].trace_add(
            "write",
            lambda *_args: crf_scale.configure(
                state="disabled" if app.vars["st_subsplash_preset"].get() else "normal"
            ),
        )

        app.vars["st_trim_fast_copy"] = tk.BooleanVar(value=True)
        trim_fast_cb = ttk.Checkbutton(
            frame, text="Fast copy (recommended)", variable=app.vars["st_trim_fast_copy"],
        )
        trim_fast_cb.grid(row=3, column=0, columnspan=3, sticky="w", pady=3)
        Tooltip(trim_fast_cb, FAST_COPY_HELP + " Only applies when re-trimming from a raw "
                "recording (right after \"Load from JSON\") — ignored otherwise, since Main clip "
                "is then assumed to already be trimmed.", font=app.ui_font)

        app.vars["st_normalize_audio"] = tk.BooleanVar(value=True)
        normalize_cb = ttk.Checkbutton(
            frame, text="Normalize audio (recommended)", variable=app.vars["st_normalize_audio"],
        )
        normalize_cb.grid(row=4, column=0, columnspan=3, sticky="w", pady=3)
        Tooltip(normalize_cb, NORMALIZE_AUDIO_HELP + " Only applies when re-trimming from a raw "
                "recording (right after \"Load from JSON\") — ignored otherwise, since Main clip "
                "is then assumed to already be trimmed.", font=app.ui_font)

        lufs_entry = app._labeled_entry(frame, 5, "Target LUFS", "st_normalize_target_lufs", width=8, col=0)
        app.vars["st_normalize_target_lufs"].set("-16.0")
        Tooltip(lufs_entry, NORMALIZE_TARGET_HELP, font=app.ui_font)

        encoder_combo = app._labeled_combobox(frame, 6, "Encoder", "st_encoder", ENCODER_CHOICES, width=14, col=0)
        encoder_combo.configure(state="readonly")
        app.vars["st_encoder"].set("nvenc")
        Tooltip(encoder_combo, ENCODER_HELP, font=app.ui_font)

        preset_combo = app._labeled_combobox(frame, 7, "Encoder preset", "st_encoder_preset", [], width=14, col=0)
        preset_combo.configure(state="readonly")
        app._wire_encoder_preset_choices("st_encoder", preset_combo, "st_encoder_preset")
        Tooltip(preset_combo, ENCODER_PRESET_HELP, font=app.ui_font)

        self.withdraw()


class SeriesEditWindow(tk.Toplevel):
    """New/Edit form for one series (see App._build_series_tab()/issue
    #12) — a fresh instance every time (like InteractiveTrimWindow),
    since it needs to be re-populated with different data (or blank, for
    New) each open, rather than built once and hidden the way
    ConfigWindow/OfflineAdvancedWindow are. Modal (grab_set()): editing
    two series at once would just let one silently clobber the other's
    save, and there's nothing useful to do in the main window while this
    is open anyway."""

    def __init__(self, app: App, series: dict | None):
        super().__init__(app)
        self.app = app
        # None means "New" — Save appends instead of replacing, and there's
        # no existing name to exclude from the uniqueness check or to
        # carry a rename forward from (see _save()).
        self.original_name = series["name"] if series else None
        self.title("New series" if series is None else f"Edit series — {series['name']}")
        self.configure(bg=PALETTE["bg"])
        self.resizable(False, False)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self.name_var = tk.StringVar(value=series["name"] if series else "")
        name_label = ttk.Label(frame, text="Name", style="Header.TLabel")
        name_label.grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(frame, textvariable=self.name_var, width=40).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=3
        )

        self.intro_var = tk.StringVar(value=series["intro"] if series else "")
        ttk.Label(frame, text="Intro clip", style="Header.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 6), pady=3
        )
        ttk.Entry(frame, textvariable=self.intro_var, width=40).grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Button(frame, text="Browse…", command=lambda: self._browse(self.intro_var)).grid(
            row=1, column=3, sticky="w", padx=(4, 0), pady=3
        )
        self.intro_duration_var = tk.StringVar(
            value=str(series["intro_duration"]) if series else str(DEFAULT_IMAGE_DURATION)
        )
        intro_dur_label = ttk.Label(frame, text="Duration (s)", style="Header.TLabel")
        intro_dur_label.grid(row=2, column=0, sticky="w", padx=(0, 6), pady=3)
        intro_dur_spin = ttk.Spinbox(
            frame, textvariable=self.intro_duration_var, from_=0.1, to=120.0, increment=0.5, width=8,
        )
        intro_dur_spin.grid(row=2, column=1, sticky="w", pady=3)
        Tooltip(intro_dur_label, IMAGE_DURATION_HELP, font=app.ui_font)
        Tooltip(intro_dur_spin, IMAGE_DURATION_HELP, font=app.ui_font)

        self.outro_var = tk.StringVar(value=series["outro"] if series else "")
        ttk.Label(frame, text="Outro clip", style="Header.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 6), pady=3
        )
        ttk.Entry(frame, textvariable=self.outro_var, width=40).grid(row=3, column=1, columnspan=2, sticky="ew", pady=3)
        ttk.Button(frame, text="Browse…", command=lambda: self._browse(self.outro_var)).grid(
            row=3, column=3, sticky="w", padx=(4, 0), pady=3
        )
        self.outro_duration_var = tk.StringVar(
            value=str(series["outro_duration"]) if series else str(DEFAULT_IMAGE_DURATION)
        )
        outro_dur_label = ttk.Label(frame, text="Duration (s)", style="Header.TLabel")
        outro_dur_label.grid(row=4, column=0, sticky="w", padx=(0, 6), pady=3)
        outro_dur_spin = ttk.Spinbox(
            frame, textvariable=self.outro_duration_var, from_=0.1, to=120.0, increment=0.5, width=8,
        )
        outro_dur_spin.grid(row=4, column=1, sticky="w", pady=3)
        Tooltip(outro_dur_label, IMAGE_DURATION_HELP, font=app.ui_font)
        Tooltip(outro_dur_spin, IMAGE_DURATION_HELP, font=app.ui_font)

        self.hidden_var = tk.BooleanVar(value=bool(series.get("hidden", False)) if series else False)
        ttk.Checkbutton(
            frame, text="Hidden (excluded from Live/Offline Series dropdowns)", variable=self.hidden_var,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=6, column=0, columnspan=4, sticky="e", pady=(10, 0))
        ttk.Button(btn_row, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btn_row, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=(0, 8))

        # Grabbed last, after every widget (including Cancel/Save) exists —
        # grab_set() before the window is fully built can make the very
        # first click go nowhere on some platforms.
        self.grab_set()
        name_label.focus_set()

    def _browse(self, var: tk.StringVar):
        path = filedialog.askopenfilename(filetypes=INTRO_OUTRO_FILETYPES, initialdir=str(SCRIPT_DIR))
        if path:
            var.set(path)

    def _save(self):
        name = self.name_var.get().strip()
        intro = self.intro_var.get().strip()
        outro = self.outro_var.get().strip()
        if not name:
            messagebox.showerror("Save series", "Name is required.")
            return
        if not intro or not outro:
            messagebox.showerror("Save series", "Intro and outro paths are both required.")
            return
        existing = self.app._find_series(name)
        if existing is not None and name != self.original_name:
            messagebox.showerror("Save series", f"A series named {name!r} already exists.")
            return
        try:
            intro_duration = to_float(
                self.intro_duration_var.get().strip() or str(DEFAULT_IMAGE_DURATION), "Intro duration"
            )
            outro_duration = to_float(
                self.outro_duration_var.get().strip() or str(DEFAULT_IMAGE_DURATION), "Outro duration"
            )
        except ValueError as e:
            messagebox.showerror("Save series", str(e))
            return

        record = {
            "name": name, "intro": intro, "intro_duration": intro_duration,
            "outro": outro, "outro_duration": outro_duration, "hidden": bool(self.hidden_var.get()),
        }
        if self.original_name is None:
            self.app.series.append(record)
        else:
            for i, s in enumerate(self.app.series):
                if s["name"] == self.original_name:
                    self.app.series[i] = record
                    break
        self.app._save_series()
        self.app._refresh_series_choices()
        # A rename should carry forward wherever it's currently selected
        # (the Live/Offline tabs' own Series dropdowns), not silently
        # leave them pointing at a name that no longer exists — done
        # *after* _refresh_series_choices() above, so the new name is
        # already in each dropdown's own value list by the time this sets
        # it (a readonly Combobox can still display a value outside its
        # values list, but it wouldn't be reselectable from the dropdown
        # itself until the list catches up).
        if self.original_name is not None and name != self.original_name:
            for selector_key, *_rest in self.app._series_bindings:
                if self.app.vars[selector_key].get() == self.original_name:
                    self.app.vars[selector_key].set(name)
        self.destroy()


# InteractiveTrimWindow layout constants — a fixed-size filmstrip made of
# this many square-ish tiles, plus the large preview above it (used both
# for a single scrubbed frame and for streamed playback).
TRIM_THUMBS = 16
TRIM_STRIP_W = 720
TRIM_STRIP_H = 60
TRIM_HANDLE_W = 10
TRIM_PREVIEW_W = 480
TRIM_PREVIEW_H = 270
TRIM_PLAYER_FPS = 30
# How often the main thread checks its inbound queue (thumbnails, preview
# frames, playback frames, status) — was 50ms, which is coarser than one
# frame at 30fps (33.3ms) and made playback look choppy independent of
# TRIM_PLAYER_FPS itself: frames would sit queued a little longer than
# necessary, land in uneven bursts across drain cycles instead of a
# steady one-per-tick cadence. Cheap to poll this often — draining an
# empty queue is just one get_nowait() raising immediately.
TRIM_QUEUE_POLL_MS = 10
TRIM_ICON_SIZE = 16  # the play/pause button's icon, in pixels

# Font Awesome Free 6.7.2 "play"/"pause" (solid) icon path data — real
# vector icon shapes, not a font glyph like "▶"/"⏸" (some fonts render
# that as a boxed/missing-glyph fallback — the bug this replaced) and not
# something that needs a specific font (a Nerd Font or otherwise)
# installed to look right. This is a tool other volunteers run, not just
# one developer's own machine, so it can't depend on that. Rasterized at
# runtime by ffmpeg's own SVG decoder (see build_svg_icon()) into a small
# transparent PNG, loaded the exact same way every other frame/thumbnail
# in this window already is — Tk's own PhotoImage, no Pillow.
#   Font Awesome Free by @fontawesome - https://fontawesome.com
#   License - https://fontawesome.com/license/free
#   (Icons: CC BY 4.0, Fonts: SIL OFL 1.1, Code: MIT License)
#   Copyright 2024 Fonticons, Inc.
TRIM_PLAY_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 384 512">'
    '<path fill="{color}" d="M73 39c-14.8-9.1-33.4-9.4-48.5-.9S0 62.6 0 80L0 432c0 17.4 9.4 33.4 24.5 41.9'
    's33.7 8.1 48.5-.9L361 297c14.3-8.7 23-24.2 23-41s-8.7-32.2-23-41L73 39z"/></svg>'
)
TRIM_PAUSE_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 512">'
    '<path fill="{color}" d="M48 64C21.5 64 0 85.5 0 112L0 400c0 26.5 21.5 48 48 48l32 0c26.5 0 48-21.5 48-48'
    'l0-288c0-26.5-21.5-48-48-48L48 64zm192 0c-26.5 0-48 21.5-48 48l0 288c0 26.5 21.5 48 48 48l32 0c26.5 0 48-21.5'
    '48-48l0-288c0-26.5-21.5-48-48-48l-32 0z"/></svg>'
)


def build_svg_icon(svg_template: str, color: str, size: int) -> tk.PhotoImage | None:
    """Rasterizes a small inline SVG into a `size` x `size` transparent PNG
    via ffmpeg's own SVG decoder (needs a build with librsvg — true of
    every mainstream build this project already tells you to install, but
    not guaranteed of every possible one), then loads it as a Tk
    PhotoImage. Returns None — the caller falls back to
    build_fallback_play_icon()/build_fallback_pause_icon() — if ffmpeg
    can't do this for any reason, rather than letting a decorative icon
    take the whole window down."""
    svg = svg_template.format(color=color)
    svg_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False, mode="w", encoding="utf-8") as f:
            f.write(svg)
            svg_path = f.name
        out_path = svg_path[:-4] + ".png"
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", svg_path, "-vf", f"scale={size}:{size}",
                "-frames:v", "1", "-loglevel", "error", out_path,
            ],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0 or not Path(out_path).exists():
            return None
        return tk.PhotoImage(file=out_path)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        for p in (svg_path, out_path):
            if p:
                Path(p).unlink(missing_ok=True)


def build_fallback_play_icon(size: int, color: str) -> tk.PhotoImage:
    """A solid right-pointing triangle, drawn pixel-by-pixel — used only
    if build_svg_icon() can't rasterize the real Font Awesome icon (see
    its own docstring). Still font-independent, just less polished."""
    img = tk.PhotoImage(width=size, height=size)
    half = size / 2
    for y in range(size):
        span = y if y <= half else size - y
        x_right = min(size, round((span / half) * size)) if half else size
        if x_right > 0:
            img.put(color, to=(0, y, x_right, y + 1))
    return img


def build_fallback_pause_icon(size: int, color: str) -> tk.PhotoImage:
    """Two solid vertical bars — see build_fallback_play_icon()'s docstring."""
    img = tk.PhotoImage(width=size, height=size)
    bar_w = max(2, round(size * 0.26))
    gap = max(2, round(size * 0.22))
    x0 = (size - (bar_w * 2 + gap)) // 2
    img.put(color, to=(x0, 0, x0 + bar_w, size))
    img.put(color, to=(x0 + bar_w + gap, 0, x0 + bar_w + gap + bar_w, size))
    return img


def build_play_pause_icons(size: int, color: str) -> tuple[tk.PhotoImage, tk.PhotoImage]:
    """The play/pause pair, preferring the real Font Awesome SVGs and
    falling back to the plain drawn shapes if ffmpeg can't rasterize them
    (see build_svg_icon()) — always both from the same source, never one
    of each, so they stay visually consistent with each other."""
    play = build_svg_icon(TRIM_PLAY_ICON_SVG, color, size)
    pause = build_svg_icon(TRIM_PAUSE_ICON_SVG, color, size)
    if play is not None and pause is not None:
        return play, pause
    return build_fallback_play_icon(size, color), build_fallback_pause_icon(size, color)


class InteractiveTrimWindow(tk.Toplevel):
    """A mobile-photo-app-style visual trimmer for the Offline tab's Sermon
    start/Sermon end fields — a filmstrip of thumbnails spanning Main
    clip's full length, with two draggable handles marking the selected
    range, a seekbar with real embedded video playback (in this window,
    not a separate player window) plus audio, and a live single-frame
    preview of whichever handle last moved while paused.

    A fresh instance every time (see App._open_interactive_trim()), unlike
    OfflineAdvancedWindow/ConfigWindow's build-once-and-withdraw pattern —
    this one is tied to a specific source file and starting range, so
    there's nothing worth keeping alive between opens.

    All the actual frame-grabbing (ffprobe for duration, ffmpeg for
    thumbnails/preview frames/playback) happens in background threads and
    is handed back via a queue drained on a Tk after() loop, the same
    cross-thread-to-Tk pattern App itself uses for streaming a subprocess's
    output (see App._drain_queue()) — necessary because Tk widgets can
    only safely be touched from the main thread.

    Video is a hand-rolled embedded player: an ffmpeg process pipes raw
    frames (rawvideo/rgb24) from the source/position, read in their own
    thread and paced against wall-clock so it doesn't just blast frames
    onto the canvas as fast as ffmpeg can decode them. Audio is
    deliberately NOT the same trick — it's an invisible `ffplay -nodisp`
    subprocess instead (see _start_audio()) rather than raw PCM piped into
    a Python audio library (sounddevice/PortAudio) from a thread in this
    same process: an earlier version did exactly that, and it reliably
    segfaulted the whole app a few seconds into playback whenever the
    video pipe was also running — a native crash no amount of Python-level
    try/except can catch, since it doesn't happen in Python at all. A
    subprocess crashing just means silent playback; it can't take the
    rest of the app down. The trade-off: video and audio aren't frame-
    locked to each other, just both started at the same position and each
    paced against real time on its own, which is close enough for a short
    preview clip (this is a trim tool, not a video editor).

    The filmstrip's own thumbnails stay deliberately coarse/approximate
    (fine for a scrubber strip spanning a two-hour service, and much
    faster to generate 16 of) — but *playback* start position and the
    single large preview frame both use accurate_seek_input_args() so
    "starts at the right time" is actually true, not just approximately
    so. Left/Right (Shift for a finer step) nudges the last-touched trim
    handle for precision beyond dragging; the exact Sermon start/end text
    fields on the Offline tab stay editable after Apply too."""

    def __init__(self, app: App, source_path: str, start_seconds: float, end_seconds: float):
        super().__init__(app)
        self.app = app
        self.source_path = source_path
        self.title(f"Trim visually — {Path(source_path).name}")
        self.configure(bg=PALETTE["bg"])
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.duration: float | None = None
        self.start = max(0.0, start_seconds)
        self.end = max(0.0, end_seconds)
        self.active_handle = "end"  # which handle Left/Right/preview follows
        self.playhead = self.end  # seekbar/playback position, independent of the trim handles
        self._drag: str | None = None
        self._closed = False
        self._preview_job = None
        self._preview_request_id = 0
        self._thumb_images: dict[int, tk.PhotoImage] = {}  # keep refs alive
        self._tmpdir = tempfile.mkdtemp(prefix="interactive_trim_")

        # Current preview/filmstrip pixel sizes — start at the module
        # defaults, track the window from there as it's resized (see
        # _on_preview_configure()/_on_canvas_configure()). Real ffmpeg-
        # decoded/extracted media, unlike the rest of the layout, so
        # resizing them means re-extracting at the new size rather than
        # anything Tk can do to existing PhotoImages on its own.
        self.preview_w, self.preview_h = TRIM_PREVIEW_W, TRIM_PREVIEW_H
        self.strip_w = TRIM_STRIP_W
        self._preview_resize_job = None
        self._filmstrip_resize_job = None
        # Bumped every time filmstrip thumbnails are (re)generated — see
        # _generate_filmstrip()'s own docstring for why.
        self._filmstrip_generation = 0

        # Playback state.
        self.playing = False
        self._play_generation = 0  # bumped on every start/stop so late frames from a just-stopped run are dropped
        self._play_stop_at: float | None = None  # set by "Play selection" to auto-pause at the trim end
        self._video_proc: subprocess.Popen | None = None
        self._audio_proc: subprocess.Popen | None = None  # an invisible `ffplay -nodisp`, see _start_playback()
        self._audio_ok = shutil.which("ffplay") is not None

        self._queue: "queue.Queue" = queue.Queue()
        self._build_ui()
        # A floor, not a fixed size — resizable (see _build_ui()'s packing:
        # the preview pane expands, everything else docks to the bottom),
        # just never shrinks below what the initial layout needs.
        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())
        self.after(TRIM_QUEUE_POLL_MS, self._drain_queue)
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _build_ui(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # Everything below is packed side="bottom", in reverse of its
        # visual top-to-bottom order (each new bottom-packed widget claims
        # space just above the previous one) — a standard pack() trick so
        # these all stay docked to the bottom, fixed height, while the
        # preview pane (packed last, side="top", fill="both", expand=True)
        # claims whatever space is left above them and actually grows/
        # shrinks with the window. See _on_preview_configure()/
        # _on_canvas_configure() for how the preview/filmstrip content
        # itself keeps up with that, not just the widgets holding it.

        btn_row = ttk.Frame(outer)
        btn_row.pack(side="bottom", fill="x")
        # Square, icon-only (drawn icons, same pixel size in both states, so
        # toggling between them doesn't resize the button) — "Play
        # selection" stays a plain labeled button since it's a distinct
        # action, not a play/pause toggle.
        self._play_icon, self._pause_icon = build_play_pause_icons(TRIM_ICON_SIZE, PALETTE["text"])
        self.play_pause_btn = ttk.Button(btn_row, image=self._play_icon, command=self._toggle_play)
        self.play_pause_btn.pack(side="left")
        self.play_selection_btn = ttk.Button(btn_row, text="Play selection", command=self._play_selection)
        self.play_selection_btn.pack(side="left", padx=(8, 0))
        if not self._audio_ok:
            for btn in (self.play_pause_btn, self.play_selection_btn):
                Tooltip(
                    btn,
                    "Playing video only, no sound — ffplay wasn't found on PATH "
                    "(it ships with the full ffmpeg suite) to play audio.",
                    font=self.app.ui_font,
                )
        ttk.Button(btn_row, text="Cancel", command=self._cancel).pack(side="right")
        ttk.Button(btn_row, text="Apply", style="Accent.TButton", command=self._apply).pack(side="right", padx=(0, 8))

        self.hint_label = ttk.Label(
            outer,
            text="Drag handles to trim. Click one, then ←/→ to nudge (Shift for finer).",
            style="Muted.TLabel", wraplength=TRIM_STRIP_W, justify="left",
        )
        self.hint_label.pack(side="bottom", anchor="w", pady=(2, 8))

        self.status_var = tk.StringVar(value="Loading…")
        ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").pack(
            side="bottom", anchor="w", pady=(4, 0)
        )

        self.canvas = tk.Canvas(
            outer, width=TRIM_STRIP_W, height=TRIM_STRIP_H,
            bg=PALETTE["bg"], highlightthickness=1, highlightbackground=PALETTE["border"],
        )
        self.canvas.pack(side="bottom", fill="x")
        self.canvas.create_text(
            TRIM_STRIP_W // 2, TRIM_STRIP_H // 2, text="Loading filmstrip…",
            fill=PALETTE["muted"], tags="loading_text",
        )
        self.canvas.tag_bind("handle_start", "<ButtonPress-1>", lambda e: self._begin_drag("start"))
        self.canvas.tag_bind("handle_end", "<ButtonPress-1>", lambda e: self._begin_drag("end"))
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.canvas.bind("<Left>", lambda e: self._nudge(-1, fine=False))
        self.canvas.bind("<Right>", lambda e: self._nudge(1, fine=False))
        self.canvas.bind("<Shift-Left>", lambda e: self._nudge(-1, fine=True))
        self.canvas.bind("<Shift-Right>", lambda e: self._nudge(1, fine=True))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.focus_set()

        readout = ttk.Frame(outer)
        readout.pack(side="bottom", fill="x", pady=(0, 6))
        # Created before the Scale below, not after — .set(0) on it fires
        # its command= callback (_on_seekbar_change()) synchronously, which
        # touches this var, so it has to exist first.
        self.position_var = tk.StringVar(value=format_timestamp(0))
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.selected_var = tk.StringVar()
        ttk.Label(readout, textvariable=self.position_var, style="Header.TLabel").pack(side="left")
        ttk.Label(readout, textvariable=self.start_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))
        ttk.Label(readout, textvariable=self.end_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))
        ttk.Label(readout, textvariable=self.selected_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))

        self.seekbar = ttk.Scale(outer, from_=0, to=100, orient="horizontal", command=self._on_seekbar_change)
        self.seekbar.set(0)
        self.seekbar.state(["disabled"])
        self.seekbar.pack(side="bottom", fill="x", pady=(0, 4))
        self.seekbar.bind("<ButtonPress-1>", self._seekbar_press)
        self.seekbar.bind("<ButtonRelease-1>", self._seekbar_release)

        # The preview pane — packed last so it claims all remaining space
        # (fill="both", expand=True) above the docked rows built above.
        # A solid black placeholder PhotoImage, not a bare Label — a Label's
        # width/height options are character-based until it actually has an
        # image assigned, so setting pixel dimensions before the first real
        # frame arrives would size it completely wrong.
        self._preview_image = tk.PhotoImage(width=self.preview_w, height=self.preview_h)
        self._preview_image.put("black", to=(0, 0, self.preview_w, self.preview_h))
        self.preview_label = tk.Label(outer, image=self._preview_image, bg="black")
        self.preview_label.pack(side="top", fill="both", expand=True, pady=(0, 8))
        self.preview_label.bind("<Configure>", self._on_preview_configure)

    # -- background work --------------------------------------------------

    def _seek_time(self, t: float) -> float:
        """The timestamp actually handed to ffmpeg for a frame grab —
        clamped just shy of self.duration, since seeking to (or past) exact
        EOF reliably yields zero frames. self.start/self.end themselves
        (the real trim points) are never touched by this, only what's used
        to render a thumbnail/preview/playback *of* them."""
        if self.duration:
            return max(0.0, min(t, self.duration - 0.05))
        return max(0.0, t)

    def _load_worker(self):
        duration = probe_duration(self.source_path)
        if not duration:
            self._queue.put(("error", "Could not read this file's duration — is ffprobe on PATH?"))
            return
        self._queue.put(("duration", duration))
        self._generate_filmstrip(duration)

    def _generate_filmstrip(self, duration: float):
        """(Re)extracts all TRIM_THUMBS filmstrip thumbnails at the current
        self.strip_w — used both for the initial load and to regenerate
        after a resize settles (see _on_canvas_configure()/
        _regenerate_filmstrip()). Stamps every queued result with the
        generation current when this call started, so _on_thumb() can drop
        stale results from a run a newer resize has already superseded —
        same guarded-background-work pattern as _preview_request_id/
        _play_generation elsewhere in this class."""
        self._filmstrip_generation += 1
        gen = self._filmstrip_generation
        strip_w = self.strip_w
        cell_w = max(strip_w // TRIM_THUMBS, 1)
        failures = 0
        for i in range(TRIM_THUMBS):
            if self._closed or gen != self._filmstrip_generation:
                return
            t = self._seek_time(duration * i / max(TRIM_THUMBS - 1, 1))
            out = Path(self._tmpdir) / f"thumb_{gen}_{i}.png"
            ok = extract_frame_png(self.source_path, t, out, width=cell_w, height=TRIM_STRIP_H)
            failures += not ok
            self._queue.put(("thumb", gen, i, strip_w, str(out) if ok else None))
            self._queue.put(("status", f"Loading filmstrip… ({i + 1}/{TRIM_THUMBS})"))
        if gen == self._filmstrip_generation:
            # Failures used to go unreported — the loop still "finished" and
            # cleared the status line to blank even if every single
            # extraction had failed, leaving a silently, permanently blank
            # filmstrip with no indication anything was wrong (this is
            # exactly how a real ffmpeg filter bug here once shipped
            # undetected). Surfacing a count here doesn't fix a bad filter
            # graph on its own, but at least it's visible when one exists.
            self._queue.put((
                "status",
                f"{failures}/{TRIM_THUMBS} filmstrip thumbnails failed to load" if failures else "",
            ))

    def _regenerate_filmstrip(self):
        self._filmstrip_resize_job = None
        if self._closed or self.duration is None:
            return
        self.canvas.delete("thumb")
        self._thumb_images.clear()
        threading.Thread(target=self._generate_filmstrip, args=(self.duration,), daemon=True).start()

    def _request_preview(self, label: str, t: float):
        self._preview_job = None
        self._preview_request_id += 1
        req_id = self._preview_request_id
        t = self._seek_time(t)
        threading.Thread(target=self._preview_worker, args=(req_id, label, t), daemon=True).start()

    def _preview_worker(self, req_id: int, label: str, t: float):
        if self._closed:
            return
        out = Path(self._tmpdir) / f"preview_{req_id}.png"
        ok = extract_frame_png(
            self.source_path, t, out, width=self.preview_w, height=self.preview_h, accurate=True, letterbox=True,
        )
        self._queue.put(("preview", req_id, str(out) if ok else None))

    @staticmethod
    def _read_exact(stream, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _video_playback_worker(self, gen: int, start_t: float, w: int, h: int):
        # w/h are passed in (the preview pane's size at the moment
        # playback started) rather than read from self.preview_w/h here,
        # so a resize mid-playback can't change them out from under an
        # already-running pipe — _apply_preview_resize() instead stops and
        # restarts playback fresh at the new size.
        frame_bytes = w * h * 3
        # Same letterbox-to-a-fixed-size idea service_video.py's own filter
        # chain uses (scale to fit, pad the rest) — needed here because the
        # raw pipe has to be a known, fixed frame size to parse back out.
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={TRIM_PLAYER_FPS}"
        cmd = [
            "ffmpeg", *accurate_seek_input_args(self.source_path, start_t),
            "-map", "0:v:0", "-vf", vf, "-pix_fmt", "rgb24",
            "-f", "rawvideo", "-loglevel", "error", "-",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        if gen != self._play_generation:
            proc.terminate()
            return
        self._video_proc = proc
        frame_interval = 1.0 / TRIM_PLAYER_FPS
        wall_start = time.monotonic()
        i = 0
        try:
            while gen == self._play_generation:
                buf = self._read_exact(proc.stdout, frame_bytes)
                if buf is None:
                    break
                target_wall = wall_start + i * frame_interval
                now = time.monotonic()
                if now < target_wall:
                    time.sleep(target_wall - now)
                self._queue.put(("frame", gen, buf, w, h, start_t + i / TRIM_PLAYER_FPS))
                i += 1
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
        if gen == self._play_generation:
            self._queue.put(("playback_ended", gen))

    def _start_audio(self, start_t: float) -> subprocess.Popen | None:
        """Launches audio-only playback as an invisible ffplay subprocess —
        `-nodisp` skips opening any window at all, so this can't reproduce
        the original bug report's separate/fullscreen player window, and
        it paces itself against real time on its own (no manual pacing
        loop needed the way the video pipe below needs one).

        Deliberately a real OS process, not (say) piping raw audio into a
        Python audio library from a thread in this same process the way
        the video side pipes raw frames: an early version of this feature
        did exactly that (sounddevice/PortAudio), and it reliably
        segfaulted the whole app a few seconds into playback when a
        second ffmpeg pipe (video) was also running — a native crash below
        Python, out of reach of any try/except. A crashed ffplay just
        means silent playback; it can't take the rest of the app down."""
        cmd = ["ffplay", "-nodisp", "-loglevel", "error", *accurate_seek_input_args(self.source_path, start_t)]
        try:
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return None

    # -- queue drain (main thread only) ------------------------------------

    def _drain_queue(self):
        if self._closed:
            return
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "error":
                    self.status_var.set(msg[1])
                elif kind == "duration":
                    self._on_duration(msg[1])
                elif kind == "thumb":
                    self._on_thumb(msg[1], msg[2], msg[3], msg[4])
                elif kind == "preview":
                    self._on_preview(msg[1], msg[2])
                elif kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "frame":
                    self._on_frame(msg[1], msg[2], msg[3], msg[4], msg[5])
                elif kind == "playback_ended":
                    self._on_playback_ended(msg[1])
        except queue.Empty:
            pass
        if not self._closed:
            self.after(TRIM_QUEUE_POLL_MS, self._drain_queue)

    def _on_duration(self, duration: float):
        self.duration = duration
        # Unset/stale timestamps from the text fields (e.g. still the
        # "00:00:00.000" default, or left over from a different file)
        # collapse to the full clip rather than a zero-length selection.
        if self.end <= self.start or self.end > duration + 0.01:
            self.start, self.end = 0.0, duration
        self.start = max(0.0, min(self.start, duration))
        self.end = max(self.start, min(self.end, duration))
        self.playhead = self.end
        self.canvas.delete("loading_text")
        self._draw_handles()
        self._update_readout()
        self.seekbar.configure(to=duration)
        self.seekbar.state(["!disabled"])
        self.seekbar.set(self.playhead)
        self._request_preview("end", self.end)

    def _on_thumb(self, gen: int, index: int, strip_w: int, path: str | None):
        if gen != self._filmstrip_generation:
            # A resize superseded this run before it finished — see
            # _generate_filmstrip()'s docstring.
            if path:
                Path(path).unlink(missing_ok=True)
            return
        if path:
            try:
                img = tk.PhotoImage(file=path)
            except tk.TclError:
                img = None
            if img:
                self._thumb_images[index] = img
                x = strip_w * index // TRIM_THUMBS
                self.canvas.create_image(x, 0, image=img, anchor="nw", tags="thumb")
                self.canvas.tag_raise("shade")
                self.canvas.tag_raise("handle")

    def _on_preview(self, req_id: int, path: str | None):
        if req_id != self._preview_request_id or self.playing:
            # Superseded by a newer drag/nudge/seek before this one
            # finished, or playback started in the meantime — discard it
            # (and its file; nothing else will ever read it) rather than
            # stomp on a newer static frame or interrupt live playback.
            if path:
                Path(path).unlink(missing_ok=True)
            return
        if path:
            try:
                img = tk.PhotoImage(file=path)
            except tk.TclError:
                return
            self._preview_image = img
            self.preview_label.configure(image=img)
            Path(path).unlink(missing_ok=True)

    def _on_frame(self, gen: int, raw: bytes, w: int, h: int, t: float):
        if gen != self._play_generation or not self.playing:
            return
        header = f"P6\n{w} {h}\n255\n".encode("ascii")
        self._preview_image = tk.PhotoImage(data=header + raw)
        self.preview_label.configure(image=self._preview_image)
        self.playhead = t
        self.seekbar.set(t)
        self.position_var.set(format_timestamp(t))
        if self._play_stop_at is not None and t >= self._play_stop_at:
            self._stop_playback()

    def _on_playback_ended(self, gen: int):
        if gen == self._play_generation:
            self._stop_playback()

    # -- filmstrip / handle geometry --------------------------------------

    def _time_to_x(self, t: float) -> int:
        if not self.duration:
            return 0
        return int(self.strip_w * t / self.duration)

    def _x_to_time(self, x: float) -> float:
        if not self.duration:
            return 0.0
        x = max(0, min(self.strip_w, x))
        return self.duration * x / self.strip_w

    def _draw_handles(self):
        self.canvas.delete("shade")
        self.canvas.delete("handle")
        if self.duration is None:
            return
        sx, ex = self._time_to_x(self.start), self._time_to_x(self.end)
        h = TRIM_STRIP_H
        # Grey out the trimmed-away regions on either side of the selection,
        # the same visual language mobile trim UIs use.
        if sx > 0:
            self.canvas.create_rectangle(0, 0, sx, h, fill="black", stipple="gray50", width=0, tags="shade")
        if ex < self.strip_w:
            self.canvas.create_rectangle(ex, 0, self.strip_w, h, fill="black", stipple="gray50", width=0, tags="shade")
        hw = TRIM_HANDLE_W
        self.canvas.create_rectangle(
            sx - hw // 2, 0, sx + hw // 2, h, fill=PALETTE["accent"], outline="", tags=("handle", "handle_start"),
        )
        self.canvas.create_rectangle(
            ex - hw // 2, 0, ex + hw // 2, h, fill=PALETTE["accent"], outline="", tags=("handle", "handle_end"),
        )

    # -- resize handling ---------------------------------------------------
    # Both the filmstrip and the preview pane are real ffmpeg-extracted
    # media, not something Tk can rescale on its own the way it can a
    # plain widget — so "resize with the window" here means re-extracting
    # at the new size, not just letting existing pixels stretch. Debounced
    # (a live window-drag fires many Configure events a second) so this
    # only actually happens once the user stops dragging, not on every
    # intermediate pixel.

    def _on_canvas_configure(self, event):
        new_w = event.width
        # A deliberately generous "did this really change" threshold — far
        # smaller than any real user resize, but big enough to absorb the
        # handful of few-pixel geometry-settling passes Tk itself can take
        # right after a window first opens (worse, and apparently more
        # numerous, on Windows than this project's own Linux dev/CI
        # environment — see this method's sibling _on_preview_configure()
        # for the same threshold on the preview pane). Each such pass this
        # absorbs is one fewer regeneration cycle, and thus one fewer
        # chance for that cycle's own geometry side effects to compound.
        if abs(new_w - self.strip_w) < 16:
            return
        self.strip_w = new_w
        self.hint_label.configure(wraplength=max(new_w, 200))
        self._draw_handles()  # cheap — instant reposition, doesn't wait on the debounce below
        if self._filmstrip_resize_job:
            self.after_cancel(self._filmstrip_resize_job)
        self._filmstrip_resize_job = self.after(300, self._regenerate_filmstrip)

    def _on_preview_configure(self, event):
        w, h = event.width, event.height
        # See _on_canvas_configure()'s comment on this threshold.
        if w < 32 or h < 32 or (abs(w - self.preview_w) < 16 and abs(h - self.preview_h) < 16):
            return
        self.preview_w, self.preview_h = w, h
        # Pin the label's own declared size to exactly this, right away —
        # a Label with no explicit width/height instead takes its size
        # from whatever image is currently on it (its *content*). Without
        # pinning, swapping in a freshly-generated frame — even one sized
        # to match this very Configure event — can still nudge the
        # label's geometry a hair on its own, firing another Configure,
        # requesting another frame, forever: the window growing (or
        # shrinking) on its own with no further input. Pinning here makes
        # the label's size something only a real Configure event (a user
        # actually resizing the window) can change, never a side effect
        # of which image happens to be displayed at the moment.
        self.preview_label.configure(width=w, height=h)
        if self._preview_resize_job:
            self.after_cancel(self._preview_resize_job)
        self._preview_resize_job = self.after(300, self._apply_preview_resize)

    def _apply_preview_resize(self):
        self._preview_resize_job = None
        if self._closed:
            return
        if self.playing:
            # Mid-playback resize: the running video pipe was already
            # handed its frame size as fixed args at start time (see
            # _video_playback_worker()), so the clean way to pick up a new
            # size is a fresh start at the same position, not trying to
            # resize a pipe that's already running.
            self._stop_playback()
            self._start_playback()
        elif self.duration is not None:
            self._request_preview("resize", self.playhead)

    def _update_readout(self):
        self.start_var.set(f"Start  {format_timestamp(self.start)}")
        self.end_var.set(f"End  {format_timestamp(self.end)}")
        self.selected_var.set(f"({format_timestamp(max(0.0, self.end - self.start))} selected)")

    # -- interaction --------------------------------------------------------

    def _begin_drag(self, handle: str):
        if self.playing:
            self._stop_playback()
        self._drag = handle
        self.active_handle = handle
        self.canvas.focus_set()

    def _on_drag(self, event):
        if self._drag is None or self.duration is None:
            return
        t = self._x_to_time(event.x)
        if self._drag == "start":
            self.start = min(t, self.end)
        else:
            self.end = max(t, self.start)
        self._draw_handles()
        self._update_readout()
        self._schedule_preview(self._drag)

    def _nudge(self, direction: int, fine: bool):
        if self.duration is None:
            return "break"
        step = (0.05 if fine else 0.5) * direction
        if self.active_handle == "start":
            self.start = max(0.0, min(self.start + step, self.end))
        else:
            self.end = max(self.start, min(self.end + step, self.duration))
        self._draw_handles()
        self._update_readout()
        self._schedule_preview(self.active_handle)
        return "break"  # keep Tk from also treating this as focus traversal

    def _schedule_preview(self, handle: str):
        if self._preview_job:
            self.after_cancel(self._preview_job)
        t = self.start if handle == "start" else self.end
        self._preview_job = self.after(120, lambda: self._request_preview(handle, t))

    def _on_seekbar_change(self, value):
        # Fires on every value change, including programmatic ones (e.g.
        # every frame during playback — see _on_frame()) — just a cheap
        # label update, safe either way. Actually *seeking* only happens
        # from _seekbar_release(), keyed off a real mouse release.
        self.position_var.set(format_timestamp(float(value)))

    def _seekbar_press(self, _event):
        self._seekbar_was_playing = self.playing
        if self.playing:
            self._stop_playback()

    def _seekbar_release(self, _event):
        if self.duration is None:
            return
        self.playhead = max(0.0, min(float(self.seekbar.get()), self.duration))
        self._play_stop_at = None  # free scrubbing clears any pending "stop at selection end"
        if getattr(self, "_seekbar_was_playing", False):
            self._start_playback()
        else:
            self._request_preview("playhead", self.playhead)

    # -- playback transport -------------------------------------------------

    def _toggle_play(self):
        if self.playing:
            self._stop_playback()
        else:
            self._play_stop_at = None
            self._start_playback()

    def _play_selection(self):
        if self.duration is None:
            return
        self.playhead = self.start
        self._play_stop_at = self.end
        self._start_playback()

    def _start_playback(self):
        if self.duration is None or self.playing:
            return
        self.playing = True
        self.play_pause_btn.configure(image=self._pause_icon)
        self._play_generation += 1
        gen = self._play_generation
        threading.Thread(
            target=self._video_playback_worker, args=(gen, self.playhead, self.preview_w, self.preview_h), daemon=True,
        ).start()
        if self._audio_ok:
            self._audio_proc = self._start_audio(self.playhead)

    def _stop_playback(self):
        self.playing = False
        self.play_pause_btn.configure(image=self._play_icon)
        self._play_generation += 1  # invalidates any in-flight worker/queued frame from this run
        self._kill_playback_procs()

    def _kill_playback_procs(self):
        if self._video_proc and self._video_proc.poll() is None:
            self._video_proc.terminate()
        self._video_proc = None
        if self._audio_proc and self._audio_proc.poll() is None:
            self._audio_proc.terminate()
        self._audio_proc = None

    # -- actions --------------------------------------------------------

    def _apply(self):
        if self.duration is None:
            messagebox.showwarning("Trim visually", "Still loading — wait for the filmstrip before applying.")
            return
        self.app.vars["st_start"].set(format_timestamp(self.start))
        self.app.vars["st_end"].set(format_timestamp(self.end))
        self._close()

    def _cancel(self):
        self._close()

    def _close(self):
        self._closed = True
        self._play_generation += 1
        self._filmstrip_generation += 1
        self._kill_playback_procs()
        for job in (self._preview_job, self._filmstrip_resize_job, self._preview_resize_job):
            if job:
                self.after_cancel(job)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.destroy()


class ConfigWindow(tk.Toplevel):
    """Everything that's set once and rarely touched again: the config file
    path, general app-wide settings (currently just the console log path),
    ProPresenter connection + slide matching (with Learn mode folded in,
    since discovering slide UIDs is a ProPresenter-configuration task),
    OBS connection, and the trim/auto-stitch settings a live Watch run uses
    afterward. Built once at App startup and hidden with withdraw()/
    deiconify() rather than destroyed on close, so state and widgets persist
    and reopening it (via the main window's "Config" button) is instant.

    All actual state (app.vars, app.config_path_var) and the subprocess/
    console machinery live on the main App; this window just hosts widgets
    bound to that state, plus its own Learn-results table."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Service Video — Config")
        self.geometry("640x760")
        self.minsize(560, 560)
        self.configure(bg=PALETTE["bg"])
        # Hide, don't destroy, so reopening via the main window's button
        # doesn't need to rebuild anything.
        self.protocol("WM_DELETE_WINDOW", self.withdraw)

        self._build_path_bar()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._build_general_tab(notebook)
        self._build_api_tab(notebook)
        self._build_propresenter_tab(notebook)
        self._build_obs_tab(notebook)
        self._build_render_settings_tab(notebook)

        self.withdraw()

    def _build_path_bar(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Config file:").pack(side="left")
        ttk.Entry(top, textvariable=self.app.config_path_var).pack(
            side="left", padx=4, fill="x", expand=True
        )
        ttk.Button(top, text="Browse…", command=self.app._browse_config).pack(side="left", padx=2)
        ttk.Button(top, text="Load", command=lambda: self.app.load_config()).pack(side="left", padx=2)
        ttk.Button(
            top, text="Save", style="Accent.TButton", command=self.app._save_config_clicked
        ).pack(side="left", padx=(6, 0))

    # -- General tab: app-wide settings not specific to any one action ----

    def _build_general_tab(self, notebook):
        app = self.app
        _outer, frame = app._make_scrollable_tab(notebook, "General")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="App-wide settings.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        app._labeled_entry(frame, 1, "Console log path", "log_path", colspan=3, help_text=LOG_PATH_HELP)
        app._add_browse(frame, 1, "log_path", save=True, filetypes=LOG_FILETYPES, col=3)

    # -- API tab: optional HTTP control API (mark start/end, get state) ---

    def _build_api_tab(self, notebook):
        app = self.app
        _outer, frame = app._make_scrollable_tab(notebook, "API")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame, text=API_HELP, style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        app.vars["api_enabled"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Enabled", variable=app.vars["api_enabled"],
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=3)

        app._labeled_entry(frame, 2, "Host", "api_host")
        app.vars["api_host"].set("127.0.0.1")
        app._labeled_entry(frame, 2, "Port", "api_port", width=10, col=2)
        app.vars["api_port"].set("8765")

        api_pw_entry = app._labeled_entry(
            frame, 3, "Password", "api_password", show="•", help_text=API_PASSWORD_HELP,
        )
        app._pw_entries.append(api_pw_entry)

    # -- ProPresenter tab: connection + slide matching + Learn -------------

    def _build_propresenter_tab(self, notebook):
        app = self.app
        _outer, frame = app._make_scrollable_tab(notebook, "ProPresenter")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="Optional — only needed for automatic begin/end slide detection. "
            "Mark Start/Mark End work without it.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        app._labeled_entry(frame, 1, "Host", "pp_host")
        app._labeled_entry(frame, 1, "Port", "pp_port", width=10, col=2)
        app.show_pw_var = tk.BooleanVar(value=False)
        pw_entry = app._labeled_entry(frame, 2, "Password", "pp_password", show="•")
        app._labeled_entry(frame, 2, "Reconnect (s)", "pp_reconnect", width=10, col=2)
        ttk.Checkbutton(
            frame, text="Show passwords", variable=app.show_pw_var, command=app._toggle_show_passwords
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        # Appended, not reset, since a tab built earlier (the API tab) may
        # have already registered its own password entry here — the list
        # itself is initialized once in App.__init__.
        app._pw_entries.append(pw_entry)

        app._build_slide_picker(frame, row=4, prefix="begin", label="Begin slide")
        app._build_slide_picker(frame, row=9, prefix="end", label="End slide")

        ttk.Separator(frame, orient="horizontal").grid(
            row=13, column=0, columnspan=4, sticky="ew", pady=(12, 8)
        )
        ttk.Label(frame, text="Learn slide UIDs", style="Header.TLabel").grid(
            row=14, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(
            frame,
            text="Step through your slides in ProPresenter; each one appears below "
            "with its UID.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=15, column=0, columnspan=4, sticky="w", pady=(2, 6))

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=16, column=0, columnspan=4, sticky="w", pady=(0, 6))
        start_btn = ttk.Button(btn_row, text="Start Learn", style="Accent.TButton", command=app._run_learn)
        start_btn.pack(side="left")
        app._start_buttons.append(start_btn)

        frame.rowconfigure(17, weight=1)
        tree_frame = ttk.Frame(frame)
        tree_frame.grid(row=17, column=0, columnspan=4, sticky="nsew", pady=(0, 6))
        self.learn_tree = ttk.Treeview(tree_frame, columns=("uid", "text"), show="headings", height=8)
        self.learn_tree.heading("uid", text="UID")
        self.learn_tree.heading("text", text="Text")
        self.learn_tree.column("uid", width=260)
        self.learn_tree.column("text", width=220)
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.learn_tree.yview)
        self.learn_tree.configure(yscrollcommand=tree_scroll.set)
        self.learn_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="left", fill="y")

        assign_row = ttk.Frame(frame)
        assign_row.grid(row=18, column=0, columnspan=4, sticky="w")
        ttk.Button(
            assign_row, text="Use selected as Begin Slide", command=lambda: self._assign_slide("begin")
        ).pack(side="left")
        ttk.Button(
            assign_row, text="Use selected as End Slide", command=lambda: self._assign_slide("end")
        ).pack(side="left", padx=(8, 0))

    def _assign_slide(self, prefix):
        sel = self.learn_tree.selection()
        if not sel:
            messagebox.showwarning("Learn", "Select a slide row first.")
            return
        uid = sel[0]
        self.app.vars[f"{prefix}_mode"].set("uid")
        self.app.vars[f"{prefix}_uid"].set(uid)
        self.app._log(f"[gui] set {prefix} slide UID -> {uid}")

    def add_learned_slide(self, uid: str, text):
        if not self.learn_tree.exists(uid):
            self.learn_tree.insert("", "end", iid=uid, values=(uid, text))

    # -- OBS tab --------------------------------------------------------

    def _build_obs_tab(self, notebook):
        app = self.app
        _outer, frame = app._make_scrollable_tab(notebook, "OBS")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="Detects recording start/stop during a live Watch run.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        app._labeled_entry(frame, 1, "Host", "obs_host")
        app._labeled_entry(frame, 1, "Port", "obs_port", width=10, col=2)
        obs_pw_entry = app._labeled_entry(frame, 2, "Password", "obs_password", show="•")
        app._pw_entries.append(obs_pw_entry)

    # -- Render tab: trim + auto-stitch settings used after a live Watch --

    def _build_render_settings_tab(self, notebook):
        app = self.app
        _outer, frame = app._make_scrollable_tab(notebook, "Render")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="Used for the automatic trim+stitch after Watch finishes — not used "
            "by Offline.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 8))

        # Lone fields (nothing sharing their row) get colspan=3 to absorb
        # both weighted columns, same as the Offline tab — Browse buttons
        # move to column 4, their own dedicated unweighted slot, so they
        # don't collide with that span.
        app._labeled_entry(frame, 1, "Trimmed output path", "trim_output", colspan=3, help_text=TIMESTAMP_HELP)
        app._add_browse(frame, 1, "trim_output", save=True, filetypes=VIDEO_FILETYPES, col=3)
        app._labeled_entry(frame, 2, "Render state output path", "trim_state_output", colspan=3, help_text=TIMESTAMP_HELP)
        app._add_browse(frame, 2, "trim_state_output", save=True, filetypes=JSON_FILETYPES, col=3)
        app._labeled_spinbox(
            frame, 3, "Start offset (s)", "trim_pad_start", colspan=3, help_text=OFFSET_HELP,
        )
        app._labeled_spinbox(
            frame, 4, "End offset (s)", "trim_pad_end", colspan=3, help_text=OFFSET_HELP,
        )

        # Same fields, same order/columns as the Offline tab, for
        # consistency — these are the defaults a live Watch run's
        # auto-stitch uses; the Offline tab always lets you override them
        # per run.
        app._labeled_combobox(
            frame, 5, "Transition type", "stitch_transition", XFADE_TRANSITIONS, width=12, col=0,
        )
        app.vars["stitch_transition"].set("fade")
        app._labeled_entry(frame, 5, "Transition duration (s)", "stitch_transition_duration", width=8, col=2, pad_left=16)
        app.vars["stitch_transition_duration"].set("1.0")

        crf_scale = app._crf_slider(frame, 6, "CRF (quality)", "trim_crf", col=0, colspan=3)

        app.vars["stitch_subsplash_preset"] = tk.BooleanVar(value=False)
        subsplash_cb = ttk.Checkbutton(
            frame, text="Subsplash On-Demand (1080p) preset (stitch only)",
            variable=app.vars["stitch_subsplash_preset"],
        )
        subsplash_cb.grid(row=7, column=0, columnspan=3, sticky="w", pady=3)
        Tooltip(subsplash_cb, SUBSPLASH_PRESET_HELP, font=app.ui_font)
        # CRF is ignored for the auto-stitch step once this is on (see
        # stitch()'s subsplash_preset) — grey the slider out so that's
        # visible, not just documented in the tooltip. Trim's own encode
        # (a separate step entirely) still uses it.
        app.vars["stitch_subsplash_preset"].trace_add(
            "write",
            lambda *_args: crf_scale.configure(
                state="disabled" if app.vars["stitch_subsplash_preset"].get() else "normal"
            ),
        )

        app.vars["trim_fast_copy"] = tk.BooleanVar(value=True)
        fast_trim_cb = ttk.Checkbutton(
            frame, text="Fast copy (recommended)", variable=app.vars["trim_fast_copy"],
        )
        fast_trim_cb.grid(row=8, column=0, columnspan=3, sticky="w", pady=3)
        Tooltip(fast_trim_cb, FAST_COPY_HELP, font=app.ui_font)

        app.vars["trim_normalize_audio"] = tk.BooleanVar(value=True)
        normalize_cb = ttk.Checkbutton(
            frame, text="Normalize audio (recommended)", variable=app.vars["trim_normalize_audio"],
        )
        normalize_cb.grid(row=9, column=0, columnspan=3, sticky="w", pady=3)
        Tooltip(normalize_cb, NORMALIZE_AUDIO_HELP, font=app.ui_font)

        lufs_entry = app._labeled_entry(frame, 10, "Target LUFS", "trim_normalize_target_lufs", width=8, col=0)
        app.vars["trim_normalize_target_lufs"].set("-16.0")
        Tooltip(lufs_entry, NORMALIZE_TARGET_HELP, font=app.ui_font)

        encoder_combo = app._labeled_combobox(frame, 11, "Encoder", "encoder", ENCODER_CHOICES, width=14, col=0)
        encoder_combo.configure(state="readonly")
        app.vars["encoder"].set("nvenc")
        Tooltip(encoder_combo, ENCODER_HELP + " Applies to both the trim and the auto-stitch step.", font=app.ui_font)

        preset_combo = app._labeled_combobox(frame, 12, "Encoder preset", "encoder_preset", [], width=14, col=0)
        preset_combo.configure(state="readonly")
        app._wire_encoder_preset_choices("encoder", preset_combo, "encoder_preset")
        Tooltip(preset_combo, ENCODER_PRESET_HELP + " Applies to both the trim and the auto-stitch step.", font=app.ui_font)

        ttk.Separator(frame, orient="horizontal").grid(
            row=13, column=0, columnspan=5, sticky="ew", pady=(12, 8)
        )
        app.vars["stitch_auto"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="Auto-stitch after trim", variable=app.vars["stitch_auto"],
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=3)


if __name__ == "__main__":
    App().mainloop()

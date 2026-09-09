#!/usr/bin/env python3
"""Produce a service recording: watch ProPresenter for a "begin" and "end"
slide, correlate those moments against an OBS recording, trim the
recording down to the body clip, and stitch it together with a provided
intro and outro using a crossfade at each join.

Subcommands:
    watch    Watch ProPresenter+OBS live for the whole service, keeping a
             render-state file continuously up to date with the begin/end
             marks and recording as they happen. Never trims or stitches
             on its own — send 'trim'/'stitch' on its stdin (or use the
             GUI's Trim/Stitch buttons) whenever you're ready, including
             before the recording actually stops.
             python service_video.py watch -c config.json [--debug]

    learn    Connect to ProPresenter only (no OBS needed) and print each
             slide's UID as you step through it, so you can find the
             begin/end slide UIDs for your config.
             python service_video.py learn -c config.json

    render   Re-run just the trim+stitch step from a render-state file a
             previous 'watch' run wrote — no OBS/ProPresenter connection.
             Edit the file's pad_start_seconds/pad_end_seconds (or
             anything else) first to adjust the result.
             python service_video.py render render_state_<timestamp>.json

    stitch   Crossfade an intro, main body, and outro clip into one video,
             standalone (no config file, no ProPresenter/OBS).
             python service_video.py stitch intro.mp4 main.mp4 outro.mp4 -o final.mp4

Two APIs are involved in 'watch'/'learn':
  - ProPresenter's legacy "stage display" WebSocket
    (ws://host:port/stagedisplay), documented at
    https://jeffmikels.github.io/ProPresenter-API/Pro7/
  - obs-websocket v5 (built into OBS 28+), via the obsws-python client.

IMPORTANT: ProPresenter has since introduced a newer, officially documented
API (Swagger docs served at http://<host>:<port>/help when enabled). This
script targets the older, better-documented protocol.

Slides are identified by UID, not by their displayed text — many slides
(title graphics, bumpers, video backgrounds) have no text layer at all, so
the stagedisplay protocol reports an empty "txt" for them; the UID is
what's always present and stable. Use 'learn' to find them.

Install dependencies first:
    pip install -r requirements.txt
"""

import argparse
import asyncio
import json
import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

# obsws_python and websockets are only needed for 'watch'/'learn' (they talk
# to OBS and ProPresenter); imported lazily inside those code paths so
# 'stitch' and 'render' keep working with nothing but ffmpeg installed.

# Set once at startup from --machine-progress (see main()) — whether every
# ffmpeg invocation in this file reports progress via -progress
# (machine-readable key=value lines on stdout, meant for the GUI to parse
# into an actual progress bar) instead of ffmpeg's normal, periodically-
# rewritten human-readable stats line. A module-level flag rather than a
# parameter threaded through trim_clip()/stitch() and everything
# fast_copy's paths call, because it's genuinely a single, process-wide
# "what output mode is ffmpeg in" setting that never changes mid-run —
# the GUI turns it on for every subprocess it starts (see gui.py's
# ProcessRunner); a terminal user running this directly never sees it and
# gets ffmpeg's normal stats output, unchanged.
_MACHINE_PROGRESS = False


def _ffmpeg_output_args() -> list[str]:
    """Extra ffmpeg args controlling how it reports progress — see
    _MACHINE_PROGRESS. -progress pipe:1 writes clean, easy-to-parse
    key=value progress lines to stdout (frame=.../out_time_ms=.../
    speed=.../progress=continue|end, repeated every ~0.5s) instead of the
    usual single line rewritten in place, paired with -nostats to suppress
    that line entirely — together, what lets the GUI show a real progress
    bar instead of a wall of frame=.../time=... lines in its console log,
    without touching anything else ffmpeg prints (banners, warnings,
    errors, the final summary)."""
    return ["-nostats", "-progress", "pipe:1"] if _MACHINE_PROGRESS else []


# How many real ffmpeg steps each fast_copy path takes when it gets far
# enough to attempt any of them at all — _fast_copy_trim's sliver+tail+
# join, _fast_copy_stitch's front+middle+tail+join. Used, together with
# FALLBACK_STEPS, to reserve an accurate step total up front (see
# _reset_steps()/_phase_worst_case_steps()) rather than discovering it
# piece by piece as the render actually happens — keep these in sync with
# _fast_copy_trim()/_fast_copy_stitch() if the number of steps either one
# takes ever changes.
TRIM_FAST_COPY_STEPS = 3
STITCH_FAST_COPY_STEPS = 4
FALLBACK_STEPS = 1
# One more reserved step whenever trim.normalize_audio is on, on top of
# whichever of the above applies — trim_clip() always measures loudness
# once (see _measure_loudness()) before choosing a video path, regardless
# of fast_copy, so this applies unconditionally rather than only on one
# branch. See _trim_worst_case_steps().
NORMALIZE_MEASURE_STEPS = 1

# Tracks a running step N/M count across a whole top-level operation —
# render() (trim_clip() then, if auto-stitch, stitch() too), a single
# _trim_worker()/_stitch_worker() pass, or one standalone 'stitch' CLI
# call — for the GUI's progress bar (see _MACHINE_PROGRESS/_print_step()).
# Trim and Stitch each get their own independent window when triggered
# from a live watch() run (see _trim_worker()/_stitch_worker()) rather
# than one count spanning both, since they're separate, independently
# triggered actions now, not one combined operation. `total` is
# fixed once, up front, by the caller (see _reset_steps()) rather than
# grown as fast_copy's actual path through each phase becomes known: even
# a fast_copy attempt that completes every one of its own steps can still
# fail its own final decode-verify and need a fallback afterward, so the
# only way to never have to revise the total mid-render is to reserve for
# that worst case from the start. A phase that turns out not to need its
# reserved fallback step (or an already-on-a-keyframe trim that skips
# fast_copy's own multi-step path entirely) just finishes short of the
# total rather than exactly hitting it — a fixed, honest total that's
# sometimes not fully used beats one that grows mid-render and contradicts
# what was already shown.
_step_state = {"current": 0, "total": 0}


def _phase_worst_case_steps(fast_copy_enabled: bool, fast_copy_step_count: int) -> int:
    """Upper bound on how many real ffmpeg steps one phase (trim_clip()'s
    or stitch()'s) could take: `fast_copy_step_count` (its own fast_copy
    path's step count — TRIM_FAST_COPY_STEPS/STITCH_FAST_COPY_STEPS) plus
    one more for a fallback, since even a fast_copy attempt that completes
    every one of its own steps can still need one afterward — or, if
    fast_copy isn't even enabled, just the one fallback step it goes
    straight to."""
    return fast_copy_step_count + FALLBACK_STEPS if fast_copy_enabled else FALLBACK_STEPS


def _trim_worst_case_steps(trim_cfg: dict) -> int:
    """_phase_worst_case_steps() for trim_clip() specifically, plus
    NORMALIZE_MEASURE_STEPS whenever trim.normalize_audio is on — separate
    from the generic helper above since this extra step is a trim-only
    concept stitch() has no equivalent of."""
    total = _phase_worst_case_steps(trim_cfg.get("fast_copy", True), TRIM_FAST_COPY_STEPS)
    if trim_cfg.get("normalize_audio", True):
        total += NORMALIZE_MEASURE_STEPS
    return total


def _reset_steps(total: int = 0):
    """Start (or restart) the step count for a new top-level operation,
    reserving `total` steps up front — see _step_state's docstring for
    why that has to be decided now rather than discovered incrementally.
    Called once at the start of render(), _trim_worker(), _stitch_worker(),
    and a standalone 'stitch' CLI call (in main()), each of which can
    compute their own accurate total from _phase_worst_case_steps() (or,
    for render(), _render_step_total()) before doing any real work;
    trim_clip()/stitch() never call this themselves, so a render() that
    runs both keeps counting continuously across the two
    instead of each restarting from 1."""
    _step_state["current"] = 0
    _step_state["total"] = total


def _print_step(duration: float, what: str):
    """Marks the start of one ffmpeg step, advancing _step_state's
    running current by one (capping total up to match on the rare chance
    a phase ever needs more steps than _reset_steps() reserved for it,
    rather than printing a nonsensical current > total). Meaningless to a
    terminal user (just another log line); the GUI parses it into the
    "[N/M]" label next to its progress bar and resets the bar itself,
    since each step has its own separate duration — `duration` (seconds)
    is that step's own expected output length, printed here rather than
    left for the GUI to work out from the ffmpeg command line (not always
    possible — a multi-input crossfade's output duration isn't simply any
    one -t/-ss/-to value on it) since this already knows it exactly, one
    way or another, in every case. Printed right before the ffmpeg call
    it describes — every step counted this way corresponds to exactly one
    _ffmpeg_output_args()-bearing call, so the two stay honest with each
    other."""
    _step_state["current"] += 1
    _step_state["total"] = max(_step_state["total"], _step_state["current"])
    print(f"[progress] step {_step_state['current']}/{_step_state['total']} duration={duration:.3f}: {what}")


def _render_step_total(trim_cfg: dict, stitch_cfg: dict) -> int:
    """The upfront step total for a whole render (trim_clip() then, if
    auto-stitch, stitch() too) — see _reset_steps()/
    _phase_worst_case_steps()/_trim_worst_case_steps(). Used by render(),
    the only place that still runs both phases from one config pair as a
    single operation — a live watch() run's Trim/Stitch are separate,
    independently triggered actions (see _trim_worker()/_stitch_worker()),
    each with its own worst-case window instead of sharing this one."""
    total = _trim_worst_case_steps(trim_cfg)
    if stitch_cfg.get("auto"):
        total += _phase_worst_case_steps(stitch_cfg.get("fast_copy", False), STITCH_FAST_COPY_STEPS)
    return total


# --------------------------------------------------------------------------
# Crossfade stitching (intro + body + outro -> one video)
# --------------------------------------------------------------------------

def probe(path: str) -> dict:
    """Return duration, width, height, fps, video codec, and whether an
    audio stream exists for a media file, via ffprobe.

    fps comes from r_frame_rate, not avg_frame_rate. avg_frame_rate is
    computed from each frame's own timestamp (nb_frames / measured
    duration), so it silently inherits any timestamp imprecision already
    present in the file — confirmed by direct testing to be a real risk
    here specifically, not just a style preference: probing a fast-copy
    trim's output before CONCAT_TIMESCALE was forced consistently (see its
    own comment for the actual bug this was hiding) could measure a
    noticeably-off avg_frame_rate — e.g. ~28.8 on a genuinely-30fps source
    — purely from that timestamp corruption, with no real frames ever
    dropped. r_frame_rate is the stream's own declared/nominal rate (a
    clean rational like "30/1"), read from the container's stream info
    rather than computed from potentially-imprecise per-frame timestamps,
    so it isn't vulnerable to this class of problem even if some other,
    not-yet-found timestamp issue crops up here again later.

    duration comes back None (rather than raising) if ffprobe's own output
    doesn't have one, confirmed by direct testing to genuinely happen for
    an MKV OBS still has open for writing (its Segment/Cues aren't
    finalized yet, so ffprobe can't report an overall duration without a
    full decode) — exactly the file _fast_copy_trim() probes when Live
    Trim runs before recording stops, its whole point. That caller never
    reads this field, so leave it to whichever caller actually needs a
    real duration (e.g. stitch(), always against already-finalized clips)
    to fail with a clear error against a None instead of every probe() call
    hard-crashing here regardless of whether its caller needed the value."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=width,height,r_frame_rate,codec_type,codec_name",
        "-of", "json",
        path,
    ]
    # stdin=DEVNULL: never let ffprobe/ffmpeg try to read from whatever
    # stdin this script itself was given — see trim_clip()'s docstring
    # note for why that matters here specifically.
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit(f"ffprobe failed on {path!r}:\n{result.stderr}")

    data = json.loads(result.stdout)
    duration_raw = data["format"].get("duration")
    duration = float(duration_raw) if duration_raw is not None else None

    video_stream = next(
        (s for s in data["streams"] if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        sys.exit(f"{path!r} has no video stream.")

    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
    fps = float(Fraction(video_stream["r_frame_rate"]))

    return {
        "duration": duration,
        "width": video_stream["width"],
        "height": video_stream["height"],
        "fps": fps,
        "has_audio": has_audio,
        # Used by the fast-copy trim/stitch paths to decide whether (and
        # in what codec) a stream copy can safely sit next to a freshly
        # re-encoded segment in the same concatenated output — see
        # FAST_COPY_CODECS/find_next_keyframe()/_fast_copy_trim()/
        # _fast_copy_stitch().
        "video_codec": video_stream.get("codec_name"),
    }


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# Used for a still-image intro/outro when no intro_duration/outro_duration is
# configured, so forgetting to set one doesn't hard-fail a stitch.
DEFAULT_IMAGE_DURATION = 5.0

# trim.state_output's default when not configured — a render-state file's
# own path, same strftime-placeholder support as trim.output/
# stitch.output (see expand_output_path()); the timestamp placeholder
# here reproduces what used to be automatic (a fixed "_YYYYMMDD_HHMMSS"
# always appended to whatever base name was configured, whether wanted or
# not) as just the default rather than special-cased behavior — leave it
# out of a custom state_output entirely if you don't want it.
DEFAULT_STATE_OUTPUT = "render_state_%Y%m%d_%H%M%S.json"


def is_image_file(path: str) -> bool:
    """Whether a path looks like a still image (by extension) rather than a
    video — intro/outro can be either: a still image gets looped into a
    fixed-length clip via ffmpeg's image2 demuxer instead of being probed
    for its own duration (most single images have none to probe)."""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def probe_image_dimensions(path: str) -> dict:
    """Return width/height for a still image via ffprobe. Separate from
    probe() (which returns None for duration rather than crashing, but
    still expects a real video stream with a frame rate) because a still
    image's duration is a config choice (see stitch()'s intro_duration/
    outro_duration), not something to read off the file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit(f"ffprobe failed on {path!r}:\n{result.stderr}")
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        sys.exit(f"{path!r} has no readable image stream.")
    return {"width": streams[0]["width"], "height": streams[0]["height"]}


# --------------------------------------------------------------------------
# Fast-copy trim/stitch — re-encode only the small windows that actually
# need it (the keyframe-alignment sliver at a cut point, and the crossfade
# transitions), and stream-copy everything else instead of decoding and
# re-encoding the whole main clip. See trim_clip()/stitch()'s fast_copy
# parameter docs for the full picture; the pieces below are the shared
# machinery both use.
# --------------------------------------------------------------------------

# A fast-copy re-encoded segment (the trim sliver, or a stitch crossfade
# window) has to be encoded in the SAME codec as the footage it's stream-
# copied next to, or concatenating the two would produce a broken/
# unplayable file — unlike the old always-re-encode fallback path (which
# always outputs h264 regardless of the source), fast copy's output codec
# follows whatever the source already is. Keyed by the codec name ffprobe
# reports; a source whose codec isn't listed here just isn't eligible for
# the fast path at all (logged, falls back to the old behavior).
#
#   encoder/extra: the ffmpeg encoder to re-encode with, and any extra
#     output args it needs for container compatibility beyond -c:v/-crf —
#     HEVC-in-MP4 in particular needs the hvc1 tag rather than ffmpeg's
#     default hev1 for broad player/QuickTime compatibility.
#   vcl_nal_types/safe_nal_types: see _is_true_random_access_point()'s
#     docstring — identifies which NAL unit types in this codec's
#     bitstream carry an actual coded picture, and which of those are
#     genuine random-access points safe to start a stream copy at.
FAST_COPY_CODECS = {
    "h264": {
        "encoder": "libx264", "extra": [],
        "vcl_nal_types": range(1, 6), "safe_nal_types": {5},  # 5 = IDR slice
    },
    "hevc": {
        "encoder": "libx265", "extra": ["-tag:v", "hvc1"],
        "vcl_nal_types": range(0, 32), "safe_nal_types": {19, 20},  # IDR_W_RADL, IDR_N_LP
    },
}

_TRACE_HEADERS_NAL_TYPE_RE = re.compile(r"nal_unit_type\s+\S+\s*=\s*(\d+)\s*$")


def _is_true_random_access_point(path: str, timestamp: float, codec_info: dict) -> bool:
    """Whether the keyframe at exactly `timestamp` in path's video stream
    is a genuine random-access point — h264 IDR, or hevc IDR specifically
    (IDR_W_RADL/IDR_N_LP) — rather than merely something a plain per-frame
    keyframe scan reports as one.

    This distinction matters because open-GOP encoding — the default for
    many encoders, confirmed common for HEVC while building this — uses
    CRA pictures: intra frames that look exactly like a keyframe to a
    plain scan (find_next_keyframe()'s first pass), but have leading
    pictures immediately after them, in decode order, that reference
    frames from *before* the cut point. Those only decode correctly when
    a decoder recognizes the CRA as the true start of a coded sequence
    (opening a fresh file, or a proper broadcast splice that rewrites the
    NAL header) — neither of which describes a plain concatenated stream
    copy, which is mid-stream from the decoder's perspective. Confirmed by
    direct testing while building this: concatenating a fresh encode with
    a stream copy starting at an open-GOP CRA plays the seam fine, but the
    rest of that GOP fails to decode ("Could not find ref with POC ...").
    An IDR frame has no such leading pictures at all — by definition, so
    always safe.

    Checked with ffmpeg's trace_headers bitstream filter (not decoding,
    just parsing NAL headers) on a tiny extraction starting exactly at
    `timestamp` — `timestamp` must already be an exact keyframe time (e.g.
    from find_next_keyframe()'s own scan), since -ss before -i with -c
    copy only seeks cleanly to a real keyframe boundary; this doesn't
    itself search for one. Cheap and fast regardless of the file's overall
    length, same as find_next_keyframe() itself."""
    cmd = [
        "ffmpeg", "-loglevel", "info", "-nostdin",
        "-ss", f"{timestamp:.3f}", "-i", path, "-t", "0.2",
        "-c:v", "copy", "-an", "-bsf:v", "trace_headers", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in result.stderr.splitlines():
        m = _TRACE_HEADERS_NAL_TYPE_RE.search(line)
        if not m:
            continue
        nal_type = int(m.group(1))
        if nal_type not in codec_info["vcl_nal_types"]:
            continue  # a parameter-set/SEI/etc. NAL, not the actual slice
        return nal_type in codec_info["safe_nal_types"]
    return False


def find_next_keyframe(path: str, start: float, codec_info: dict, windows: tuple[float, ...] = (30.0, 300.0)) -> float | None:
    """Return the timestamp (seconds) of the first *safe* video keyframe
    (see _is_true_random_access_point()) at or after `start` in path's
    video stream, or None if none turns up within the largest window
    tried. Scans via ffprobe's -read_intervals (which seeks straight there
    rather than reading from the start of the file) combined with
    -skip_frame nokey (which only decodes keyframes, not everything in
    between) to list keyframe candidates cheaply regardless of how long
    `path` is or how far into it `start` falls, then checks each one in
    turn (skipping any that aren't actually safe to cut at) — critical,
    since the whole point of the fast-copy path is to avoid touching the
    rest of the file, and it can't safely stream-copy from an unsafe one.
    Tries progressively wider windows if a single one turns up nothing
    (an unusually long GOP, a run of unsafe candidates, or a window that
    happens to start right after the last usable keyframe)."""
    for window in windows:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-read_intervals", f"{start}%+{window}",
            "-show_entries", "frame=pts_time",
            "-of", "csv=p=0",
            path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            return None
        # -read_intervals' "%" seek lands at/before `start` (it's a
        # keyframe seek), so the earliest entries can be before `start` —
        # only a timestamp actually at or after it is usable here. csv=p=0
        # still trails a stray "," on at least the first row of some
        # ffprobe builds — stripped here rather than relied on to be absent.
        tokens = [t.strip().rstrip(",") for t in result.stdout.split()]
        times = sorted(float(t) for t in tokens if t)
        for t in times:
            if t >= start - 0.001 and _is_true_random_access_point(path, t, codec_info):
                return t
    return None


def probe_bitrate(path: str) -> int | None:
    """Video stream bitrate in bits/sec, or None if ffprobe can't report
    one (some containers, e.g. Matroska, only expose an overall
    format-level bitrate rather than a per-stream one — checked as a
    fallback). Used so a fast-copy stitch's re-encoded segments (the
    crossfade windows, and any looped-image intro/outro) can target
    roughly the same bitrate as the untouched, copied middle of the main
    clip, rather than an arbitrary configured CRF that might look like a
    quality jump at the seams."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=bit_rate", "-of", "json", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    bit_rate = None
    if result.returncode == 0:
        streams = json.loads(result.stdout).get("streams") or []
        if streams:
            bit_rate = streams[0].get("bit_rate")
    if bit_rate is None:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate", "-of", "json", path]
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode == 0:
            bit_rate = json.loads(result.stdout).get("format", {}).get("bit_rate")
    try:
        return int(bit_rate)
    except (TypeError, ValueError):
        return None


def _quality_match_args(reference_path: str, crf: int) -> list[str]:
    """ffmpeg video-encode args for a fast-copy segment that needs to
    visually match a reference clip's own quality (the untouched, copied
    footage it'll be concatenated next to) rather than an arbitrary
    configured CRF: target that clip's actual bitrate if ffprobe can
    report one, with modest headroom for the encoder to work with; fall
    back to the configured CRF (the old, pre-fast-copy behavior) if it
    can't — still functional, just without the quality-matching
    guarantee, so this never blocks the fast path outright."""
    bit_rate = probe_bitrate(reference_path)
    if bit_rate is None:
        print(f"[stitch] fast copy: couldn't read {reference_path!r}'s bitrate — using CRF {crf} for the "
              "re-encoded segments instead of matching it", file=sys.stderr)
        return ["-crf", str(crf)]
    return ["-b:v", str(bit_rate), "-maxrate", str(int(bit_rate * 1.5)), "-bufsize", str(bit_rate * 2)]


def _concat_list_entry(path: Path) -> str:
    """Quote a path for the ffmpeg concat demuxer's list file format:
    single-quoted, with any literal single quote escaped as '\\''."""
    return "'" + str(path).replace("'", "'\\''") + "'"


# Forced onto every segment _run_concat() ever joins (via -video_track_timescale)
# — confirmed by direct testing to be necessary, not just tidiness. ffmpeg's mp4
# muxer doesn't use the same video track timescale for every output: a freshly
# re-encoded stream gets one derived from the encoder's own frame rate (e.g.
# 15360 for 30fps), while a plain -c:v copy gets a different one (e.g. 16000)
# picked some other way. The concat demuxer's -c copy join does NOT rescale
# timestamps between segments with different timebases — it was confirmed, by
# reproducing this exact failure end to end, to just carry each segment's raw
# tick counts through as if they shared one timebase, which silently stretches
# or compresses whichever segment's timebase differs from the first one's. A
# 16000-vs-15360 mismatch is a 16000/15360 = 1.041666... ratio — confirmed to
# match, almost exactly, a real-world video-runs-~4% -slower-than-audio report
# (video and audio start in sync, both end at their correct real content, but
# video visibly drifts behind audio over a long clip) that traced back to
# exactly this: a fast-copy trim's stream-copied tail segment silently
# stretched relative to its freshly re-encoded sliver once concatenated,
# despite both segments' own audio tracks staying perfectly in sync throughout
# (audio doesn't have this problem — aac's timebase is sample-rate-derived and
# consistent regardless of which ffmpeg code path wrote a given segment).
# Forcing every segment (both fast-copy trim's sliver/tail and fast-copy
# stitch's front/middle/tail) onto this one fixed timescale up front — rather
# than trying to detect and reconcile a mismatch after the fact — sidesteps
# the whole problem. 90000 (a 90kHz clock) is the standard industry choice for
# this (used throughout MPEG-TS/broadcast) specifically because it divides
# evenly into every common frame rate, including fractional ones like
# 30000/1001 (29.97), so it never introduces new rounding of its own.
CONCAT_TIMESCALE = 90000

# How much to decode on each side of a join point when verifying one —
# see _verify_join_points_decode_cleanly()'s docstring for why a small
# fixed window is just as reliable a check as decoding the whole file.
JOIN_VERIFY_WINDOW_SECONDS = 2.0


def _verify_join_points_decode_cleanly(path: str, join_offsets: list[float]) -> bool:
    """Sanity-check a fast-copy result by decoding a short window
    straddling each point where two independently-produced segments were
    joined, and checking whether the decoder reported any problem there —
    rather than just trusting that matching codecs and a safe keyframe
    (see _is_true_random_access_point()) were enough to guarantee a
    correct result.

    This exists because concatenating independently-encoded segments can
    go wrong in ways neither of those checks predicts: confirmed by direct
    testing while building this, MP4 can only hold one set of codec
    parameters (SPS/PPS/etc.) per track, so a segment whose parameters
    genuinely differ from the one before it — normal between two separate
    encoder runs, even at identical settings, since real content drives
    some of those decisions — can decode wrong from that point on, with
    no error raised at mux time, despite each segment individually being
    perfectly valid.

    Checking only near each join, instead of decoding the whole result, is
    just as reliable for this specific failure mode and far cheaper: a
    parameter mismatch is a property of the *encoded segment*, applied
    identically to every frame in it, so if it's going to cause a decode
    error it does so at the very first frame that uses it — right at the
    join — not gradually partway through. A problem starting anywhere
    later than that would mean something new-and-different went wrong,
    not this. Costs roughly the same regardless of how long the untouched,
    copied middle between joins is, which is the whole point of fast copy
    in the first place."""
    for offset in join_offsets:
        start = max(0.0, offset - JOIN_VERIFY_WINDOW_SECONDS / 2)
        cmd = [
            "ffmpeg", "-v", "error", "-nostdin",
            "-ss", f"{start:.3f}", "-i", path, "-t", f"{JOIN_VERIFY_WINDOW_SECONDS:.3f}",
            "-map", "0:v:0", "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0 or result.stderr.strip():
            return False
    return True


def _run_concat(parts: list[Path], output: str, faststart: bool = False, verify_join_offsets: list[float] | None = None):
    """Join `parts` (already-produced media files, in order) into `output`
    via the concat demuxer with -c copy — a fast, no-re-encode join, used
    to reassemble a fast-copy trim/stitch's separately-produced segments.
    The list file is written next to `output` and always cleaned up.

    Every part MUST have been written with -video_track_timescale
    CONCAT_TIMESCALE (every caller here does) — this join doesn't rescale
    timestamps between segments with differing timebases, so a mismatch
    silently stretches or compresses whichever part's timebase differs
    from the first one's, without any error at join time. See
    CONCAT_TIMESCALE's own comment for the real desync this caused before
    it was forced consistently.

    faststart: pass True when `output` is a final deliverable meant for
    web/streaming playback (mirrors stitch()'s old path, which always sets
    this) rather than an intermediate file like trim_clip()'s output —
    just rewrites where the moov atom sits, no re-encoding involved.

    verify_join_offsets: the timestamp(s) in `output`, in seconds, where
    two independently-produced segments meet — pass one per join whenever
    `parts` includes more than one independently re-encoded segment
    (front+middle+tail, or sliver+tail); see
    _verify_join_points_decode_cleanly() for why. Raises RuntimeError
    (after removing the bad output) if any join doesn't decode cleanly,
    the same way a failed ffmpeg call raises CalledProcessError, so
    callers can catch both the same way and fall back."""
    list_path = Path(output).with_name(f".{Path(output).stem}.concat.txt")
    list_path.write_text("".join(f"file {_concat_list_entry(p)}\n" for p in parts))
    try:
        cmd = ["ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(), "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy"]
        if faststart:
            cmd += ["-movflags", "+faststart"]
        cmd.append(output)
        print("Running:", " ".join(cmd))
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    finally:
        list_path.unlink(missing_ok=True)

    if verify_join_offsets:
        print(f"[fast copy] verifying {output} decodes cleanly at its {len(verify_join_offsets)} join point(s)...")
        if not _verify_join_points_decode_cleanly(output, verify_join_offsets):
            Path(output).unlink(missing_ok=True)
            raise RuntimeError(
                f"{output} didn't decode cleanly at a join point — the segments' codec parameters "
                "likely drifted enough between separate encoder runs to not actually be "
                "interchangeable, even though they matched on codec name"
            )


def build_filter_complex(
    clips: list[dict],
    audio_inputs: list[str],
    width: int,
    height: int,
    fps: float,
    transition: str,
    duration: float,
) -> tuple[str, str, str]:
    """Return (filter_complex string, output video label, output audio label)."""
    parts = []

    # Normalize each clip to the same resolution/fps/pixel format so xfade
    # has consistent input geometry to work with.
    for i in range(3):
        parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"fps={fps},format=yuv420p[v{i}]"
        )

    # Video: chain two xfade transitions, intro->main, then result->outro.
    offset1 = clips[0]["duration"] - duration
    parts.append(
        f"[v0][v1]xfade=transition={transition}:duration={duration}:"
        f"offset={offset1:.3f}[v01]"
    )
    offset2 = clips[0]["duration"] + clips[1]["duration"] - 2 * duration
    parts.append(
        f"[v01][v2]xfade=transition={transition}:duration={duration}:"
        f"offset={offset2:.3f}[vout]"
    )

    # Audio: chain two acrossfade transitions the same way. Any clip missing
    # an audio track was backfilled with a silent one at the matching input index.
    parts.append(f"[{audio_inputs[0]}][{audio_inputs[1]}]acrossfade=d={duration}[a01]")
    parts.append(f"[a01][{audio_inputs[2]}]acrossfade=d={duration}[aout]")

    return ";".join(parts), "[vout]", "[aout]"


def _clip_input_args(path: str, is_image: bool, duration: float) -> list[str]:
    """ffmpeg input-side args to read a whole clip — a still image gets
    looped for `duration` via the image2 demuxer (same as stitch()'s main
    input-building loop); a video is just -i."""
    if is_image:
        return ["-loop", "1", "-t", f"{duration:.3f}", "-i", path]
    return ["-i", path]


def _build_pair_xfade_cmd(
    input_args_a: list[str], has_audio_a: bool, duration_a: float,
    input_args_b: list[str], has_audio_b: bool, duration_b: float,
    width: int, height: int, fps: float, transition: str, transition_duration: float,
    encoder: str, video_encode_args: list[str], encoder_extra_args: list[str], output: str,
) -> list[str]:
    """Build an ffmpeg command crossfading exactly two inputs (each
    described by its own input-side ffmpeg args) into `output` — the
    two-clip version of build_filter_complex()'s chained three-clip
    crossfade, used by _fast_copy_stitch() for the short intro/main-start
    and main-end/outro transition windows. `encoder` matches whatever
    codec the untouched, copied footage this gets concatenated next to is
    already in (see FAST_COPY_CODECS) — this can't default to h264 the way
    the old always-re-encode fallback does."""
    cmd = ["ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(), *input_args_a, *input_args_b]

    audio_inputs = []
    next_input_index = 2
    for i, (has_audio, duration) in enumerate(((has_audio_a, duration_a), (has_audio_b, duration_b))):
        if has_audio:
            audio_inputs.append(f"{i}:a")
        else:
            cmd += [
                "-f", "lavfi", "-t", f"{duration:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
            audio_inputs.append(f"{next_input_index}:a")
            next_input_index += 1

    offset = duration_a - transition_duration
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v0];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v1];"
        f"[v0][v1]xfade=transition={transition}:duration={transition_duration}:offset={offset:.3f}[vout];"
        f"[{audio_inputs[0]}][{audio_inputs[1]}]acrossfade=d={transition_duration}[aout]"
    )
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", encoder, *video_encode_args, "-preset", "veryfast", *encoder_extra_args,
        "-c:a", "aac", "-b:a", "192k",
        "-video_track_timescale", str(CONCAT_TIMESCALE),
        output,
    ]
    return cmd


def _fast_copy_stitch(
    intro: str, main_clip: str, outro: str, output: str,
    is_image_flags: list[bool], clips: list[dict],
    width: int, height: int, fps: float,
    transition: str, transition_duration: float, crf: int,
) -> str | None:
    """stitch()'s fast path: rather than crossfading the whole intro+main+
    outro timeline in one pass (which decodes and re-encodes all of it,
    including however long the main body is), build only the two short
    crossfade windows — intro blending into the start of main, and the end
    of main blending into outro — as their own small encodes, and stream-
    copy the untouched middle of main between them. The three pieces are
    joined with the concat demuxer, so the vast majority of a long main
    clip is never decoded or re-encoded at all. The re-encoded windows
    target the main clip's own bitrate (see _quality_match_args()) rather
    than an arbitrary configured CRF, so they don't look like a quality
    jump where they meet the untouched footage.

    Only applicable when main_clip's codec is one fast copy knows how to
    match (see FAST_COPY_CODECS — currently h264 and hevc; the re-encoded
    transition windows have to be the same codec as the copied middle they
    sit next to) and main_clip is long enough to actually have an
    untouched middle. Returns output on success; returns None (after
    cleaning up) if the fast path isn't usable here, so the caller can
    fall back to the old single-pass crossfade — this always logs why."""
    intro_clip, main_info, outro_clip = clips

    codec_info = FAST_COPY_CODECS.get(main_info.get("video_codec"))
    if codec_info is None:
        print(
            f"[stitch] fast copy: main clip's codec is {main_info.get('video_codec')!r}, which fast "
            f"copy doesn't know how to match (supported: {', '.join(FAST_COPY_CODECS)}) — the "
            "re-encoded transition windows couldn't be joined with a copied middle of a different "
            "codec, skipping",
            file=sys.stderr,
        )
        return None

    main_duration = main_info["duration"]
    mid_start = find_next_keyframe(main_clip, transition_duration, codec_info)
    if mid_start is None:
        print(
            f"[stitch] fast copy: couldn't find a safe keyframe (a true random-access point — see "
            f"find_next_keyframe()) at/after {transition_duration:.3f}s into the main clip, skipping",
            file=sys.stderr,
        )
        return None
    mid_end = main_duration - transition_duration
    if mid_start >= mid_end:
        print(
            f"[stitch] fast copy: main clip isn't long enough to have an untouched middle once "
            f"both {transition_duration:.3f}s crossfade windows are set aside (next keyframe at "
            f"{mid_start:.3f}s, need it before {mid_end:.3f}s) — skipping",
            file=sys.stderr,
        )
        return None

    video_encode_args = _quality_match_args(main_clip, crf)
    margin = FAST_COPY_END_TRIM_FRAMES / fps if fps > 0 else 0.0

    front_path = Path(output).with_name(f".{Path(output).stem}.front{Path(output).suffix}")
    middle_path = Path(output).with_name(f".{Path(output).stem}.middle{Path(output).suffix}")
    tail_path = Path(output).with_name(f".{Path(output).stem}.tail{Path(output).suffix}")
    front_duration = intro_clip["duration"] + mid_start - transition_duration
    try:
        front_cmd = _build_pair_xfade_cmd(
            _clip_input_args(intro, is_image_flags[0], intro_clip["duration"]), intro_clip["has_audio"], intro_clip["duration"],
            ["-t", f"{mid_start:.3f}", "-i", main_clip], main_info["has_audio"], mid_start,
            width, height, fps, transition, transition_duration,
            codec_info["encoder"], video_encode_args, codec_info["extra"], str(front_path),
        )
        print(f"[stitch] fast copy: encoding the intro crossfade (intro + main's first {mid_start:.3f}s)")
        _print_step(front_duration, "encoding intro crossfade")
        print("Running:", " ".join(front_cmd))
        subprocess.run(front_cmd, check=True, stdin=subprocess.DEVNULL)

        middle_duration = max(0.0, mid_end - mid_start - margin)
        middle_cmd = [
            "ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(),
            "-ss", f"{mid_start:.3f}", "-i", main_clip, "-t", f"{middle_duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-video_track_timescale", str(CONCAT_TIMESCALE),
            str(middle_path),
        ]
        print(
            f"[stitch] fast copy: stream-copying the untouched middle of main "
            f"({mid_start:.3f}s -> {mid_end:.3f}s, no re-encoding)"
        )
        _print_step(middle_duration, "copying middle")
        print("Running:", " ".join(middle_cmd))
        subprocess.run(middle_cmd, check=True, stdin=subprocess.DEVNULL)

        tail_duration = main_duration - mid_end
        tail_out_duration = tail_duration + outro_clip["duration"] - transition_duration
        tail_cmd = _build_pair_xfade_cmd(
            ["-ss", f"{mid_end:.3f}", "-i", main_clip], main_info["has_audio"], tail_duration,
            _clip_input_args(outro, is_image_flags[2], outro_clip["duration"]), outro_clip["has_audio"], outro_clip["duration"],
            width, height, fps, transition, transition_duration,
            codec_info["encoder"], video_encode_args, codec_info["extra"], str(tail_path),
        )
        print(f"[stitch] fast copy: encoding the outro crossfade (main's last {tail_duration:.3f}s + outro)")
        _print_step(tail_out_duration, "encoding outro crossfade")
        print("Running:", " ".join(tail_cmd))
        subprocess.run(tail_cmd, check=True, stdin=subprocess.DEVNULL)

        print("[stitch] fast copy: joining the intro crossfade, copied middle, and outro crossfade")
        _print_step(front_duration + middle_duration + tail_out_duration, "joining")
        join_offsets = [front_duration, front_duration + middle_duration]
        _run_concat([front_path, middle_path, tail_path], output, faststart=True, verify_join_offsets=join_offsets)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"[stitch] fast copy step failed ({e}) — falling back to a full re-encode", file=sys.stderr)
        return None
    finally:
        front_path.unlink(missing_ok=True)
        middle_path.unlink(missing_ok=True)
        tail_path.unlink(missing_ok=True)

    print(
        f"\nDone (fast copy: ~{front_duration + tail_out_duration:.1f}s re-encoded across both "
        f"crossfades, {middle_duration:.1f}s of the main clip copied) -> {output}"
    )
    return output


def expand_output_path(path: str) -> str:
    """Expand strftime placeholders (%Y, %m, %d, %H, %M, %S, etc.)
    anywhere in an output path — filename and any directory components —
    with the current date/time, so a whole dated folder hierarchy can be
    produced, not just a dated filename, e.g.
    "recordings/%Y-%m-%d/final_%H-%M-%S.mp4". A path with no '%' in it
    passes through unchanged. Applied wherever a path is actually
    written (stitch's output, trim_clip's dst, the render-state file),
    so it works the same from the CLI, the GUI, or a render-state file.

    Also creates any directory component of the expanded path that
    doesn't already exist yet (recursively, like `mkdir -p`) — needed
    now that a directory name itself can be date-based, so it's never
    going to already exist the first time a given day/hour/etc. rolls
    around. A failure here (e.g. no permission) is treated the same as
    any other fatal output-path problem in this file: sys.exit() with a
    clear message, since there's nowhere useful to write the actual
    output otherwise."""
    expanded = datetime.now().strftime(path)
    parent = Path(expanded).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"Could not create directory {str(parent)!r} for output path {expanded!r}: {e}")
    return expanded


# --------------------------------------------------------------------------
# Optional hardware-accelerated encoding for trim_clip()'s/stitch()'s full
# re-encode paths (fast_copy off, or fast_copy tried and fell back) — the
# only ffmpeg encode steps expensive enough for this to matter; see
# _encode_with_fallback()'s docstring for the rest.
# --------------------------------------------------------------------------

# Selectable via trim.encoder/stitch.encoder (config/render-state) or
# --encoder (the stitch subcommand's CLI). "software" (libx264) is what
# this project originally used and needs no translation; "nvenc" is now
# the default (at cq 23/preset p4 — a good speed/quality balance per real
# hardware testing) and, like every other hardware encoder here, needs its
# own quality and speed-preset flags
# translated from the same 0-51 CRF-like scale and "fastest available
# preset" choice used everywhere else in this project, since none of them
# actually implement x264's own -crf/-preset. These map to each vendor's
# documented ffmpeg options, and were confirmed to at least be valid,
# recognized options against this project's own ffmpeg build (they get
# past argument parsing to an actual attempt to open the hardware) — but
# none could be verified end-to-end against real encoded output, since no
# GPU encoder was available to test against while building this. Worth a
# real side-by-side quality/timing check against "software" once
# configured, and please report back if any of these need adjusting for
# your actual hardware/driver combination.
ENCODER_PROFILES = {
    "software": {
        "codec": "libx264",
        "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
        "default_preset": "veryfast",
        "args": lambda crf, preset: ["-crf", str(crf), "-preset", preset],
    },
    "nvenc": {
        # NVIDIA. -cq is NVENC's closest equivalent to x264's -crf (same
        # 0-51 scale, lower = better quality) — only honored under a
        # rate-control mode that supports it, hence the explicit -rc vbr.
        # Confirmed by direct testing: the SAME crf number does not mean
        # the same thing on NVENC as on x264 — NVENC needs a meaningfully
        # lower (better/bigger) number to land at comparable quality/size,
        # so don't assume parity with whatever crf is set for software.
        # p4 ("medium" per NVENC's own docs) is its default preset — a
        # much better size/quality tradeoff than p1 (fastest) for not
        # much speed given up, since NVENC stays fast across its whole
        # preset range unlike x264.
        "codec": "h264_nvenc",
        "presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        "default_preset": "p4",
        "args": lambda crf, preset: ["-rc", "vbr", "-cq", str(crf), "-preset", preset],
    },
    "qsv": {
        # Intel Quick Sync. -global_quality under QSV's default ICQ
        # (Intelligent Constant Quality) mode is its closest equivalent to
        # -crf, same 0-51 scale. Unlike the others, QSV's own preset names
        # happen to match x264's, "veryfast" included.
        "codec": "h264_qsv",
        "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        "default_preset": "veryfast",
        "args": lambda crf, preset: ["-global_quality", str(crf), "-preset", preset],
    },
    "amf": {
        # AMD. No direct CRF equivalent; constant-QP mode (-rc cqp) with
        # one QP value applied to all frame types is the closest
        # fixed-quality analog, same 0-51 scale. -quality doubles as AMF's
        # named preset (an alias of -preset, per its own ffmpeg -h output —
        # only one of the two needs setting).
        "codec": "h264_amf",
        "presets": ["speed", "balanced", "quality", "high_quality"],
        "default_preset": "speed",
        "args": lambda crf, preset: [
            "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-qp_b", str(crf), "-quality", preset,
        ],
    },
    "videotoolbox": {
        # Apple (macOS only — this encoder doesn't exist on other
        # platforms' ffmpeg builds at all, so it's untested even at the
        # argument-parsing level covered above). No CRF-equivalent or
        # named presets at all; -q:v is its only fixed-quality knob, 0-100
        # where *higher* is better — the inverse of x264's scale — so this
        # rescales/inverts crf onto it.
        "codec": "h264_videotoolbox",
        "presets": [],
        "default_preset": None,
        "args": lambda crf, preset: ["-q:v", str(max(1, min(100, round(100 - (crf / 51) * 100))))],
    },
}


def _encode_with_fallback(build_cmd, encoder: str, crf: int, preset: str | None, label: str):
    """Run an ffmpeg encode command for one of trim_clip()'s/stitch()'s
    full re-encode paths, using the requested encoder profile (see
    ENCODER_PROFILES) — `build_cmd(codec, codec_args)` returns the full
    ffmpeg argv given that profile's -c:v value and quality/preset args.
    `preset` is one of that profile's own "presets" list; None (or
    anything not actually in that list) falls back to the profile's own
    "default_preset", logging why for anything that was actually a typo
    rather than simply left unset. Falls back to "software" (libx264)
    automatically, logging why, if the requested encoder fails to run at
    all (wrong/missing hardware, driver mismatch, unsupported card, etc.
    — the way a hardware encoder typically fails, distinct from a normal
    encode failure) — since this is meant to be a speed option, not a new
    way for a render to fail outright. Exits (like the rest of this
    file's ffmpeg calls) if software itself fails too."""
    profile = ENCODER_PROFILES.get(encoder)
    if profile is None:
        print(
            f"[{label}] unknown encoder {encoder!r} (known: {', '.join(ENCODER_PROFILES)}) — using software",
            file=sys.stderr,
        )
        encoder, profile = "software", ENCODER_PROFILES["software"]

    if preset is not None and preset not in profile["presets"]:
        if profile["presets"]:
            print(
                f"[{label}] {encoder!r} has no preset {preset!r} (known: {', '.join(profile['presets'])}) — "
                f"using its default, {profile['default_preset']!r}",
                file=sys.stderr,
            )
        preset = None
    if preset is None:
        preset = profile["default_preset"]

    cmd = build_cmd(profile["codec"], profile["args"](crf, preset))
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
    if result.returncode == 0:
        return
    if encoder == "software":
        sys.exit("ffmpeg failed.")

    print(f"[{label}] {encoder} failed to encode — falling back to software (libx264)", file=sys.stderr)
    sw = ENCODER_PROFILES["software"]
    sw_cmd = build_cmd(sw["codec"], sw["args"](crf, sw["default_preset"]))
    print("Running:", " ".join(sw_cmd))
    result = subprocess.run(sw_cmd, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit("ffmpeg failed (both the requested encoder and the software fallback).")


def stitch(
    intro: str, main_clip: str, outro: str, output: str = "output.mp4",
    transition_duration: float = 1.0, transition: str = "fade", crf: int = 23,
    intro_duration: float | None = None, outro_duration: float | None = None,
    fast_copy: bool = False, encoder: str = "nvenc", encoder_preset: str | None = None,
) -> str:
    """Crossfade an intro, main body, and outro clip into one video. intro/
    outro can each be either a video or a still image (jpg/png/etc.) — a
    still image is looped into a fixed-length clip using intro_duration/
    outro_duration (falling back to DEFAULT_IMAGE_DURATION if not given).
    main_clip must be a real video (it's the trimmed recording).
    Returns the resolved output path (after strftime expansion).

    fast_copy (default OFF, unlike trim_clip()'s — see below): try
    _fast_copy_stitch() first — only the two short crossfade windows get
    re-encoded, and the untouched middle of main_clip is stream-copied
    instead of being decoded and re-encoded along with everything else.
    _fast_copy_stitch() always verifies its own result actually decodes
    cleanly before trusting it (see _verify_join_points_decode_cleanly()),
    and falls straight through to the full single-pass crossfade below
    (logging why) if that check — or an earlier one (codec, a safe
    keyframe, main_clip being long enough) — didn't pass. So this is never
    unsafe to leave on.

    It's just not USEFUL in practice: every re-encode this produces (the
    two crossfade windows) goes through a scale/pad/format/xfade filter
    chain, and empirically — confirmed by direct, repeated testing while
    building this, including with h264 and with intro/outro cut from
    main_clip itself so the content genuinely matches — that consistently
    produces different-enough codec parameters (SPS/PPS/etc.) from
    main_clip's own that the decode-clean check fails every time, not just
    sometimes. MP4 (and concat generally) can only carry one set of those
    per track, so this isn't a narrow bug to chase further; it's a real
    limit of the format for this technique specifically. Left here,
    default off, in case a future encoder/container combination
    (or trim_clip()'s plain re-encode, which has no such filter chain and
    reliably passes the same check) changes that.

    encoder (default "nvenc", at cq 23/preset p4 — a good speed/quality
    balance per real hardware testing): which encoder the full
    single-pass crossfade below uses — see ENCODER_PROFILES/
    _encode_with_fallback() for the available names and the automatic
    fallback-to-software behavior if a hardware one fails to run. Only
    applies to that full re-encode, not fast_copy's own (much smaller,
    rarely-taken) re-encode windows.

    encoder_preset (default None): that encoder's own speed/quality
    preset — None (or anything not one of that encoder's own presets, per
    ENCODER_PROFILES) uses its default. Presets aren't comparable across
    encoders (each has its own names and its own speed/efficiency curve),
    so this only makes sense together with a specific `encoder` choice."""
    output = expand_output_path(output)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg and ffprobe must be installed and on PATH.")

    paths = [intro, main_clip, outro]
    for p in paths:
        if not Path(p).is_file():
            sys.exit(f"Input file not found: {p}")
    if is_image_file(main_clip):
        sys.exit(f"Main clip must be a video, not a still image: {main_clip!r}")

    is_image_flags = [is_image_file(p) for p in paths]
    duration_overrides = [intro_duration, None, outro_duration]

    clips = []
    for p, is_image, override in zip(paths, is_image_flags, duration_overrides):
        if not is_image:
            clips.append(probe(p))
            continue
        duration = override if override is not None else DEFAULT_IMAGE_DURATION
        if duration <= 0:
            sys.exit(f"{p!r} is a still image; its duration must be positive (got {duration}).")
        if override is None:
            print(f"No duration set for still image {p!r} — using the default {DEFAULT_IMAGE_DURATION}s.")
        dims = probe_image_dimensions(p)
        clips.append({"duration": duration, "width": dims["width"], "height": dims["height"], "has_audio": False})

    for name, clip in zip(("intro", "main", "outro"), clips):
        if clip["duration"] <= transition_duration:
            sys.exit(
                f"{name} clip is only {clip['duration']:.2f}s, which is too short "
                f"for a {transition_duration}s crossfade."
            )

    # Use the main clip's resolution/fps as the target for the whole video.
    width, height, fps = clips[1]["width"], clips[1]["height"], clips[1]["fps"]
    # xfade requires even dimensions for yuv420p.
    width -= width % 2
    height -= height % 2

    if fast_copy:
        result = _fast_copy_stitch(
            intro, main_clip, outro, output,
            is_image_flags, clips, width, height, fps,
            transition, transition_duration, crf,
        )
        if result is not None:
            return result
        print("[stitch] continuing with a full re-encode")

    cmd = ["ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args()]
    for p, is_image, clip in zip(paths, is_image_flags, clips):
        if is_image:
            # -loop 1: repeat the single frame indefinitely; -t: cut that
            # off at exactly the chosen duration. Both are input options,
            # so they have to come before this input's -i.
            cmd += ["-loop", "1", "-t", f"{clip['duration']:.3f}", "-i", p]
        else:
            cmd += ["-i", p]

    # Any clip without an audio track gets a silent track generated so the
    # audio crossfade chain always has three real streams to work with.
    audio_inputs = []
    next_input_index = 3
    for i, clip in enumerate(clips):
        if clip["has_audio"]:
            audio_inputs.append(f"{i}:a")
        else:
            cmd += [
                "-f", "lavfi", "-t", f"{clip['duration']:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
            audio_inputs.append(f"{next_input_index}:a")
            next_input_index += 1

    filter_complex, v_out, a_out = build_filter_complex(
        clips, audio_inputs, width, height, fps, transition, transition_duration,
    )

    # This is the path that actually runs for virtually every stitch
    # (fast_copy defaults off for stitch specifically — see stitch()'s
    # docstring for why it's not worth attempting), so its speed is what
    # matters day to day — hence the encoder choice (see
    # ENCODER_PROFILES/_encode_with_fallback()). Always h264 regardless of
    # main_clip's own codec, same as this fallback has always done.
    base_cmd = cmd + ["-filter_complex", filter_complex, "-map", v_out, "-map", a_out]

    def build_cmd(codec, codec_args):
        return base_cmd + [
            "-c:v", codec, *codec_args,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output,
        ]

    total_duration = clips[0]["duration"] + clips[1]["duration"] + clips[2]["duration"] - 2 * transition_duration
    _print_step(total_duration, "encoding")
    _encode_with_fallback(build_cmd, encoder, crf, encoder_preset, "stitch")

    print(f"\nDone -> {output}")
    return output


# --------------------------------------------------------------------------
# ProPresenter stage-display connection
# --------------------------------------------------------------------------

def extract_current_slide(raw_message: str):
    """Pull the current slide's (uid, text) out of a stagedisplay WebSocket
    message. Expected shape (per the legacy ProPresenter 7 protocol):
        {"acn": "fv", "ary": [{"acn": "cs", "uid": "...", "txt": "..."}, ...]}
    "txt" is frequently empty — most slides don't have a text layer — so
    "uid" is the identifier to key matching off of.
    Returns None if this message isn't a slide update, or isn't parseable.
    """
    try:
        msg = json.loads(raw_message)
    except json.JSONDecodeError:
        return None
    for item in msg.get("ary", []) if isinstance(msg, dict) else []:
        if item.get("acn") == "cs":
            return item.get("uid"), item.get("txt")
    return None


def slide_matches(uid: str | None, text: str | None, slide_cfg: dict) -> bool:
    """A slide config entry matches by UID if configured (exact, and
    preferred — always present and stable); otherwise falls back to exact
    or regex text matching for slides that do carry a text layer.

    match_mode "exact" does a plain equality check; anything else
    (including the old "substring" mode from before regex support, and the
    new default "regex") runs target_text as a regex pattern via
    re.search — a plain literal pattern with no regex metacharacters
    behaves identically to old-style substring containment, so existing
    configs saved with match_mode "substring" keep working unchanged."""
    target_uid = slide_cfg.get("uid")
    if target_uid:
        return uid == target_uid
    target_text = slide_cfg.get("text")
    if not target_text or text is None:
        return False
    case_sensitive = slide_cfg.get("case_sensitive", False)
    if slide_cfg.get("match_mode", "exact") == "exact":
        a, b = text, target_text
        if not case_sensitive:
            a, b = a.lower(), b.lower()
        return a == b
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.search(target_text, text, flags) is not None
    except re.error as e:
        sys.exit(f"Invalid regex in begin_slide/end_slide text {target_text!r}: {e}")


def learn_mode(pp_cfg: dict):
    """Connect to ProPresenter only (no OBS needed) and print each distinct
    slide's uid/text as the operator steps through slides, so they can be
    copied into the config's begin_slide / end_slide uid fields."""
    events_q: "queue.Queue" = queue.Queue()
    start_propresenter_thread(pp_cfg, events_q)
    print("Learn mode: step through your slides in ProPresenter. Ctrl+C to stop.\n")
    last_uid = None
    while True:
        kind, _ts, payload = events_q.get()
        if kind != "pp_raw":
            continue
        result = extract_current_slide(payload)
        if result is None:
            continue
        uid, text = result
        if uid == last_uid:
            continue
        last_uid = uid
        print(f'uid: "{uid}"   text: {text!r}')


async def propresenter_loop(pp_cfg: dict, out_queue: "queue.Queue", stop_event: threading.Event):
    """Connect to ProPresenter's stage display socket and forward every raw
    message (with a receive timestamp) into out_queue.

    The legacy stagedisplay socket drops periodically as a matter of course
    — it's not a sign of a broken setup, and there's no client-side ack or
    heartbeat reply that prevents it (confirmed by how Bitfocus Companion's
    widely-deployed ProPresenter integration handles it: it just polls every
    ~5s and reconnects, treating drops as routine). So reconnect on a fast,
    fixed interval rather than growing backoff — a live service can't afford
    a widening gap where a slide-change event could be missed."""
    import websockets

    uri = f"ws://{pp_cfg['host']}:{pp_cfg.get('port', 1025)}/stagedisplay"
    reconnect_interval = pp_cfg.get("reconnect_interval_seconds", 4)
    while not stop_event.is_set():
        try:
            async with websockets.connect(uri, open_timeout=10) as ws:
                # ptl=610 is what the /stagedisplay endpoint expects even on
                # current Pro7 builds (confirmed against Bitfocus Companion's
                # ProPresenter module) — it's a different, fixed namespace
                # from the /remote endpoint's protocol version (701).
                auth_msg = {"pwd": pp_cfg.get("password", ""), "ptl": 610, "acn": "ath"}
                await ws.send(json.dumps(auth_msg))
                ack_raw = await ws.recv()
                try:
                    ack = json.loads(ack_raw)
                except json.JSONDecodeError:
                    ack = {}
                if ack.get("acn") == "ath" and ack.get("ath") is False:
                    print(f"[propresenter] auth rejected: {ack.get('err', ack)}", file=sys.stderr)
                    raise ConnectionRefusedError("ProPresenter auth rejected")
                print(f"[propresenter] connected to {uri}")
                if ack.get("acn") != "ath":
                    # Not an auth ack — might be a real slide update, don't drop it.
                    out_queue.put(("pp_raw", time.time(), ack_raw))
                async for raw in ws:
                    out_queue.put(("pp_raw", time.time(), raw))
        except Exception as e:
            print(f"[propresenter] connection dropped: {e!r}; reconnecting in {reconnect_interval}s", file=sys.stderr)
            await asyncio.sleep(reconnect_interval)


def start_propresenter_thread(pp_cfg: dict, out_queue: "queue.Queue") -> threading.Event:
    stop_event = threading.Event()

    def runner():
        asyncio.run(propresenter_loop(pp_cfg, out_queue, stop_event))

    threading.Thread(target=runner, daemon=True).start()
    return stop_event


def start_stdin_thread(out_queue: "queue.Queue"):
    """Reads manual override commands from stdin, one per line —
    'mark_begin'/'mark_end' (lets an operator, or the GUI, manually
    trigger what a slide match would normally trigger, for when
    something's gone wrong live and there's no time to fix ProPresenter
    itself) and 'trim'/'stitch' (see _trim_worker()/_stitch_worker()) —
    and feeds them into the same event queue as ProPresenter/OBS events,
    tagged "manual". Works the same typed directly into a terminal
    running 'watch' interactively."""
    def runner():
        for raw in sys.stdin:
            cmd = raw.strip()
            if cmd:
                out_queue.put(("manual", time.time(), cmd))

    threading.Thread(target=runner, daemon=True).start()


# --------------------------------------------------------------------------
# Trim + render (trim the OBS recording, then hand off to stitch())
# --------------------------------------------------------------------------

# -nostdin plus stdin=DEVNULL everywhere a subprocess is run in this file,
# belt and suspenders: since watch() added a background thread reading
# this script's own stdin (for the manual mark_begin/mark_end/trim/stitch
# commands) and the GUI now keeps that stdin open as a pipe rather than
# leaving it unset/closed, ffmpeg would otherwise inherit that same pipe
# and — on at least some platforms — treat it as something to read
# interactive keyboard commands from ("Press [q] to stop, [?] for help"),
# which can make it hang waiting on stdin instead of just encoding and
# exiting. ffmpeg has no legitimate reason to ever want that here.

# How much the fast-copy path's stream-copied end trim is allowed to run
# past the requested end offset before it's nudged back: -c copy can only
# cut at a packet boundary, and empirically tends to round up to include
# whatever packet contains the requested end timestamp rather than
# stopping just short of it (confirmed by direct measurement while
# building this) — subtracting a small, fixed margin based on the clip's
# own frame rate brings that back in line with what a full re-encode would
# have produced, at the cost of a small, deliberate under-run instead of
# an over-run if the true rounding behavior ever differs (e.g. a different
# ffmpeg build). Either way this is a matter of at most a couple of frames
# — inaudible/invisible for this pipeline's purposes.
FAST_COPY_END_TRIM_FRAMES = 1.5

# loudnorm's own defaults for the two knobs this project doesn't expose as
# separate config — true peak ceiling and loudness range, in that order.
# Only the integrated-loudness target (trim.normalize_target_lufs) is
# actually meant to vary per user/platform; these two rarely need tuning
# alongside it.
NORMALIZE_TARGET_TP = -2.0
NORMALIZE_TARGET_LRA = 7.0


def _measure_loudness(src: str, start: float, end: float, target_i: float) -> dict | None:
    """Run loudnorm's first analysis pass over exactly [start, end] of
    src's audio (video untouched — -vn, so this is cheap regardless of how
    long the video itself is, or which video path trim_clip() ends up
    taking) and return the measured stats (input_i/input_tp/input_lra/
    input_thresh/target_offset) loudnorm's own second pass needs.

    This matters specifically because trim_clip()'s fast-copy path
    (see _fast_copy_trim()) can encode this same audio range as two
    separate pieces (a re-encoded sliver and a stream-copied tail, each
    with their own -c:a aac re-encode) rather than one continuous pass —
    loudnorm's single-pass ("streaming") mode makes its own gain decision
    from a limited look-ahead window, which isn't guaranteed to agree
    between two disjoint chunks of the same program, risking an audible
    level jump right at the join. Measuring once, up front, over the
    *whole* trimmed range and feeding the identical measured values into
    both pieces' second-pass filters (see _loudnorm_filter_arg()) applies
    one single, fixed, program-consistent correction to each of them
    instead, the way ffmpeg's own documentation recommends normalizing a
    program that has to be encoded in more than one pass.

    Returns None (after logging why) if the analysis pass fails or its
    report can't be parsed, so the caller can skip normalization for this
    render rather than fail it outright over what's meant to be a quality
    improvement, not a hard requirement."""
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-ss", f"{start:.3f}", "-i", src, "-t", f"{end - start:.3f}",
        "-vn", "-af", f"loudnorm=I={target_i}:TP={NORMALIZE_TARGET_TP}:LRA={NORMALIZE_TARGET_LRA}:print_format=json",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    # loudnorm prints its JSON report on stderr partway through (ffmpeg's
    # own muxing summary and final progress line still follow it, so it's
    # not simply the last thing printed) mixed in with the rest of
    # ffmpeg's normal console output — it's the only brace-delimited block
    # in there, confirmed by direct testing, so the last one found (in
    # case ffmpeg ever logs another one first) is always it.
    matches = re.findall(r"\{[^{}]*\}", result.stderr)
    if not matches:
        print(
            "[trim] normalize audio: couldn't find loudnorm's measurement report in ffmpeg's output "
            "(the analysis pass may have failed) — skipping normalization for this trim",
            file=sys.stderr,
        )
        return None
    try:
        measured = json.loads(matches[-1])
    except json.JSONDecodeError:
        print(
            "[trim] normalize audio: loudnorm's measurement report wasn't valid JSON — "
            "skipping normalization for this trim",
            file=sys.stderr,
        )
        return None

    # Silent (or otherwise degenerate, e.g. a test recording with no real
    # audio) source measures at -inf LUFS — loudnorm's own second pass then
    # rejects that outright ("Value -inf for parameter 'measured_I' out of
    # range"), which would otherwise only surface much later, inside
    # trim_clip()'s actual encode. Catch it here instead, the same as any
    # other failed measurement: there's no real loudness to normalize in a
    # silent clip anyway, so skipping is the correct outcome, not just a
    # crash to avoid.
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        try:
            valid = math.isfinite(float(measured[key]))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            print(
                f"[trim] normalize audio: measured {key} is {measured.get(key)!r} — likely "
                "silent source audio — skipping normalization for this trim",
                file=sys.stderr,
            )
            return None
    return measured


def _loudnorm_filter_arg(target_i: float, measured: dict | None) -> str:
    """Build loudnorm's -af argument. With `measured` (see
    _measure_loudness()), this is its precise second pass: a fixed gain
    correction computed from stats measured over the *entire* trimmed
    range, safe to apply identically to each separately-encoded piece of
    it (see _measure_loudness()'s docstring for why that consistency
    matters). Without it (measured is None, e.g. trim.normalize_audio is
    off), this is a plain single-pass filter — only ever used for
    trim_clip()'s full re-encode fallback, which is always one continuous
    pass over the whole range and so has no cross-piece consistency
    concern to begin with."""
    if measured is None:
        return f"loudnorm=I={target_i}:TP={NORMALIZE_TARGET_TP}:LRA={NORMALIZE_TARGET_LRA}"
    return (
        f"loudnorm=I={target_i}:TP={NORMALIZE_TARGET_TP}:LRA={NORMALIZE_TARGET_LRA}:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true:print_format=summary"
    )


def _fast_copy_trim(
    src: str, dst: str, start: float, end: float, crf: int,
    normalize_audio: bool = False, normalize_target_lufs: float = -16.0,
    measured_loudness: dict | None = None,
) -> str | None:
    """trim_clip()'s fast path: re-encode only the sliver from `start` to
    the nearest keyframe at/after it (needed because a decode has to begin
    on a keyframe), then stream-copy the video from that keyframe through
    to `end` — a stream copy's end doesn't need to land on a keyframe the
    way its start does, only its start, so the entire rest of the clip
    (very likely almost all of it) never gets decoded or re-encoded at
    all. The two pieces are joined with the concat demuxer.

    normalize_audio/normalize_target_lufs/measured_loudness: whether (and
    to what target) to loudness-normalize the audio in both the sliver's
    and the tail's own -c:a re-encode (audio is always re-encoded here,
    even though video is stream-copied for the tail — see trim_clip()'s
    docstring). `measured_loudness` must be trim_clip()'s own single,
    upfront measurement over this whole [start, end] range (see
    _measure_loudness()), not measured separately per piece here — that's
    what keeps the two pieces' corrections consistent with each other
    across their join. Ignored entirely when normalize_audio is False.

    Returns dst on success. Returns None (after cleaning up any partial
    output) if the fast path isn't usable or safe here, so the caller can
    fall back to trim_clip()'s old, always-correct full re-encode; this
    always logs why."""
    info = probe(src)
    codec_info = FAST_COPY_CODECS.get(info.get("video_codec"))
    if codec_info is None:
        print(
            f"[trim] fast copy: source's codec is {info.get('video_codec')!r}, which fast copy "
            f"doesn't know how to match (supported: {', '.join(FAST_COPY_CODECS)}) — the re-encoded "
            "sliver couldn't be joined with a copied tail of a different codec, skipping",
            file=sys.stderr,
        )
        return None

    keyframe_time = find_next_keyframe(src, start, codec_info)
    if keyframe_time is None:
        print(
            f"[trim] fast copy: couldn't find a safe keyframe (a true random-access point — see "
            f"find_next_keyframe()) at/after {start:.3f}s, skipping",
            file=sys.stderr,
        )
        return None
    if keyframe_time >= end:
        print(
            f"[trim] fast copy: the next keyframe ({keyframe_time:.3f}s) is at/after the trim's "
            f"end ({end:.3f}s) — this cut is shorter than one GOP, skipping",
            file=sys.stderr,
        )
        return None

    fps = info["fps"]
    tail_margin = FAST_COPY_END_TRIM_FRAMES / fps if fps > 0 else 0.0
    audio_filter_args = (
        ["-af", _loudnorm_filter_arg(normalize_target_lufs, measured_loudness)] if normalize_audio else []
    )

    # Close enough to already be a keyframe: skip the sliver re-encode
    # entirely and just copy the whole range, no re-encoding at all.
    if keyframe_time - start <= 0.02:
        tail_duration = max(0.0, end - keyframe_time - tail_margin)
        cmd = [
            "ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(),
            "-ss", f"{keyframe_time:.3f}", "-i", src, "-t", f"{tail_duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *audio_filter_args,
            "-video_track_timescale", str(CONCAT_TIMESCALE),
            dst,
        ]
        print(f"[trim] fast copy: start ({start:.3f}s) is already on a keyframe — copying the whole clip, no re-encoding")
        _print_step(tail_duration, "copying")
        print("Running:", " ".join(cmd))
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            print("[trim] fast copy failed, falling back to a full re-encode", file=sys.stderr)
            return None
        print(f"\nDone (fast copy, fully copied, no re-encoding) -> {dst}")
        return dst

    sliver_path = Path(dst).with_name(f".{Path(dst).stem}.sliver{Path(dst).suffix}")
    tail_path = Path(dst).with_name(f".{Path(dst).stem}.tail{Path(dst).suffix}")
    try:
        sliver_cmd = [
            "ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(),
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{keyframe_time - start:.3f}",
            "-c:v", codec_info["encoder"], "-crf", str(crf), "-preset", "veryfast", *codec_info["extra"],
            "-c:a", "aac", "-b:a", "192k", *audio_filter_args,
            "-video_track_timescale", str(CONCAT_TIMESCALE),
            str(sliver_path),
        ]
        print(
            f"[trim] fast copy: re-encoding {start:.3f}s -> {keyframe_time:.3f}s "
            f"({keyframe_time - start:.3f}s, up to the nearest keyframe)"
        )
        _print_step(keyframe_time - start, "re-encoding sliver")
        print("Running:", " ".join(sliver_cmd))
        subprocess.run(sliver_cmd, check=True, stdin=subprocess.DEVNULL)

        tail_duration = max(0.0, end - keyframe_time - tail_margin)
        tail_cmd = [
            "ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(),
            "-ss", f"{keyframe_time:.3f}", "-i", src, "-t", f"{tail_duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", *audio_filter_args,
            "-video_track_timescale", str(CONCAT_TIMESCALE),
            str(tail_path),
        ]
        print(
            f"[trim] fast copy: stream-copying video {keyframe_time:.3f}s -> {end:.3f}s "
            f"({tail_duration:.3f}s, no re-encoding)"
        )
        _print_step(tail_duration, "copying tail")
        print("Running:", " ".join(tail_cmd))
        subprocess.run(tail_cmd, check=True, stdin=subprocess.DEVNULL)

        print("[trim] fast copy: joining the re-encoded sliver and copied tail")
        _print_step((keyframe_time - start) + tail_duration, "joining")
        _run_concat([sliver_path, tail_path], dst, verify_join_offsets=[keyframe_time - start])
    except (subprocess.CalledProcessError, RuntimeError) as e:
        print(f"[trim] fast copy step failed ({e}) — falling back to a full re-encode", file=sys.stderr)
        return None
    finally:
        sliver_path.unlink(missing_ok=True)
        tail_path.unlink(missing_ok=True)

    print(f"\nDone (fast copy: {keyframe_time - start:.3f}s re-encoded, {tail_duration:.3f}s copied) -> {dst}")
    return dst


def trim_clip(
    src: str, dst: str, start: float, end: float, crf: int = 23,
    fast_copy: bool = True, encoder: str = "nvenc", encoder_preset: str | None = None,
    normalize_audio: bool = True, normalize_target_lufs: float = -16.0,
) -> str:
    """Trim src down to [start, end] and write it to dst. Returns the
    resolved destination path (dst after strftime expansion) — use this,
    not the original dst, for anything downstream that needs to find the
    file that actually got written.

    fast_copy (default on): try _fast_copy_trim() first — re-encode just a
    short sliver up to the nearest keyframe, then stream-copy the rest, so
    a long clip doesn't get fully decoded and re-encoded just to cut its
    ends off. Falls straight through to the full re-encode below (logging
    why) if that's not applicable or safe for this source — only works
    for the codecs in FAST_COPY_CODECS (h264, hevc), and not worth it for
    a very short trim range. Note this means fast_copy's output ends up
    in src's own codec (hevc stays hevc, etc.) rather than always being
    h264 the way the full-re-encode fallback below always is.

    encoder/encoder_preset (default "nvenc"/None, i.e. cq 23 at nvenc's
    own default preset p4 — a good speed/quality balance per real
    hardware testing): which encoder (and that encoder's own speed/quality
    preset) the full re-encode below uses if fast_copy is off or falls
    back — see ENCODER_PROFILES/
    _encode_with_fallback() for the available names, per-encoder presets,
    and the automatic fallback-to-software behavior if a hardware one
    fails to run. Doesn't apply to fast_copy's own (much smaller) sliver
    re-encode.

    normalize_audio/normalize_target_lufs (default on/-16.0): loudness-
    normalize the trimmed clip's audio to `normalize_target_lufs`
    integrated LUFS via ffmpeg's loudnorm filter — useful since a live
    recording's levels can vary service to service (mic gain, distance
    from the mic, etc.) in a way a fixed CRF/encoder choice has no bearing
    on. Audio only; doesn't touch video. Measured once, here, over the
    *whole* [start, end] range regardless of which video path ends up
    being used below (fast_copy on or off) — see _measure_loudness()'s
    docstring for why that has to happen exactly once, up front, rather
    than being left to each encode below to work out on its own. A failed
    measurement (logged, either way) just skips normalization for this
    render rather than failing it outright."""
    dst = expand_output_path(dst)

    measured_loudness = None
    if normalize_audio:
        _print_step(end - start, "measuring loudness")
        measured_loudness = _measure_loudness(src, start, end, normalize_target_lufs)
        if measured_loudness is None:
            normalize_audio = False

    if fast_copy:
        result = _fast_copy_trim(
            src, dst, start, end, crf,
            normalize_audio=normalize_audio, normalize_target_lufs=normalize_target_lufs,
            measured_loudness=measured_loudness,
        )
        if result is not None:
            return result
        print("[trim] continuing with a full re-encode")

    # Frame-accurate trim: -ss/-to placed after -i forces ffmpeg to decode
    # from the start rather than snapping to the nearest keyframe.
    audio_filter_args = (
        ["-af", _loudnorm_filter_arg(normalize_target_lufs, measured_loudness)] if normalize_audio else []
    )

    def build_cmd(codec, codec_args):
        return [
            "ffmpeg", "-y", "-nostdin", *_ffmpeg_output_args(),
            "-i", src,
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-c:v", codec, *codec_args,
            "-c:a", "aac", "-b:a", "192k", *audio_filter_args,
            dst,
        ]

    _print_step(end - start, "re-encoding")
    _encode_with_fallback(build_cmd, encoder, crf, encoder_preset, "trim")
    return dst


TIMESTAMP_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)(\.\d+)?$")


def format_timestamp(seconds: float) -> str:
    """Render a seconds value as HH:MM:SS.mmm — this is how render_state
    JSON files store raw_begin_offset/raw_end_offset, so they're readable
    (and hand-editable) as clock timestamps rather than raw seconds."""
    total_ms = round(seconds * 1000)
    sign = "-" if total_ms < 0 else ""
    total_ms = abs(total_ms)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, ms = divmod(rem_ms, 1000)
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def parse_timestamp(value) -> float:
    """Parse a render_state.json timestamp: HH:MM:SS.mmm (the current
    format), or a plain number of seconds (older files, or anyone hand-
    editing one that way)."""
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
    try:
        return float(value)
    except (TypeError, ValueError):
        sys.exit(f"Could not parse timestamp {value!r} (expected HH:MM:SS.mmm or a number of seconds).")


def compute_trim_offsets(raw_begin_offset: float, raw_end_offset: float, pad_start: float, pad_end: float) -> tuple[float, float]:
    """Positive pad pushes a cut point forward (later) in time; negative
    pushes it back (earlier) — same sign convention for both ends."""
    start_offset = max(0.0, raw_begin_offset + pad_start)
    end_offset = max(0.0, raw_end_offset + pad_end)
    if start_offset >= end_offset:
        sys.exit(
            f"pad_start_seconds/pad_end_seconds push the trim range past itself "
            f"(start={start_offset:.3f}s, end={end_offset:.3f}s) — reduce the padding."
        )
    return start_offset, end_offset


def render(state: dict):
    """Trim the recording and (optionally) stitch it with the intro/outro,
    entirely from a self-contained state dict — no OBS/ProPresenter
    connection involved. This is what both 'watch' and 'render' call into,
    so a live run's trim+stitch step is always reproducible offline."""
    trim_cfg = state.get("trim", {})
    stitch_cfg = state.get("stitch", {})
    if state.get("recording_path") is None or state.get("raw_begin_offset") is None or state.get("raw_end_offset") is None:
        sys.exit(
            "This render-state file doesn't have a complete recording yet "
            "(recording_path/raw_begin_offset/raw_end_offset is still null) — "
            "wait for Watch to finish capturing both marks (and, if trimming "
            "before the recording stops, for a recording file to be found), "
            "or use Trim from the Live tab instead."
        )
    _reset_steps(_render_step_total(trim_cfg, stitch_cfg))

    start_offset, end_offset = compute_trim_offsets(
        parse_timestamp(state["raw_begin_offset"]), parse_timestamp(state["raw_end_offset"]),
        trim_cfg.get("pad_start_seconds", 0.0), trim_cfg.get("pad_end_seconds", 0.0),
    )

    trimmed_path = trim_clip(
        state["recording_path"], trim_cfg.get("output", "body_trimmed.mp4"),
        start_offset, end_offset, crf=trim_cfg.get("crf", 23),
        fast_copy=trim_cfg.get("fast_copy", True), encoder=trim_cfg.get("encoder", "nvenc"),
        encoder_preset=trim_cfg.get("encoder_preset"),
        normalize_audio=trim_cfg.get("normalize_audio", True),
        normalize_target_lufs=trim_cfg.get("normalize_target_lufs", -16.0),
    )
    print(f"\nTrimmed body clip -> {trimmed_path}")

    if stitch_cfg.get("auto"):
        stitch(
            stitch_cfg["intro"], trimmed_path, stitch_cfg["outro"],
            output=stitch_cfg.get("output", "final.mp4"),
            transition_duration=stitch_cfg.get("transition_duration", 1.0),
            transition=stitch_cfg.get("transition", "fade"),
            crf=stitch_cfg.get("crf", 23),
            intro_duration=stitch_cfg.get("intro_duration"),
            outro_duration=stitch_cfg.get("outro_duration"),
            fast_copy=stitch_cfg.get("fast_copy", False),
            encoder=stitch_cfg.get("encoder", "nvenc"), encoder_preset=stitch_cfg.get("encoder_preset"),
        )


def _resolve_state_path(trim_cfg: dict) -> Path:
    """Expand trim.state_output's strftime placeholders exactly once for a
    whole watch() run (see expand_output_path()) — watch() now writes the
    render-state file repeatedly, as information becomes available, rather
    than once at the end, so the actual filename has to be decided a
    single time up front and reused; re-expanding it on every write would
    silently produce a *different* file each time (a new timestamp)
    whenever state_output has a placeholder in it, the default included."""
    return Path(expand_output_path(trim_cfg.get("state_output") or DEFAULT_STATE_OUTPUT))


def _write_render_state(
    state_path: Path, recording_path: str | None, raw_begin_offset: float | None,
    raw_end_offset: float | None, trim_cfg: dict, stitch_cfg: dict,
) -> dict:
    """Builds the render-state dict and (over)writes it to `state_path`
    (see _resolve_state_path()) — the same self-contained record
    documented in the README. recording_path/raw_begin_offset/
    raw_end_offset may each be None (written as JSON null) when watch()
    calls this before that information is known yet — it writes this file
    the moment it starts, then rewrites it in place every time a mark (or
    the recording itself) actually happens, so what's on disk always
    reflects the best information available so far rather than only ever
    appearing once, fully formed, at the very end."""
    render_state = {
        "recording_path": recording_path,
        "raw_begin_offset": format_timestamp(raw_begin_offset) if raw_begin_offset is not None else None,
        "raw_end_offset": format_timestamp(raw_end_offset) if raw_end_offset is not None else None,
        "trim": trim_cfg,
        "stitch": stitch_cfg,
    }
    state_path.write_text(json.dumps(render_state, indent=2))
    print(f"[watcher] wrote render state -> {state_path}")
    print(f"[watcher] to redo just the trim/stitch later (no OBS/ProPresenter needed): python {Path(__file__).name} render {state_path}")
    return render_state


# --------------------------------------------------------------------------
# Trim/Stitch, triggered manually at any point during a live watch() run —
# including while OBS is still recording, instead of only once it stops.
# (See watch()'s "manual" event handling for the 'trim'/'stitch' commands.)
# --------------------------------------------------------------------------

RECORDING_EXTENSIONS = {".mkv", ".mp4", ".mov", ".flv", ".ts", ".m4v", ".avi"}


def find_active_recording_file(directory: str, max_age_seconds: float = 120.0) -> str | None:
    """The video file in `directory` most likely to be the one OBS is
    currently recording to: whichever video file has the newest
    modification time, since OBS keeps appending to it continuously —
    everything else in a dedicated recording folder should be static by
    comparison. Nothing is returned (rather than guessing) if there are no
    video files at all, or if even the newest one hasn't been touched more
    recently than max_age_seconds — a plain sanity check against picking a
    stale file, since a real in-progress recording should be updated far
    more often than that even accounting for OBS's own write-buffering."""
    try:
        candidates = [p for p in Path(directory).iterdir() if p.suffix.lower() in RECORDING_EXTENSIONS]
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if time.time() - newest.stat().st_mtime > max_age_seconds:
        return None
    return str(newest)


def _trim_worker(
    events_q: "queue.Queue", raw_begin_offset: float, raw_end_offset: float, trim_cfg: dict,
    record_dir: str | None, recording_stopped_event: threading.Event, get_final_output_path,
):
    """Attempts a trim right now, triggered by watch()'s 'trim' manual
    command — against whichever file OBS is currently still writing (see
    find_active_recording_file()) if recording hasn't stopped yet, or the
    finalized file if it has (get_final_output_path(), a callable rather
    than a plain value since it may not be known yet when this thread
    starts but become known while it's running).

    Reading a file OBS still has open for writing isn't universally
    guaranteed to work, and there's a real lag between what's been
    recorded and what's actually been flushed to disk and safe to read
    (encoder lookahead, Matroska's cluster-based writes) — relies on
    Matroska (MKV) not needing a finalized index the way MP4's `moov`
    atom does. If that first attempt fails and recording is still going,
    this waits for it to actually finish (recording_stopped_event, set by
    watch()'s main loop) and tries exactly once more against the
    finalized file — not indefinitely, so a real failure once the
    recording is genuinely done still surfaces as a real failure rather
    than retrying forever.

    Reports back over events_q rather than returning anything, since this
    runs in its own background thread — the main watch() loop is what
    updates its own bookkeeping and prints status."""
    _reset_steps(_trim_worst_case_steps(trim_cfg))

    def attempt(recording_path: str) -> str:
        start_offset, end_offset = compute_trim_offsets(
            raw_begin_offset, raw_end_offset,
            trim_cfg.get("pad_start_seconds", 0.0), trim_cfg.get("pad_end_seconds", 0.0),
        )
        return trim_clip(
            recording_path, trim_cfg.get("output", "body_trimmed.mp4"), start_offset, end_offset,
            crf=trim_cfg.get("crf", 23), fast_copy=trim_cfg.get("fast_copy", True),
            encoder=trim_cfg.get("encoder", "nvenc"), encoder_preset=trim_cfg.get("encoder_preset"),
            normalize_audio=trim_cfg.get("normalize_audio", True),
            normalize_target_lufs=trim_cfg.get("normalize_target_lufs", -16.0),
        )

    candidate = get_final_output_path() or (find_active_recording_file(record_dir) if record_dir else None)
    if candidate is None:
        print(
            "[watcher] trim: couldn't identify a recording file yet (no recording "
            "directory known, or no video file there was modified recently)",
            file=sys.stderr,
        )
        events_q.put(("trim_result", time.time(), {"succeeded": False, "trimmed_path": None}))
        return

    try:
        trimmed_path = attempt(candidate)
    except (SystemExit, Exception) as e:
        # trim_clip()/compute_trim_offsets() call sys.exit() on error — in
        # this background thread that only ends the thread (Python's
        # threading module doesn't propagate SystemExit to the process),
        # so this is here to log it cleanly instead of a bare traceback,
        # same as the plain Exception case.
        if recording_stopped_event.is_set():
            print(f"[watcher] trim failed: {e}", file=sys.stderr)
            events_q.put(("trim_result", time.time(), {"succeeded": False, "trimmed_path": None}))
            return
        print(
            f"[watcher] trim: attempt against the in-progress recording failed ({e}) "
            "— waiting for the recording to finish and trying again",
            file=sys.stderr,
        )
        recording_stopped_event.wait()
        try:
            trimmed_path = attempt(get_final_output_path())
        except (SystemExit, Exception) as e2:
            print(f"[watcher] trim failed: {e2}", file=sys.stderr)
            events_q.put(("trim_result", time.time(), {"succeeded": False, "trimmed_path": None}))
            return

    # Same wording render()'s own trim step prints (see TRIMMED_PATH_RE in
    # gui.py) — one canonical "trim succeeded, here's the path" line for
    # both the offline and live paths, rather than two differently-worded
    # ones, so the GUI can recognize either one the same way.
    print(f"\nTrimmed body clip -> {trimmed_path}")
    events_q.put(("trim_result", time.time(), {"succeeded": True, "trimmed_path": trimmed_path}))


def _stitch_worker(events_q: "queue.Queue", trimmed_path: str, stitch_cfg: dict):
    """Stitches a clip a previous Trim already produced with the
    intro/outro, triggered by watch()'s 'stitch' manual command. Reports
    back over events_q the same way _trim_worker() does."""
    _reset_steps(_phase_worst_case_steps(stitch_cfg.get("fast_copy", False), STITCH_FAST_COPY_STEPS))
    try:
        final_path = stitch(
            stitch_cfg["intro"], trimmed_path, stitch_cfg["outro"],
            output=stitch_cfg.get("output", "final.mp4"),
            transition_duration=stitch_cfg.get("transition_duration", 1.0),
            transition=stitch_cfg.get("transition", "fade"),
            crf=stitch_cfg.get("crf", 23),
            intro_duration=stitch_cfg.get("intro_duration"),
            outro_duration=stitch_cfg.get("outro_duration"),
            fast_copy=stitch_cfg.get("fast_copy", False),
            encoder=stitch_cfg.get("encoder", "nvenc"), encoder_preset=stitch_cfg.get("encoder_preset"),
        )
    except (SystemExit, Exception) as e:
        print(f"[watcher] stitch failed: {e}", file=sys.stderr)
        events_q.put(("stitch_result", time.time(), {"succeeded": False, "final_path": None}))
        return
    print(f"[watcher] stitch: complete -> {final_path}")
    events_q.put(("stitch_result", time.time(), {"succeeded": True, "final_path": final_path}))


def _get_record_dir(obs_req_client) -> str | None:
    """Best-effort: learn OBS's recording directory, needed to find the
    in-progress recording file if Trim is used before recording stops. If
    this fails for any reason (older OBS/obs-websocket, permissions,
    whatever), Trim just won't work until recording actually stops (it'll
    say why when tried) — nothing else depends on this."""
    try:
        resp = obs_req_client.get_record_directory()
        return getattr(resp, "record_directory", None) or None
    except Exception as e:
        print(
            f"[watcher] could not determine the OBS recording directory "
            f"(Trim won't work until recording stops this run): {e!r}",
            file=sys.stderr,
        )
        return None


# --------------------------------------------------------------------------
# Live watch (the state machine tying ProPresenter + OBS together)
# --------------------------------------------------------------------------

def watch(cfg: dict, pp_cfg: dict, debug: bool = False):
    """Connects to ProPresenter+OBS and tracks a live service: recording
    start/stop, and the begin/end moments (via slide detection or manual
    marks). Purely an observer — it never trims or stitches anything
    itself. Instead, it keeps a render-state file (see
    _resolve_state_path()/_write_render_state()) continuously up to date
    with whatever's known so far: created the moment this starts, then
    rewritten every time a mark lands (auto or manual) or the recording
    actually stops. Trim/Stitch are triggered independently, at any time,
    via the 'trim'/'stitch' manual commands (the GUI's Trim/Stitch
    buttons; see _trim_worker()/_stitch_worker()) — Trim works even
    before recording stops, reading the in-progress file.

    Each connection closes as soon as its own job is done rather than
    staying open for the rest of the run: ProPresenter (if connected at
    all) disconnects the moment Trim is actually triggered, since begin/end
    are decided by then; OBS disconnects the moment either Trim resolves
    (succeeded or failed — see the 'trim_result' handling below) or
    recording itself stops, whichever comes first, since neither has
    anything further to report after that. Once Trim has resolved, this
    process itself exits (there's nothing further only a live connection
    can do — Stitch and any retried Trim work fine offline, see render());
    if recording stops before Trim is ever triggered, only OBS disconnects
    at that point — the process stays up so Trim (still a pending action)
    keeps working, now straight off the finalized file with no OBS needed."""
    import obsws_python as obs

    obs_cfg = cfg["obs"]
    trim_cfg = cfg.get("trim", {})
    stitch_cfg = cfg.get("stitch", {})
    state_path = _resolve_state_path(trim_cfg)

    events_q: "queue.Queue" = queue.Queue()

    def on_record_state_changed(data):
        events_q.put(("obs_record_state", time.time(), data))

    print(f"[obs] connecting to {obs_cfg['host']}:{obs_cfg['port']}")
    obs_event_client = obs.EventClient(
        host=obs_cfg["host"], port=obs_cfg["port"], password=obs_cfg.get("password", "")
    )
    obs_event_client.callback.register(on_record_state_changed)

    obs_req_client = obs.ReqClient(
        host=obs_cfg["host"], port=obs_cfg["port"], password=obs_cfg.get("password", "")
    )
    record_status = obs_req_client.get_record_status()
    already_recording = record_status.output_active

    # ProPresenter is optional: Mark Start/Mark End (the 'manual' events
    # below) can drive the whole state machine by hand, so a service can be
    # run with no ProPresenter connection at all, or with a slide config
    # that never matches — only OBS is actually required. Only start the
    # connection thread if a host was actually configured; otherwise there's
    # nothing to connect to, and trying would just reconnect-loop forever
    # against an empty host for no benefit.
    # Captured so a later 'trim' can signal this connection to stop
    # reconnecting once begin/end are decided and it's no longer needed —
    # see the 'trim' manual-command handling below.
    pp_stop_event: threading.Event | None = None
    if pp_cfg.get("host"):
        pp_stop_event = start_propresenter_thread(pp_cfg, events_q)
    else:
        print("[watcher] no ProPresenter host configured — skipping that connection; use manual Mark Start/Mark End instead")
    start_stdin_thread(events_q)

    begin_slide_cfg = pp_cfg.get("begin_slide") or {}
    end_slide_cfg = pp_cfg.get("end_slide") or {}

    t0 = None
    t_begin = None
    t_end = None
    final_output_path: str | None = None
    record_dir = None
    recording_stopped_event = threading.Event()
    trim_thread: threading.Thread | None = None
    stitch_thread: threading.Thread | None = None
    trimmed_path: str | None = None

    def sync_state():
        """Rewrites the render-state file at `state_path` with whatever's
        currently known — see watch()'s own docstring for when this is
        called."""
        recording_path = final_output_path or (find_active_recording_file(record_dir) if record_dir else None)
        _write_render_state(
            state_path, recording_path,
            (t_begin - t0) if (t_begin is not None and t0 is not None) else None,
            (t_end - t0) if (t_end is not None and t0 is not None) else None,
            trim_cfg, stitch_cfg,
        )

    obs_disconnected = False

    def disconnect_obs():
        """Closes both OBS connections — idempotent (safe to call from more
        than one place, see watch()'s own docstring for the two triggers)
        since there's nothing left for OBS to tell this process once either
        fires."""
        nonlocal obs_disconnected
        if obs_disconnected:
            return
        obs_disconnected = True
        obs_event_client.disconnect()
        obs_req_client.disconnect()
        print("[watcher] disconnected from OBS — nothing further to watch for from it this run")

    if already_recording:
        print(
            "WARNING: OBS is already recording. t0 (recording start) will be "
            "approximated as 'now', not the real start time — start this "
            "watcher before OBS starts recording for an accurate trim.",
            file=sys.stderr,
        )
        t0 = time.time()
        state = "WAIT_BEGIN_SLIDE"
        record_dir = _get_record_dir(obs_req_client)
    else:
        state = "WAIT_RECORD_START"

    print(f"[watcher] state = {state}")
    sync_state()

    try:
        while True:
            kind, ts, payload = events_q.get()

            if debug:
                print(f"[debug] {kind} @ {ts:.3f}: {payload}")

            if kind == "obs_record_state":
                out_state = getattr(payload, "output_state", "")
                if out_state == "OBS_WEBSOCKET_OUTPUT_STARTED" and state == "WAIT_RECORD_START":
                    t0 = ts
                    state = "WAIT_BEGIN_SLIDE"
                    print(f"[watcher] recording started -> state = {state}")
                    record_dir = _get_record_dir(obs_req_client)
                elif out_state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
                    final_output_path = getattr(payload, "output_path", None)
                    print(f"[watcher] recording stopped, file: {final_output_path}")
                    if t_begin is None or t_end is None:
                        print(
                            "WARNING: recording stopped before both the begin and "
                            "end slides were seen. Trim will refuse to run until "
                            "both are marked by hand.",
                            file=sys.stderr,
                        )
                    state = "RECORDING_STOPPED"
                    print(f"[watcher] -> state = {state}")
                    recording_stopped_event.set()
                    sync_state()
                    disconnect_obs()

            elif kind == "pp_raw":
                result = extract_current_slide(payload)
                if result is None:
                    continue
                uid, text = result
                if state == "WAIT_BEGIN_SLIDE" and slide_matches(uid, text, begin_slide_cfg):
                    t_begin = ts
                    state = "WAIT_END_SLIDE"
                    print(f"[watcher] begin slide (uid {uid}) shown (offset {t_begin - t0:.2f}s) -> state = {state}")
                    sync_state()
                elif state == "WAIT_END_SLIDE" and slide_matches(uid, text, end_slide_cfg):
                    t_end = ts
                    state = "WAIT_RECORD_STOP"
                    print(f"[watcher] end slide (uid {uid}) shown (offset {t_end - t0:.2f}s) -> state = {state}")
                    sync_state()

            elif kind == "manual":
                # 'mark_begin'/'mark_end', from an operator or the GUI —
                # the manual equivalent of a slide match, for when
                # something's gone wrong live. Re-sending the same command
                # after it's already applied re-marks that timestamp in
                # place rather than being ignored, so a mistaken mark can
                # be corrected without derailing the state machine; a
                # command that doesn't apply to the current state (e.g.
                # 'mark_end' before 'mark_begin' has ever applied) is
                # ignored rather than erroring — recording hasn't started
                # yet (t0 is None) counts as not applying either. Only
                # meaningful while recording hasn't stopped yet — once it
                # has, t0-relative marking no longer makes sense (there's
                # no more live position to mark "now" against), and the
                # offsets already captured are what Trim will use.
                if payload == "mark_begin" and t0 is not None and state in ("WAIT_BEGIN_SLIDE", "WAIT_END_SLIDE"):
                    t_begin = ts
                    if state == "WAIT_BEGIN_SLIDE":
                        state = "WAIT_END_SLIDE"
                        print(f"[watcher] manually marked begin (offset {t_begin - t0:.2f}s) -> state = {state}")
                    else:
                        print(f"[watcher] manually re-marked begin (offset {t_begin - t0:.2f}s)")
                    sync_state()
                elif payload == "mark_end" and t0 is not None and state in ("WAIT_END_SLIDE", "WAIT_RECORD_STOP"):
                    t_end = ts
                    if state == "WAIT_END_SLIDE":
                        state = "WAIT_RECORD_STOP"
                        print(f"[watcher] manually marked end (offset {t_end - t0:.2f}s) -> state = {state}")
                    else:
                        print(f"[watcher] manually re-marked end (offset {t_end - t0:.2f}s)")
                    sync_state()
                elif payload == "trim":
                    # See _trim_worker()'s docstring. Only meaningful once
                    # both a start and an end are marked (by either
                    # means) — works whether or not recording has stopped
                    # yet. Doesn't touch `state`/t_begin/t_end, and runs in
                    # its own background thread so the real state machine
                    # above keeps running undisturbed while it works.
                    # Always re-triggerable (no "already done" lockout) —
                    # unlike the old one-shot prerender, this is meant to
                    # be clicked freely.
                    if t_begin is None or t_end is None:
                        print("[watcher] ignoring trim — both begin and end need to be marked first", file=sys.stderr)
                    elif trim_thread is not None and trim_thread.is_alive():
                        print("[watcher] ignoring trim — one is already running", file=sys.stderr)
                    else:
                        # Begin/end are decided the moment Trim is actually
                        # triggered — ProPresenter has nothing further to do
                        # this run, whether or not this particular attempt
                        # ends up succeeding.
                        if pp_stop_event is not None:
                            pp_stop_event.set()
                        print("[watcher] trim status = RUNNING")
                        sync_state()
                        trim_thread = threading.Thread(
                            target=_trim_worker,
                            args=(events_q, t_begin - t0, t_end - t0, trim_cfg, record_dir, recording_stopped_event, lambda: final_output_path),
                            daemon=True,
                        )
                        trim_thread.start()
                elif payload == "stitch":
                    # Only meaningful once a trim in this run has actually
                    # succeeded (trimmed_path known).
                    if trimmed_path is None:
                        print("[watcher] ignoring stitch — no trimmed clip yet this run (run Trim first)", file=sys.stderr)
                    elif stitch_thread is not None and stitch_thread.is_alive():
                        print("[watcher] ignoring stitch — one is already running", file=sys.stderr)
                    else:
                        print("[watcher] stitch status = RUNNING")
                        sync_state()
                        stitch_thread = threading.Thread(
                            target=_stitch_worker, args=(events_q, trimmed_path, stitch_cfg), daemon=True,
                        )
                        stitch_thread.start()
                else:
                    print(f"[watcher] ignoring manual command {payload!r} — not applicable in state {state}", file=sys.stderr)

            elif kind == "trim_result":
                if payload["succeeded"]:
                    trimmed_path = payload["trimmed_path"]
                    print("[watcher] trim status = DONE")
                else:
                    print("[watcher] trim status = FAILED")
                # Trim is genuinely resolved now (no retry still pending
                # either way — see _trim_worker()) — nothing live is left
                # to do: OBS has nothing further to report (this also
                # covers succeeding while still recording, which no longer
                # has to wait for a stop event to matter), and Stitch (or a
                # retried Trim, if this one failed) works fine offline from
                # here — see render(). Exit rather than sit idle.
                disconnect_obs()
                sync_state()
                print("[watcher] nothing left to watch for — exiting")
                return

            elif kind == "stitch_result":
                if payload["succeeded"]:
                    print("[watcher] stitch status = DONE")
                else:
                    print("[watcher] stitch status = FAILED")
                sync_state()
    except KeyboardInterrupt:
        sys.exit("\nStopped.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    # Not documented in --help on any of these (add_argument's help=
    # omitted) — an internal switch the GUI passes to every subprocess it
    # starts (see gui.py's ProcessRunner), not something a terminal user
    # would want: it replaces ffmpeg's normal human-readable progress
    # stats with machine-readable ones meant for the GUI's progress bar to
    # parse (see _MACHINE_PROGRESS/_ffmpeg_output_args()), which would
    # just look like log spam in a terminal.
    machine_progress_kwargs = dict(action="store_true")

    p_watch = sub.add_parser("watch", help="Watch ProPresenter+OBS live and keep a render-state file up to date; trim/stitch on request (stdin)")
    p_watch.add_argument("-c", "--config", default="config.json", help="Path to config JSON file")
    p_watch.add_argument("--debug", action="store_true", help="Print every raw message received from ProPresenter and OBS")
    p_watch.add_argument("--machine-progress", **machine_progress_kwargs)

    p_learn = sub.add_parser("learn", help="Print slide uid/text as you step through ProPresenter (no OBS needed)")
    p_learn.add_argument("-c", "--config", default="config.json", help="Path to config JSON file")

    p_render = sub.add_parser("render", help="Re-run just the trim+stitch step from a saved render-state file (no OBS/ProPresenter needed)")
    p_render.add_argument("state_json", help="Path to a render_state_*.json file written by a previous 'watch' run")
    p_render.add_argument("--machine-progress", **machine_progress_kwargs)

    p_stitch = sub.add_parser("stitch", help="Crossfade an intro, main body, and outro clip into one video")
    p_stitch.add_argument("intro", help="Path to the intro clip")
    p_stitch.add_argument("main_clip", help="Path to the main body clip")
    p_stitch.add_argument("outro", help="Path to the outro clip")
    p_stitch.add_argument("-o", "--output", default="output.mp4", help="Output file path")
    p_stitch.add_argument("-d", "--transition-duration", type=float, default=1.0, help="Crossfade duration in seconds (default: 1.0)")
    p_stitch.add_argument("-t", "--transition", default="fade", help="ffmpeg xfade transition name (default: fade, i.e. a simple crossfade)")
    p_stitch.add_argument("--crf", type=int, default=23, help="CRF/CQ quality, lower is better (default: 23)")
    p_stitch.add_argument("--intro-duration", type=float, default=None, help=f"Seconds to show the intro for, if it's a still image (default: {DEFAULT_IMAGE_DURATION})")
    p_stitch.add_argument("--outro-duration", type=float, default=None, help=f"Seconds to show the outro for, if it's a still image (default: {DEFAULT_IMAGE_DURATION})")
    p_stitch.add_argument(
        "--fast-copy", action=argparse.BooleanOptionalAction, default=False,
        help="Re-encode only the two crossfade windows and stream-copy the untouched middle of "
             "the main clip, instead of fully re-encoding everything (default: off — always "
             "verifies its own result before trusting it, and safely falls back to a full "
             "re-encode otherwise, but in practice that check has never actually passed in "
             "testing; see stitch()'s docstring)",
    )
    p_stitch.add_argument(
        "--encoder", default="nvenc", choices=list(ENCODER_PROFILES),
        help="Encoder for the full re-encode (default: nvenc, at cq 23/preset p4 — a good "
             "speed/quality balance per real hardware testing). Falls back to software "
             "automatically, logging why, if it fails to run — see ENCODER_PROFILES for the full "
             "list and important caveats about how well-tested each one actually is.",
    )
    p_stitch.add_argument(
        "--encoder-preset", default=None,
        help="That encoder's own speed/quality preset (names differ per encoder — see "
             "ENCODER_PROFILES; e.g. libx264: veryfast/medium/slow/etc., nvenc: p1-p7). Default: "
             "that encoder's own default preset.",
    )
    p_stitch.add_argument("--machine-progress", **machine_progress_kwargs)

    args = parser.parse_args()

    global _MACHINE_PROGRESS
    _MACHINE_PROGRESS = getattr(args, "machine_progress", False)

    if args.command == "stitch":
        _reset_steps(_phase_worst_case_steps(args.fast_copy, STITCH_FAST_COPY_STEPS))
        stitch(
            args.intro, args.main_clip, args.outro, output=args.output,
            transition_duration=args.transition_duration, transition=args.transition, crf=args.crf,
            intro_duration=args.intro_duration, outro_duration=args.outro_duration,
            fast_copy=args.fast_copy, encoder=args.encoder, encoder_preset=args.encoder_preset,
        )
        return

    if args.command == "render":
        state = json.loads(Path(args.state_json).read_text())
        render(state)
        return

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        sys.exit(f"Config file not found: {cfg_path}. Copy config.example.json and edit it.")
    cfg = json.loads(cfg_path.read_text())
    # ProPresenter is optional for 'watch' (see watch()'s docstring-level
    # comment on start_propresenter_thread) — but 'learn' exists solely to
    # observe ProPresenter's own slide feed, so it still needs a host.
    pp_cfg = cfg.get("propresenter", {})

    if args.command == "learn":
        if not pp_cfg.get("host"):
            sys.exit("Learn mode needs a ProPresenter host configured (Config > ProPresenter > Host) — there's nothing to connect to otherwise.")
        try:
            learn_mode(pp_cfg)
        except KeyboardInterrupt:
            return
        return

    watch(cfg, pp_cfg, debug=args.debug)


if __name__ == "__main__":
    main()

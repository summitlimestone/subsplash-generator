#!/usr/bin/env python3
"""Produce a service recording: watch ProPresenter for a "begin" and "end"
slide, correlate those moments against an OBS recording, trim the
recording down to the body clip, and stitch it together with a provided
intro and outro using a crossfade at each join.

Subcommands:
    watch    Watch ProPresenter+OBS live for the whole service, then trim
             and stitch once OBS stops recording.
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


# --------------------------------------------------------------------------
# Crossfade stitching (intro + body + outro -> one video)
# --------------------------------------------------------------------------

def probe(path: str) -> dict:
    """Return duration, width, height, fps, video codec, and whether an
    audio stream exists for a media file, via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=width,height,avg_frame_rate,codec_type,codec_name",
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
    duration = float(data["format"]["duration"])

    video_stream = next(
        (s for s in data["streams"] if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        sys.exit(f"{path!r} has no video stream.")

    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
    fps = float(Fraction(video_stream["avg_frame_rate"]))

    return {
        "duration": duration,
        "width": video_stream["width"],
        "height": video_stream["height"],
        "fps": fps,
        "has_audio": has_audio,
        # Used by the fast-copy trim/stitch paths to decide whether a
        # stream copy can safely sit next to a fresh libx264 re-encode in
        # the same concatenated output — see find_next_keyframe()/
        # _fast_copy_trim()/_fast_copy_stitch().
        "video_codec": video_stream.get("codec_name"),
    }


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# Used for a still-image intro/outro when no intro_duration/outro_duration is
# configured, so forgetting to set one doesn't hard-fail a stitch.
DEFAULT_IMAGE_DURATION = 5.0


def is_image_file(path: str) -> bool:
    """Whether a path looks like a still image (by extension) rather than a
    video — intro/outro can be either: a still image gets looped into a
    fixed-length clip via ffmpeg's image2 demuxer instead of being probed
    for its own duration (most single images have none to probe)."""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def probe_image_dimensions(path: str) -> dict:
    """Return width/height for a still image via ffprobe. Separate from
    probe() because a lone image file typically has no "duration" entry at
    all in ffprobe's output (probe() would KeyError on it) — a still
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

def find_next_keyframe(path: str, start: float, windows: tuple[float, ...] = (30.0, 300.0)) -> float | None:
    """Return the timestamp (seconds) of the first video keyframe at or
    after `start` in path's video stream, or None if none turns up within
    the largest window tried. Scans via ffprobe's -read_intervals (which
    seeks straight there rather than reading from the start of the file)
    combined with -skip_frame nokey (which only decodes keyframes, not
    everything in between), so this stays fast and cheap no matter how
    long `path` is or how far into it `start` falls — critical, since the
    whole point of the fast-copy path is to avoid touching the rest of the
    file. Tries progressively wider windows in case a single one lands
    entirely between two keyframes (an unusually long GOP, or a search
    window that happens to start right after one)."""
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
            if t >= start - 0.001:
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


def _run_concat(parts: list[Path], output: str, faststart: bool = False):
    """Join `parts` (already-produced media files, in order) into `output`
    via the concat demuxer with -c copy — a fast, no-re-encode join, used
    to reassemble a fast-copy trim/stitch's separately-produced segments.
    The list file is written next to `output` and always cleaned up.

    faststart: pass True when `output` is a final deliverable meant for
    web/streaming playback (mirrors stitch()'s old path, which always sets
    this) rather than an intermediate file like trim_clip()'s output —
    just rewrites where the moov atom sits, no re-encoding involved."""
    list_path = Path(output).with_name(f".{Path(output).stem}.concat.txt")
    list_path.write_text("".join(f"file {_concat_list_entry(p)}\n" for p in parts))
    try:
        cmd = ["ffmpeg", "-y", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy"]
        if faststart:
            cmd += ["-movflags", "+faststart"]
        cmd.append(output)
        print("Running:", " ".join(cmd))
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
    finally:
        list_path.unlink(missing_ok=True)


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
    video_encode_args: list[str], output: str,
) -> list[str]:
    """Build an ffmpeg command crossfading exactly two inputs (each
    described by its own input-side ffmpeg args) into `output` — the
    two-clip version of build_filter_complex()'s chained three-clip
    crossfade, used by _fast_copy_stitch() for the short intro/main-start
    and main-end/outro transition windows."""
    cmd = ["ffmpeg", "-y", "-nostdin", *input_args_a, *input_args_b]

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
        "-c:v", "libx264", *video_encode_args, "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
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

    Only applicable when main_clip is h264 (so the copied middle can sit
    in the same stream as the freshly-encoded h264 pieces either side of
    it) and long enough to actually have an untouched middle. Returns
    output on success; returns None (after cleaning up) if the fast path
    isn't usable here, so the caller can fall back to the old single-pass
    crossfade — this always logs why."""
    intro_clip, main_info, outro_clip = clips

    if main_info.get("video_codec") != "h264":
        print(
            f"[stitch] fast copy: main clip is {main_info.get('video_codec')!r}, not h264 — the "
            "re-encoded transition windows (always h264) couldn't be joined with a copied middle "
            "of a different codec, skipping",
            file=sys.stderr,
        )
        return None

    main_duration = main_info["duration"]
    mid_start = find_next_keyframe(main_clip, transition_duration)
    if mid_start is None:
        print(
            f"[stitch] fast copy: couldn't find a keyframe at/after {transition_duration:.3f}s "
            "into the main clip, skipping",
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
    try:
        front_cmd = _build_pair_xfade_cmd(
            _clip_input_args(intro, is_image_flags[0], intro_clip["duration"]), intro_clip["has_audio"], intro_clip["duration"],
            ["-t", f"{mid_start:.3f}", "-i", main_clip], main_info["has_audio"], mid_start,
            width, height, fps, transition, transition_duration, video_encode_args, str(front_path),
        )
        print(f"[stitch] fast copy: encoding the intro crossfade (intro + main's first {mid_start:.3f}s)")
        print("Running:", " ".join(front_cmd))
        subprocess.run(front_cmd, check=True, stdin=subprocess.DEVNULL)

        middle_duration = max(0.0, mid_end - mid_start - margin)
        middle_cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-ss", f"{mid_start:.3f}", "-i", main_clip, "-t", f"{middle_duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(middle_path),
        ]
        print(
            f"[stitch] fast copy: stream-copying the untouched middle of main "
            f"({mid_start:.3f}s -> {mid_end:.3f}s, no re-encoding)"
        )
        print("Running:", " ".join(middle_cmd))
        subprocess.run(middle_cmd, check=True, stdin=subprocess.DEVNULL)

        tail_duration = main_duration - mid_end
        tail_cmd = _build_pair_xfade_cmd(
            ["-ss", f"{mid_end:.3f}", "-i", main_clip], main_info["has_audio"], tail_duration,
            _clip_input_args(outro, is_image_flags[2], outro_clip["duration"]), outro_clip["has_audio"], outro_clip["duration"],
            width, height, fps, transition, transition_duration, video_encode_args, str(tail_path),
        )
        print(f"[stitch] fast copy: encoding the outro crossfade (main's last {tail_duration:.3f}s + outro)")
        print("Running:", " ".join(tail_cmd))
        subprocess.run(tail_cmd, check=True, stdin=subprocess.DEVNULL)

        print("[stitch] fast copy: joining the intro crossfade, copied middle, and outro crossfade")
        _run_concat([front_path, middle_path, tail_path], output, faststart=True)
    except subprocess.CalledProcessError as e:
        print(f"[stitch] fast copy step failed ({e}) — falling back to a full re-encode", file=sys.stderr)
        return None
    finally:
        front_path.unlink(missing_ok=True)
        middle_path.unlink(missing_ok=True)
        tail_path.unlink(missing_ok=True)

    front_duration = intro_clip["duration"] + mid_start - transition_duration
    tail_out_duration = tail_duration + outro_clip["duration"] - transition_duration
    print(
        f"\nDone (fast copy: ~{front_duration + tail_out_duration:.1f}s re-encoded across both "
        f"crossfades, {middle_duration:.1f}s of the main clip copied) -> {output}"
    )
    return output


def expand_output_path(path: str) -> str:
    """Expand strftime placeholders (%Y, %m, %d, %H, %M, %S, etc.) in an
    output path's filename with the current date/time, so a filename can
    carry when it was produced — e.g. "final_%Y-%m-%d_%H-%M-%S.mp4". A
    filename with no '%' in it passes through unchanged. Applied wherever
    a path is actually written (stitch's output, trim_clip's dst), so it
    works the same from the CLI, the GUI, or a render-state file.

    Only the filename is expanded, not any directory part of the path —
    both because that's what this is for (dated filenames, not a dated
    folder hierarchy) and because a few strftime directives are locale-
    dependent and can embed a literal "/" of their own (%D and, on some
    platforms, %c/%x all expand to something like "09/01/26"), which
    would otherwise silently turn one filename into unwanted nested
    directories. Any "/" or "\\" a directive still manages to produce
    inside the filename is replaced with "-" rather than left to do that."""
    p = Path(path)
    name = datetime.now().strftime(p.name).replace("/", "-").replace("\\", "-")
    return str(p.with_name(name)) if p.name else path


def stitch(
    intro: str, main_clip: str, outro: str, output: str = "output.mp4",
    transition_duration: float = 1.0, transition: str = "fade", crf: int = 18,
    intro_duration: float | None = None, outro_duration: float | None = None,
    fast_copy: bool = True,
) -> str:
    """Crossfade an intro, main body, and outro clip into one video. intro/
    outro can each be either a video or a still image (jpg/png/etc.) — a
    still image is looped into a fixed-length clip using intro_duration/
    outro_duration (falling back to DEFAULT_IMAGE_DURATION if not given).
    main_clip must be a real video (it's the trimmed recording).
    Returns the resolved output path (after strftime expansion).

    fast_copy (default on): try _fast_copy_stitch() first — only the two
    short crossfade windows get re-encoded, and the untouched middle of
    main_clip (very likely almost all of it) is stream-copied instead of
    being decoded and re-encoded along with everything else. Falls
    straight through to the full single-pass crossfade below (logging
    why) if that's not applicable or safe for this main_clip — an
    h264-only technique, and not worth it for a short main clip."""
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

    cmd = ["ffmpeg", "-y", "-nostdin"]
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

    cmd += [
        "-filter_complex", filter_complex,
        "-map", v_out, "-map", a_out,
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output,
    ]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        sys.exit("ffmpeg failed.")

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
    """Reads manual override commands from stdin, one per line — 'mark_begin'
    / 'mark_end' — and feeds them into the same event queue as ProPresenter/
    OBS events, tagged "manual". Lets an operator (or the GUI, which pipes
    these in over the subprocess's stdin) manually trigger what a slide
    match would normally trigger, for when something's gone wrong live and
    there's no time to fix ProPresenter itself. Works the same typed
    directly into a terminal running 'watch' interactively."""
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
# this script's own stdin (for the manual mark_begin/mark_end/prerender
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


def _fast_copy_trim(src: str, dst: str, start: float, end: float, crf: int) -> str | None:
    """trim_clip()'s fast path: re-encode only the sliver from `start` to
    the nearest keyframe at/after it (needed because a decode has to begin
    on a keyframe), then stream-copy the video from that keyframe through
    to `end` — a stream copy's end doesn't need to land on a keyframe the
    way its start does, only its start, so the entire rest of the clip
    (very likely almost all of it) never gets decoded or re-encoded at
    all. The two pieces are joined with the concat demuxer.

    Returns dst on success. Returns None (after cleaning up any partial
    output) if the fast path isn't usable or safe here, so the caller can
    fall back to trim_clip()'s old, always-correct full re-encode; this
    always logs why."""
    info = probe(src)
    if info.get("video_codec") != "h264":
        print(
            f"[trim] fast copy: source is {info.get('video_codec')!r}, not h264 — the re-encoded "
            "sliver (always h264) couldn't be joined with a copied tail of a different codec, skipping",
            file=sys.stderr,
        )
        return None

    keyframe_time = find_next_keyframe(src, start)
    if keyframe_time is None:
        print(f"[trim] fast copy: couldn't find a keyframe at/after {start:.3f}s, skipping", file=sys.stderr)
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

    # Close enough to already be a keyframe: skip the sliver re-encode
    # entirely and just copy the whole range, no re-encoding at all.
    if keyframe_time - start <= 0.02:
        tail_duration = max(0.0, end - keyframe_time - tail_margin)
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-ss", f"{keyframe_time:.3f}", "-i", src, "-t", f"{tail_duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            dst,
        ]
        print(f"[trim] fast copy: start ({start:.3f}s) is already on a keyframe — copying the whole clip, no re-encoding")
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
            "ffmpeg", "-y", "-nostdin",
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{keyframe_time - start:.3f}",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k",
            str(sliver_path),
        ]
        print(
            f"[trim] fast copy: re-encoding {start:.3f}s -> {keyframe_time:.3f}s "
            f"({keyframe_time - start:.3f}s, up to the nearest keyframe)"
        )
        print("Running:", " ".join(sliver_cmd))
        subprocess.run(sliver_cmd, check=True, stdin=subprocess.DEVNULL)

        tail_duration = max(0.0, end - keyframe_time - tail_margin)
        tail_cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-ss", f"{keyframe_time:.3f}", "-i", src, "-t", f"{tail_duration:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(tail_path),
        ]
        print(
            f"[trim] fast copy: stream-copying video {keyframe_time:.3f}s -> {end:.3f}s "
            f"({tail_duration:.3f}s, no re-encoding)"
        )
        print("Running:", " ".join(tail_cmd))
        subprocess.run(tail_cmd, check=True, stdin=subprocess.DEVNULL)

        print("[trim] fast copy: joining the re-encoded sliver and copied tail")
        _run_concat([sliver_path, tail_path], dst)
    except subprocess.CalledProcessError as e:
        print(f"[trim] fast copy step failed ({e}) — falling back to a full re-encode", file=sys.stderr)
        return None
    finally:
        sliver_path.unlink(missing_ok=True)
        tail_path.unlink(missing_ok=True)

    print(f"\nDone (fast copy: {keyframe_time - start:.3f}s re-encoded, {tail_duration:.3f}s copied) -> {dst}")
    return dst


def trim_clip(src: str, dst: str, start: float, end: float, crf: int = 18, fast_copy: bool = True) -> str:
    """Trim src down to [start, end] and write it to dst. Returns the
    resolved destination path (dst after strftime expansion) — use this,
    not the original dst, for anything downstream that needs to find the
    file that actually got written.

    fast_copy (default on): try _fast_copy_trim() first — re-encode just a
    short sliver up to the nearest keyframe, then stream-copy the rest, so
    a long clip doesn't get fully decoded and re-encoded just to cut its
    ends off. Falls straight through to the full re-encode below (logging
    why) if that's not applicable or safe for this source — an h264-only
    technique, and not worth it for a very short trim range."""
    dst = expand_output_path(dst)

    if fast_copy:
        result = _fast_copy_trim(src, dst, start, end, crf)
        if result is not None:
            return result
        print("[trim] continuing with a full re-encode")

    # Frame-accurate trim: -ss/-to placed after -i forces ffmpeg to decode
    # from the start rather than snapping to the nearest keyframe.
    cmd = [
        "ffmpeg", "-y", "-nostdin",
        "-i", src,
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        dst,
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)
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

    start_offset, end_offset = compute_trim_offsets(
        parse_timestamp(state["raw_begin_offset"]), parse_timestamp(state["raw_end_offset"]),
        trim_cfg.get("pad_start_seconds", 0.0), trim_cfg.get("pad_end_seconds", 0.0),
    )

    trimmed_path = trim_clip(
        state["recording_path"], trim_cfg.get("output", "body_trimmed.mp4"),
        start_offset, end_offset, crf=trim_cfg.get("crf", 18),
        fast_copy=trim_cfg.get("fast_copy", True),
    )
    print(f"\nTrimmed body clip -> {trimmed_path}")

    if stitch_cfg.get("auto"):
        stitch(
            stitch_cfg["intro"], trimmed_path, stitch_cfg["outro"],
            output=stitch_cfg.get("output", "final.mp4"),
            transition_duration=stitch_cfg.get("transition_duration", 1.0),
            transition=stitch_cfg.get("transition", "fade"),
            crf=stitch_cfg.get("crf", 18),
            intro_duration=stitch_cfg.get("intro_duration"),
            outro_duration=stitch_cfg.get("outro_duration"),
            fast_copy=stitch_cfg.get("fast_copy", True),
        )


def _write_render_state(recording_path: str, raw_begin_offset: float, raw_end_offset: float, trim_cfg: dict, stitch_cfg: dict) -> tuple[dict, Path]:
    """Builds the render-state dict and writes it to a timestamped file
    (base name from trim.state_output) — the same self-contained record
    documented in the README, used both by the normal end-of-recording
    path and by prerender, so a render-state file looks identical
    regardless of which one produced it."""
    render_state = {
        "recording_path": recording_path,
        "raw_begin_offset": format_timestamp(raw_begin_offset),
        "raw_end_offset": format_timestamp(raw_end_offset),
        "trim": trim_cfg,
        "stitch": stitch_cfg,
    }
    state_base = Path(trim_cfg.get("state_output", "render_state.json"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    state_path = state_base.with_name(f"{state_base.stem}_{timestamp}{state_base.suffix}")
    state_path.write_text(json.dumps(render_state, indent=2))
    print(f"[watcher] wrote render state -> {state_path}")
    print(f"[watcher] to redo just the trim/stitch later (no OBS/ProPresenter needed): python {Path(__file__).name} render {state_path}")
    return render_state, state_path


# --------------------------------------------------------------------------
# Prerender — trim+stitch early, while OBS is still recording, instead of
# waiting for the recording to actually stop. Only ever triggered manually
# (see watch()'s "manual" event handling for the 'prerender'/'skip_render'
# commands).
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


def prerender_worker(events_q: "queue.Queue", recording_path: str, raw_begin_offset: float, raw_end_offset: float, trim_cfg: dict, stitch_cfg: dict):
    """Runs the real trim+stitch against the recording while OBS is still
    writing to it, reading only the byte range through raw_end_offset — by
    the time this is ever called (only valid once an end has been
    marked), OBS has already moved on to writing the rest of the service
    past that point. This writes the render-state file and the actual
    configured trim/stitch outputs, exactly as if recording had already
    stopped — watch()'s own end-of-recording render is skipped once this
    succeeds (see the "prerender_result" handling below), so there's no
    double work and no chance of the two racing on the same output files.

    Relies on Matroska (MKV) not needing a finalized index to be read,
    unlike MP4's `moov` atom — but a second process reading a file another
    process still holds open for writing isn't universally guaranteed to
    work, and there's a real lag between what's been recorded and what's
    actually been flushed to disk and safe to read (encoder lookahead,
    Matroska's cluster-based writes) — trying again a little later if this
    fails is expected and fine, not a sign it'll never work.

    Reports back over events_q rather than returning anything, since this
    runs in its own background thread — the main watch() loop is what
    updates its own bookkeeping and prints the final status line."""
    try:
        start_offset, end_offset = compute_trim_offsets(
            raw_begin_offset, raw_end_offset,
            trim_cfg.get("pad_start_seconds", 0.0), trim_cfg.get("pad_end_seconds", 0.0),
        )
        _write_render_state(recording_path, raw_begin_offset, raw_end_offset, trim_cfg, stitch_cfg)
        trimmed_path = trim_clip(
            recording_path, trim_cfg.get("output", "body_trimmed.mp4"), start_offset, end_offset,
            crf=trim_cfg.get("crf", 18), fast_copy=trim_cfg.get("fast_copy", True),
        )
        print(f"[watcher] prerender: trimmed -> {trimmed_path}")
        if stitch_cfg.get("auto"):
            final_path = stitch(
                stitch_cfg["intro"], trimmed_path, stitch_cfg["outro"],
                output=stitch_cfg.get("output", "final.mp4"),
                transition_duration=stitch_cfg.get("transition_duration", 1.0),
                transition=stitch_cfg.get("transition", "fade"),
                crf=stitch_cfg.get("crf", 18),
                intro_duration=stitch_cfg.get("intro_duration"),
                outro_duration=stitch_cfg.get("outro_duration"),
                fast_copy=stitch_cfg.get("fast_copy", True),
            )
            print(f"[watcher] prerender complete -> {final_path}")
        else:
            print("[watcher] prerender: trim only (stitch.auto is off)")
        events_q.put(("prerender_result", time.time(), {"succeeded": True, "recording_path": recording_path}))
    except SystemExit as e:
        # trim_clip/stitch/compute_trim_offsets call sys.exit() on error —
        # in this background thread that only ends the thread (Python's
        # threading module doesn't propagate SystemExit to the process),
        # so this is just here to log it cleanly instead of a bare
        # traceback. watch()'s own end-of-recording render still runs
        # normally as a fallback once recording actually stops.
        print(f"[watcher] prerender failed: {e}", file=sys.stderr)
        events_q.put(("prerender_result", time.time(), {"succeeded": False, "recording_path": recording_path}))
    except Exception as e:
        print(f"[watcher] prerender failed: {e!r}", file=sys.stderr)
        events_q.put(("prerender_result", time.time(), {"succeeded": False, "recording_path": recording_path}))


def _get_record_dir(obs_req_client) -> str | None:
    """Best-effort, for prerender only: learn OBS's recording directory. If
    this fails for any reason (older OBS/obs-websocket, permissions,
    whatever), prerender just silently won't be offered for this run —
    nothing else depends on it."""
    try:
        resp = obs_req_client.get_record_directory()
        return getattr(resp, "record_directory", None) or None
    except Exception as e:
        print(
            f"[watcher] could not determine the OBS recording directory "
            f"(prerender won't be available this run): {e!r}",
            file=sys.stderr,
        )
        return None


# --------------------------------------------------------------------------
# Live watch (the state machine tying ProPresenter + OBS together)
# --------------------------------------------------------------------------

def watch(cfg: dict, pp_cfg: dict, debug: bool = False):
    import obsws_python as obs

    obs_cfg = cfg["obs"]
    trim_cfg = cfg.get("trim", {})
    stitch_cfg = cfg.get("stitch", {})

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
    status = obs_req_client.get_record_status()
    already_recording = status.output_active

    # ProPresenter is optional: Mark Start/Mark End (the 'manual' events
    # below) can drive the whole state machine by hand, so a service can be
    # run with no ProPresenter connection at all, or with a slide config
    # that never matches — only OBS is actually required. Only start the
    # connection thread if a host was actually configured; otherwise there's
    # nothing to connect to, and trying would just reconnect-loop forever
    # against an empty host for no benefit.
    if pp_cfg.get("host"):
        start_propresenter_thread(pp_cfg, events_q)
    else:
        print("[watcher] no ProPresenter host configured — skipping that connection; use manual Mark Start/Mark End instead")
    start_stdin_thread(events_q)

    begin_slide_cfg = pp_cfg.get("begin_slide") or {}
    end_slide_cfg = pp_cfg.get("end_slide") or {}

    t0 = None
    t_begin = None
    t_end = None
    output_path = None
    record_dir = None
    prerender_thread: threading.Thread | None = None
    prerendered = False
    prerender_recording_path: str | None = None
    skip_render = False

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

    try:
        while state not in ("TRIM", "ABORT"):
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
                    output_path = getattr(payload, "output_path", None)
                    print(f"[watcher] recording stopped, file: {output_path}")
                    if t_begin is None or t_end is None:
                        print(
                            "WARNING: recording stopped before both the begin and "
                            "end slides were seen. Cannot trim.",
                            file=sys.stderr,
                        )
                        state = "ABORT"
                    else:
                        state = "TRIM"
                        print(f"[watcher] -> state = {state}")

            elif kind == "pp_raw":
                result = extract_current_slide(payload)
                if result is None:
                    continue
                uid, text = result
                if state == "WAIT_BEGIN_SLIDE" and slide_matches(uid, text, begin_slide_cfg):
                    t_begin = ts
                    state = "WAIT_END_SLIDE"
                    print(f"[watcher] begin slide (uid {uid}) shown (offset {t_begin - t0:.2f}s) -> state = {state}")
                elif state == "WAIT_END_SLIDE" and slide_matches(uid, text, end_slide_cfg):
                    t_end = ts
                    state = "WAIT_RECORD_STOP"
                    print(f"[watcher] end slide (uid {uid}) shown (offset {t_end - t0:.2f}s) -> state = {state}")

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
                # yet (t0 is None) counts as not applying either.
                if payload == "mark_begin" and t0 is not None and state in ("WAIT_BEGIN_SLIDE", "WAIT_END_SLIDE"):
                    t_begin = ts
                    if state == "WAIT_BEGIN_SLIDE":
                        state = "WAIT_END_SLIDE"
                        print(f"[watcher] manually marked begin (offset {t_begin - t0:.2f}s) -> state = {state}")
                    else:
                        print(f"[watcher] manually re-marked begin (offset {t_begin - t0:.2f}s)")
                elif payload == "mark_end" and t0 is not None and state in ("WAIT_END_SLIDE", "WAIT_RECORD_STOP"):
                    t_end = ts
                    if state == "WAIT_END_SLIDE":
                        state = "WAIT_RECORD_STOP"
                        print(f"[watcher] manually marked end (offset {t_end - t0:.2f}s) -> state = {state}")
                    else:
                        print(f"[watcher] manually re-marked end (offset {t_end - t0:.2f}s)")
                elif payload == "prerender":
                    # See prerender_worker()'s docstring. Only meaningful
                    # once both a start and an end are marked (by either
                    # means); doesn't change `state` at all, doesn't touch
                    # t_begin/t_end, and runs in its own background thread
                    # so the real state machine above keeps running
                    # undisturbed while it works. If it succeeds, it's
                    # treated as the real render — watch()'s own end-of-
                    # recording render is skipped once recording actually
                    # stops (see the "prerender_result" handling below).
                    if state != "WAIT_RECORD_STOP":
                        print(f"[watcher] ignoring prerender — not applicable in state {state}", file=sys.stderr)
                    elif skip_render:
                        print("[watcher] ignoring prerender — render was already marked to be skipped", file=sys.stderr)
                    elif prerendered:
                        print("[watcher] ignoring prerender — one already completed successfully", file=sys.stderr)
                    elif prerender_thread is not None and prerender_thread.is_alive():
                        print("[watcher] ignoring prerender — one is already running", file=sys.stderr)
                    elif record_dir is None:
                        print(
                            "[watcher] ignoring prerender — couldn't determine the OBS "
                            "recording directory when recording started",
                            file=sys.stderr,
                        )
                    else:
                        candidate = find_active_recording_file(record_dir)
                        if candidate is None:
                            print(
                                f"[watcher] prerender: couldn't identify an in-progress "
                                f"recording file in {record_dir} (no video file there was "
                                "modified recently) — skipping (the real render at end of "
                                "recording is unaffected)",
                                file=sys.stderr,
                            )
                        else:
                            print(f"[watcher] prerender: starting early trim+stitch from {candidate} in the background")
                            print("[watcher] prerender status = RUNNING")
                            prerender_recording_path = candidate
                            prerender_thread = threading.Thread(
                                target=prerender_worker,
                                args=(events_q, candidate, t_begin - t0, t_end - t0, trim_cfg, stitch_cfg),
                                daemon=True,
                            )
                            prerender_thread.start()
                elif payload == "skip_render":
                    # Also only meaningful once an end is marked. A one-
                    # way decision for this run, same as a successful
                    # prerender — there's nothing to "undo" back to once
                    # you've said you'll adjust and render this by hand
                    # later. The render-state file still gets written
                    # normally once recording stops; only the automatic
                    # trim+stitch is skipped.
                    if state != "WAIT_RECORD_STOP":
                        print(f"[watcher] ignoring skip_render — not applicable in state {state}", file=sys.stderr)
                    elif prerendered or (prerender_thread is not None and prerender_thread.is_alive()):
                        print("[watcher] ignoring skip_render — prerender already ran or is running", file=sys.stderr)
                    elif skip_render:
                        print("[watcher] ignoring skip_render — already set", file=sys.stderr)
                    else:
                        skip_render = True
                        print("[watcher] render will be skipped once recording stops (the render-state file will still be written)")
                        print("[watcher] prerender status = SKIPPED")
                else:
                    print(f"[watcher] ignoring manual command {payload!r} — not applicable in state {state}", file=sys.stderr)

            elif kind == "prerender_result":
                if payload["succeeded"]:
                    prerendered = True
                    print("[watcher] prerender status = DONE")
                else:
                    print("[watcher] prerender status = FAILED")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted, exiting without trimming.")

    if state == "ABORT" or output_path is None:
        sys.exit(1)

    if prerender_thread is not None and prerender_thread.is_alive():
        print("[watcher] waiting for the in-progress prerender to finish before deciding on the final render...")
        prerender_thread.join()
        # prerender_thread.join() only waits for the thread; the
        # "prerender_result" event it puts on events_q still needs to be
        # drained and processed so `prerendered` reflects the outcome.
        while True:
            kind, ts, payload = events_q.get()
            if kind == "prerender_result":
                if payload["succeeded"]:
                    prerendered = True
                    print("[watcher] prerender status = DONE")
                else:
                    print("[watcher] prerender status = FAILED")
                break

    if prerendered and prerender_recording_path is not None and Path(prerender_recording_path) == Path(output_path):
        print(f"[watcher] prerender already produced the final result from {output_path} — skipping the normal render")
    elif skip_render:
        _write_render_state(output_path, t_begin - t0, t_end - t0, trim_cfg, stitch_cfg)
        print("[watcher] render skipped (Skip Render was used) — adjust the state file above and render it manually when ready")
    else:
        if prerendered:
            # prerender ran, but against a file that turned out not to be
            # the actual final recording (a heuristic mismatch) — fall
            # back to the normal render rather than trust a result that
            # might be wrong.
            print(
                f"[watcher] prerender used {prerender_recording_path!r} but the final "
                f"recording is {output_path!r} — running the normal render instead",
                file=sys.stderr,
            )
        render_state, _state_path = _write_render_state(output_path, t_begin - t0, t_end - t0, trim_cfg, stitch_cfg)
        render(render_state)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser("watch", help="Watch ProPresenter+OBS live, then trim and stitch once the service ends")
    p_watch.add_argument("-c", "--config", default="config.json", help="Path to config JSON file")
    p_watch.add_argument("--debug", action="store_true", help="Print every raw message received from ProPresenter and OBS")

    p_learn = sub.add_parser("learn", help="Print slide uid/text as you step through ProPresenter (no OBS needed)")
    p_learn.add_argument("-c", "--config", default="config.json", help="Path to config JSON file")

    p_render = sub.add_parser("render", help="Re-run just the trim+stitch step from a saved render-state file (no OBS/ProPresenter needed)")
    p_render.add_argument("state_json", help="Path to a render_state_*.json file written by a previous 'watch' run")

    p_stitch = sub.add_parser("stitch", help="Crossfade an intro, main body, and outro clip into one video")
    p_stitch.add_argument("intro", help="Path to the intro clip")
    p_stitch.add_argument("main_clip", help="Path to the main body clip")
    p_stitch.add_argument("outro", help="Path to the outro clip")
    p_stitch.add_argument("-o", "--output", default="output.mp4", help="Output file path")
    p_stitch.add_argument("-d", "--transition-duration", type=float, default=1.0, help="Crossfade duration in seconds (default: 1.0)")
    p_stitch.add_argument("-t", "--transition", default="fade", help="ffmpeg xfade transition name (default: fade, i.e. a simple crossfade)")
    p_stitch.add_argument("--crf", type=int, default=18, help="x264 CRF quality, lower is better (default: 18)")
    p_stitch.add_argument("--intro-duration", type=float, default=None, help=f"Seconds to show the intro for, if it's a still image (default: {DEFAULT_IMAGE_DURATION})")
    p_stitch.add_argument("--outro-duration", type=float, default=None, help=f"Seconds to show the outro for, if it's a still image (default: {DEFAULT_IMAGE_DURATION})")
    p_stitch.add_argument(
        "--fast-copy", action=argparse.BooleanOptionalAction, default=True,
        help="Re-encode only the two crossfade windows and stream-copy the untouched middle of "
             "the main clip, instead of fully re-encoding everything (default: on; needs an h264 "
             "main clip, falls back to a full re-encode automatically otherwise)",
    )

    args = parser.parse_args()

    if args.command == "stitch":
        stitch(
            args.intro, args.main_clip, args.outro, output=args.output,
            transition_duration=args.transition_duration, transition=args.transition, crf=args.crf,
            intro_duration=args.intro_duration, outro_duration=args.outro_duration,
            fast_copy=args.fast_copy,
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

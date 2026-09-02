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
    """Return duration, width, height, fps, and whether an audio stream
    exists for a media file, via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-show_entries", "stream=width,height,avg_frame_rate,codec_type",
        "-of", "json",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
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
    }


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
) -> str:
    """Crossfade an intro, main body, and outro clip into one video.
    Returns the resolved output path (after strftime expansion)."""
    output = expand_output_path(output)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg and ffprobe must be installed and on PATH.")

    paths = [intro, main_clip, outro]
    for p in paths:
        if not Path(p).is_file():
            sys.exit(f"Input file not found: {p}")

    clips = [probe(p) for p in paths]

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

    cmd = ["ffmpeg", "-y"]
    for p in paths:
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
    result = subprocess.run(cmd)
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

    uri = f"ws://{pp_cfg['host']}:{pp_cfg['port']}/stagedisplay"
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

def trim_clip(src: str, dst: str, start: float, end: float, crf: int = 18) -> str:
    """Frame-accurate trim: -ss/-to placed after -i forces ffmpeg to decode
    from the start rather than snapping to the nearest keyframe. Returns
    the resolved destination path (dst after strftime expansion) — use
    this, not the original dst, for anything downstream that needs to find
    the file that actually got written."""
    dst = expand_output_path(dst)
    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k",
        dst,
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
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
    )
    print(f"\nTrimmed body clip -> {trimmed_path}")

    if stitch_cfg.get("auto"):
        stitch(
            stitch_cfg["intro"], trimmed_path, stitch_cfg["outro"],
            output=stitch_cfg.get("output", "final.mp4"),
            transition_duration=stitch_cfg.get("transition_duration", 1.0),
            transition=stitch_cfg.get("transition", "fade"),
            crf=stitch_cfg.get("crf", 18),
        )


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

    start_propresenter_thread(pp_cfg, events_q)
    start_stdin_thread(events_q)

    begin_slide_cfg = pp_cfg["begin_slide"]
    end_slide_cfg = pp_cfg["end_slide"]

    t0 = None
    t_begin = None
    t_end = None
    output_path = None

    if already_recording:
        print(
            "WARNING: OBS is already recording. t0 (recording start) will be "
            "approximated as 'now', not the real start time — start this "
            "watcher before OBS starts recording for an accurate trim.",
            file=sys.stderr,
        )
        t0 = time.time()
        state = "WAIT_BEGIN_SLIDE"
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
                else:
                    print(f"[watcher] ignoring manual command {payload!r} — not applicable in state {state}", file=sys.stderr)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted, exiting without trimming.")

    if state == "ABORT" or output_path is None:
        sys.exit(1)

    render_state = {
        "recording_path": output_path,
        "raw_begin_offset": format_timestamp(t_begin - t0),
        "raw_end_offset": format_timestamp(t_end - t0),
        "trim": trim_cfg,
        "stitch": stitch_cfg,
    }
    state_base = Path(trim_cfg.get("state_output", "render_state.json"))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    state_path = state_base.with_name(f"{state_base.stem}_{timestamp}{state_base.suffix}")
    state_path.write_text(json.dumps(render_state, indent=2))
    print(f"[watcher] wrote render state -> {state_path}")
    print(f"[watcher] to redo just the trim/stitch later (no OBS/ProPresenter needed): python {Path(__file__).name} render {state_path}")

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

    args = parser.parse_args()

    if args.command == "stitch":
        stitch(
            args.intro, args.main_clip, args.outro, output=args.output,
            transition_duration=args.transition_duration, transition=args.transition, crf=args.crf,
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
    pp_cfg = cfg["propresenter"]

    if args.command == "learn":
        try:
            learn_mode(pp_cfg)
        except KeyboardInterrupt:
            return
        return

    watch(cfg, pp_cfg, debug=args.debug)


if __name__ == "__main__":
    main()

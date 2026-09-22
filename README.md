# sclc-subsplash-generator

Turns a live OBS recording into a finished service video: detects the
sermon's start and end, trims the recording to that range, and
crossfades it with an intro and outro. Start and end can come from
ProPresenter slide detection or be marked by hand.

Everything runs through `service_video.py`, a command line tool with
five subcommands. `gui.py` is an optional desktop GUI over the same
functionality.

## Features

- Live pipeline: watches OBS and (optionally) ProPresenter, marks the
  sermon's start and end automatically, and trims and stitches once
  it's over.
- Offline trim and stitch: rework a recording by hand, including a
  visual trim tool with an embedded video preview.
- Bulk render: trim and stitch many recordings in one pass from an
  editable list, with per entry validation and live status.
- Series Manager: save named intro/outro/transition bundles and pick
  them from a dropdown instead of re-entering paths every run.
- Optional HTTP control API for marking start and end, or checking
  state, from outside the app.

## Quickstart

1. Install [ffmpeg](https://ffmpeg.org/) (`ffmpeg` and `ffprobe` must
   be on your PATH).
2. `pip install -r requirements.txt`
3. In OBS: **Tools > WebSocket Server Settings**, enable it, and note
   the port and password.
4. Run `python gui.py`. It creates a starter `config.json` in the
   config directory for your OS (see "Configuration" below) the first
   time it runs.
5. In Config > OBS, enter the host, port, and password from step 3.
6. On the Series Manager tab, add a series with your intro and outro
   clips.
7. On the Live tab, pick that series, then click Start Watch. Use Mark
   Sermon Start and Mark Sermon End to mark the recording by hand, or
   configure ProPresenter in Config > ProPresenter to do it
   automatically.

For command line only use, see "Command line" below instead of steps
4 to 7.

## Requirements

- `ffmpeg` and `ffprobe` on PATH.
- Python 3.10+.
- `pip install -r requirements.txt` for `watch` and `learn`. `stitch`
  and `render` only need ffmpeg. `fastapi` and `uvicorn` are only
  needed for the GUI's optional control API.
- For the GUI: a Tk enabled Python (`sudo pacman -S tk` or
  `sudo apt install python3-tk` on Linux; bundled on Windows and Mac).
- `ffplay` is optional, for audio in the Offline tab's visual trim
  preview. Video plays without it.

## GUI

```
python gui.py
```

The console pane at the bottom mirrors everything the underlying
`service_video.py` process prints, plus a progress bar for the current
ffmpeg step.

### Live tab

Pick a series, set an output path, and click Start Watch. Mark Sermon
Start and Mark Sermon End mark the recording by hand, whether or not
ProPresenter is connected. Trim becomes available once an end is
marked, and works even before the recording stops. Once Trim finishes,
Watch disconnects and Stitch becomes available.

"Mini controls" opens a small window with just these buttons and the
current status, for keeping the workflow visible without the full main
window.

### Offline tab

Trim and stitch a recording by hand, or load an existing render state
file to redo one. Sermon start and end fields drive Trim; "Trim
visually" sets them by dragging a filmstrip instead of typing
timestamps, with an embedded preview and playback. "Export to JSON"
writes the current fields out as a render state file. "Advanced"
holds CRF, the Subsplash preset, fast copy, audio normalization, and
encoder settings.

### Bulk Render tab

An editable list of render state entries, trimmed and stitched in one
pass. Import and export the list as JSON; entries only need to specify
the fields they care about, the rest fill in from the loaded config.
Add, edit, delete, and reorder entries directly in the list, or bulk
edit several selected entries at once. Each entry is validated before
any entry starts, and the Status column shows live, color coded
progress per entry.

### Series Manager tab

Named intro/outro/transition bundles, selectable from the Live and
Offline tabs instead of typing paths every run. Both series dropdowns
are type ahead: typing filters the list by fuzzy match. A series can
be hidden to keep it out of those dropdowns without deleting it.

### Config window

General, API, ProPresenter, OBS, and Render settings. OK saves and
closes the window; Apply saves without closing; Cancel (or closing the
window) discards unsaved changes, confirming first if there are any.

### Control API

An optional HTTP API for marking sermon start and end, or checking
state, from outside the app. Enable it in Config > API. It listens on
`api.host`/`api.port` (default `127.0.0.1:8765`) whenever the checkbox
is on, independent of whether a watch session is active.

| Method | Path | Does |
|---|---|---|
| `POST` | `/mark/start` | Mark sermon start |
| `POST` | `/mark/end` | Mark sermon end |
| `GET` | `/state` | Current state, recording/mark flags, trim/stitch status, render state path |

Requests return 200 once applied, or 409 if the action doesn't apply
right now (for example marking end before start). Interactive docs are
served at `/swagger`. Every request needs HTTP Basic Auth with
`api.password` as the password (any username works); leave it blank to
run with no authentication.

## Command line

### `stitch`: crossfade three clips into one video

```
python service_video.py stitch intro.mp4 main.mp4 outro.mp4 -o final.mp4
```

| Flag | Default | Meaning |
|---|---|---|
| `-o`, `--output` | `output.mp4` | Output file path |
| `-d`, `--transition-duration` | `1.0` | Crossfade length in seconds |
| `-t`, `--transition` | `fade` | Any ffmpeg `xfade` transition name |
| `--crf` | `23` | CRF/CQ quality (lower is better) |
| `--intro-duration`/`--outro-duration` | `5.0` | Shown length, only if that clip is a still image |
| `--fast-copy`/`--no-fast-copy` | off | Skip re-encoding untouched footage |
| `--encoder` | `nvenc` | `software`, `nvenc`, `qsv`, `amf`, or `videotoolbox` |
| `--encoder-preset` | encoder's own default | That encoder's speed/quality preset |
| `--subsplash-preset` | off | Match Subsplash's recommended settings instead of `--crf` |

`intro`/`outro` can each be a video or a still image (jpg, png, bmp,
tif, webp); `main` must be a video. Output paths accept strftime
placeholders anywhere in the path, including directories, for example
`recordings/%Y-%m-%d/final_%H-%M-%S.mp4`. Any directory that doesn't
exist yet is created automatically.

**Fast copy** re-encodes only the two crossfade windows and stream
copies the untouched middle, which is much faster on a long clip. Off
by default for `stitch` (the re-encoded crossfade windows rarely end
up compatible enough with the untouched middle to stream copy, so it
adds time before falling back to a full re-encode). On by default for
`trim`, where that problem doesn't apply. The result is always verified
before it's trusted either way.

**Encoder**: `nvenc` at CRF 23 is the default, and falls back to
`software` (libx264) automatically if the hardware encoder fails to
run. A hardware encoder's CRF/CQ scale isn't quite the same as
software's; treat the number as a starting point and adjust by eye.

**Subsplash preset** matches Subsplash's recommended On-Demand 1080p
settings instead of `--crf` and the output's own resolution and
framerate: profile High, level 4.0, `keyint=60`, 1920x1080 at 30fps
(letterboxed or pillarboxed and upscaled as needed), roughly 2400kbps
video, AAC 160kbps audio, no `+faststart`. `--encoder` still picks the
encode backend; this only changes the quality control flags and
picture format. Ignored by `--fast-copy`, which falls back to a full
re-encode if both are set.

### `watch`: the live pipeline

```
python service_video.py watch [--debug]
```

Use `-c`/`--config` to point at a config file other than the default
location (see "Configuration" below).

Needs OBS (obs-websocket v5, built into OBS 28+) to know when
recording starts and stops. ProPresenter's Network API is optional;
leave `propresenter.host` blank to run on manual marking alone.

State machine: wait for recording to start, wait for the begin slide,
wait for the end slide, wait for recording to stop, then keep running
until Trim resolves. `watch` never trims or stitches on its own; Trim
is the only way that happens.

Type `mark_begin`/`mark_end` into `watch`'s stdin (or use the GUI's
Mark Sermon Start/End buttons) to mark those moments by hand. Once an
end is marked, send `trim` to trim right away, even before recording
stops. Trim disconnects ProPresenter; once it resolves, `watch`
disconnects OBS and exits. From there, redo a failed trim or run
stitch against the render state file `watch` printed the path to:
`render` re-trims (and stitches, if `stitch.auto` is set); plain
`stitch` crossfades an already trimmed clip directly.

While still connected, send `stitch <series name>` to crossfade the
trimmed clip with that series' intro/outro immediately, without
waiting for `watch` to exit. A bare `stitch` with no name reuses
whatever series is already on record for that render, or fails if
none is.

### `learn`: find your begin/end slide UIDs

```
python service_video.py learn
```

Connects to ProPresenter only. Step through your slides and it prints
each one's UID (and text, if any) as you land on it.

### `render`: redo just the trim and stitch, no live connection needed

Every `watch` run writes a render state JSON file (path configurable
via `trim.state_output`): the recording's path, the raw begin/end
timestamps, the trimmed clip's path once Trim has produced one, and
the trim/stitch settings used. `watch` prints the exact command to
reuse it. Edit the file (most often `pad_start_seconds`/
`pad_end_seconds`) and rerun:

```
python service_video.py render render_state_20260823_133005.json
```

### `bulk-render`: trim and stitch many render state files in one pass

```
python service_video.py bulk-render states.json --mode {trim,stitch,full}
```

`states.json` is a JSON array of render state objects, the same shape
`render` takes one of. `--mode trim` trims every entry and writes each
result back into that entry's `trimmed_path`. `--mode stitch` stitches
every entry from its current `trimmed_path`; an entry with none is
skipped. `--mode full` does both, per entry. Every mode ignores
`stitch.auto` and always does what `--mode` says.

Every entry is validated before any of them starts: that its main clip
exists, its sermon start/end are set and land inside the main clip's
length, its trimmed range is long enough for its series' transition,
its series resolves, and its output paths are usable, whichever of
these the chosen mode needs. If any entry fails validation, the whole
run stops before anything starts, with a summary of which entries and
why. Once a run is underway, a single entry failing (a bad ffmpeg run,
for example) doesn't stop the rest; it's reported and skipped, and the
process exits non-zero at the end with a summary of which entries
failed.

## Configuration

`config.json` and `series.json` live in one config directory per OS,
and console logs in a separate logs directory. Both are created
automatically.

| OS | Config directory | Logs directory |
|---|---|---|
| Windows | `%appdata%\subsplash-generator\` | `%localappdata%\subsplash-generator\` |
| Mac/Linux | `$HOME/.config/subsplash-generator/` | `$HOME/.local/share/subsplash-generator/` |

```jsonc
{
  "api": {                                // GUI only, entirely optional
    "enabled": false,                     // optional, default false
    "host": "127.0.0.1",                  // optional, default shown
    "port": 8765,                         // optional, default shown
    "password": ""                        // optional, default "" (no authentication)
  },
  "propresenter": {                       // entirely optional; leave "host" "" to run on manual marking alone
    "host": "192.168.1.50",
    "port": 1025,
    "password": "",
    "reconnect_interval_seconds": 4,      // optional, default 4
    "begin_slide": { "uid": "..." },      // from `learn`; or {"text": "...", "match_mode": "exact"|"regex", "case_sensitive": false}
    "end_slide": { "uid": "..." }
  },
  "obs": {
    "host": "localhost",
    "port": 4455,
    "password": "your-obs-websocket-password"
  },
  "trim": {
    "output": "body_trimmed.mp4",
    "state_output": "render_state_%Y%m%d_%H%M%S.json", // optional, default shown
    "pad_start_seconds": 0,               // + = later/tighter start, - = earlier/more buffer
    "pad_end_seconds": 0,                 // + = later/more buffer, - = earlier/tighter end
    "crf": 23,
    "fast_copy": true,                    // optional, default true
    "normalize_audio": true,              // optional, default true; see "Normalize audio" below
    "normalize_target_lufs": -16.0,       // optional, default -16.0
    "encoder": "nvenc",                   // optional, default "nvenc"
    "encoder_preset": null                // optional, default null (encoder's own default, p4 for nvenc)
  },
  "stitch": {
    "auto": true,
    "output": "final.mp4",
    "crf": 23,                            // optional, default 23; ignored if subsplash_preset is true
    "subsplash_preset": false,            // optional, default false; see "Subsplash preset" above
    "fast_copy": false,                   // optional, default false; see "Fast copy" above (usually a no-op)
    "encoder": "nvenc",                   // optional, default "nvenc"
    "encoder_preset": null                // optional, default null (encoder's own default, p4 for nvenc)
  }
}
```

`config.json` never carries `series`, `intro`, `outro`,
`intro_duration`, `outro_duration`, `transition`, or
`transition_duration`. A stitch's intro, outro, and transition always
come from a series set up in the Series Manager tab (or `series.json`
directly), referenced only by name in `stitch.series`.

`pad_start_seconds`/`pad_end_seconds` accept fractional and negative
values. `stitch.crf` applies to the final crossfaded output, separately
from `trim.crf` for the trimmed intermediate clip. The GUI exposes one
"Fast copy" checkbox (`trim.fast_copy`) and one "Encoder" dropdown
(sets both `trim.encoder` and `stitch.encoder`).

**Normalize audio** (`trim.normalize_audio`, on by default;
`trim.normalize_target_lufs`, default `-16.0`) loudness normalizes the
trimmed clip's audio via ffmpeg's `loudnorm` filter, since recording
levels can vary from service to service. It applies to the trimmed
clip only; the intro, outro, and final stitched crossfade are left as
is. `-16` LUFS is a common streaming target; `-23` is the EBU R128
broadcast standard, quieter with more headroom. A failed measurement
skips normalization for that render rather than failing it.

## Tests

```
pip install -r requirements-dev.txt
pytest tests/
```

Covers `service_video.py`'s ffmpeg-facing logic and the GUI, including
the Offline tab's visual trim window. Generates its own small
synthetic test videos with ffmpeg rather than committing binary
fixtures, so `ffmpeg`/`ffprobe` need to be on PATH.

The GUI tests create real Tk windows, so they need a real or virtual
X11 display: `xvfb-run -a pytest tests/` in a headless environment; on
a normal desktop no wrapper is needed.

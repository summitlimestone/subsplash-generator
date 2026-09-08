# sclc-subsplash-generator

Produces a service recording: watches for a "begin" and "end" moment,
correlates those against an OBS recording, trims it down to the body
clip, and stitches it together with an intro and outro (crossfade at each
join). Begin/end can come from ProPresenter slide detection or be marked
by hand. ProPresenter is entirely optional; OBS is the only connection
actually required.

Everything lives in `service_video.py`, with four subcommands. `gui.py`
is an optional desktop GUI over the same four subcommands.

## Requirements

- `ffmpeg` and `ffprobe` on PATH.
- Python 3.10+.
- `pip install -r requirements.txt`: only needed for `watch`/`learn`.
  `stitch` and `render` only need ffmpeg.
- For `gui.py`: a Tk-enabled Python (`sudo pacman -S tk` /
  `sudo apt install python3-tk` on Linux; bundled on Windows/Mac).

## GUI

```
python gui.py
```

Runs the same `service_video.py` CLI as a subprocess per action; the
console pane shows its output (also mirrored to a log file, see Config >
General), with a progress bar tracking ffmpeg's own steps. Creates a
starter `config.json` next to the script on first run if none exists.

**Main window**: Live/Offline tabs plus the console.
- **Live**: intro/outro/output fields, Start Watch, Mark Sermon
  Start/Mark Sermon End (the only way to run without ProPresenter, or to
  override it live), Trim/Stitch once an end is marked (works even
  before recording stops; nothing renders automatically), and the
  render-state file path, shown as soon as Watch starts and kept current
  throughout.
- **Offline**: crossfade an intro/main/outro by hand via separate Trim
  and Stitch buttons, or "Load from JSON" a render-state file (even one
  still in progress) to redo one; this also enables Sermon start/end
  fields so Trim re-trims the raw recording to those exact points before
  a follow-up Stitch. "Export to JSON" builds a render-state file from
  the fields as-is. "Advanced…" holds CRF, Fast copy, Normalize audio
  (and its Target LUFS), and Encoder settings.

**Config window** (the main window's "Config" button): General (console
log path), ProPresenter (connection + slide matching + Learn mode), OBS
(connection), and Render (the trim/stitch defaults the Live tab's
Trim/Stitch buttons use, same fields as Offline's Advanced). Starting
Watch or Learn auto-saves both windows' fields to the config path shown
here first. `watch` never exits on its own now: it keeps tracking the
service (and answering Trim/Stitch clicks) until you click Stop or press
Ctrl+C, the normal way every Watch session ends.

## Subcommands

### `stitch`: crossfade three clips into one video

Standalone; no config file or live connection needed.

```
python service_video.py stitch intro.mp4 main.mp4 outro.mp4 -o final.mp4
```

| Flag | Default | Meaning |
|---|---|---|
| `-o`, `--output` | `output.mp4` | Output file path |
| `-d`, `--transition-duration` | `1.0` | Crossfade length in seconds |
| `-t`, `--transition` | `fade` | Any ffmpeg `xfade` transition name |
| `--crf` | `23` | CRF/CQ quality (lower = better) |
| `--intro-duration`/`--outro-duration` | `5.0` | Shown length, only if that clip is a still image |
| `--fast-copy`/`--no-fast-copy` | off | Skip re-encoding untouched footage (see below) |
| `--encoder` | `nvenc` | `software`, `nvenc`, `qsv`, `amf`, or `videotoolbox` |
| `--encoder-preset` | encoder's own default | That encoder's speed/quality preset |

`intro`/`outro` can each be a video or a still image (jpg/png/bmp/tif/
webp); `main` must be a video. Output paths (here and in
`trim.output`/`stitch.output`) accept strftime placeholders in the
filename, e.g. `final_%Y-%m-%d_%H-%M-%S.mp4`.

**Fast copy**: re-encodes only the two crossfade windows and stream-
copies the untouched middle instead of re-encoding everything, which is
much faster on a long clip. Off by default for `stitch`: testing found the
re-encoded crossfade windows never end up compatible enough with the
untouched middle to actually stream-copy, so it just adds time before
falling back to a full re-encode anyway. `trim`'s fast copy has no such
problem and defaults on. Always verifies its own result before trusting
it either way, so it's never unsafe to leave on.

**Encoder**: `nvenc` at CRF 23 is the default (benchmarked as a good
speed/quality balance); falls back to `software` (libx264) automatically,
logging why, if the hardware encoder fails to run. A hardware encoder's
CRF/CQ scale isn't quite the same as software's; treat the number as a
starting point and adjust by eye against your own footage.

### `watch`: the live pipeline

```
python service_video.py watch -c config.json [--debug]
```

Needs OBS (obs-websocket v5, built into OBS 28+) to know when recording
starts/stops. ProPresenter's legacy stage-display API is optional; leave
`propresenter.host` blank to run on manual marking alone.

State machine: wait for recording to start → wait for begin slide → wait
for end slide → wait for recording to stop → keep running. `watch` never
trims or stitches on its own; Trim/Stitch (below) are the only way either
happens.

**Manual marking**: type `mark_begin`/`mark_end` into `watch`'s stdin (or
use the GUI's Mark Sermon Start/End buttons) to mark those moments by
hand: the only way when ProPresenter isn't configured, and an override
if something goes wrong with it live.

**Trim/Stitch**: once an end is marked, send `trim` (the GUI's "Trim"
button) to trim right then, even before recording stops (it reads the
in-progress recording file; if that fails, it waits for recording to
finish and retries once more). Send `stitch` once a trim has succeeded to
crossfade it with the intro/outro. Neither is automatic, and both stay
re-clickable for as long as `watch` keeps running.

### `learn`: find your begin/end slide UIDs

```
python service_video.py learn -c config.json
```

Connects to ProPresenter only. Step through your slides and it prints
each one's UID (and text, if any) as you land on it.

### `render`: redo just the trim+stitch, no live connection needed

Every `watch` run writes a render-state JSON file (path configurable via
`trim.state_output`): the recording's path, the raw begin/end timestamps,
and the `trim`/`stitch` settings used. Created the moment `watch` starts
and kept up to date as marks land and recording stops, rather than only
written once at the end; `render` needs it complete (not still `null`) to
run. `watch` prints the exact command to reuse it. Edit the file (most
often `pad_start_seconds`/`pad_end_seconds`) and rerun:

```
python service_video.py render render_state_20260823_133005.json
```

## Setup

1. `pip install -r requirements.txt`
2. In OBS: **Tools → WebSocket Server Settings**, enable it, note the port
   (default `4455`) and password.
3. *(Optional; skip to step 6 for manual marking only.)* In
   ProPresenter: **Preferences → Network**, enable the network API, note
   the port (and password, if set).
4. Copy `config.example.json` → `config.json` and fill in
   `propresenter`/`obs` host, port, password (leave `propresenter.host`
   blank to skip it). Skip this if using `gui.py`, which creates a starter
   config on first run.
5. *(Optional, requires step 3)* Find your begin/end slide UIDs:
   ```
   python service_video.py learn -c config.json
   ```
   Copy the two UIDs into `begin_slide.uid` / `end_slide.uid`.
6. Fill in `stitch.intro` / `stitch.outro` with your intro/outro paths.
7. Before a real service, dry-run `watch -c config.json --debug` and
   confirm begin/end are detected correctly (or that manual marking
   works, if not using ProPresenter).

## Config reference (`config.json`)

```jsonc
{
  "general": {                            // GUI-only
    "log_path": "console_%Y%m%d_%H%M%S.log" // optional, default shown; "" turns off file logging
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
    "intro": "intro.mp4",                 // video, or a still image (jpg/png/bmp/tif/tiff/webp)
    "outro": "outro.mp4",                 // same
    "intro_duration": 5.0,                // optional, default 5.0; only used if intro is a still image
    "outro_duration": 5.0,                // optional, default 5.0; only used if outro is a still image
    "output": "final.mp4",
    "transition_duration": 1.0,
    "transition": "fade",                 // optional, default "fade"; any ffmpeg xfade transition name
    "crf": 23,                            // optional, default 23
    "fast_copy": false,                   // optional, default false; see "Fast copy" above (usually a no-op)
    "encoder": "nvenc",                   // optional, default "nvenc"
    "encoder_preset": null                // optional, default null (encoder's own default, p4 for nvenc)
  }
}
```

`pad_start_seconds`/`pad_end_seconds` accept fractional/negative values;
`stitch.transition`/`stitch.crf` apply to the final crossfaded output,
separately from `trim.crf` (the trimmed intermediate clip). The GUI
exposes one "Fast copy" checkbox (`trim.fast_copy`) and one "Encoder"
dropdown (sets both `trim.encoder` and `stitch.encoder` together); see
"Fast copy"/"Encoder" under `stitch` above for what each does.

**Normalize audio** (`trim.normalize_audio`, on by default;
`trim.normalize_target_lufs`, default `-16.0`): loudness-normalizes the
trimmed clip's audio via ffmpeg's `loudnorm` filter, since a live
recording's levels can vary service to service in a way CRF/encoder
choice has no bearing on. Trim-only: intro/outro and the final stitched
crossfade are untouched, on the assumption they're already mixed at their
own intentional level. `-16` LUFS is a common streaming/YouTube target;
`-23` is the EBU R128 broadcast standard, quieter with more headroom. A
failed measurement just skips normalization for that render rather than
failing it outright.

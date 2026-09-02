# sclc-subsplash-generator

Produces a service recording: watches ProPresenter for a "begin" and "end"
slide, correlates those moments against an OBS recording, trims the
recording down to the body clip, and stitches it together with a provided
intro and outro using a crossfade at each join.

Everything lives in one script, `service_video.py`, with four subcommands.
`gui.py` is an optional desktop GUI over the same four subcommands, for
anyone who'd rather not use the terminal.

## Requirements

- `ffmpeg` and `ffprobe` on PATH.
- Python 3.10+.
- `pip install -r requirements.txt` — only needed for `watch`/`learn`
  (`obsws-python`, `websockets`). `stitch` and `render` only need ffmpeg.
- For `gui.py` only: a Tk-enabled Python. Tk ships with the standard
  Windows/Mac Python installers; on Linux it's usually a separate package
  (e.g. `sudo pacman -S tk` or `sudo apt install python3-tk`).

## GUI

```
python gui.py
```

Backed by the exact same `service_video.py` CLI — every action runs it as a
subprocess and streams its output into the main window's console pane;
nothing about the underlying logic differs from using the terminal. On
first run, if there's no `config.json` next to the script yet, it creates
a starter one — real defaults where one genuinely exists (ports, CRF,
transition, reconnect interval), honest empty fields where it can't be
guessed (host, password, slide UIDs, clip paths). Open Config and fill
those in before running Watch or Learn. Two windows:

**Main window** — the day-to-day view, for whoever's running the service
each week. A Live/Offline notebook plus the console:

- **Live** — the watch pipeline. Only the fields that tend to change week
  to week live here: intro clip, outro clip, and output path. Everything
  else (connection details, slide matching, trim padding) comes from
  Config. Start/Stop, a status readout tracking the state machine (waiting
  for recording, waiting for begin/end slide, rendering, done), Mark
  Sermon Start/Mark Sermon End buttons for manually overriding ProPresenter
  slide detection live if something goes wrong (each enabled only when
  it's actually meaningful for the current state), and the render-state
  file path captured once Watch finishes, with a button to jump straight
  to Offline if a redo is needed.
- **Offline** — crossfade an intro, main clip, and outro into the final
  video. Fill in the fields yourself, or click "Load from JSON" to pull
  them out of a `render_state_*.json` file a previous Watch run wrote.
  Doesn't need a config file or any live connection either way. Includes
  Sermon start/Sermon end fields — absolute timestamps into the recording,
  entered/shown as `HH:MM:SS.mmm`, not the offsets from Config's Render
  tab, since there's no slide detection here to offset from — just someone
  looking at the footage and picking exact points. They only take effect right after
  "Load from JSON" (prefilled with the raw slide-detected timestamps, i.e.
  `raw_begin_offset`/`raw_end_offset` plus whatever padding was applied
  live): Main clip gets pointed at the *raw* OBS recording instead of the
  already-trimmed clip, so Run re-trims it to those exact timestamps
  before stitching (the `render` subcommand under the hood, updating that
  same `render_state_*.json` file in place with whatever you changed). If
  you then change Main clip to something else, the timestamps stop
  applying and Run goes back to a plain crossfade (`stitch`) of whatever's
  in the fields, on the assumption that clip is already trimmed.

**Config window** — everything set once and rarely touched again, opened
via the main window's "Config" button. Hidden rather than closed when
you dismiss it, so reopening is instant. The config file path (Load/Save)
lives at the top, then three tabs:

- **ProPresenter** — host/port/password/reconnect interval, begin/end
  slide matching (by UID or by text), and Learn mode: connect and watch
  discovered slide UIDs appear in a table as you step through them in
  ProPresenter; select a row and click "Use as Begin/End Slide" to fill
  them in.
- **OBS** — host/port/password.
- **Render** — the trim + auto-stitch settings a live Watch run uses once
  it finishes (trimmed output path, pad start/end, transition type,
  transition duration, CRF, auto-stitch toggle) — same fields, layout, and
  order as the Offline tab, since these are exactly what it defaults to
  before you override them per run. The Offline tab's manual crossfade
  tool doesn't read these values, though.

Starting Watch or Learn auto-saves whatever's currently in both windows to
the config file path shown in Config, so there's no separate "save before
running" step. Only one operation runs at a time across both windows; the
Stop button (in the main window's console bar) terminates it immediately
(for `watch`, this skips the graceful "interrupted, exiting without
trimming" message the terminal version prints on Ctrl+C — it's an abrupt
kill, not a clean cancel).

## Subcommands

### `stitch` — crossfade three clips into one video

Standalone; no config file, no ProPresenter/OBS.

```
python service_video.py stitch intro.mp4 main.mp4 outro.mp4 -o final.mp4
```

| Flag | Default | Meaning |
|---|---|---|
| `-o`, `--output` | `output.mp4` | Output file path |
| `-d`, `--transition-duration` | `1.0` | Crossfade length in seconds |
| `-t`, `--transition` | `fade` | Any ffmpeg `xfade` transition name |
| `--crf` | `18` | x264 quality (lower = better) |

It normalizes all three clips to the main clip's resolution/framerate
before crossfading (so mismatched intro/outro resolutions are fine), and
backfills a silent audio track for any clip that doesn't have one so the
audio crossfade never breaks.

Output paths (`-o` here, `trim.output`/`stitch.output` in config/render-
state files) accept strftime placeholders in the filename, filled in with
the current date/time when the file is written — e.g.
`final_%Y-%m-%d_%H-%M-%S.mp4` → `final_2026-09-01_14-30-05.mp4`. A
filename with no `%` in it is unaffected. Only the filename itself is
expanded, not any folder in the path — both because that's what this is
for, and because a few strftime codes are locale-dependent and can embed a
literal `/` of their own (`%D`, and on some platforms `%c`/`%x`, all
expand to something like `09/01/26`); any `/` or `\` a code still manages
to produce ends up replaced with `-` rather than turning into an unwanted
extra folder.

### `watch` — the live pipeline

```
python service_video.py watch -c config.json [--debug]
```

Runs on the OBS machine for the whole service. Two connections stay open
the entire time:

- **ProPresenter's legacy "stage display" WebSocket**
  (`ws://host:port/stagedisplay`). ProPresenter also has a newer,
  officially documented API, but this targets the older, better-documented
  one.
- **obs-websocket v5** (built into OBS 28+), via `obsws-python`.

Slides are matched by **UID**, not by displayed text — most slides
(title graphics, bumpers, video backgrounds) have no text layer, so
ProPresenter reports an empty `"txt"` for them regardless of version. The
UID is always present and stable.

State machine: wait for OBS recording to start → wait for the begin slide
→ wait for the end slide → wait for OBS recording to stop (to get the
final file path) → trim → stitch.

The ProPresenter connection drops periodically as a matter of course —
that's normal behavior of the legacy protocol, not a sign of a broken
setup — so it reconnects on a fast, fixed interval (default 4s,
configurable) rather than growing backoff, so a live service never has a
widening gap where a slide-change event could be missed.

**Manual override**, for when something's gone wrong live and there's no
time to fix ProPresenter itself — typing `mark_begin` or `mark_end` (each
on its own line) into `watch`'s stdin does exactly what the matching slide
being shown would do. The GUI's Live tab exposes this as "Mark Sermon
Start"/"Mark Sermon End" buttons, but it works the same typed directly
into a terminal running `watch` interactively. Each is only accepted when
it's actually meaningful for the current state — Mark Start needs the
recording already running; Mark End needs a start already marked (by
either means) — and sending the same one again while it's still valid
re-marks it at the new time rather than being ignored, so a mistaken mark
can be corrected. Once an end is marked, start locks — nothing later can
re-open it.

### `learn` — find your begin/end slide UIDs

```
python service_video.py learn -c config.json
```

Connects to ProPresenter only (no OBS needed). Step through your slides in
ProPresenter and it prints each slide's UID (and text, if any) as you land
on it — no need to hand-parse `--debug` output.

### `render` — redo just the trim+stitch, no live connection needed

Every `watch` run writes a timestamped `render_state_<timestamp>.json`
(base name configurable via `trim.state_output`) right before trimming.
It's fully self-contained: the OBS recording's path, the raw begin/end
timestamps (`raw_begin_offset`/`raw_end_offset`, as `HH:MM:SS.mmm` —
plain numbers of seconds still work too, for older files or hand-editing),
and the `trim`/`stitch` settings used — no ProPresenter/OBS credentials in
it. `watch` prints the exact command to reuse it.

If the timing was off or a render step failed, edit that file (most often
`pad_start_seconds`/`pad_end_seconds`) and rerun:

```
python service_video.py render render_state_20260823_133005.json
```

This only re-runs the trim + stitch step — no network connection to
ProPresenter or OBS is made.

## Setup

1. `pip install -r requirements.txt`
2. In ProPresenter: **Preferences → Network**, enable the network API,
   note the port (and password, if set).
3. In OBS: **Tools → WebSocket Server Settings**, enable it, note the port
   (default `4455`) and password.
4. Copy `config.example.json` → `config.json` and fill in your
   `propresenter`/`obs` host, port, and password. (Skip this if you're
   using `gui.py` — it creates a starter `config.json` with sensible
   defaults the first time it runs and finds none.)
5. Find your begin/end slide UIDs:
   ```
   python service_video.py learn -c config.json
   ```
   Step through your slides in ProPresenter — copy the two UIDs you need
   into `begin_slide.uid` / `end_slide.uid` in the config.
6. Fill in `stitch.intro` / `stitch.outro` with your intro/outro clip
   paths.
7. Before a real service, do a dry run with `watch -c config.json --debug`
   and confirm the begin/end slides are detected correctly.

## Config reference (`config.json`)

```jsonc
{
  "propresenter": {
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
    "state_output": "render_state.json",  // optional, base name for the timestamped state file
    "pad_start_seconds": 0,               // + = later/tighter start, - = earlier/more buffer
    "pad_end_seconds": 0,                 // + = later/more buffer, - = earlier/tighter end
    "crf": 18
  },
  "stitch": {
    "auto": true,
    "intro": "intro.mp4",
    "outro": "outro.mp4",
    "output": "final.mp4",
    "transition_duration": 1.0,
    "transition": "fade",                 // optional, default "fade" — any ffmpeg xfade transition name
    "crf": 18                             // optional, default 18 — quality of the final stitched output
  }
}
```

`pad_start_seconds`/`pad_end_seconds` accept fractional seconds and
negative values. Both use the same sign convention: positive pushes that
cut point forward (later) in time, negative pushes it back (earlier).

`stitch.transition` and `stitch.crf` apply to the *final* crossfaded
output produced by `render`'s auto-stitch step, separately from
`trim.crf` (which only affects the trimmed intermediate clip). Both are
optional and default to what they've always defaulted to, so existing
config/render-state files don't need updating.

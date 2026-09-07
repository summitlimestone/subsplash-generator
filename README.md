# sclc-subsplash-generator

Produces a service recording: watches for a "begin" and "end" moment,
correlates those moments against an OBS recording, trims the recording
down to the body clip, and stitches it together with a provided intro and
outro using a crossfade at each join. The begin/end moments can come from
ProPresenter slide detection, or be marked by hand (the GUI's Mark Sermon
Start/Mark Sermon End buttons, or `mark_begin`/`mark_end` typed into a
terminal `watch` run) — ProPresenter is entirely optional; OBS is the only
connection actually required.

Everything lives in one script, `service_video.py`, with four subcommands.
`gui.py` is an optional desktop GUI over the same four subcommands, for
anyone who'd rather not use the terminal.

## Requirements

- `ffmpeg` and `ffprobe` on PATH.
- Python 3.10+.
- `pip install -r requirements.txt` — only needed for `watch`/`learn`
  (`obsws-python`, `websockets`). `stitch` and `render` only need ffmpeg.
  `fastapi`/`uvicorn` (also in there) are only needed if you turn on the
  optional control API (`api.enabled` — see "Control API" below); `watch`
  runs fine without them installed as long as it stays off.
- For `gui.py` only: a Tk-enabled Python. Tk ships with the standard
  Windows/Mac Python installers; on Linux it's usually a separate package
  (e.g. `sudo pacman -S tk` or `sudo apt install python3-tk`).

## GUI

```
python gui.py
```

Backed by the exact same `service_video.py` CLI — every action runs it as a
subprocess and streams its output into the main window's console pane,
mirroring the same lines to a log file on disk (Config > General >
Console log path — see below); nothing about the underlying logic differs
from using the terminal, except one thing: ffmpeg's own progress (Watch/
Render/Stitch runs only — Learn
never touches ffmpeg) is shown as a step counter and a bar next to the
console status, rather than the usual wall of `frame=…/time=…/speed=…`
lines repeated every step — those still don't appear in the console pane,
on purpose, since the bar already shows what they'd say. The step counter
runs continuously across a whole render (trim, then stitch, if both run),
not restarting at each — and the total is fixed up front, before any
step runs, by reserving the worst case for each phase (its own fast-copy
step count plus one more in case a fallback re-encode turns out to be
needed) rather than growing mid-render as that becomes known. Whether a
fallback is actually needed can only be discovered by running a phase's
own fast-copy steps and decode-verifying the result, so this is the
earliest point the total can honestly be known; the tradeoff is that a
phase which turns out not to need its reserved fallback step (or skips
fast-copy's multi-step path entirely) simply finishes short of the shown
total instead of exactly reaching it. Next to the
bar, current encode speed (e.g. "15x") while running is replaced with the
total time the run took once it finishes, success or not — so that's
still there to check after the fact even once the live number's gone.
Everything else ffmpeg prints (the command itself, warnings, errors, the
final summary) is unaffected. On
first run, if there's no `config.json` next to the script yet, it creates
a starter one — real defaults where one genuinely exists (ports, CRF,
transition, reconnect interval), honest empty fields where it can't be
guessed (host, password, slide UIDs, clip paths). Open Config and fill in
at least the OBS host before running Watch — ProPresenter is optional (see
below) and only needed if you want the begin/end slides detected
automatically instead of marking them by hand. Two windows:

**Main window** — the day-to-day view, for whoever's running the service
each week. A Live/Offline notebook plus the console:

- **Live** — the watch pipeline. Only the fields that tend to change week
  to week live here: intro clip, outro clip, and output path. Intro/outro
  can each be a video or a still image — a Duration field next to each
  sets how long to show it for if it is one (ignored otherwise). Everything
  else (connection details, slide matching, trim padding) comes from
  Config. Start/Stop, a status readout tracking the state machine (waiting
  for recording, waiting for begin/end slide, recording stopped), Mark
  Sermon Start/Mark Sermon End buttons for marking those moments by hand —
  either as the primary way to run a service with no ProPresenter
  connection at all, or to override ProPresenter slide detection live if
  something goes wrong — and Trim/Stitch buttons (see below) once an end
  is marked. Nothing renders on its own: Watch only tracks the service and
  keeps a render-state file up to date (see below) — trimming and
  stitching only ever happen when you click Trim/Stitch (or run `render`/
  `stitch` yourself against that file later). The render-state file path
  is shown as soon as it exists (right when Watch starts, not just once
  it finishes), with a button to jump straight to Offline.
- **Offline** — crossfade an intro, main clip, and outro into the final
  video, via separate Trim and Stitch buttons. Fill in the fields
  yourself, or click "Load from JSON" to pull them out of a
  `render_state_*.json` file a Watch run wrote — even one still in
  progress, read straight off disk. Doesn't need a config file or any
  live connection either way. Includes Sermon start/Sermon end fields —
  absolute timestamps into the recording, entered/shown as
  `HH:MM:SS.mmm`, not the offsets from Config's Render tab, since there's
  no slide detection here to offset from — just someone looking at the
  footage and picking exact points. They only take effect right after
  "Load from JSON" (prefilled with the raw slide-detected timestamps,
  i.e. `raw_begin_offset`/`raw_end_offset` plus whatever padding was
  applied live), and only for Trim: Main clip gets pointed at the *raw*
  OBS recording instead of the already-trimmed clip, so Trim re-trims it
  to those exact timestamps (the `render` subcommand under the hood,
  writing to a throwaway temp file rather than overwriting your saved
  render-state) and then points Main clip at the result, ready for a
  follow-up Stitch click. If Main clip isn't the raw recording a loaded
  file named, Trim refuses (with a clear message) rather than doing
  nothing meaningful — Stitch works either way, crossfading whatever's
  currently in Intro/Main clip/Outro on the assumption Main clip is
  already trimmed.
  "Export to JSON" is the reverse of "Load from JSON": it writes a
  `render_state_*.json` file — the same format Watch itself writes — from
  whatever's currently in the fields, treating Main clip as the raw
  recording and Sermon start/Sermon end as the exact cut points, whether or
  not that JSON was ever loaded from a real Watch run. Useful for building
  a render-state file by hand (e.g. from a recording that was never run
  through Watch/ProPresenter at all) to hand off or run later with
  `render`, rather than only ever being able to redo one Watch already
  produced. "Advanced…" opens a small window (hidden rather than closed
  when dismissed, like Config) holding CRF, Fast copy, Encoder, and
  Encoder preset — tuning knobs set once and rarely touched, split out
  from the fields above that actually change per run.

**Config window** — everything set once and rarely touched again, opened
via the main window's "Config" button. Hidden rather than closed when
you dismiss it, so reopening is instant. The config file path (Load/Save)
lives at the top, then five tabs:

- **General** — currently just Console log path: where the console pane's
  own output is mirrored to disk, in addition to what's shown on screen.
  Supports the same strftime placeholders as any other output path here
  (e.g. `console_%Y%m%d_%H%M%S.log`, the default) — unlike those, though,
  the timestamp is only filled in once, the first time the app opens the
  file (on startup, and again if you change the path and Save/Load), not
  re-expanded per line, so one whole session's worth of console output —
  across as many Watch/Render/Stitch/Learn runs as you do in it — lands in
  a single file rather than being split one-per-run. Leave the field
  blank to turn file logging off entirely; the console pane itself is
  unaffected either way.
- **API** — the optional control API (see "Control API" below): an
  Enabled checkbox (off by default), Host/Port, and Password (HTTP Basic
  Auth, checked on every request — leave blank to run with no
  authentication at all).
- **ProPresenter** — entirely optional (leave it blank to run on Mark
  Sermon Start/Mark Sermon End alone): host/port/password/reconnect
  interval, begin/end slide matching (by UID or by text), and Learn mode:
  connect and watch discovered slide UIDs appear in a table as you step
  through them in ProPresenter; select a row and click "Use as Begin/End
  Slide" to fill them in.
- **OBS** — host/port/password.
- **Render** — the trim/stitch settings the Live tab's Trim/Stitch
  buttons use (trimmed output path, pad start/end, transition type,
  transition duration, CRF, the fast copy toggle) — same fields, layout,
  and order as the Offline tab, since these are exactly what it defaults
  to before you override them per run. The auto-stitch toggle here
  (`stitch.auto`) only matters for `render` run by hand later against a
  saved render-state file (or a script that calls it) — the Live tab's
  Trim and Stitch are always two separate clicks regardless of it. The
  Offline tab's manual crossfade tool doesn't read any of these values,
  though.

Starting Watch or Learn auto-saves whatever's currently in both windows to
the config file path shown in Config, so there's no separate "save before
running" step. Only one operation runs at a time across both windows; the
Stop button (in the main window's console bar) terminates it immediately.
`watch` never exits on its own any more — it just keeps tracking the
service (and answering Trim/Stitch clicks) until you click Stop, or press
Ctrl+C in a terminal — so this is the normal, expected way every Watch
session ends, not a sign anything went wrong.

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
| `--crf` | `23` | CRF/CQ quality (lower = better) |
| `--intro-duration` | `5.0` | Seconds to show `intro` for, **if it's a still image** |
| `--outro-duration` | `5.0` | Seconds to show `outro` for, **if it's a still image** |
| `--fast-copy`/`--no-fast-copy` | **off** | Attempt to skip re-encoding the untouched middle of `main` (see below — usually a no-op) |
| `--encoder` | `nvenc` | Encoder for the full re-encode — `software`, `nvenc`, `qsv`, `amf`, or `videotoolbox` (see below) |
| `--encoder-preset` | *(encoder's own default)* | That encoder's own speed/quality preset — names vary per encoder (see below) |

It normalizes all three clips to the main clip's resolution/framerate
before crossfading (so mismatched intro/outro resolutions are fine), and
backfills a silent audio track for any clip that doesn't have one so the
audio crossfade never breaks.

**Fast copy** (off by default — read before turning it on): crossfading
normally means decoding and re-encoding the *entire* intro+main+outro
timeline, even though the actual blending only happens in two short
windows (intro into the start of main, the end of main into outro) — for
a long main clip (a whole service), that's most of the cost of a render
for no real benefit, since the middle of it never actually changes. Fast
copy tries to re-encode only those two crossfade windows and stream-copy
the untouched middle of `main` straight through instead. It needs `main`
to be h264 or hevc, needs a safe cut point to copy from (a real keyframe,
not merely something that looks like one — see the next paragraph), and —
critically — **always decodes its own result before trusting it**, falling
straight back to the normal full re-encode (and saying why) if that check
fails, so it's never unsafe to leave on.

The trouble is that check has never actually passed in testing. Each
re-encoded crossfade window goes through a scale/pad/format/xfade filter
chain, and that consistently produces different-enough codec parameters
(SPS/PPS/etc.) from `main`'s own that the two can't share an output file —
confirmed with h264 and hevc, and even with intro/outro cut from `main`
itself so the content genuinely matched. MP4 (and stream concatenation
generally) can only carry one set of those per track, so this isn't a
narrow bug to chase further; it's a real limit of the format for this
specific technique. It's left in, off by default, in case a future
encoder/container combination changes that — `trim`'s fast copy (below)
has no such filter chain and reliably passes the same check, which is why
*that* one defaults on.

Since the full re-encode is the one that actually runs in practice, it —
and every other re-encode this file does, including `trim`'s — uses
x264/x265's `veryfast` preset rather than the slower `medium`: a real,
reliable speed win everywhere it applies, instead of chasing the fast
path that doesn't pan out for `stitch`. `stitch`'s full re-encode always
uses h264 regardless of `main`'s own codec, same as it always has.

Some encoders — many HEVC ones by default — use "open GOP": what looks
like a keyframe to a plain scan is actually a picture that depends on
frames from *before* it, which breaks when copied out on its own. Fast
copy checks for this specifically (a real IDR frame, not just an
intra frame) before ever trusting a cut point, on top of the decode
check above (which only actually decodes a couple of seconds around each
join, not the whole result — a parameter mismatch always shows up on the
first frame that uses it, never gradually, so that's just as reliable a
check and stays cheap no matter how long the untouched, copied middle is).

**Encoder** (`--encoder`, default `nvenc`): which encoder does the full
re-encode. `software` is libx264, same as this project originally used.
`nvenc`, `qsv`, `amf`, and `videotoolbox` target NVIDIA/Intel/AMD/Apple
hardware encoders respectively, translating `--crf` onto each one's own
closest equivalent — worth trying if your machine has one, since a
hardware encoder can be dramatically faster than any CPU preset (confirmed
with NVENC: over 2x faster end to end once the machine's NVIDIA driver was
current enough — an outdated driver is the most likely reason one would
silently fail to run at all, below). If the requested one fails to run at
all (no such hardware, wrong/outdated driver, etc.) it automatically falls
back to `software` and says why, rather than failing the render outright
— safe to leave set even if you're not sure it'll actually be used.

`--encoder-preset` picks that encoder's own speed/quality preset — names
and effect vary per encoder (e.g. libx264: `ultrafast` through `placebo`;
nvenc: `p1` through `p7`, fastest to slowest/best), so it only makes sense
alongside a specific `--encoder`; an unset or invalid preset falls back to
that encoder's own default and says why, the same way an invalid
`--encoder` does. This exists because a hardware encoder's fastest preset
tends to trade away noticeably more compression efficiency for its extra
speed than a CPU encoder's presets do — NVENC's `p1` produced files
around 4x the size of `software` at the same `--crf` number in testing;
`p4` (its own documented default, used here unless overridden) is a much
closer match without losing much of the speed advantage over `software`.
NVENC at `p4`/`--crf 23` is this project's own default (`--encoder`
above) — confirmed by real side-by-side benchmarking to be a good
speed/quality balance for this project's footage.

Beyond the preset, quality/size still won't match `software` at the exact
same `--crf` number — **`-cq`/`-global_quality`/etc. aren't the same scale
as x264's own `-crf`, just numbered similarly.** Treat `--crf` as a
starting point for a hardware encoder, not a guarantee, and adjust by eye
against your own footage. The rest of these mappings weren't verified
against real encoded output beyond that one NVENC confirmation — no other
GPU encoder was available while building this, only confirmed each one's
flags are valid and get as far as an actual attempt to open the hardware.
Worth reporting back if a particular card/driver/encoder needs different
flags than what's here.

`intro` and `outro` can each be either a video or a still image (jpg, png,
bmp, tif/tiff, webp) — `main` (the body clip) must always be a video. An
image is looped into a fixed-length clip using `--intro-duration`/
`--outro-duration` (or the config/render-state equivalents,
`stitch.intro_duration`/`stitch.outro_duration`); leaving it unset falls
back to 5 seconds rather than failing, so forgetting to set one for a
still-image intro/outro doesn't break the render. It's ignored entirely
for a clip that's a video — that clip's own duration is used, same as
always.

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

Runs on the OBS machine for the whole service. **obs-websocket v5** (built
into OBS 28+, via `obsws-python`) stays connected the entire time — this
one's required, since `watch` needs it to know when the recording starts
and stops.

**ProPresenter's legacy "stage display" WebSocket**
(`ws://host:port/stagedisplay`) is optional: leave `propresenter.host`
blank in the config and `watch` skips that connection entirely, relying
solely on the manual marking described below to know when the begin/end
moments happen. Configure it if you'd rather have the begin/end slides
detected automatically instead. ProPresenter also has a newer, officially
documented API, but this targets the older, better-documented one.

Slides are matched by **UID**, not by displayed text — most slides
(title graphics, bumpers, video backgrounds) have no text layer, so
ProPresenter reports an empty `"txt"` for them regardless of version. The
UID is always present and stable.

State machine: wait for OBS recording to start → wait for the begin slide
→ wait for the end slide → wait for OBS recording to stop (to get the
final file path) → keep running. `watch` itself never trims or stitches
anything at any point in this — see "Trim/Stitch" below for the only way
either one actually happens — and there's no state after "recording
stopped" to wait for either: it just keeps tracking things (and keeping
the render-state file current) for as long as it's left running. The
begin/end transitions happen either from a ProPresenter slide match (if
configured) or a manual mark (below); nothing else in the state machine
cares which one drove it.

When ProPresenter is configured, its connection drops periodically as a
matter of course — that's normal behavior of the legacy protocol, not a
sign of a broken setup — so it reconnects on a fast, fixed interval
(default 4s, configurable) rather than growing backoff, so a live service
never has a widening gap where a slide-change event could be missed.

**Manual marking** — typing `mark_begin` or `mark_end` (each on its own
line) into `watch`'s stdin does exactly what the matching slide being
shown would do. The GUI's Live tab exposes this as "Mark Sermon
Start"/"Mark Sermon End" buttons, but it works the same typed directly
into a terminal running `watch` interactively. This is the only way the
begin/end moments get marked when ProPresenter isn't configured at all,
and doubles as an override for when something's gone wrong live and
there's no time to fix ProPresenter itself. Each is only accepted when
it's actually meaningful for the current state — Mark Start needs the
recording already running; Mark End needs a start already marked (by
either means) — and sending the same one again while it's still valid
re-marks it at the new time rather than being ignored, so a mistaken mark
can be corrected. Once an end is marked, start locks — nothing later can
re-open it.

**Trim/Stitch** — `watch` itself never trims or stitches anything; it only
tracks the service and keeps a render-state file up to date (see below).
Sending `trim` (the GUI's "Trim" button) once an end is marked starts the
real trim right then — reading the recording while OBS is still writing
the rest of the service if it hasn't stopped yet, instead of waiting for
it to. It's treated as a real trim, not a preview: it writes the
configured `trim.output` path for real. Sending `stitch` (the GUI's
"Stitch" button) crossfades that trimmed clip with the intro/outro,
writing `stitch.output` — only available once a trim this run has
actually succeeded. Neither is a one-way decision: both stay clickable
(and re-clickable) for as long as `watch` keeps running, which is until
you stop it.

Trimming before the recording stops relies on Matroska (MKV) not needing
a finalized index to be read, unlike MP4's `moov` atom — but a second
process reading a file OBS still holds open for writing isn't universally
guaranteed to work, and testing this while building it turned up a real,
expected failure mode worth knowing about: there's a lag between what's
been recorded and what's actually flushed to disk and safe to read
(encoder lookahead, Matroska's cluster-based writes) — a couple of
seconds isn't necessarily enough, even once the on-screen recording time
is well past what you need. If Trim is clicked and that first attempt
fails, it doesn't just give up: it automatically waits for the recording
to actually finish and tries again once more against the finalized file,
logging that it's doing so — only a second failure (after the recording
is genuinely done) is reported as a real failure.

Trimming before recording stops finds the in-progress recording file
itself, since OBS doesn't expose that while still recording (only once it
stops) — it asks OBS for the configured recording directory, then picks
whichever video file there has been modified most recently, on the
assumption that OBS is the only thing actively appending to a file in
that folder. That's why it's important this points at a directory OBS
actually uses for recording and not something shared with other video
files being actively touched by something else.

**Control API** — an optional HTTP API, off by default (`api.enabled`),
for marking sermon start/end and checking the current watch state from
something other than this app's own GUI/terminal — a phone, a separate
control surface, etc. Starts (and stops) with `watch` itself, listening
on `api.host`/`api.port` (default `127.0.0.1:8765`):

| Method | Path | Does |
|---|---|---|
| `POST` | `/mark/start` | Same as Mark Sermon Start/`mark_begin` |
| `POST` | `/mark/end` | Same as Mark Sermon End/`mark_end` |
| `GET` | `/state` | Current state, recording/begin/end-marked flags, Trim/Stitch status, render-state file path |

`POST` requests are fire-and-forget, same as typing `mark_begin`/`mark_end`
into `watch`'s own stdin — they're only actually applied if the state
machine is currently expecting one (marking an end before a begin is
ignored, same as everywhere else this can happen), so check `GET /state`
afterward to see whether it took. Interactive Swagger docs are served at
`/swagger` (and the raw schema at `/openapi.json`).

Requires `fastapi`/`uvicorn` (see Requirements above) — if they're not
installed, `watch` logs why and runs on without the API rather than
failing outright. Secured with HTTP Basic Auth against `api.password`
(any username is accepted; only the password is checked, since this is a
single shared secret, not real user management) — leave it blank to run
with no authentication at all, which is logged loudly every time `watch`
starts that way, since it means anyone who can reach `host:port` can mark
start/end. Every route requires it when a password is set, `/swagger`
included.

### `learn` — find your begin/end slide UIDs

```
python service_video.py learn -c config.json
```

Connects to ProPresenter only (no OBS needed). Step through your slides in
ProPresenter and it prints each slide's UID (and text, if any) as you land
on it — no need to hand-parse `--debug` output.

### `render` — redo just the trim+stitch, no live connection needed

Every `watch` run writes a render-state JSON file (path configurable via
`trim.state_output`, default `render_state_%Y%m%d_%H%M%S.json` — supports
the same strftime placeholders as `trim.output`/`stitch.output`, see
"Output paths" above) — created the moment `watch` starts, then kept up
to date as the service actually happens (every time a begin/end mark
lands, auto or manual, and once recording stops), rather than only once
at the end. It's fully self-contained: the OBS recording's path (`null`
until known), the raw begin/end timestamps (`raw_begin_offset`/
`raw_end_offset`, as `HH:MM:SS.mmm` — plain numbers of seconds still work
too, for older files or hand-editing — `null` until both are marked),
and the `trim`/`stitch` settings used — no ProPresenter/OBS credentials in
it. `watch` prints the exact command to reuse it every time it rewrites
the file.

`render` needs the file to actually be complete (`recording_path`/
`raw_begin_offset`/`raw_end_offset` all filled in, not `null`) — if you
open one mid-service it may not be yet; use Trim from the Live tab
instead, or wait.

If the timing was off or a render step failed, edit that file (most often
`pad_start_seconds`/`pad_end_seconds`) and rerun:

```
python service_video.py render render_state_20260823_133005.json
```

This only re-runs the trim + stitch step — no network connection to
ProPresenter or OBS is made.

## Setup

1. `pip install -r requirements.txt`
2. In OBS: **Tools → WebSocket Server Settings**, enable it, note the port
   (default `4455`) and password. This one's required — `watch` always
   needs OBS to know when the recording starts and stops.
3. *(Optional — skip to step 6 if you'd rather just mark the begin/end
   moments by hand every service via Mark Sermon Start/Mark Sermon End.)*
   In ProPresenter: **Preferences → Network**, enable the network API,
   note the port (and password, if set).
4. Copy `config.example.json` → `config.json` and fill in your
   `propresenter`/`obs` host, port, and password (leave `propresenter.host`
   blank to skip ProPresenter entirely). (Skip this if you're using
   `gui.py` — it creates a starter `config.json` with sensible defaults the
   first time it runs and finds none.)
5. *(Optional, requires step 3)* Find your begin/end slide UIDs:
   ```
   python service_video.py learn -c config.json
   ```
   Step through your slides in ProPresenter — copy the two UIDs you need
   into `begin_slide.uid` / `end_slide.uid` in the config.
6. Fill in `stitch.intro` / `stitch.outro` with your intro/outro clip
   paths — each can be a video or a still image; if it's an image, also
   set `stitch.intro_duration` / `stitch.outro_duration` (in seconds).
7. Before a real service, do a dry run with `watch -c config.json --debug`
   and confirm the begin/end slides are detected correctly (or, with no
   ProPresenter configured, confirm `mark_begin`/`mark_end` — or the GUI's
   Mark Sermon Start/Mark Sermon End buttons — move the state machine
   along as expected).

## Config reference (`config.json`)

```jsonc
{
  "general": {                            // GUI-only (gui.py) — service_video.py itself ignores this section
    "log_path": "console_%Y%m%d_%H%M%S.log" // optional, default shown — same placeholders as "output" fields
                                           // below, but expanded only once per app session, not per line; "" turns
                                           // off file logging (the console pane itself still works either way)
  },
  "api": {                                // entirely optional — see "Control API" above
    "enabled": false,                     // optional, default false
    "host": "127.0.0.1",                  // optional, default shown
    "port": 8765,                         // optional, default shown
    "password": ""                        // optional, default "" (no authentication — see "Control API" above)
  },
  "propresenter": {                       // entirely optional — leave "host" "" (or omit
                                           // this whole section) to run on manual marking alone
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
    "state_output": "render_state_%Y%m%d_%H%M%S.json", // optional, default shown — same placeholders as "output"
    "pad_start_seconds": 0,               // + = later/tighter start, - = earlier/more buffer
    "pad_end_seconds": 0,                 // + = later/more buffer, - = earlier/tighter end
    "crf": 23,
    "fast_copy": true,                    // optional, default true — see "Fast copy" below
    "encoder": "nvenc",                   // optional, default "nvenc" — see "Encoder" below
    "encoder_preset": null                // optional, default null (that encoder's own default preset, p4 for nvenc)
  },
  "stitch": {
    "auto": true,
    "intro": "intro.mp4",                 // video, or a still image (jpg/png/bmp/tif/tiff/webp)
    "outro": "outro.mp4",                 // same
    "intro_duration": 5.0,                // optional, default 5.0 — only used if intro is a still image
    "outro_duration": 5.0,                // optional, default 5.0 — only used if outro is a still image
    "output": "final.mp4",
    "transition_duration": 1.0,
    "transition": "fade",                 // optional, default "fade" — any ffmpeg xfade transition name
    "crf": 23,                            // optional, default 23 — quality of the final stitched output
    "fast_copy": false,                   // optional, default false — see "Fast copy" below (usually a no-op)
    "encoder": "nvenc",                   // optional, default "nvenc" — see "Encoder" below
    "encoder_preset": null                // optional, default null (that encoder's own default preset, p4 for nvenc)
  }
}
```

`pad_start_seconds`/`pad_end_seconds` accept fractional seconds and
negative values. Both use the same sign convention: positive pushes that
cut point forward (later) in time, negative pushes it back (earlier).

`intro_duration`/`outro_duration` are ignored for a video clip (its own
duration is used, same as always) — they only matter when `intro`/`outro`
is a still image, since an image has no duration of its own to read.

`stitch.transition` and `stitch.crf` apply to the *final* crossfaded
output produced by `render`'s auto-stitch step, separately from
`trim.crf` (which only affects the trimmed intermediate clip). Both are
optional and default to what they've always defaulted to, so existing
config/render-state files don't need updating.

**Fast copy** (`trim.fast_copy` — on by default; `stitch.fast_copy` — off
by default): skips re-encoding footage that doesn't actually need it, so
`trim` finishes in a fraction of the time a full re-encode would take.
`trim` re-encodes only a short sliver at the very start of the cut, up to
the nearest *safe* keyframe (a real IDR frame — a cut can only start
decoding there, and some encoders, HEVC ones especially, use "open GOP"
where a plain scan finds intra frames that aren't actually safe to cut
at) and stream-copies everything after it, verifying that result actually
decodes cleanly before trusting it. `stitch` (see the `stitch` subcommand
section above for the full explanation of why) can do the equivalent for
its two crossfade windows, but its own decode-clean check has never
actually passed in testing — so it defaults off, since attempting it
first just adds wasted time ahead of the full re-encode it falls back to
anyway. Both need an h264 or hevc source; `trim`'s fast copy specifically
is reliable enough that turning it off is rarely necessary — it's there
mainly as an escape hatch, or for comparing output quality/timing against
the always-correct full re-encode. The GUI only exposes a single "Fast
copy" checkbox, for `trim.fast_copy` — `stitch.fast_copy` is config/
render-state/CLI (`stitch --fast-copy`) only, given it's never actually
had anything to offer in testing.

**Encoder** (`trim.encoder`/`stitch.encoder`, both `"nvenc"` by
default; `trim.encoder_preset`/`stitch.encoder_preset`, both null/that
encoder's own default by default — `p4` for `nvenc`): which encoder — and
which of that encoder's own speed/quality presets — `trim`'s and
`stitch`'s full re-encodes use. `"nvenc"` at `crf`/`cq` 23 is this
project's own default, confirmed by real side-by-side benchmarking to be
a good speed/quality balance; `"software"` (libx264, what this project
originally used) or another hardware encoder name (`"qsv"`, `"amf"`,
`"videotoolbox"`) both remain available if the machine actually running
this doesn't have an NVIDIA GPU, or you'd rather compare for yourself. See
the `stitch` subcommand's own
"Encoder" paragraph above for the full explanation — the automatic,
logged fallback to `"software"` if the requested hardware (or preset)
doesn't pan out, why the preset matters more for a hardware encoder than
it might seem, and why `--crf`'s number doesn't mean quite the same thing
across encoders — same behavior here. Neither applies to `fast_copy`'s
own re-encoded sliver/crossfade windows, only the full re-encode fallback
each of those already has. The GUI only exposes one "Encoder" dropdown
(no separate preset control yet) that sets both `trim.encoder` and
`stitch.encoder` together.

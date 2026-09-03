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
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
SERVICE_SCRIPT = SCRIPT_DIR / "service_video.py"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"


def default_config() -> dict:
    """A starter config.json: real defaults where one genuinely exists
    (ports, CRF, transition, reconnect interval, auto-stitch), honest empty
    placeholders where it can't be guessed (host, password, slide UIDs,
    clip paths) — not example.json's fake example values, which could be
    mistaken for already being configured."""
    return {
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
            "state_output": "render_state.json",
            "pad_start_seconds": 0.0,
            "pad_end_seconds": 0.0,
            "crf": 18,
        },
        "stitch": {
            "auto": True,
            "intro": "",
            "outro": "",
            "intro_duration": DEFAULT_IMAGE_DURATION,
            "outro_duration": DEFAULT_IMAGE_DURATION,
            "output": "final.mp4",
            "transition_duration": 1.0,
            "transition": "fade",
        },
    }

STATE_RE = re.compile(r"state = (\w+)")
RENDER_STATE_PATH_RE = re.compile(r"wrote render state -> (.+)$")
SLIDE_RE = re.compile(r'^uid: "(.*)"\s+text: (.*)$')
PRERENDER_STATUS_RE = re.compile(r"prerender status = (\w+)")

# service_video.py's internal state machine names, relabeled for display —
# names not listed here (WAIT_RECORD_START etc.) show as-is.
WATCH_STATE_LABELS = {
    "TRIM": "Rendering",
}

JSON_FILETYPES = [("JSON files", "*.json"), ("All files", "*.*")]
VIDEO_FILETYPES = [("Video files", "*.mp4 *.mov *.mkv *.m4v *.avi"), ("All files", "*.*")]
# Intro/outro can be either a video or a still image (see IMAGE_DURATION_HELP) —
# their Browse buttons use this instead of VIDEO_FILETYPES.
INTRO_OUTRO_FILETYPES = [
    ("Video/image files", "*.mp4 *.mov *.mkv *.m4v *.avi *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp"),
    ("All files", "*.*"),
]
DEFAULT_IMAGE_DURATION = 5.0

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


CRF_HELP = (
    "Video quality (x264 CRF). Lower = better quality, larger file; higher = "
    "more compression, smaller file. 18 is visually lossless; 23 is x264's "
    "own default."
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

PRERENDER_HELP = (
    "Starts trim+stitch now, reading the recording while OBS is still "
    "writing it, instead of waiting for the recording to stop. Counts as "
    "the real render — nothing more runs automatically once recording "
    "actually ends. Can fail if clicked too soon after marking the end "
    "(the encoder hasn't flushed that far yet) — safe to just try again."
)

SKIP_RENDER_HELP = (
    "Writes the render-state file as usual when recording stops, but "
    "skips the automatic trim+stitch — for when you already know the "
    "timing will need adjusting by hand afterward. Use the Offline tab or "
    "'render' on that file whenever you're ready."
)

IMAGE_DURATION_HELP = (
    "Only used if the clip above is a still image (jpg/png/etc.) rather "
    f"than a video — how long to show it for. Ignored for a video clip. "
    f"Defaults to {DEFAULT_IMAGE_DURATION}s if left blank."
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
        # Main clip no longer matches — see _run_render().
        self._offline_raw_state: dict | None = None
        # Set once Prerender succeeds or Skip Render is used, so that
        # terminal status ("Done (Prerendered)"/"Done (Skipped Render)")
        # sticks instead of being overwritten by the state-machine
        # transitions (e.g. "-> state = TRIM" when recording stops) or the
        # generic "done" the process exiting would otherwise set — see
        # _handle_watch_line()/_on_process_exit(). Reset at the start of
        # each watch run.
        self._prerender_locked = False
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

        self._build_header()
        self._build_body()

        # Built eagerly (but hidden) so every config field/var exists right
        # away — load_config() below, and autosave-before-run later, both
        # need the full field set regardless of whether the user has ever
        # opened the Config window.
        self.config_window = ConfigWindow(self)

        if not DEFAULT_CONFIG_PATH.is_file():
            DEFAULT_CONFIG_PATH.write_text(json.dumps(default_config(), indent=2))
            self._log(
                f"[gui] no config.json found next to this script — created a "
                f"starter one at {DEFAULT_CONFIG_PATH} with sensible defaults. "
                "Open Config and fill in ProPresenter/OBS host + slide UIDs "
                "before running Watch or Learn."
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

    def _crf_slider(self, parent, row, label, key, col=0, colspan=1, default=18):
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

    # -- Live tab (the watch pipeline; only the fields that change week to
    #    week — everything else lives in the Config window) ----------------

    def _build_live_tab(self):
        _outer, frame = self._make_scrollable_tab(self.mode_notebook, "Live")
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Runs the live pipeline for the whole service: waits for OBS to start "
            "recording, then the begin slide, then the end slide, then OBS to stop — "
            "then trims and stitches automatically. Connection, slide-matching, and "
            "trim settings live in Config.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 10))

        self._labeled_entry(frame, 1, "Intro clip", "stitch_intro")
        self._add_browse(frame, 1, "stitch_intro", filetypes=INTRO_OUTRO_FILETYPES)
        self._labeled_spinbox(
            frame, 1, "Duration (s)", "stitch_intro_duration", from_=0.1, to=120.0,
            default=str(DEFAULT_IMAGE_DURATION), width=6, col=4, help_text=IMAGE_DURATION_HELP,
        )
        self._labeled_entry(frame, 2, "Outro clip", "stitch_outro")
        self._add_browse(frame, 2, "stitch_outro", filetypes=INTRO_OUTRO_FILETYPES)
        self._labeled_spinbox(
            frame, 2, "Duration (s)", "stitch_outro_duration", from_=0.1, to=120.0,
            default=str(DEFAULT_IMAGE_DURATION), width=6, col=4, help_text=IMAGE_DURATION_HELP,
        )
        self._labeled_entry(frame, 3, "Output path", "stitch_output", help_text=TIMESTAMP_HELP)
        self._add_browse(frame, 3, "stitch_output", save=True, filetypes=VIDEO_FILETYPES)

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=4, column=0, columnspan=6, sticky="w", pady=(10, 10))
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
        self.prerender_btn = ttk.Button(
            btn_row, text="Prerender", command=self._prerender, state="disabled",
        )
        self.prerender_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.prerender_btn, PRERENDER_HELP, font=self.ui_font)
        self.skip_render_btn = ttk.Button(
            btn_row, text="Skip Render", command=self._skip_render, state="disabled",
        )
        self.skip_render_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.skip_render_btn, SKIP_RENDER_HELP, font=self.ui_font)
        self.watch_debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn_row, text="Debug", variable=self.watch_debug_var).pack(
            side="left", padx=(10, 0)
        )

        status_frame = ttk.LabelFrame(frame, text="Status", padding=10)
        status_frame.grid(row=5, column=0, columnspan=6, sticky="ew")
        self.watch_state_var = tk.StringVar(value="idle")
        self.watch_status_label = ttk.Label(
            status_frame, textvariable=self.watch_state_var,
            font=(self.ui_font[0], 18, "bold"), foreground=PALETTE["muted"],
        )
        self.watch_status_label.pack(side="left")

        state_path_frame = ttk.Frame(frame, padding=(0, 10, 0, 0))
        state_path_frame.grid(row=6, column=0, columnspan=6, sticky="ew")
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
            text="Crossfade an intro, main body clip, and outro into the final video. "
            "Fill in the fields yourself, or click \"Load from JSON\" to pull them out "
            "of a render_state_*.json file a previous Watch run wrote. Loading from "
            "JSON also enables the sermon start/end timestamps below, which re-trim "
            "the raw recording to those exact points before stitching — otherwise "
            "Main clip is assumed to already be trimmed and the timestamps are "
            "ignored. Doesn't need a config file or any live connection either way.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).grid(row=0, column=0, columnspan=7, sticky="w", pady=(0, 6))

        ttk.Button(frame, text="Load from JSON…", command=self._browse_render_state).grid(
            row=1, column=0, sticky="w", pady=(0, 10)
        )

        self._labeled_entry(frame, 2, "Intro clip", "st_intro", colspan=3)
        self._add_browse(frame, 2, "st_intro", filetypes=INTRO_OUTRO_FILETYPES, col=3)
        self._labeled_spinbox(
            frame, 2, "Duration (s)", "st_intro_duration", from_=0.1, to=120.0,
            default=str(DEFAULT_IMAGE_DURATION), width=6, col=5, help_text=IMAGE_DURATION_HELP,
        )
        self._labeled_entry(frame, 3, "Main clip", "st_main", colspan=3)
        self._add_browse(frame, 3, "st_main", filetypes=VIDEO_FILETYPES, col=3)
        self._labeled_entry(frame, 4, "Outro clip", "st_outro", colspan=3)
        self._add_browse(frame, 4, "st_outro", filetypes=INTRO_OUTRO_FILETYPES, col=3)
        self._labeled_spinbox(
            frame, 4, "Duration (s)", "st_outro_duration", from_=0.1, to=120.0,
            default=str(DEFAULT_IMAGE_DURATION), width=6, col=5, help_text=IMAGE_DURATION_HELP,
        )
        self._labeled_entry(frame, 5, "Output path", "st_output", colspan=3, help_text=TIMESTAMP_HELP)
        self._add_browse(frame, 5, "st_output", save=True, filetypes=VIDEO_FILETYPES, col=3)
        self.vars["st_output"].set("output.mp4")

        self._labeled_entry(frame, 6, "Sermon start", "st_start", width=13, col=0)
        self.vars["st_start"].set("00:00:00.000")
        self._labeled_entry(frame, 6, "Sermon end", "st_end", width=13, col=2, pad_left=16)
        self.vars["st_end"].set("00:00:00.000")

        self._labeled_combobox(
            frame, 7, "Transition type", "st_transition", XFADE_TRANSITIONS, width=12, col=0,
        )
        self.vars["st_transition"].set("fade")
        self._labeled_entry(frame, 7, "Transition duration (s)", "st_duration", width=8, col=2, pad_left=16)
        self.vars["st_duration"].set("1.0")

        self._crf_slider(frame, 8, "CRF (quality)", "st_crf", col=0, colspan=3)

        start_btn = ttk.Button(frame, text="Run Render", style="Accent.TButton", command=self._run_render)
        start_btn.grid(row=9, column=0, sticky="w", pady=(10, 0))
        self._start_buttons.append(start_btn)

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
        # This one field drives both trim.crf and stitch.crf when Run
        # re-trims (see _run_render) — on load, prefer stitch.crf (what
        # actually determines the final video's visible quality) and fall
        # back to trim.crf so a file that only sets one still reflects it.
        crf_val = stitch_cfg.get("crf", trim_cfg.get("crf"))
        if crf_val is not None:
            self.vars["st_crf"].set(max(0, min(51, round(crf_val))))

        has_raw = "recording_path" in state and "raw_begin_offset" in state and "raw_end_offset" in state
        if has_raw:
            # Point Main clip at the raw recording (not the already-trimmed
            # clip) so the timestamp fields below have something meaningful
            # to trim from — see _run_render(). Shown as the actual computed
            # trim points (raw slide-detected offset + any padding that was
            # applied live), not as a raw/pad split — there's no slide
            # detection here, just a person looking at footage and picking
            # exact timestamps.
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
                "trim_output": trim_cfg.get("output", "body_trimmed.mp4"),
                "state_output": trim_cfg.get("state_output", "render_state.json"),
                "state_path": path,
            }
            self._log(f"[gui] loaded render fields from {path} (timestamps active — Run will re-trim the raw recording)")
        else:
            # Older/hand-built state file missing the raw recording info —
            # fall back to the pre-trimmed clip, same as before this file
            # could drive a re-trim at all. Reset the timestamp fields so
            # they don't show stale numbers left over from a previous load.
            if "output" in trim_cfg:
                self.vars["st_main"].set(trim_cfg["output"])
            self.vars["st_start"].set("00:00:00.000")
            self.vars["st_end"].set("00:00:00.000")
            self._offline_raw_state = None
            self._log(
                f"[gui] loaded render fields from {path} (no raw recording info in "
                "this file — timestamps won't apply; Main clip is treated as already trimmed)"
            )

    def _open_last_state_in_offline_tab(self):
        path = self.render_state_var.get().strip()
        if path:
            self._load_render_state_json(path)
        self.mode_notebook.select(self.offline_tab)

    # -- Console ----------------------------------------------------------

    def _build_console(self, parent):
        p = PALETTE
        header = ttk.Frame(parent)
        header.pack(fill="x")
        ttk.Label(header, text="Console output", style="Header.TLabel").pack(side="left")
        self.status_var = tk.StringVar(value="idle")
        self.status_label = ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel")
        self.status_label.pack(side="left", padx=(10, 0))
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

        pp = cfg.get("propresenter", {})
        obs = cfg.get("obs", {})
        trim = cfg.get("trim", {})
        stitch = cfg.get("stitch", {})

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
        self.vars["trim_state_output"].set(trim.get("state_output", "render_state.json"))
        self.vars["trim_pad_start"].set(str(trim.get("pad_start_seconds", 0)))
        self.vars["trim_pad_end"].set(str(trim.get("pad_end_seconds", 0)))
        self.vars["trim_crf"].set(max(0, min(51, round(trim.get("crf", 18)))))

        self.vars["stitch_auto"].set(bool(stitch.get("auto", True)))
        self.vars["stitch_intro"].set(stitch.get("intro", ""))
        self.vars["stitch_outro"].set(stitch.get("outro", ""))
        self.vars["stitch_intro_duration"].set(str(stitch.get("intro_duration", DEFAULT_IMAGE_DURATION)))
        self.vars["stitch_outro_duration"].set(str(stitch.get("outro_duration", DEFAULT_IMAGE_DURATION)))
        self.vars["stitch_output"].set(stitch.get("output", "final.mp4"))
        self.vars["stitch_transition_duration"].set(str(stitch.get("transition_duration", 1.0)))
        self.vars["stitch_transition"].set(stitch.get("transition", "fade"))

        self.config_path_var.set(str(path))
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
        mode = self.vars[f"{prefix}_mode"].get()
        if mode == "uid":
            uid = self.vars[f"{prefix}_uid"].get().strip()
            if not uid:
                raise ValueError(f"{prefix.capitalize()} slide UID is empty (or switch to text matching)")
            return {"uid": uid}
        text = self.vars[f"{prefix}_text"].get().strip()
        if not text:
            raise ValueError(f"{prefix.capitalize()} slide text is empty")
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
            "propresenter": {
                "host": v["pp_host"].get().strip(),
                "port": to_int(v["pp_port"].get().strip(), "ProPresenter port"),
                "password": v["pp_password"].get(),
                "reconnect_interval_seconds": to_int(v["pp_reconnect"].get().strip(), "Reconnect interval"),
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
                "state_output": v["trim_state_output"].get().strip() or "render_state.json",
                "pad_start_seconds": to_float(v["trim_pad_start"].get().strip() or "0", "Pad start seconds"),
                "pad_end_seconds": to_float(v["trim_pad_end"].get().strip() or "0", "Pad end seconds"),
                "crf": v["trim_crf"].get(),
            },
            "stitch": {
                "auto": bool(v["stitch_auto"].get()),
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
        self._log(f"[gui] running: {' '.join([sys.executable, '-u', str(SERVICE_SCRIPT), *args])}")
        try:
            self.runner.start(args)
        except RuntimeError as e:
            self._log(f"[gui] {e}")
            self._set_busy(False)

    def _stop(self):
        if self.runner.running():
            self._log("[gui] stop requested — terminating process (no graceful trim/exit message)...")
            self.runner.stop()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "line":
                    self._log(payload)
                    if self._current_command == "learn":
                        self._handle_learn_line(payload)
                    elif self._current_command == "watch":
                        self._handle_watch_line(payload)
                elif kind == "exit":
                    self._on_process_exit(payload)
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)

    def _on_process_exit(self, code: int):
        label = self._current_command or "process"
        if code == 0:
            self._log(f"[gui] {label} finished successfully.\n")
        else:
            self._log(f"[gui] {label} exited with code {code}.\n")
        if label == "watch":
            # render() doesn't print another "state = ..." line, so the
            # status label would otherwise freeze on the last live state —
            # except when Prerender/Skip Render already set a terminal
            # status (_prerender_locked), which should stick rather than
            # being overwritten by a plain "done". A real failure (nonzero
            # exit) still always gets surfaced, locked or not.
            if code != 0:
                self.watch_state_var.set(f"stopped (exit {code})")
                self.watch_status_label.configure(foreground=PALETTE["danger"])
            elif not self._prerender_locked:
                self.watch_state_var.set("done")
                self.watch_status_label.configure(foreground=PALETTE["success"])
            self._update_mark_buttons(None)
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

    def _handle_watch_line(self, line: str):
        m = STATE_RE.search(line)
        if m:
            raw_state = m.group(1)
            # Once Prerender/Skip Render has set a terminal status, later
            # state-machine transitions (recording stopping moves the
            # internal state to TRIM even though nothing more will
            # actually render) shouldn't overwrite it.
            if not self._prerender_locked:
                self.watch_state_var.set(WATCH_STATE_LABELS.get(raw_state, raw_state))
                self.watch_status_label.configure(foreground=PALETTE["accent"])
            self._update_mark_buttons(raw_state)
        m2 = RENDER_STATE_PATH_RE.search(line)
        if m2:
            path = m2.group(1).strip()
            self.render_state_var.set(path)
            self._log(f"[gui] captured render-state path for the Offline tab: {path}")
        m3 = PRERENDER_STATUS_RE.search(line)
        if m3:
            self._handle_prerender_status(m3.group(1))

    def _update_mark_buttons(self, raw_state: str | None):
        """Mirrors service_video.py's own guard on manual mark_begin/
        mark_end/prerender/skip_render commands (see watch()'s "manual"
        event handling) so a click is never possible when the backend
        would just ignore it: Mark Start is live in WAIT_BEGIN_SLIDE
        (first mark) and WAIT_END_SLIDE (re-mark); Mark End is live in
        WAIT_END_SLIDE (first mark) and WAIT_RECORD_STOP (re-mark) — and
        once end is marked, start locks (WAIT_RECORD_STOP has Start
        disabled). Prerender/Skip Render both need an end already marked."""
        start_enabled = raw_state in ("WAIT_BEGIN_SLIDE", "WAIT_END_SLIDE")
        end_enabled = raw_state in ("WAIT_END_SLIDE", "WAIT_RECORD_STOP")
        # Prerender is deliberately left clickable throughout
        # WAIT_RECORD_STOP rather than disabling after one use: it can
        # fail simply from being tried too soon after the end was marked
        # (the encoder hasn't flushed that far yet, not a real
        # incompatibility), so retrying needs to stay possible — the
        # backend's own "one already running"/"already done" guards are
        # what actually prevent redoing it once it's succeeded. Once it
        # actually succeeds (or Skip Render is used), _handle_prerender_
        # status() takes over and locks all four buttons for the rest of
        # this run — that's not state-driven, so it can't be expressed
        # here (this only fires on a genuine "state = ..." transition).
        prerender_enabled = raw_state == "WAIT_RECORD_STOP"
        self.mark_start_btn.configure(state="normal" if start_enabled else "disabled")
        self.mark_end_btn.configure(state="normal" if end_enabled else "disabled")
        self.prerender_btn.configure(state="normal" if prerender_enabled else "disabled")
        self.skip_render_btn.configure(state="normal" if prerender_enabled else "disabled")

    def _handle_prerender_status(self, status: str):
        if status == "RUNNING":
            self.watch_state_var.set("Prerendering")
            self.watch_status_label.configure(foreground=PALETTE["info"])
            self._set_prerender_buttons(enabled=False)
        elif status == "DONE":
            self.watch_state_var.set("Done (Prerendered)")
            self.watch_status_label.configure(foreground=PALETTE["success"])
            self._set_prerender_buttons(enabled=False)
            self._prerender_locked = True
        elif status == "SKIPPED":
            self.watch_state_var.set("Done (Skipped Render)")
            self.watch_status_label.configure(foreground=PALETTE["warning"])
            self._set_prerender_buttons(enabled=False)
            self._prerender_locked = True
        elif status == "FAILED":
            self._log("[gui] prerender failed — see the console output above for why; safe to try again")
            # Back to the ordinary WAIT_RECORD_STOP button state so
            # retrying (prerender again, or Skip Render instead) is
            # possible — nothing was actually marked done.
            self._update_mark_buttons("WAIT_RECORD_STOP")
            self.watch_state_var.set(WATCH_STATE_LABELS.get("WAIT_RECORD_STOP", "WAIT_RECORD_STOP"))
            self.watch_status_label.configure(foreground=PALETTE["accent"])

    def _set_prerender_buttons(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.mark_start_btn.configure(state=state)
        self.mark_end_btn.configure(state=state)
        self.prerender_btn.configure(state=state)
        self.skip_render_btn.configure(state=state)

    def _mark_sermon_start(self):
        self.runner.send_line("mark_begin")
        self._log("[gui] sent: mark sermon start")

    def _mark_sermon_end(self):
        self.runner.send_line("mark_end")
        self._log("[gui] sent: mark sermon end")

    def _prerender(self):
        self.runner.send_line("prerender")
        self._log("[gui] sent: prerender")

    def _skip_render(self):
        self.runner.send_line("skip_render")
        self._log("[gui] sent: skip render")

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
        self._prerender_locked = False
        self.watch_state_var.set("starting…")
        self.watch_status_label.configure(foreground=PALETTE["accent"])
        self._update_mark_buttons(None)
        self._start("watch", args)

    def _run_render(self):
        intro = self.vars["st_intro"].get().strip()
        main_clip = self.vars["st_main"].get().strip()
        outro = self.vars["st_outro"].get().strip()
        output = self.vars["st_output"].get().strip() or "output.mp4"
        if not intro or not main_clip or not outro:
            messagebox.showerror("Render", "Intro, main clip, and outro paths are all required.")
            return
        try:
            duration = to_float(self.vars["st_duration"].get().strip() or "1.0", "Transition duration")
            intro_duration = to_float(
                self.vars["st_intro_duration"].get().strip() or str(DEFAULT_IMAGE_DURATION), "Intro duration"
            )
            outro_duration = to_float(
                self.vars["st_outro_duration"].get().strip() or str(DEFAULT_IMAGE_DURATION), "Outro duration"
            )
            crf = self.vars["st_crf"].get()
            start_ts = to_timestamp(self.vars["st_start"].get().strip() or "00:00:00.000", "Sermon start")
            end_ts = to_timestamp(self.vars["st_end"].get().strip() or "00:00:00.000", "Sermon end")
        except ValueError as e:
            messagebox.showerror("Render", str(e))
            return
        transition = self.vars["st_transition"].get().strip() or "fade"

        raw = self._offline_raw_state
        use_raw_trim = raw is not None and raw["recording_path"] == main_clip

        if not use_raw_trim and (start_ts or end_ts):
            self._log(
                "[gui] note: the sermon start/end timestamps are ignored — Main "
                "clip isn't the raw recording from a loaded render-state file "
                "(they only apply right after \"Load from JSON\", before Main clip "
                "is changed)."
            )

        if use_raw_trim:
            # Re-trim the raw recording to these exact timestamps, then
            # stitch — i.e. everything 'watch' does after a live run,
            # reusing the same render_state file so it stays the documented,
            # editable/rerunnable record of this redo. They're absolute
            # timestamps a person picked by eye, not a raw/pad split (there's
            # no slide detection here), so pass them straight through as the
            # offsets with zero padding.
            render_state = {
                "recording_path": raw["recording_path"],
                "raw_begin_offset": format_timestamp(start_ts),
                "raw_end_offset": format_timestamp(end_ts),
                "trim": {
                    "output": raw["trim_output"],
                    "state_output": raw["state_output"],
                    "pad_start_seconds": 0,
                    "pad_end_seconds": 0,
                    "crf": crf,
                },
                "stitch": {
                    "auto": True,
                    "intro": intro,
                    "outro": outro,
                    "intro_duration": intro_duration,
                    "outro_duration": outro_duration,
                    "output": output,
                    "transition_duration": duration,
                    "transition": transition,
                    "crf": crf,
                },
            }
            state_path = raw["state_path"]
            Path(state_path).write_text(json.dumps(render_state, indent=2))
            self._log(f"[gui] updated {state_path} with the current timestamps/settings")
            self._start("render", ["render", state_path])
        else:
            args = [
                "stitch", intro, main_clip, outro,
                "-o", output, "-d", str(duration), "-t", transition, "--crf", str(crf),
                "--intro-duration", str(intro_duration), "--outro-duration", str(outro_duration),
            ]
            self._start("stitch", args)

    def _on_close(self):
        if self.runner.running():
            if not messagebox.askyesno(
                "Quit", "A process is still running. Stop it and quit?"
            ):
                return
            self.runner.stop()
        self.destroy()


class ConfigWindow(tk.Toplevel):
    """Everything that's set once and rarely touched again: the config file
    path, ProPresenter connection + slide matching (with Learn mode folded
    in, since discovering slide UIDs is a ProPresenter-configuration task),
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

    # -- ProPresenter tab: connection + slide matching + Learn -------------

    def _build_propresenter_tab(self, notebook):
        app = self.app
        _outer, frame = app._make_scrollable_tab(notebook, "ProPresenter")
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        app._labeled_entry(frame, 0, "Host", "pp_host")
        app._labeled_entry(frame, 0, "Port", "pp_port", width=10, col=2)
        app.show_pw_var = tk.BooleanVar(value=False)
        pw_entry = app._labeled_entry(frame, 1, "Password", "pp_password", show="•")
        app._labeled_entry(frame, 1, "Reconnect (s)", "pp_reconnect", width=10, col=2)
        ttk.Checkbutton(
            frame, text="Show passwords", variable=app.show_pw_var, command=app._toggle_show_passwords
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        app._pw_entries = [pw_entry]

        app._build_slide_picker(frame, row=3, prefix="begin", label="Begin slide")
        app._build_slide_picker(frame, row=8, prefix="end", label="End slide")

        ttk.Separator(frame, orient="horizontal").grid(
            row=12, column=0, columnspan=4, sticky="ew", pady=(12, 8)
        )
        ttk.Label(frame, text="Learn slide UIDs", style="Header.TLabel").grid(
            row=13, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(
            frame,
            text="Connects to ProPresenter only (no OBS needed). Step through your "
            "slides in ProPresenter — each distinct slide shown appears below with "
            "its UID.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=14, column=0, columnspan=4, sticky="w", pady=(2, 6))

        btn_row = ttk.Frame(frame)
        btn_row.grid(row=15, column=0, columnspan=4, sticky="w", pady=(0, 6))
        start_btn = ttk.Button(btn_row, text="Start Learn", style="Accent.TButton", command=app._run_learn)
        start_btn.pack(side="left")
        app._start_buttons.append(start_btn)

        frame.rowconfigure(16, weight=1)
        tree_frame = ttk.Frame(frame)
        tree_frame.grid(row=16, column=0, columnspan=4, sticky="nsew", pady=(0, 6))
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
        assign_row.grid(row=17, column=0, columnspan=4, sticky="w")
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
            text="Used to detect when the recording starts and stops during a live "
            "Watch run.",
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
            text="Settings for the automatic trim + stitch a live Watch run does when "
            "it finishes. The Offline tab's manual crossfade tool doesn't use these.",
            style="Muted.TLabel", wraplength=540, justify="left",
        ).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 8))

        # Lone fields (nothing sharing their row) get colspan=3 to absorb
        # both weighted columns, same as the Offline tab — Browse buttons
        # move to column 4, their own dedicated unweighted slot, so they
        # don't collide with that span.
        app._labeled_entry(frame, 1, "Trimmed output path", "trim_output", colspan=3, help_text=TIMESTAMP_HELP)
        app._add_browse(frame, 1, "trim_output", save=True, filetypes=VIDEO_FILETYPES, col=3)
        app._labeled_entry(frame, 2, "Render-state base name", "trim_state_output", colspan=3)
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

        app._crf_slider(frame, 6, "CRF (quality)", "trim_crf", col=0, colspan=3)

        ttk.Separator(frame, orient="horizontal").grid(
            row=7, column=0, columnspan=5, sticky="ew", pady=(12, 8)
        )
        app.vars["stitch_auto"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="Auto-stitch after trim", variable=app.vars["stitch_auto"],
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=3)


if __name__ == "__main__":
    App().mainloop()

#!/usr/bin/env python3
"""Sermon Marker: mark the sermon's start and end on a backlog of livestream
recordings, building a Bulk Render states file as it goes.

Put recordings in the `input` folder next to this program (subfolders are
fine) and run it. Each saved mark is appended to `bulk_states.json` in the
same folder, which is also this program's saved progress: reopen it and
already-marked recordings show as done. Paths in that file are relative to
it, so the whole folder can be copied to the machine that renders, then
imported on the main app's Bulk Render tab.

Built for Windows as a single SermonMarker.exe with ffmpeg/ffprobe/ffplay
bundled inside (see packaging/build_marker.py). Also runs from source:
`python marker.py`.
"""

import datetime as dt
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # The bundled ffmpeg/ffprobe/ffplay are unpacked here; putting it first
    # on PATH makes every existing shutil.which()/subprocess call find them.
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")

import gui  # noqa: E402

APP_TITLE = "Sermon Marker"
INPUT_DIR_NAME = "input"
OUTPUT_DIR_NAME = "output"
STATES_FILE_NAME = "bulk_states.json"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".flv", ".ts"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A date somewhere in a filename, e.g. OBS's "2024-01-07 10-30-12.mkv" or
# "20240107_service.mp4".
DATE_IN_NAME_RE = re.compile(r"(?<!\d)(\d{4})[-_. ]?(\d{2})[-_. ]?(\d{2})(?!\d)")


def base_dir() -> Path:
    """The folder holding the program (the .exe when frozen)."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def parse_date(text: str) -> dt.date | None:
    text = text.strip()
    if not DATE_RE.match(text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def guess_date(path: Path) -> str:
    """A date from the filename if it has one, else the file's modified date."""
    for m in DATE_IN_NAME_RE.finditer(path.name):
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            continue
    try:
        return dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return dt.date.today().isoformat()


def output_paths(date: str) -> tuple[str, str]:
    """(trim output, stitch output) for a recording marked with `date`,
    relative to the states file."""
    return f"{OUTPUT_DIR_NAME}/{date}_trimmed.mp4", f"{OUTPUT_DIR_NAME}/{date}.mp4"


class StatesFileError(Exception):
    pass


class MarkerStore:
    """The recordings in `input/` and their marks in `bulk_states.json`.
    No Tk here, so it's testable on its own."""

    def __init__(self, base: Path):
        self.base = base.resolve()
        self.input_dir = self.base / INPUT_DIR_NAME
        self.states_path = self.base / STATES_FILE_NAME
        self.recordings: list[str] = []
        self.entries: list[dict] = []
        self._by_key: dict[str, dict] = {}
        self.last_series = ""

    def refresh(self):
        """Rescans input/ and reloads the states file. Raises
        StatesFileError if the file exists but isn't a valid states array
        (it's then never written to, so nothing in it is lost)."""
        self.recordings = self._scan()
        self.entries = self._load()
        self._match()
        self.last_series = next(
            (e["stitch"]["series"] for e in reversed(self.entries)
             if isinstance(e.get("stitch"), dict) and e["stitch"].get("series")),
            "",
        )

    def _scan(self) -> list[str]:
        if not self.input_dir.is_dir():
            return []
        found = [
            p.relative_to(self.base).as_posix()
            for p in self.input_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ]
        return sorted(found, key=str.lower)

    def _load(self) -> list[dict]:
        if not self.states_path.exists():
            return []
        try:
            data = json.loads(self.states_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            raise StatesFileError(f"Couldn't read {self.states_path.name}: {e}") from e
        if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
            raise StatesFileError(f"{self.states_path.name} isn't a list of recordings.")
        return data

    def _entry_key(self, entry: dict) -> str | None:
        raw = entry.get("recording_path")
        if not isinstance(raw, str) or not raw.strip():
            return None
        norm = raw.strip().replace("\\", "/")
        if not Path(norm).is_absolute():
            return posixpath.normpath(norm)
        try:
            return Path(norm).resolve().relative_to(self.base).as_posix()
        except ValueError:
            return None

    def _match(self):
        """Pairs entries with recordings: by path relative to this folder,
        then (for entries written elsewhere) by filename if that's
        unambiguous. Entries matching no recording are kept as they are."""
        recordings = set(self.recordings)
        self._by_key = {}
        unmatched = []
        for entry in self.entries:
            key = self._entry_key(entry)
            if key in recordings and key not in self._by_key:
                self._by_key[key] = entry
            else:
                unmatched.append(entry)
        by_name: dict[str, list[str]] = {}
        for key in self.recordings:
            by_name.setdefault(posixpath.basename(key), []).append(key)
        for entry in unmatched:
            raw = entry.get("recording_path")
            if not isinstance(raw, str):
                continue
            candidates = by_name.get(posixpath.basename(raw.replace("\\", "/")), [])
            if len(candidates) == 1 and candidates[0] not in self._by_key:
                self._by_key[candidates[0]] = entry

    def entry_for(self, key: str) -> dict | None:
        return self._by_key.get(key)

    def is_marked(self, key: str) -> bool:
        return key in self._by_key

    def marked_count(self) -> int:
        return sum(1 for key in self.recordings if key in self._by_key)

    def next_unmarked(self, after: str | None = None) -> str | None:
        """The first unmarked recording after `after` (wrapping around)."""
        keys = self.recordings
        start = keys.index(after) + 1 if after in keys else 0
        for key in keys[start:] + keys[:start]:
            if key not in self._by_key:
                return key
        return None

    def series_suggestions(self) -> list[str]:
        names = {
            e["stitch"]["series"] for e in self.entries
            if isinstance(e.get("stitch"), dict) and isinstance(e["stitch"].get("series"), str)
            and e["stitch"]["series"].strip()
        }
        return sorted(names, key=str.lower)

    def date_for(self, key: str) -> str:
        """The date a recording was saved with, or a best guess if it hasn't
        been marked (or its saved outputs aren't date-named)."""
        entry = self.entry_for(key)
        stitch = entry.get("stitch") if entry else None
        if isinstance(stitch, dict) and isinstance(stitch.get("output"), str):
            stem = Path(stitch["output"].replace("\\", "/")).stem
            if parse_date(stem):
                return stem
        return guess_date(self.base / key)

    def series_for(self, key: str) -> str:
        entry = self.entry_for(key)
        stitch = entry.get("stitch") if entry else None
        if isinstance(stitch, dict) and isinstance(stitch.get("series"), str):
            return stitch["series"]
        return ""

    def start_end_for(self, key: str) -> tuple[float, float]:
        entry = self.entry_for(key)
        if not entry:
            return 0.0, 0.0
        try:
            return gui.parse_timestamp(entry["raw_begin_offset"]), gui.parse_timestamp(entry["raw_end_offset"])
        except (KeyError, TypeError, ValueError):
            return 0.0, 0.0

    def date_conflict(self, key: str, date: str) -> str | None:
        """Another recording already saved with `date` (so its rendered
        files would be overwritten), or None."""
        for other in self.recordings:
            if other != key and other in self._by_key and self.date_for(other) == date:
                return other
        return None

    def save_mark(self, key: str, start: float, end: float, date: str, series: str):
        """Records a mark: updates the recording's entry if it has one,
        else appends a new one, then writes the file atomically."""
        trim_output, stitch_output = output_paths(date)
        entry = self._by_key.get(key)
        if entry is None:
            entry = {}
            self.entries.append(entry)
            self._by_key[key] = entry
        entry["recording_path"] = key
        entry["raw_begin_offset"] = gui.format_timestamp(start)
        entry["raw_end_offset"] = gui.format_timestamp(end)
        # Any earlier trim was of the old range.
        entry["trimmed_path"] = None
        if not isinstance(entry.get("trim"), dict):
            entry["trim"] = {}
        entry["trim"]["output"] = trim_output
        if not isinstance(entry.get("stitch"), dict):
            entry["stitch"] = {}
        entry["stitch"]["series"] = series.strip()
        entry["stitch"]["output"] = stitch_output
        if series.strip():
            self.last_series = series.strip()
        self._write()

    def _write(self):
        fd, tmp = tempfile.mkstemp(prefix=".bulk_states_", suffix=".json", dir=self.base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, indent=2)
            os.replace(tmp, self.states_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class MarkerApp(tk.Tk):
    def __init__(self, base: Path):
        super().__init__()
        self.title(APP_TITLE)
        gui.apply_style(self)
        self.geometry("860x560")
        self.minsize(640, 400)
        self.store = MarkerStore(base)
        # Created here, not assumed: first run, or someone deleted it.
        self.store.input_dir.mkdir(parents=True, exist_ok=True)
        self.writable = True
        self._trim_win: gui.InteractiveTrimWindow | None = None
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame, text=f"Recordings folder: {self.store.input_dir}", style="Muted.TLabel",
        ).pack(anchor="w")

        progress_row = ttk.Frame(frame)
        progress_row.pack(fill="x", pady=(8, 8))
        self.progress_var = tk.StringVar()
        ttk.Label(progress_row, textvariable=self.progress_var, style="Header.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))

        btn_row = ttk.Frame(frame)
        btn_row.pack(side="bottom", fill="x", pady=(8, 0))
        self.mark_next_btn = ttk.Button(btn_row, text="Mark next", style="Accent.TButton", command=self.mark_next)
        self.mark_next_btn.pack(side="left")
        self.mark_selected_btn = ttk.Button(btn_row, text="Mark selected", command=self.mark_selected)
        self.mark_selected_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Refresh", command=self.refresh).pack(side="right")
        ttk.Button(btn_row, text="Open recordings folder", command=self.open_input_folder).pack(
            side="right", padx=(0, 8)
        )

        self.empty_label = ttk.Label(
            frame,
            text="No recordings yet. Put livestream recordings in the recordings folder, then click Refresh.",
            style="Muted.TLabel",
        )

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill="both", expand=True)
        self.tree_frame = tree_frame
        columns = ("done", "recording", "date", "series", "start", "end")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        for col, text, width, stretch in (
            ("done", "", 36, False), ("recording", "Recording", 300, True), ("date", "Date", 100, False),
            ("series", "Series", 150, False), ("start", "Start", 100, False), ("end", "End", 100, False),
        ):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.mark_selected())
        self.tree.bind("<Return>", lambda _e: self.mark_selected())

    # -- state ---------------------------------------------------------------

    def refresh(self):
        selected = self.selected_key()
        try:
            self.store.refresh()
            self.writable = True
        except StatesFileError as e:
            self.writable = False
            self.store.recordings = self.store._scan()
            messagebox.showerror(
                APP_TITLE,
                f"{e}\n\nMarking is turned off so nothing in that file gets overwritten. "
                "Fix or move the file, then click Refresh.",
                parent=self,
            )
        self.tree.delete(*self.tree.get_children())
        for key in self.store.recordings:
            marked = self.store.is_marked(key)
            start, end = self.store.start_end_for(key)
            self.tree.insert("", "end", iid=key, values=(
                "✓" if marked else "",
                key.removeprefix(f"{INPUT_DIR_NAME}/"),
                self.store.date_for(key) if marked else "",
                self.store.series_for(key) if marked else "",
                gui.format_timestamp(start) if marked else "",
                gui.format_timestamp(end) if marked else "",
            ))
        if selected in self.store.recordings:
            self.tree.selection_set(selected)
            self.tree.see(selected)

        total = len(self.store.recordings)
        done = self.store.marked_count()
        self.progress_var.set(f"{done} of {total} marked")
        self.progress.configure(maximum=max(total, 1), value=done)
        if total:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(anchor="w", before=self.tree_frame, pady=(0, 8))
        state = "normal" if total and self.writable else "disabled"
        self.mark_next_btn.configure(state=state)
        self.mark_selected_btn.configure(state=state)

    def selected_key(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    # -- actions -------------------------------------------------------------

    def mark_next(self):
        key = self.store.next_unmarked(self.selected_key())
        if key is None:
            messagebox.showinfo(APP_TITLE, "Every recording is marked.", parent=self)
            return
        self.open_mark(key)

    def mark_selected(self):
        key = self.selected_key()
        if key is None:
            messagebox.showinfo(APP_TITLE, "Pick a recording in the list first.", parent=self)
            return
        self.open_mark(key)

    def open_mark(self, key: str):
        if not self.writable:
            return
        if self._trim_win is not None and self._trim_win.winfo_exists():
            self._trim_win.lift()
            self._trim_win.focus_force()
            return
        self.tree.selection_set(key)
        self.tree.see(key)
        was_marked = self.store.is_marked(key)
        start, end = self.store.start_end_for(key)
        self.date_var = tk.StringVar(value=self.store.date_for(key))
        self.series_var = tk.StringVar(value=self.store.series_for(key) if was_marked else self.store.last_series)

        def build_extra(frame: ttk.Frame):
            ttk.Label(frame, text="Date", style="Header.TLabel").pack(side="left")
            date_entry = ttk.Entry(frame, textvariable=self.date_var, width=12)
            date_entry.pack(side="left", padx=(6, 0))
            gui.Tooltip(
                date_entry,
                "YYYY-MM-DD. Names the finished video: YYYY-MM-DD.mp4",
                font=self.ui_font,
            )
            ttk.Label(frame, text="Series", style="Header.TLabel").pack(side="left", padx=(16, 0))
            ttk.Combobox(
                frame, textvariable=self.series_var, values=self.store.series_suggestions(), width=28,
            ).pack(side="left", padx=(6, 0))

        def on_apply(start_s: float, end_s: float):
            return self._save(key, start_s, end_s, was_marked)

        self._trim_win = gui.InteractiveTrimWindow(
            self, str(self.store.base / key), start, end,
            on_apply=on_apply, apply_text="Save", build_extra=build_extra,
        )
        self._trim_win.title(f"{APP_TITLE}: {Path(key).name}")

    def _save(self, key: str, start: float, end: float, was_marked: bool) -> bool:
        parent = self._trim_win if self._trim_win is not None else self
        date = self.date_var.get().strip()
        if parse_date(date) is None:
            messagebox.showerror(APP_TITLE, "Enter the date as YYYY-MM-DD, for example 2024-01-07.", parent=parent)
            return False
        if end - start < 1.0:
            messagebox.showerror(APP_TITLE, "Set the sermon's start and end first.", parent=parent)
            return False
        other = self.store.date_conflict(key, date)
        if other and not messagebox.askyesno(
            APP_TITLE,
            f"{Path(other).name} is already saved with {date}, so rendering would overwrite "
            "one video with the other. Save anyway?",
            parent=parent, default="no",
        ):
            return False
        try:
            self.store.save_mark(key, start, end, date, self.series_var.get())
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Couldn't save: {e}", parent=parent)
            return False
        self.refresh()
        if not was_marked:
            # Keep going through the backlog.
            self.after(100, lambda: self._advance_after(key))
        return True

    def _advance_after(self, key: str):
        next_key = self.store.next_unmarked(key)
        if next_key is None:
            messagebox.showinfo(APP_TITLE, "Every recording is marked.", parent=self)
        else:
            self.open_mark(next_key)

    def open_input_folder(self):
        path = str(self.store.input_dir)
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Couldn't open {path}: {e}", parent=self)


# -- self test (used by the Windows build job) -------------------------------

def self_test(log_path: Path) -> int:
    lines: list[str] = []
    ok = True

    def check(name: str, passed: bool, detail: str = ""):
        nonlocal ok
        ok = ok and passed
        lines.append(f"{'PASS' if passed else 'FAIL'} {name}{': ' + detail if detail else ''}")

    for tool in ("ffmpeg", "ffprobe", "ffplay"):
        found = shutil.which(tool)
        passed = False
        if found:
            try:
                result = subprocess.run([tool, "-version"], capture_output=True, timeout=30, **gui._NO_WINDOW)
                passed = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        check(tool, passed, found or "not found")

    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip.mp4"
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10",
             "-pix_fmt", "yuv420p", str(clip)],
            capture_output=True, timeout=60, **gui._NO_WINDOW,
        )
        check("generate test clip", result.returncode == 0 and clip.exists(), result.stderr.decode(errors="replace")[-300:])
        duration = gui.probe_duration(str(clip))
        check("probe duration", bool(duration and duration > 1.5), str(duration))
        frame = Path(tmp) / "frame.png"
        check("extract frame", gui.extract_frame_png(str(clip), 1.0, frame, width=160, height=90) and frame.exists())

    try:
        root = tk.Tk()
        gui.apply_style(root)
        root.update()
        root.destroy()
        check("tk window", True)
    except tk.TclError as e:
        check("tk window", False, str(e))

    lines.append("OK" if ok else "FAILED")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test(base_dir() / "selftest.log"))
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    MarkerApp(base_dir()).mainloop()


if __name__ == "__main__":
    main()

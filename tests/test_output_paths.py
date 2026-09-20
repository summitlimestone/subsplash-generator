"""expand_output_path() (service_video.py and gui.py each have their own
copy — see gui.py's docstring for why it's duplicated rather than
imported): strftime placeholders apply anywhere in an output path,
directory components included, and any directory that doesn't exist yet
is created recursively. Regression coverage for both the real behavior
and gui.py's TIMESTAMP_HELP tooltip text, which previously claimed the
opposite (only the filename was expanded) after the underlying behavior
had already been extended to cover directories too.

Also covers a real reported bug: starting a watch with strftime in a
directory name raised "invalid format string". The original
implementation called datetime.now().strftime(path) on the WHOLE raw
path, delegating the entire parse to the current platform's C library —
glibc (Linux/Mac) silently passes an unrecognized or malformed directive
through as a literal, but Windows' C runtime is much stricter and raises
ValueError("Invalid format string") for anything outside its own smaller
supported set, which a real directory name can trip over with no
intent to use a date code at all (a stray '%' in a folder name, or a
directive glibc tolerates that Windows doesn't). That specific ValueError
can't be reproduced on this (Linux) sandbox — glibc's strftime never
raises it for any of the inputs tried here — so the fix (see both
expand_output_path()s' _STRFTIME_CODES) is verified by its actual
mechanism instead: the whole raw path is never handed to strftime()
at all anymore, only individually pre-vetted, always-portable 2-character
tokens are, so nothing in an arbitrary path can ever reach the
platform's own strftime parser unvetted — checked directly below by
spying on every strftime() call expand_output_path() makes."""

from pathlib import Path

import gui
import service_video as sv


class _SpyNow:
    """Stands in for datetime.now()'s return value, recording every
    format string passed to .strftime() while still returning the real
    result — see test_expand_output_path_never_hands_the_whole_path_to_strftime()."""

    def __init__(self, real_now, calls):
        self._real_now = real_now
        self._calls = calls

    def strftime(self, fmt):
        self._calls.append(fmt)
        return self._real_now.strftime(fmt)


class _SpyDatetime:
    """Stands in for the `datetime` class itself (both modules do
    `from datetime import datetime`, so `sv.datetime`/`gui.datetime` is
    just that class bound as a module attribute — swappable via
    monkeypatch.setattr like any other one)."""

    def __init__(self, calls):
        self._calls = calls

    def now(self):
        import datetime as _dt_module
        return _SpyNow(_dt_module.datetime.now(), self._calls)


def test_expand_output_path_creates_dated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expanded = sv.expand_output_path("recordings/%Y-%m-%d/final_%H-%M-%S.mp4")
    assert Path(expanded).parent.is_dir()
    assert Path(expanded).parent != tmp_path  # actually descended into a dated subdirectory


def test_expand_output_path_creates_nested_dirs_even_without_a_placeholder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expanded = sv.expand_output_path("a/b/c/output.mp4")
    assert expanded == "a/b/c/output.mp4"
    assert (tmp_path / "a" / "b" / "c").is_dir()


def test_gui_expand_output_path_matches_service_video_behavior(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expanded = gui.expand_output_path("logs/%Y-%m-%d/console.log")
    assert Path(expanded).parent.is_dir()


def test_timestamp_help_documents_directory_support():
    assert "directory" in gui.TIMESTAMP_HELP.lower()
    assert "only the filename" not in gui.TIMESTAMP_HELP.lower()


def test_expand_output_path_never_hands_the_whole_path_to_strftime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(sv, "datetime", _SpyDatetime(calls))
    # A stray '%' with no intent to be a date code (a literal folder
    # named "100%_done"), an unsupported directive (%Q isn't strftime at
    # all), and a real supported one (%Y), all in the same path — none of
    # this should ever reach strftime() as anything but an isolated,
    # pre-vetted 2-character token.
    sv.expand_output_path("weird/100%_done/%Y-%m-%d/%Q/file.mp4")
    assert calls, "expand_output_path() should have expanded at least %Y"
    assert all(len(c) == 2 and c[0] == "%" for c in calls), (
        f"strftime() was called with something other than an isolated 2-char token: {calls!r}"
    )


def test_expand_output_path_leaves_unsupported_percent_sequences_literal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expanded = sv.expand_output_path("weird/100%_done/%Q/file.mp4")
    assert "100%_done" in expanded
    assert "%Q" in expanded

"""expand_output_path() (service_video.py and gui.py each have their own
copy — see gui.py's docstring for why it's duplicated rather than
imported): strftime placeholders apply anywhere in an output path,
directory components included, and any directory that doesn't exist yet
is created recursively. Regression coverage for both the real behavior
and gui.py's TIMESTAMP_HELP tooltip text, which previously claimed the
opposite (only the filename was expanded) after the underlying behavior
had already been extended to cover directories too."""

from pathlib import Path

import gui
import service_video as sv


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

"""Play/pause icon rendering — see gui.build_play_pause_icons()'s own
docstring for why these are drawn (real Font Awesome SVGs, rasterized via
ffmpeg's SVG decoder) rather than a font glyph like "▶"/"⏸", and why a
fallback path exists (an ffmpeg build without SVG support shouldn't take
the whole window down over a decorative icon)."""

import subprocess

import gui


def test_build_svg_icon_produces_correctly_sized_image(app):
    icon = gui.build_svg_icon(gui.TRIM_PLAY_ICON_SVG, gui.PALETTE["text"], 16)
    assert icon is not None
    assert (icon.width(), icon.height()) == (16, 16)


def test_build_play_pause_icons_are_real_and_same_size(app):
    play, pause = gui.build_play_pause_icons(16, gui.PALETTE["text"])
    assert (play.width(), play.height()) == (16, 16)
    assert (pause.width(), pause.height()) == (16, 16)


def test_build_svg_icon_returns_none_on_ffmpeg_failure(app, monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    assert gui.build_svg_icon(gui.TRIM_PLAY_ICON_SVG, gui.PALETTE["text"], 16) is None


def test_build_play_pause_icons_falls_back_when_svg_rendering_unavailable(app, monkeypatch):
    monkeypatch.setattr(gui, "build_svg_icon", lambda *a, **k: None)
    play, pause = gui.build_play_pause_icons(16, gui.PALETTE["text"])
    assert (play.width(), play.height()) == (16, 16)
    assert (pause.width(), pause.height()) == (16, 16)

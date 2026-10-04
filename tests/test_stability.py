"""Robustness fixes from the stability review: malformed input files,
handler errors in the subprocess output pump, and stopping a subprocess
along with everything it started."""

import json
import os
import subprocess
import time

import pytest

import gui


def test_clamp_crf_tolerates_strings_and_garbage():
    assert gui.clamp_crf("18") == 18
    assert gui.clamp_crf(99) == 51
    assert gui.clamp_crf(-3) == 0
    assert gui.clamp_crf("high") == 23
    assert gui.clamp_crf(None) == 23


def test_config_saves_the_encoder_preset_for_stitch_too(app):
    app.vars["encoder"].set("software")
    app.vars["encoder_preset"].set("slow")

    cfg = app.collect_config()

    assert cfg["stitch"]["encoder_preset"] == "slow"
    assert cfg["trim"]["encoder_preset"] == "slow"


def test_drain_queue_survives_a_handler_error(app, monkeypatch):
    calls = []

    def broken_handler(line):
        calls.append(line)
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "_handle_watch_line", broken_handler)
    app._current_command = "watch"
    app._queue.put(("line", "first"))
    app._queue.put(("line", "second"))

    app._drain_queue()

    assert calls == ["first", "second"]
    assert "internal error handling process output: RuntimeError: boom" in app.console.get("1.0", "end")


def test_failed_exit_handling_still_leaves_the_gui_idle(app, monkeypatch):
    def broken_exit(_code):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "_on_process_exit", broken_exit)
    app._current_command = "trim"
    app._set_busy(True, "trim")
    app._queue.put(("exit", 0))

    app._drain_queue()

    assert app._current_command is None
    assert app.status_var.get() == "idle"


def test_live_trim_does_not_send_to_a_non_watch_process(app, monkeypatch):
    sent = []
    monkeypatch.setattr(app.runner, "send_line", lambda line: sent.append(line))
    monkeypatch.setattr(app.runner, "running", lambda: True)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: None)
    app._current_command = "bulk_full"

    app._trim_live()

    assert sent == []


def test_load_render_state_rejects_a_bulk_list(app, tmp_path, monkeypatch):
    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, msg: errors.append(msg))
    path = tmp_path / "bulk.json"
    path.write_text(json.dumps([{"recording_path": "/a.mp4"}]))

    app._load_render_state_json(str(path))

    assert errors and "Bulk Render" in errors[0]


def test_load_render_state_accepts_a_utf8_bom(app, tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes("﻿".encode() + json.dumps({"trimmed_path": "/clips/été.mp4"}).encode())

    app._load_render_state_json(str(path))

    assert app.vars["st_trimmed"].get() == "/clips/été.mp4"


def test_series_entries_without_a_name_are_ignored(app):
    gui.SERIES_PATH.write_text(json.dumps([{"intro": "/a.mp4"}, {"name": "Real", "intro": "/i.mp4"}]))

    app._load_series()

    assert app._series_names() == ["Real"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group check")
def test_process_runner_stop_kills_grandchildren(tmp_path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "fake_service.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(gui, "SERVICE_SCRIPT", script)
    exited = []
    runner = gui.ProcessRunner(on_line=lambda _line: None, on_exit=exited.append)
    runner.start(["learn"])
    deadline = time.monotonic() + 10
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())

    runner.stop()

    deadline = time.monotonic() + 10
    while True:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        # A killed-but-unreaped grandchild is a zombie owned by init; check
        # its state rather than waiting for init to reap it.
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)], capture_output=True, text=True)
        if stat.stdout.strip().startswith("Z") or not stat.stdout.strip():
            break
        assert time.monotonic() < deadline, "grandchild still running after stop()"
        time.sleep(0.05)


def test_process_runner_round_trips_non_ascii_output(tmp_path, monkeypatch):
    script = tmp_path / "fake_service.py"
    script.write_text("print('slide: ♪ 찬양 →', flush=True)\n", encoding="utf-8")
    monkeypatch.setattr(gui, "SERVICE_SCRIPT", script)
    lines, exited = [], []
    runner = gui.ProcessRunner(on_line=lines.append, on_exit=exited.append)
    runner.start(["learn"])
    deadline = time.monotonic() + 10
    while not exited:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert exited == [0]
    assert lines == ["slide: ♪ 찬양 →"]


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_config_save_does_not_restart_an_unchanged_api(app, pump):
    pytest.importorskip("uvicorn")
    app.vars["api_port"].set(str(_free_port()))
    app.vars["api_enabled"].set(True)
    server = app._api_server
    pump(lambda: server.started, timeout=10)

    assert app._write_config()
    assert app._api_server is server

    app.vars["api_port"].set(str(_free_port()))
    assert app._write_config()
    assert app._api_server is not server
    app.vars["api_enabled"].set(False)


def test_api_bind_failure_is_reported(app, pump):
    pytest.importorskip("uvicorn")
    import socket

    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        app.vars["api_port"].set(str(blocker.getsockname()[1]))
        app.vars["api_enabled"].set(True)
        pump(lambda: "control API failed to start" in app.console.get("1.0", "end"), timeout=10)
    assert app._api_server is None
    app.vars["api_enabled"].set(False)

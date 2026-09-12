"""MiniLiveControlsWindow: the Live tab's five buttons (Start Watch/Mark
Sermon Start/Mark Sermon End/Trim/Stitch) and status, stacked vertically in
a small popup window opened via the Live tab's "Mini controls…" button.
Its own buttons never carry independent state — App._sync_mini_live_controls()
mirrors the real Live tab buttons' enabled/disabled state and the status
text/color into it on every _drain_queue() tick, so these tests drive that
directly rather than waiting on a real after() cycle."""

import gui


def test_mini_window_starts_withdrawn(app):
    assert not app.mini_live_window.winfo_viewable()


def test_mini_buttons_drive_the_same_workflow_as_the_live_tab(app, monkeypatch):
    """The mini window's buttons were built with command=app._run_watch
    etc. directly (see MiniLiveControlsWindow) — the same bound methods
    the Live tab's own buttons use, not a separate code path. Since
    those methods were already captured by reference before any test
    runs, monkeypatching the top-level app._run_watch etc. after the
    fact wouldn't be seen by an already-built button; instead this
    patches what each method calls *inside* its own body (self.runner,
    self._start, self._run_trim/_run_stitch — all looked up fresh on
    every call), and drives each mini button to confirm the real
    workflow methods actually ran, in order."""
    calls = []
    monkeypatch.setattr(app.runner, "send_line", lambda line: calls.append(("send_line", line)))
    monkeypatch.setattr(app.runner, "running", lambda: False)
    monkeypatch.setattr(app, "_autosave_for_run", lambda: (calls.append(("autosave",)), True)[1])
    monkeypatch.setattr(app, "_start", lambda *a: calls.append(("start", a)))
    monkeypatch.setattr(app, "_run_trim", lambda: calls.append(("run_trim",)))
    monkeypatch.setattr(app, "_run_stitch", lambda: calls.append(("run_stitch",)))

    mini = app.mini_live_window
    for _main_btn, mini_btn in mini.button_pairs:
        mini_btn.configure(state="normal")  # invoke() refuses a disabled button
        mini_btn.invoke()

    kinds = [c[0] for c in calls]
    assert kinds == ["autosave", "start", "send_line", "send_line", "run_trim", "run_stitch"]
    assert calls[1][1][0] == "watch"
    assert calls[2][1] == "mark_begin"
    assert calls[3][1] == "mark_end"


def test_sync_is_skipped_while_withdrawn(app):
    mini = app.mini_live_window
    app.mark_start_btn.configure(state="normal")
    mini.button_pairs[1][1].configure(state="disabled")  # Mark Sermon Start's mini button
    app._sync_mini_live_controls()
    # Still withdrawn — sync should have done nothing, not flipped it to "normal".
    assert str(mini.button_pairs[1][1]["state"]) == "disabled"


def test_sync_mirrors_button_state_while_open(app):
    mini = app.mini_live_window
    app.deiconify()
    mini.deiconify()
    app.update()

    app.start_watch_btn.configure(state="disabled")
    app.mark_start_btn.configure(state="normal")
    app.mark_end_btn.configure(state="disabled")
    app.live_trim_btn.configure(state="disabled")
    app.live_stitch_btn.configure(state="disabled")
    app._sync_mini_live_controls()

    states = {main_btn["text"]: str(mini_btn["state"]) for main_btn, mini_btn in mini.button_pairs}
    assert states == {
        "Start Watch": "disabled",
        "Mark Sermon Start": "normal",
        "Mark Sermon End": "disabled",
        "Trim": "disabled",
        "Stitch": "disabled",
    }


def test_sync_mirrors_status_text_and_color(app):
    mini = app.mini_live_window
    app.deiconify()
    mini.deiconify()
    app.update()

    app.watch_state_var.set("Watching…")
    app.watch_status_label.configure(foreground=gui.PALETTE["accent"])
    app._sync_mini_live_controls()

    assert mini.status_label.cget("text") == "Watching…"
    assert str(mini.status_label.cget("foreground")) == gui.PALETTE["accent"]


def test_open_mini_live_controls_deiconifies_it(app):
    assert not app.mini_live_window.winfo_viewable()
    app._open_mini_live_controls()
    app.update()
    assert app.mini_live_window.winfo_viewable()

"""The control API's token auth and the web control panel's endpoints (an
OBS custom browser dock over the same five Live-tab actions as Mini
controls). Drives the real FastAPI app via TestClient — each request
runs on a background thread (as uvicorn's would) while the test thread
keeps pumping Tk's event loop, so App._call_on_main_thread()'s
handoff to the Tk thread is exercised too (a queue drained by the pumping
loop below, standing in for after(0, ...) — which itself can't be called
from a non-main thread unless Tk is inside a real mainloop()). Needs a
real (or Xvfb) display for the `app` fixture."""

import json
import queue
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import gui  # noqa: E402


class PumpedClient:
    """TestClient wrapper: runs each request on its own thread while
    this (Tk main) thread pumps app.update() until it's done."""

    def __init__(self, app, monkeypatch):
        self.app = app
        self.jobs: queue.Queue = queue.Queue()

        def call_on_main_thread(fn):
            done = threading.Event()
            result = {}
            self.jobs.put((fn, result, done))
            done.wait(timeout=5)
            return result.get("value")

        monkeypatch.setattr(app, "_call_on_main_thread", call_on_main_thread)
        self.inner = TestClient(gui._build_api_app(app))

    def _request(self, method, *args, **kwargs):
        box = {}

        def run():
            box["resp"] = getattr(self.inner, method)(*args, **kwargs)

        thread = threading.Thread(target=run)
        thread.start()
        while thread.is_alive():
            while not self.jobs.empty():
                fn, result, done = self.jobs.get()
                result["value"] = fn()
                done.set()
            self.app.update()
            thread.join(timeout=0.01)
        return box["resp"]

    def get(self, *a, **k):
        return self._request("get", *a, **k)

    def post(self, *a, **k):
        return self._request("post", *a, **k)

    def put(self, *a, **k):
        return self._request("put", *a, **k)


@pytest.fixture
def client(app, monkeypatch):
    return PumpedClient(app, monkeypatch)


@pytest.fixture
def auth(app):
    return {"Authorization": f"Bearer {app.vars['api_token'].get()}"}


def _enable(btn):
    btn.configure(state="normal")


def _add_series(app, name):
    app.series = [{
        "name": name, "intro": "/i.mp4", "intro_duration": 5.0, "outro": "/o.mp4",
        "outro_duration": 5.0, "transition": "fade", "transition_duration": 1.0, "hidden": False,
    }]


# -- token auth ---------------------------------------------------------------

def test_requests_without_a_token_are_rejected(client):
    assert client.get("/state").status_code == 401
    assert client.get("/").status_code == 401
    assert client.post("/trim").status_code == 401


def test_bearer_header_is_accepted(client, auth):
    assert client.get("/state", headers=auth).status_code == 200


def test_query_param_token_is_accepted(client, app):
    token = app.vars["api_token"].get()
    assert client.get(f"/state?token={token}").status_code == 200


def test_wrong_token_is_rejected(client):
    assert client.get("/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/state?token=nope").status_code == 401


def test_old_basic_auth_password_no_longer_works(client, app):
    token = app.vars["api_token"].get()
    resp = client.get("/state", auth=("anyone", token))
    assert resp.status_code == 401


def test_non_ascii_token_guess_is_a_clean_401(client):
    assert client.get("/state?token=%C3%A9").status_code == 401


def test_swagger_and_openapi_also_require_the_token(client, app):
    assert client.get("/swagger").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    token = app.vars["api_token"].get()
    page = client.get(f"/swagger?token={token}")
    assert page.status_code == 200
    assert f"/openapi.json?token={token}" in page.text
    assert client.get(f"/openapi.json?token={token}").status_code == 200


# -- the web control panel ------------------------------------------------------

def test_root_serves_the_control_panel_page(client, auth):
    resp = client.get("/", headers=auth)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    for label in ("Start Watch", "Mark Sermon Start", "Mark Sermon End", "Trim", "Stitch"):
        assert label in resp.text
    assert "__" not in resp.text.split("<script>")[0]  # every style placeholder was substituted


def test_state_includes_what_the_panel_needs(client, app, auth):
    _add_series(app, "Fall")
    app.vars["stitch_series"].set("Fall")
    app.watch_state_var.set("Watching…")
    data = client.get("/state", headers=auth).json()
    assert data["status"] == "Watching…"
    assert set(data["buttons"]) == {"watch", "mark_start", "mark_end", "trim", "stitch"}
    assert data["series"] == ["Fall"]
    assert data["selected_series"] == "Fall"
    assert data["status_color"]
    assert data["state"] == "idle"  # the original keys are still there


# -- actions: gated on the real buttons' enabled state ---------------------------

def test_watch_start_refused_when_button_disabled(client, app, auth, monkeypatch):
    called = []
    monkeypatch.setattr(app, "_run_watch", lambda: called.append(1))
    app.start_watch_btn.configure(state="disabled")
    assert client.post("/watch/start", headers=auth).status_code == 409
    assert not called


def test_watch_start_runs_the_same_handler_as_the_button(client, app, auth, monkeypatch):
    called = []
    monkeypatch.setattr(app, "_run_watch", lambda: called.append(1))
    _enable(app.start_watch_btn)
    assert client.post("/watch/start", headers=auth).status_code == 200
    assert called == [1]


def test_trim_gated_and_dispatched(client, app, auth, monkeypatch):
    called = []
    monkeypatch.setattr(app, "_trim_live", lambda: called.append(1))
    app.live_trim_btn.configure(state="disabled")
    assert client.post("/trim", headers=auth).status_code == 409
    _enable(app.live_trim_btn)
    assert client.post("/trim", headers=auth).status_code == 200
    assert called == [1]


def test_stitch_refused_when_disabled(client, app, auth, monkeypatch):
    called = []
    monkeypatch.setattr(app, "_stitch_live", lambda: called.append(1))
    app.live_stitch_btn.configure(state="disabled")
    assert client.post("/stitch", headers=auth).status_code == 409
    assert not called


def test_stitch_refused_with_no_series_selected(client, app, auth, monkeypatch):
    called = []
    monkeypatch.setattr(app, "_stitch_live", lambda: called.append(1))
    _enable(app.live_stitch_btn)
    app.vars["stitch_series"].set("")
    resp = client.post("/stitch", headers=auth)
    assert resp.status_code == 409
    assert "series" in resp.json()["detail"]
    assert not called  # never reaches _stitch_live()'s own modal error dialog


def test_stitch_uses_the_currently_selected_series(client, app, auth, monkeypatch):
    _add_series(app, "Fall")
    called = []
    monkeypatch.setattr(app, "_stitch_live", lambda: called.append(app.vars["stitch_series"].get()))
    _enable(app.live_stitch_btn)
    app.vars["stitch_series"].set("Fall")
    assert client.post("/stitch", headers=auth).status_code == 200
    assert called == ["Fall"]


def test_stitch_body_series_selects_it_first(client, app, auth, monkeypatch):
    _add_series(app, "Fall")
    called = []
    monkeypatch.setattr(app, "_stitch_live", lambda: called.append(app.vars["stitch_series"].get()))
    _enable(app.live_stitch_btn)
    assert client.post("/stitch", headers=auth, json={"series": "Fall"}).status_code == 200
    assert called == ["Fall"]


def test_stitch_with_an_unknown_series_is_refused(client, app, auth, monkeypatch):
    called = []
    monkeypatch.setattr(app, "_stitch_live", lambda: called.append(1))
    _enable(app.live_stitch_btn)
    assert client.post("/stitch", headers=auth, json={"series": "Nope"}).status_code == 409
    assert not called


def test_put_series_sets_the_live_dropdown(client, app, auth):
    _add_series(app, "Fall")
    assert client.put("/series", headers=auth, json={"name": "Fall"}).status_code == 200
    assert app.vars["stitch_series"].get() == "Fall"


def test_put_series_unknown_name_refused(client, app, auth):
    assert client.put("/series", headers=auth, json={"name": "Nope"}).status_code == 409


def test_get_series_lists_visible_series(client, app, auth):
    _add_series(app, "Fall")
    data = client.get("/series", headers=auth).json()
    assert data == {"series": ["Fall"], "selected": app.vars["stitch_series"].get()}


def test_marks_still_409_with_no_watch_running(client, auth):
    assert client.post("/mark/start", headers=auth).status_code == 409
    assert client.post("/mark/end", headers=auth).status_code == 409


# -- config: token replaces password ---------------------------------------------

def test_collect_config_carries_a_token_not_a_password(app):
    api = app.collect_config()["api"]
    assert api["token"]
    assert "password" not in api


def test_default_config_generates_a_token():
    api = gui.default_config()["api"]
    assert len(api["token"]) >= 24
    assert "password" not in api
    assert gui.default_config()["api"]["token"] != api["token"]


def test_loading_a_config_without_a_token_generates_and_persists_one(app):
    cfg = json.loads(gui.CONFIG_PATH.read_text())
    cfg["api"].pop("token")
    cfg["api"]["password"] = "old-basic-auth-password"
    gui.CONFIG_PATH.write_text(json.dumps(cfg))

    app.load_config()

    token = app.vars["api_token"].get()
    assert token
    assert json.loads(gui.CONFIG_PATH.read_text())["api"]["token"] == token
    assert "password" not in app.collect_config()["api"]
    assert app._config_is_dirty() is False


def test_regenerate_changes_the_token_and_marks_config_dirty(app):
    before = app.vars["api_token"].get()
    app._regenerate_api_token()
    assert app.vars["api_token"].get() != before
    assert app._config_is_dirty() is True


def test_dock_url_contains_host_port_and_token(app):
    app.vars["api_host"].set("192.168.1.5")
    app.vars["api_port"].set("9000")
    url = app._api_dock_url()
    assert url == f"http://192.168.1.5:9000/?token={app.vars['api_token'].get()}"


def test_dock_url_swaps_a_wildcard_bind_address_for_loopback(app):
    app.vars["api_host"].set("0.0.0.0")
    assert app._api_dock_url().startswith("http://127.0.0.1:")


def test_copy_dock_url_puts_it_on_the_clipboard(app):
    app._copy_api_dock_url()
    assert app.clipboard_get() == app._api_dock_url()

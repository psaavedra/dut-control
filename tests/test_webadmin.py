#!/usr/bin/env python3

import subprocess
import sys

import dut_control.webadmin as webadmin_mod
import pytest
from pathlib import Path

# Ensure project root (containing dut_control/) is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_sessions():
    """Reset the module-level session store before & after each test."""
    with webadmin_mod._sessions_lock:
        webadmin_mod._sessions.clear()
    yield
    with webadmin_mod._sessions_lock:
        webadmin_mod._sessions.clear()


@pytest.fixture
def web_client():
    """Flask test client for the web admin service."""
    webadmin_mod.app.config["TESTING"] = True
    webadmin_mod.app.secret_key = "test-secret"
    return webadmin_mod.app.test_client()


def _ok(data):
    return {"ok": True, "auth": False, "data": data, "error": None}


def _fail(error, auth=False):
    return {"ok": False, "auth": auth, "data": None, "error": error}


class _Resp:
    """Minimal stand-in for a requests response."""

    def __init__(self, status_code=200, payload=None, text_body=None):
        self.status_code = status_code
        self._payload = payload
        self._text_body = text_body

    def json(self):
        if self._text_body is not None:
            raise ValueError("not json")
        return self._payload


def _api_returning(result, calls=None):
    """Fake _api_post that records its calls and returns `result`."""
    def fake(path, payload):
        if calls is not None:
            calls.append((path, payload))
        return result
    return fake


def _log_in(web_client, monkeypatch, key="admin-key-01"):
    """Log a client in, stubbing the key probe, and return the CSRF token."""
    monkeypatch.setattr(webadmin_mod, "_api_post", _api_returning(_ok([])))
    resp = web_client.post("/login", data={"admin-key": key})
    assert resp.status_code == 302
    with web_client.session_transaction() as sess:
        return sess["csrf"]


def _stub_api(monkeypatch, result, calls=None):
    """Point _api_post at a canned result and return the call log."""
    calls = [] if calls is None else calls
    monkeypatch.setattr(webadmin_mod, "_api_post",
                        _api_returning(result, calls))
    return calls


def _make_node(name="node-01", duts=None):
    return {
        "name": name,
        "ssh": {"ip": "192.0.2.20", "port": 22, "user": "runner"},
        "duts": duts if duts is not None else [],
    }


def _make_dut(name="rpi5-01", pool="rpi5", enabled=True):
    return {
        "name": name,
        "metadata": {"pool": pool, "enabled": enabled},
        "network": {"ip": "192.0.2.30", "ssh-port": 22},
        "storage": {"driver": "usbsdmux", "control": "/dev/sg1",
                    "device": "/dev/sda1"},
        "power": {"driver": "script", "power-on": "up.sh",
                  "power-off": "down.sh"},
    }


def _make_api_client(name="client-01", key="fac72a9494cd132a"):
    return {
        "name": name,
        "key": key,
        "ssh": {"ip": "192.0.2.10", "port": 22, "user": "tester"},
        "ports-range": {"from": 5000, "to": 5010},
    }


# ---------------------------------------------------------------------------
# Unit tests: transport
# ---------------------------------------------------------------------------

def test_api_post_reports_forbidden(monkeypatch):
    """A 403 must stay readable instead of becoming an HTTPError."""
    monkeypatch.setattr(webadmin_mod._HTTP, "post",
                        lambda *a, **k: _Resp(403, {"error": "nope"}))
    result = webadmin_mod._api_post("/conf/reload", {})
    assert result["ok"] is False
    assert result["auth"] is True
    assert "rejected the admin key" in result["error"]


def test_api_post_reports_a_non_json_body(monkeypatch):
    """/conf/reload answers with an HTML traceback on a broken config."""
    monkeypatch.setattr(
        webadmin_mod._HTTP, "post",
        lambda *a, **k: _Resp(500, text_body="<html>traceback</html>"))
    result = webadmin_mod._api_post("/conf/reload", {})
    assert result["ok"] is False
    assert result["auth"] is False
    assert "non-JSON" in result["error"]
    assert "500" in result["error"]


def test_api_post_reports_a_connection_error(monkeypatch):
    def boom(*args, **kwargs):
        raise webadmin_mod.requests.ConnectionError("refused")

    monkeypatch.setattr(webadmin_mod._HTTP, "post", boom)
    result = webadmin_mod._api_post("/conf/reload", {})
    assert result["ok"] is False
    assert "cannot reach dut-control" in result["error"]


def test_api_post_returns_parsed_json(monkeypatch):
    monkeypatch.setattr(webadmin_mod._HTTP, "post",
                        lambda *a, **k: _Resp(200, {"result": 0}))
    result = webadmin_mod._api_post("/conf/reload", {})
    assert result == {"ok": True, "auth": False,
                      "data": {"result": 0}, "error": None}


def test_full_url_joins_without_doubling_slashes(monkeypatch):
    monkeypatch.setattr(webadmin_mod, "BASE_URL", "http://lab:8000/")
    assert webadmin_mod._full_url("/conf/reload") == \
        "http://lab:8000/conf/reload"


# ---------------------------------------------------------------------------
# Login / logout / access control
# ---------------------------------------------------------------------------

def test_dashboard_redirects_to_login_when_anonymous(web_client):
    resp = web_client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_accepts_a_valid_admin_key(web_client, monkeypatch):
    calls = []
    monkeypatch.setattr(webadmin_mod, "_api_post",
                        _api_returning(_ok([]), calls))

    resp = web_client.post("/login", data={"admin-key": "secret-key"})
    assert resp.status_code == 302
    assert resp.headers["Location"] in ("/", "http://localhost/")

    # The key is validated against the smallest admin endpoint
    assert calls == [("/conf/info/processes", {"admin-key": "secret-key"})]

    # It is kept server side, not in the cookie
    assert list(webadmin_mod._sessions.values()) == ["secret-key"]
    with web_client.session_transaction() as sess:
        assert "secret-key" not in str(dict(sess))

    assert web_client.get("/").status_code == 200


def test_login_rejects_an_invalid_admin_key(web_client, monkeypatch):
    monkeypatch.setattr(
        webadmin_mod, "_api_post",
        _api_returning(_fail("the service rejected the admin key",
                             auth=True)))

    resp = web_client.post("/login", data={"admin-key": "wrong"})
    assert resp.status_code == 401
    assert b"rejected the admin key" in resp.data
    assert webadmin_mod._sessions == {}


def test_login_reports_an_unreachable_service(web_client, monkeypatch):
    monkeypatch.setattr(webadmin_mod, "_api_post",
                        _api_returning(_fail("cannot reach dut-control")))

    resp = web_client.post("/login", data={"admin-key": "any"})
    assert resp.status_code == 502
    assert b"cannot reach dut-control" in resp.data
    assert webadmin_mod._sessions == {}


def test_login_rejects_an_empty_key(web_client, monkeypatch):
    calls = []
    monkeypatch.setattr(webadmin_mod, "_api_post",
                        _api_returning(_ok([]), calls))

    resp = web_client.post("/login", data={"admin-key": "   "})
    assert resp.status_code == 400
    assert b"admin key is required" in resp.data
    assert calls == []


def test_logout_drops_the_session(web_client, monkeypatch):
    token = _log_in(web_client, monkeypatch)

    resp = web_client.post("/logout", data={"csrf": token})
    assert resp.status_code == 302
    assert webadmin_mod._sessions == {}
    assert web_client.get("/").status_code == 302


def test_logout_rejects_get(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    assert web_client.get("/logout").status_code == 405


def test_post_without_a_csrf_token_is_rejected(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)

    resp = web_client.post("/logout", data={})
    assert resp.status_code == 400
    assert b"invalid CSRF token" in resp.data
    # The session survives a rejected request
    assert list(webadmin_mod._sessions.values()) == ["admin-key-01"]


def test_post_with_a_wrong_csrf_token_is_rejected(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)

    resp = web_client.post("/logout", data={"csrf": "forged"})
    assert resp.status_code == 400
    assert list(webadmin_mod._sessions.values()) == ["admin-key-01"]


# ---------------------------------------------------------------------------
# /nodes view
# ---------------------------------------------------------------------------

def test_nodes_view_requires_login(web_client):
    resp = web_client.get("/nodes")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_nodes_view_lists_duts_per_node(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    node = _make_node(duts=[_make_dut("rpi5-01"),
                            _make_dut("rpi5-02", enabled=False)])
    calls = _stub_api(monkeypatch, _ok([node]))

    resp = web_client.get("/nodes")
    assert resp.status_code == 200
    body = resp.data.decode()

    # The admin key is injected into the request the view makes
    assert calls == [("/conf/info/nodes",
                      {"admin-key": "admin-key-01"})]

    assert "node-01" in body
    assert "rpi5-01" in body and "rpi5-02" in body
    assert "rpi5" in body
    # The disabled DUT is marked as such
    assert "disabled" in body


def test_nodes_view_renders_open_ended_dut_fields(web_client, monkeypatch):
    """Sub-dicts come straight from YAML, so unknown keys must show up."""
    _log_in(web_client, monkeypatch)
    dut = _make_dut()
    dut["metadata"]["site"] = "rack-7"
    dut["storage"]["surprise"] = "value-42"
    _stub_api(monkeypatch, _ok([_make_node(duts=[dut])]))

    body = web_client.get("/nodes").data.decode()
    assert "surprise" in body and "value-42" in body
    assert "/dev/sg1" in body


def test_nodes_view_handles_an_empty_inventory(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    _stub_api(monkeypatch, _ok([]))

    resp = web_client.get("/nodes")
    assert resp.status_code == 200
    assert b"no nodes configured" in resp.data


def test_nodes_view_reports_a_service_error(web_client, monkeypatch):
    """A reachable service that errors still renders a usable page."""
    _log_in(web_client, monkeypatch)
    _stub_api(monkeypatch, _fail("cannot reach dut-control at x"))

    resp = web_client.get("/nodes")
    assert resp.status_code == 200
    assert b"cannot reach dut-control" in resp.data


def test_nodes_view_logs_out_when_the_key_is_rejected(
        web_client, monkeypatch):
    """A key revoked mid session must not leave a half-working UI."""
    _log_in(web_client, monkeypatch)
    _stub_api(monkeypatch, _fail("the service rejected the admin key",
                                 auth=True))

    resp = web_client.get("/nodes")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert webadmin_mod._sessions == {}


# ---------------------------------------------------------------------------
# /clients view
# ---------------------------------------------------------------------------

def test_mask_key_keeps_only_the_ends():
    assert webadmin_mod._mask_key("fac72a9494cd132a") == "fac7...132a"


def test_mask_key_handles_short_and_missing_values():
    # Nothing to keep from a short secret, so reveal no characters
    assert webadmin_mod._mask_key("abcd") == "****"
    assert webadmin_mod._mask_key("abcdefgh") == "********"
    assert webadmin_mod._mask_key(None) == ""
    assert webadmin_mod._mask_key("") == ""


def test_clients_view_requires_login(web_client):
    assert web_client.get("/clients").status_code == 302


def test_clients_view_masks_client_keys(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    calls = _stub_api(monkeypatch, _ok([_make_api_client()]))

    resp = web_client.get("/clients")
    assert resp.status_code == 200
    body = resp.data.decode()

    assert calls == [("/conf/info/clients", {"admin-key": "admin-key-01"})]
    assert "client-01" in body
    # The mask is what the page shows first
    assert "fac7...132a" in body


def test_clients_view_shows_the_ports_range(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    _stub_api(monkeypatch, _ok([_make_api_client()]))

    body = web_client.get("/clients").data.decode()
    assert "5000-5010" in body
    assert "tester" in body


def test_clients_view_handles_a_client_without_a_ports_range(
        web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    entry = _make_api_client()
    entry["ports-range"] = {}
    _stub_api(monkeypatch, _ok([entry]))

    resp = web_client.get("/clients")
    assert resp.status_code == 200
    assert b"none" in resp.data


def test_clients_view_handles_an_empty_list(web_client, monkeypatch):
    _log_in(web_client, monkeypatch)
    _stub_api(monkeypatch, _ok([]))

    resp = web_client.get("/clients")
    assert resp.status_code == 200
    assert b"no clients configured" in resp.data


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------

def test_webadmin_imports_without_a_config_dir():
    """
    The module must not import dut_control.server, which calls
    reload_config() at import time and would need a config directory.
    Checked in a subprocess: by the time this test runs in-process,
    dut_control.server is already imported by the other test module.
    """
    code = ("import dut_control.webadmin as w, sys;"
            "assert 'dut_control.server' not in sys.modules")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env={"PATH": "/usr/bin:/bin", "DUT_CONTROL_DIR": "/nonexistent"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

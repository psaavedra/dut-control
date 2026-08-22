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

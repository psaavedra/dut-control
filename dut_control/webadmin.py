#!/usr/bin/env python3
"""
Web admin UI for the dut-control service.

This is a separate HTTP service acting as a client of the dut-control
REST API: it renders HTML views and proxies every call server side with
`requests`, the same way dut_control/admin.py does. It deliberately does
not import dut_control.server, which would require a config directory at
import time.
"""
import argparse
import os
import secrets
import sys
import threading
from functools import wraps

import requests
from flask import (Flask, flash, redirect, render_template, request,
                   session, url_for)

DEFAULT_BASE_URL = os.environ.get("DUT_CONTROL_URL",
                                  "http://localhost:8000")
SECRET_ENV = "DUT_CONTROL_WEBADMIN_SECRET"

# Rebound by main(); module level so tests can point them anywhere.
BASE_URL = DEFAULT_BASE_URL
TIMEOUT = 10.0

# One session keeps the connection to dut-control alive across the
# several calls a single page may need.
_HTTP = requests.Session()

# Server side session store. The browser cookie carries only an opaque
# id, so the admin key never leaves this process: Flask cookies are
# signed but not encrypted, and this service is expected to run over
# plain HTTP inside a lab.
_sessions = {}
_sessions_lock = threading.Lock()

# POST endpoints exempt from the CSRF check. Logging in cannot present a
# token because the session does not exist yet.
_CSRF_EXEMPT = frozenset({"login"})

app = Flask(__name__, template_folder="webadmin_templates",
            static_folder=None)
app.secret_key = os.environ.get(SECRET_ENV) or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _full_url(path: str) -> str:
    return BASE_URL.rstrip("/") + path


def _classify(resp):
    """
    Map a dut-control response onto the common result dict.

    Unlike admin.py this never calls raise_for_status(): the 403 body
    identifying a bad admin key has to survive, and /conf/reload can
    answer with an HTML traceback page instead of JSON.
    """
    if resp.status_code == 403:
        return {"ok": False, "auth": True, "data": None,
                "error": "the service rejected the admin key"}
    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "auth": False, "data": None,
                "error": ("the service returned a non-JSON response "
                          f"(HTTP {resp.status_code}); check its log")}
    return {"ok": True, "auth": False, "data": data, "error": None}


def _api_post(path: str, payload: dict):
    """POST to dut-control and classify the outcome."""
    try:
        resp = _HTTP.post(_full_url(path), json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return {"ok": False, "auth": False, "data": None,
                "error": f"cannot reach dut-control at {BASE_URL}: {exc}"}
    return _classify(resp)


def _admin_post(path: str, extra: dict = None):
    """POST with the logged in admin key injected into the body."""
    payload = {"admin-key": _session_key()}
    payload.update(extra or {})
    return _api_post(path, payload)


# ---------------------------------------------------------------------------
# Sessions and access control
# ---------------------------------------------------------------------------

def _session_key():
    """The admin key of the current browser session, if any."""
    sid = session.get("sid")
    if not sid:
        return None
    with _sessions_lock:
        return _sessions.get(sid)


def _store_key(key: str) -> None:
    sid = secrets.token_urlsafe(24)
    with _sessions_lock:
        _sessions[sid] = key
    session["sid"] = sid
    session["csrf"] = secrets.token_urlsafe(24)


def _drop_session() -> None:
    sid = session.get("sid")
    if sid:
        with _sessions_lock:
            _sessions.pop(sid, None)
    session.clear()


def _auth_failed():
    """Handle a key the service stopped accepting mid session."""
    _drop_session()
    flash("the admin key was rejected; log in again", "error")
    return redirect(url_for("login"))


def login_required(func):
    """Send anonymous visitors to the login form."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not _session_key():
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


@app.before_request
def _require_csrf():
    if request.method != "POST" or request.endpoint in _CSRF_EXEMPT:
        return None
    expected = session.get("csrf", "")
    sent = request.form.get("csrf", "")
    if expected and secrets.compare_digest(expected, sent):
        return None
    return "invalid CSRF token", 400


@app.context_processor
def _inject_globals():
    return {"csrf_token": session.get("csrf", ""),
            "logged_in": bool(_session_key()),
            "base_url": BASE_URL}


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def _mask_key(value, keep: int = 4) -> str:
    """
    Shorten a secret for display, keeping only its ends.

    /conf/info/clients hands back every client key in the clear, and
    those keys are enough to reserve DUTs, so no view renders one
    verbatim without being asked.
    """
    text = str(value or "")
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}...{text[-keep:]}"


app.jinja_env.filters["mask"] = _mask_key


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _try_login(raw_key):
    """
    Validate a submitted key against the smallest admin endpoint.

    Returns a response to render on failure, or None once the key is
    stored.
    """
    key = (raw_key or "").strip()
    if not key:
        flash("an admin key is required", "error")
        return render_template("login.html"), 400
    result = _api_post("/conf/info/processes", {"admin-key": key})
    if not result["ok"]:
        flash(result["error"], "error")
        return render_template("login.html"), 401 if result["auth"] else 502
    _store_key(key)
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        failure = _try_login(request.form.get("admin-key"))
        if failure is not None:
            return failure
        return redirect(url_for("dashboard"))
    return render_template("login.html")


def _fetch_list(path: str, extra: dict = None):
    """
    Read a list endpoint.

    Returns (rows, failure). `failure` is a response to return as is
    when the session must end; a reachable service that answered with
    an error yields empty rows and a flashed message instead, so the
    page still renders its navigation.
    """
    result = _admin_post(path, extra)
    if result["auth"]:
        return [], _auth_failed()
    if not result["ok"]:
        flash(result["error"], "error")
        return [], None
    return result["data"] or [], None


@app.route("/nodes")
@login_required
def nodes():
    rows, failure = _fetch_list("/conf/info/nodes")
    if failure is not None:
        return failure
    return render_template("nodes.html", nodes=rows)


@app.route("/clients")
@login_required
def clients():
    rows, failure = _fetch_list("/conf/info/clients")
    if failure is not None:
        return failure
    return render_template("clients.html", clients=rows)


@app.route("/processes")
@login_required
def processes():
    rows, failure = _fetch_list("/conf/info/processes")
    if failure is not None:
        return failure
    return render_template("processes.html", processes=rows)


@app.route("/logout", methods=["POST"])
def logout():
    _drop_session()
    flash("logged out", "info")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dut-control-webadmin",
        description="Web admin UI for the dut-control service",
    )
    p.add_argument(
        "-u",
        "--url",
        default=DEFAULT_BASE_URL,
        help=(
            "Base URL of the dut-control service "
            "(default: %(default)s or env DUT_CONTROL_URL)"
        ),
    )
    p.add_argument(
        "--host",
        default="0.0.0.0",
        help="Address to listen on (default: %(default)s)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: %(default)s)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds for API calls (default: %(default)s)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    global BASE_URL, TIMEOUT

    args = build_parser().parse_args(argv)
    BASE_URL = args.url
    TIMEOUT = args.timeout

    if not os.environ.get(SECRET_ENV):
        print(f"warning: {SECRET_ENV} is not set, so logins do not "
              "survive a restart", file=sys.stderr)

    # debug stays off on purpose: the Werkzeug debugger would expose an
    # interactive console on a service that holds the admin key.
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

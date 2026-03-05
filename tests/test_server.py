#!/usr/bin/env python3

import dut_control.server as server_mod
import pytest
import time
import sys
from pathlib import Path

# Ensure project root (containing dut_control/) is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_state():
    """Reset mutable global state before & after each test."""
    with server_mod.state_lock:
        server_mod.reserves.clear()
        server_mod.processes.clear()
    yield
    with server_mod.state_lock:
        server_mod.reserves.clear()
        server_mod.processes.clear()


@pytest.fixture
def flask_client():
    """Flask test client for calling endpoints."""
    return server_mod.server.test_client()


def _make_client(
    name="client-01",
    key="client-key-01",
    ip="192.0.2.10",
    port=22,
    user="tester",
    port_from=5000,
    port_to=5005,
):
    return {
        "name": name,
        "key": key,
        "ssh": {
            "ip": ip,
            "port": port,
            "user": user,
        },
        "ports-range": {
            "from": port_from,
            "to": port_to,
        },
    }


def _make_node_dut(
    node_name="node-01",
    node_ip="192.0.2.20",
    node_port=22,
    node_user="runner",
    dut_name="dut-01",
    pool="pool-01",
    dut_ip="192.0.2.30",
    dut_ssh_port=22,
):
    node = {
        "name": node_name,
        "ssh": {
            "ip": node_ip,
            "port": node_port,
            "user": node_user,
        },
        "duts": [
            {
                "name": dut_name,
                "metadata": {"pool": pool},
                "network": {
                    "ip": dut_ip,
                    "ssh-port": dut_ssh_port,
                },
                "storage": {},
                "power": {},
            }
        ],
    }
    return node, node["duts"][0]


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------

def test_normalize_section_list_and_dict():
    # YAML-style list of single-key dicts
    value = [
        {"ip": "192.168.1.1"},
        {"port": 22},
        {"user": "root"},
    ]
    out = server_mod._normalize_section(value)
    assert out == {"ip": "192.168.1.1", "port": 22, "user": "root"}

    # Plain dict is returned as-is
    d = {"foo": "bar"}
    assert server_mod._normalize_section(d) is d

    # Other types -> empty dict
    assert server_mod._normalize_section("x") == {}


def test_find_free_port_for_client():
    client = _make_client(port_from=6000, port_to=6002)

    # No processes yet -> first port in range
    port = server_mod._find_free_port_for_client(client)
    assert port == 6000

    # Simulate one process using 6000
    with server_mod.state_lock:
        server_mod.processes.append(
            {
                "client-name": client["name"],
                "ports-in-use": [6000],
            }
        )

    port = server_mod._find_free_port_for_client(client)
    assert port == 6001

    # Mark all ports used -> None
    with server_mod.state_lock:
        server_mod.processes.append(
            {
                "client-name": client["name"],
                "ports-in-use": [6001, 6002],
            }
        )
    assert server_mod._find_free_port_for_client(client) is None


# ---------------------------------------------------------------------------
# Decorator / validation tests
# ---------------------------------------------------------------------------

def test_validate_client_missing_key(flask_client):
    resp = flask_client.post("/reserve", json={})
    data = resp.get_json()
    assert data["status"] == -1
    assert "client-key missing" in data["error"]


def test_validate_client_invalid_key(flask_client):
    # No clients configured -> invalid client-key
    resp = flask_client.post(
        "/reserve",
        json={
            "client-key": "unknown",
            "pool": "x"})
    data = resp.get_json()
    assert data["status"] == -1
    assert "client key is not valid" in data["error"]


def test_validate_pool_missing_pool(flask_client):
    # Need a valid client to get past validate_client
    client = _make_client()
    with server_mod.state_lock:
        server_mod.clients[:] = [client]

    resp = flask_client.post("/reserve", json={"client-key": client["key"]})
    data = resp.get_json()
    assert data["status"] == -2
    assert "pool missing" in data["error"]


def test_validate_token_missing_token(flask_client):
    resp = flask_client.post("/power/on", json={})
    data = resp.get_json()
    assert data["status"] == -1
    assert "token missing" in data["error"]


# ---------------------------------------------------------------------------
# /reserve endpoint
# ---------------------------------------------------------------------------

def test_reserve_success(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    started = {}

    def fake_start_ssh_tunnel(c, d, remote_port, token):
        # Do not spawn real ssh; just record parameters and append a fake entry
        entry = {
            "pid": 12345,
            "reserve-token": token,
            "client-name": c["name"],
            "ports-in-use": [remote_port],
            "process": None,
        }
        with server_mod.state_lock:
            server_mod.processes.append(entry)
        started.update(
            dict(
                client=c,
                dut=d,
                remote_port=remote_port,
                token=token,
            )
        )
        return entry

    monkeypatch.setattr(server_mod, "_start_ssh_tunnel", fake_start_ssh_tunnel)

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-01"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == 0
    assert "token" in data
    assert "ssh-port" in data

    # Reservation stored
    with server_mod.state_lock:
        assert len(server_mod.reserves) == 1
        r = server_mod.reserves[0]
        assert r["token"] == data["token"]
        assert r["dut-name"] == dut["name"]
        assert r["client-key"] == client["key"]

        # Tunnel started with same token / port
        assert started["token"] == data["token"]
        assert started["remote_port"] == data["ssh-port"]
        assert len(server_mod.processes) == 1


def test_reserve_all_duts_in_use(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

        now = int(time.time())
        # Single reservation already valid for that DUT
        server_mod.reserves.append(
            {
                "token": "t1",
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

    fake_start_called = False

    def fake_start_ssh_tunnel(*args, **kwargs):
        nonlocal fake_start_called
        fake_start_called = True

    monkeypatch.setattr(server_mod, "_start_ssh_tunnel", fake_start_ssh_tunnel)

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-01"},
    )
    data = resp.get_json()
    assert data["status"] == -4
    assert "all duts in use already" in data["error"]
    assert fake_start_called is False


def test_reserve_no_free_ports(flask_client, monkeypatch):
    client = _make_client(port_from=6000, port_to=6000)
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        # Mark the only port as already in use
        server_mod.processes.append(
            {
                "client-name": client["name"],
                "ports-in-use": [6000],
            }
        )

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-01"},
    )
    data = resp.get_json()
    assert data["status"] == -4
    assert "no free ports for client" in data["error"]


# ---------------------------------------------------------------------------
# /lease endpoint
# ---------------------------------------------------------------------------

def test_lease_release_by_token(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-123"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )
        server_mod.processes.append(
            {
                "reserve-token": token,
                "client-name": client["name"],
                "ports-in-use": [5000],
                "process": None,
            }
        )

    # Avoid real process killing
    monkeypatch.setattr(server_mod, "_stop_process_entry", lambda entry: None)

    resp = flask_client.post(
        "/lease",
        json={"client-key": client["key"], "token": token},
    )
    data = resp.get_json()
    assert data["status"] == 0

    with server_mod.state_lock:
        # Processes removed
        assert len(server_mod.processes) == 0
        # Reserve expired (valid-until == now or earlier)
        assert server_mod.reserves[0]["valid-until"] <= int(time.time())


def test_lease_nothing_to_release(flask_client):
    client = _make_client()
    with server_mod.state_lock:
        server_mod.clients[:] = [client]

    # No reserves -> status 0 but nothing changed
    resp = flask_client.post(
        "/lease",
        json={"client-key": client["key"], "token": "non-existent"},
    )
    data = resp.get_json()
    assert data["status"] == 0


# ---------------------------------------------------------------------------
# /power endpoint
# ---------------------------------------------------------------------------

def test_power_invalid_action(flask_client):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-xxx"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

    resp = flask_client.post("/power/invalid", json={"token": token})
    data = resp.get_json()
    assert data["status"] == -99
    assert "invalid action" in data["error"]


def test_power_on_success(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-yyy"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

        # Add power info to DUT
        dut["power"] = {
            "power-on": "echo on",
            "power-off": "echo off",
        }

    called = {"script": None}

    def fake_run_remote_power_script(node_arg, script):
        called["script"] = script
        return True

    monkeypatch.setattr(
        server_mod,
        "_run_remote_power_script",
        fake_run_remote_power_script)

    resp = flask_client.post("/power/on", json={"token": token})
    data = resp.get_json()
    assert data["status"] == 0
    assert called["script"] == "echo on"


# ---------------------------------------------------------------------------
# /flash endpoint
# ---------------------------------------------------------------------------

def test_flash_missing_path(flask_client):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-flash"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

    resp = flask_client.post("/flash", json={"token": token})
    data = resp.get_json()
    assert data["status"] == -99
    assert "path missing" in data["error"]


def test_flash_success(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-flash-ok"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

    called = {"args": None}

    def fake_flash_image(node_arg, dut_arg, client_arg, client_path):
        called["args"] = (node_arg, dut_arg, client_arg, client_path)

    monkeypatch.setattr(server_mod, "_flash_image", fake_flash_image)

    resp = flask_client.post(
        "/flash",
        json={"token": token, "path": "/remote/image.wic"},
    )
    data = resp.get_json()
    assert data["status"] == 0
    assert called["args"][3] == "/remote/image.wic"


# ---------------------------------------------------------------------------
# /dut/status endpoint
# ---------------------------------------------------------------------------

def test_dut_status_offline(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-status-offline"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

    monkeypatch.setattr(server_mod, "_ping_host", lambda ip,
                        timeout_sec=1: False)

    resp = flask_client.post("/dut/status", json={"token": token})
    data = resp.get_json()
    assert data["status"] == "offline"


def test_dut_status_ping_vs_ssh(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-status-ssh"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        now = int(time.time())
        server_mod.reserves.append(
            {
                "token": token,
                "valid-from": now - 10,
                "valid-until": now + 3600,
                "client-key": client["key"],
                "dut-name": dut["name"],
            }
        )

    # Case 1: ssh reachable
    monkeypatch.setattr(server_mod, "_ping_host", lambda ip,
                        timeout_sec=1: True)
    monkeypatch.setattr(
        server_mod,
        "_check_ssh",
        lambda ip,
        port,
        timeout_sec=2.0: True)

    resp = flask_client.post("/dut/status", json={"token": token})
    data = resp.get_json()
    assert data["status"] == "ssh"

    # Case 2: ping only
    monkeypatch.setattr(
        server_mod,
        "_check_ssh",
        lambda ip,
        port,
        timeout_sec=2.0: False)
    resp = flask_client.post("/dut/status", json={"token": token})
    data = resp.get_json()
    assert data["status"] == "ping"

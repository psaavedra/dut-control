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
    assert "ip" in data
    assert "ssh-port" in data
    assert "tunnel-ssh-port" in data

    # Reservation stored
    with server_mod.state_lock:
        assert len(server_mod.reserves) == 1
        r = server_mod.reserves[0]
        assert r["token"] == data["token"]
        assert r["dut-name"] == dut["name"]
        assert r["client-key"] == client["key"]

        # Tunnel started with same token / port
        assert started["token"] == data["token"]
        assert started["remote_port"] == data["tunnel-ssh-port"]
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


def test_reserve_picks_random_dut_from_pool(flask_client, monkeypatch):
    client = _make_client()
    node = {
        "name": "node-01",
        "ssh": {"ip": "192.0.2.20", "port": 22, "user": "runner"},
        "duts": [
            {
                "name": "dut-a",
                "metadata": {"pool": "pool-01", "enabled": True},
                "network": {"ip": "192.0.2.30", "ssh-port": 22},
                "storage": {},
                "power": {},
            },
            {
                "name": "dut-b",
                "metadata": {"pool": "pool-01", "enabled": True},
                "network": {"ip": "192.0.2.31", "ssh-port": 22},
                "storage": {},
                "power": {},
            },
        ],
    }

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    picked = {}

    def fake_choice(seq):
        picked["candidates"] = [dut["name"] for _, dut in seq]
        return seq[1]  # deliberately not the first entry

    monkeypatch.setattr(server_mod.random, "choice", fake_choice)

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-01"},
    )
    data = resp.get_json()
    assert data["status"] == 0

    # Selection went through random.choice() over all available DUTs...
    assert sorted(picked["candidates"]) == ["dut-a", "dut-b"]
    # ...and the reservation reflects whatever it returned, not always [0]
    with server_mod.state_lock:
        assert server_mod.reserves[0]["dut-name"] == "dut-b"


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


def test_power_on_off_also_switch_sd_card(flask_client, monkeypatch):
    """Power on/off run the power script and then point the SD mux to
    dut/off when the DUT has one; DUTs without one skip the usbsdmux
    call."""
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-off"

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
        dut["power"] = {"power-on": "echo on", "power-off": "echo off"}
        dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}

    node_cmds = []

    monkeypatch.setattr(
        server_mod, "_run_remote_power_script", lambda n, s: True)
    monkeypatch.setattr(
        server_mod, "_run_node_command",
        lambda n, c: node_cmds.append(c) or True)

    resp = flask_client.post("/power/off", json={"token": token})
    assert resp.get_json()["status"] == 0
    assert node_cmds == ["usbsdmux /dev/sg1 off"]

    node_cmds.clear()
    resp = flask_client.post("/power/on", json={"token": token})
    assert resp.get_json()["status"] == 0
    assert node_cmds == ["usbsdmux /dev/sg1 dut"]

    # Without a storage mux, no usbsdmux command is issued
    node_cmds.clear()
    with server_mod.state_lock:
        dut["storage"] = {}
    for action in ("on", "off"):
        resp = flask_client.post(f"/power/{action}", json={"token": token})
        assert resp.get_json()["status"] == 0
    assert node_cmds == []

    # A mux switch failure is reported distinctly from a script failure
    with server_mod.state_lock:
        dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    monkeypatch.setattr(server_mod, "_run_node_command", lambda n, c: False)
    resp = flask_client.post("/power/off", json={"token": token})
    data = resp.get_json()
    assert data["status"] == -99
    assert data["error"] == "usbsdmux switch to off failed"


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


def test_flash_image_uses_unique_node_tmp_path(monkeypatch):
    """Two concurrent-ish flashes of the same file name must not share a
    node temp path, so one cannot overwrite the other in flight."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    scp_to_node_targets = []
    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "scp" and "@" in cmd[-1] and ":" in cmd[-1]:
            scp_to_node_targets.append(cmd[-1].split(":", 1)[1])
        elif cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
        return server_mod.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(node, dut, client, "/remote/image.wic")
    server_mod._flash_image(node, dut, client, "/remote/image.wic")

    # Same source image name, but each call gets a distinct node tmp path
    assert len(scp_to_node_targets) == 2
    assert scp_to_node_targets[0] != scp_to_node_targets[1]

    # bmaptool referenced the matching per-call path, and each was cleaned
    # up afterwards
    bmaptool_cmds = [c for c in ssh_cmds if "bmaptool" in c]
    rm_cmds = [c for c in ssh_cmds if c.startswith("rm -f ")]
    assert len(bmaptool_cmds) == 2
    assert len(rm_cmds) == 2
    for target, bmaptool_cmd, rm_cmd in zip(
            scp_to_node_targets, bmaptool_cmds, rm_cmds):
        assert target in bmaptool_cmd
        assert rm_cmd == f"rm -f {target}"


def test_flash_image_quotes_awkward_image_names(monkeypatch):
    """An image basename with spaces/metacharacters must reach the node
    shell as one quoted argument, in scp, bmaptool, verify and rm."""
    import shlex

    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    scp_remote_specs = []
    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "scp":
            scp_remote_specs.extend(
                a for a in cmd if "@" in a and ":" in a)
        elif cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
        return server_mod.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(node, dut, client, "/remote/im age;$(x).wic")

    # scp remote paths are shell-quoted (client source and node target)
    assert len(scp_remote_specs) == 2
    for spec in scp_remote_specs:
        assert spec.split(":", 1)[1].startswith("'")

    # Every node command referencing the image uses the quoted tmp path
    node_tmp = scp_remote_specs[1].split(":", 1)[1]
    quoted = shlex.quote(shlex.split(node_tmp)[0])
    assert quoted == node_tmp
    for marker in ("bmaptool", "sha256sum", "rm -f"):
        cmds = [c for c in ssh_cmds if marker in c]
        assert cmds, f"no ssh command for {marker}"
        assert all(quoted in c for c in cmds)


def test_flash_image_verifies_device_content(monkeypatch):
    """After bmaptool, the device is read back and checksummed against
    the image before the mux is handed back to the DUT."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
        return server_mod.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(node, dut, client, "/remote/image.wic")

    verify_cmds = [c for c in ssh_cmds if "sha256sum" in c]
    assert len(verify_cmds) == 1
    assert "/dev/sda1" in verify_cmds[0]

    # Verification happens between the flash and the switch back to dut
    flash_idx = next(i for i, c in enumerate(ssh_cmds) if "bmaptool" in c)
    verify_idx = ssh_cmds.index(verify_cmds[0])
    dut_idx = ssh_cmds.index("usbsdmux /dev/sg1 dut")
    assert flash_idx < verify_idx < dut_idx


def test_flash_image_raises_when_verification_fails(monkeypatch):
    """A checksum mismatch aborts the flash, but the mux is still handed
    back to the DUT and the node temp file is cleaned up."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        rc = 0
        if cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
            if "sha256sum" in cmd[-1]:
                rc = 1
        return server_mod.subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="verification failed"):
        server_mod._flash_image(node, dut, client, "/remote/image.wic")

    assert "usbsdmux /dev/sg1 dut" in ssh_cmds
    assert any(c.startswith("rm -f ") for c in ssh_cmds)


def test_flash_image_switch_back_failure_takes_precedence(monkeypatch):
    """When verification and the mux switch-back both fail, the error
    reports the stuck mux first (it needs operator action) but still
    mentions the verification failure."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    def fake_run(cmd, **kwargs):
        rc = 0
        if cmd[0] == "ssh" and (
                "sha256sum" in cmd[-1] or cmd[-1].endswith(" dut")):
            rc = 1
        return server_mod.subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    with pytest.raises(
            RuntimeError,
            match="switch storage back to dut.*verification also failed"):
        server_mod._flash_image(node, dut, client, "/remote/image.wic")


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


# ---------------------------------------------------------------------------
# DUT "enabled" metadata
# ---------------------------------------------------------------------------

def _write_node_yaml(nodes_dir, extra_metadata_lines=""):
    nodes_dir.mkdir(exist_ok=True)
    (nodes_dir / "node-01.yml").write_text(
        f"""
- name: node-01
  ssh:
    - ip: 192.0.2.1
    - port: 22
    - user: runner
  duts:
    - name: dut-01
      metadata:
        - pool: pool-01
{extra_metadata_lines}
      network:
        - ip: 192.0.2.2
        - ssh-port: 22
"""
    )


def test_load_nodes_defaults_enabled_true(tmp_path):
    _write_node_yaml(tmp_path / "nodes")

    loaded = server_mod._load_nodes(tmp_path)
    assert loaded[0]["duts"][0]["metadata"]["enabled"] is True


def test_load_nodes_respects_explicit_enabled_false(tmp_path):
    _write_node_yaml(tmp_path / "nodes", "        - enabled: false")

    loaded = server_mod._load_nodes(tmp_path)
    assert loaded[0]["duts"][0]["metadata"]["enabled"] is False


def test_dut_enabled_defaults_true_when_missing():
    dut = {"metadata": {"pool": "pool-01"}}
    assert server_mod._dut_enabled(dut) is True


def test_dut_enabled_respects_false():
    dut = {"metadata": {"pool": "pool-01", "enabled": False}}
    assert server_mod._dut_enabled(dut) is False


def test_pool_exists_ignores_disabled_duts():
    node, dut = _make_node_dut(pool="pool-01")
    dut["metadata"]["enabled"] = False

    with server_mod.state_lock:
        server_mod.nodes[:] = [node]

    assert server_mod._pool_exists("pool-01") is False
    assert server_mod._list_duts_in_pool("pool-01") == []


def test_list_duts_in_pool_excludes_disabled_but_keeps_enabled():
    node = {
        "name": "node-01",
        "ssh": {"ip": "192.0.2.20", "port": 22, "user": "runner"},
        "duts": [
            {
                "name": "dut-enabled",
                "metadata": {"pool": "pool-01", "enabled": True},
                "network": {"ip": "192.0.2.30", "ssh-port": 22},
                "storage": {},
                "power": {},
            },
            {
                "name": "dut-disabled",
                "metadata": {"pool": "pool-01", "enabled": False},
                "network": {"ip": "192.0.2.31", "ssh-port": 22},
                "storage": {},
                "power": {},
            },
        ],
    }

    with server_mod.state_lock:
        server_mod.nodes[:] = [node]

    result = server_mod._list_duts_in_pool("pool-01")
    assert [dut["name"] for _, dut in result] == ["dut-enabled"]


def test_reserve_skips_disabled_dut(flask_client, monkeypatch):
    client = _make_client()
    node = {
        "name": "node-01",
        "ssh": {"ip": "192.0.2.20", "port": 22, "user": "runner"},
        "duts": [
            {
                "name": "dut-disabled",
                "metadata": {"pool": "pool-01", "enabled": False},
                "network": {"ip": "192.0.2.31", "ssh-port": 22},
                "storage": {},
                "power": {},
            },
            {
                "name": "dut-enabled",
                "metadata": {"pool": "pool-01", "enabled": True},
                "network": {"ip": "192.0.2.30", "ssh-port": 22},
                "storage": {},
                "power": {},
            },
        ],
    }

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-01"},
    )
    data = resp.get_json()
    assert data["status"] == 0

    with server_mod.state_lock:
        assert server_mod.reserves[0]["dut-name"] == "dut-enabled"


def test_reserve_fails_when_all_duts_in_pool_disabled(flask_client):
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    dut["metadata"]["enabled"] = False

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-01"},
    )
    data = resp.get_json()
    assert data["status"] == -2
    assert "pool does not exist" in data["error"]

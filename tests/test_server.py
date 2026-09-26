#!/usr/bin/env python3

import concurrent.futures
import threading

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


def _is_bmap_scp(cmd) -> bool:
    """True for the scp call probing for the image's optional .bmap."""
    return cmd[0] == "scp" and any(".bmap" in a for a in cmd)


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
# /pools endpoint
# ---------------------------------------------------------------------------

def _make_pool_dut(name, pool, enabled=True):
    return {
        "name": name,
        "metadata": {"pool": pool, "enabled": enabled},
        "network": {"ip": "192.0.2.30", "ssh-port": 22},
        "storage": {},
        "power": {},
    }


def test_pools_counts_enabled_and_free_duts(flask_client):
    """Each pool reports its enabled DUTs and how many are unreserved;
    pools whose DUTs are all disabled are not reservable, so they are
    left out entirely."""
    client = _make_client()
    node = {
        "name": "node-01",
        "ssh": {"ip": "192.0.2.20", "port": 22, "user": "runner"},
        "duts": [
            _make_pool_dut("rpi5-01", "rpi5"),
            _make_pool_dut("rpi5-02", "rpi5"),
            _make_pool_dut("rpi5-03", "rpi5", enabled=False),
            _make_pool_dut("rpi4-01", "rpi4"),
            _make_pool_dut("old-01", "retired", enabled=False),
        ],
    }

    now = int(time.time())
    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
        # One active reservation and one already expired
        server_mod.reserves.extend([
            {"token": "t1", "valid-from": now - 10, "valid-until": now + 600,
             "client-key": client["key"], "dut-name": "rpi5-01"},
            {"token": "t2", "valid-from": now - 100, "valid-until": now - 10,
             "client-key": client["key"], "dut-name": "rpi4-01"},
        ])

    resp = flask_client.post("/pools", json={"client-key": client["key"]})
    data = resp.get_json()
    assert data["status"] == 0
    # Sorted by pool name; the fully disabled pool is absent
    assert data["pools"] == [
        {"name": "rpi4", "enabled-duts": 1, "free-duts": 1},
        {"name": "rpi5", "enabled-duts": 2, "free-duts": 1},
    ]


def test_list_pools_reads_one_locked_snapshot(monkeypatch):
    """The reservation set and the node walk must come from a single
    state_lock acquisition; taken separately, the counts could mix two
    states and report a total that never held."""
    with server_mod.state_lock:
        server_mod.nodes[:] = [{
            "name": "node-01",
            "ssh": {"ip": "192.0.2.20"},
            "duts": [_make_pool_dut("rpi5-01", "rpi5")],
        }]

    real_reserved_dut_names = server_mod._reserved_dut_names
    locked_during_call = []

    def probing_reserved_dut_names(now):
        # Another thread cannot take the lock while this runs, so the
        # non-blocking acquire must fail. Probing from this thread
        # would always succeed: state_lock is reentrant.
        def probe():
            acquired = server_mod.state_lock.acquire(blocking=False)
            locked_during_call.append(not acquired)
            if acquired:
                server_mod.state_lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return real_reserved_dut_names(now)

    monkeypatch.setattr(
        server_mod, "_reserved_dut_names", probing_reserved_dut_names)

    assert server_mod._list_pools() == [
        {"name": "rpi5", "enabled-duts": 1, "free-duts": 1},
    ]
    assert locked_during_call == [True]


def test_pools_requires_valid_client_key(flask_client):
    with server_mod.state_lock:
        server_mod.clients[:] = [_make_client()]

    resp = flask_client.post("/pools", json={"client-key": "nope"})
    data = resp.get_json()
    assert data["status"] == -1
    assert "client key is not valid" in data["error"]


def test_pools_empty_when_no_duts_configured(flask_client):
    client = _make_client()
    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = []

    resp = flask_client.post("/pools", json={"client-key": client["key"]})
    data = resp.get_json()
    assert data["status"] == 0
    assert data["pools"] == []


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
    assert data["dut-name"] == dut["name"]
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


def test_flash_error_is_not_double_prefixed(flask_client, monkeypatch):
    """The endpoint returns the pipeline error verbatim; the CLI adds its
    own "flash failed" prefix, so the server must not add one too."""
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-flash-err"

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

    def fake_flash_image(node_arg, dut_arg, client_arg, client_path):
        raise RuntimeError("flash command failed on node")

    monkeypatch.setattr(server_mod, "_flash_image", fake_flash_image)

    resp = flask_client.post(
        "/flash",
        json={"token": token, "path": "/remote/image.wic"},
    )
    data = resp.get_json()
    assert data["status"] == -99
    assert data["error"] == "flash command failed on node"


def test_flash_image_uses_unique_node_tmp_path(monkeypatch):
    """Two concurrent-ish flashes of the same file name must not share a
    node temp path, so one cannot overwrite the other in flight."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    scp_to_node_targets = []
    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        if _is_bmap_scp(cmd):
            # No bmap next to this image; exercise the --nobmap flow
            return server_mod.subprocess.CompletedProcess(cmd, 1)
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
        rc = 0
        if cmd[0] == "scp":
            scp_remote_specs.extend(
                a for a in cmd if "@" in a and ":" in a)
            if _is_bmap_scp(cmd):
                rc = 1  # no bmap next to this image
        elif cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
        return server_mod.subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(node, dut, client, "/remote/im age;$(x).wic")

    # scp remote paths are shell-quoted: the image (client source and
    # node target) and the bmap probe derived from its name
    image_specs = [s for s in scp_remote_specs if ".bmap" not in s]
    bmap_specs = [s for s in scp_remote_specs if ".bmap" in s]
    assert len(image_specs) == 2
    assert len(bmap_specs) == 1
    for spec in scp_remote_specs:
        assert spec.split(":", 1)[1].startswith("'")

    # Every node command referencing the image uses the quoted tmp path
    node_tmp = image_specs[1].split(":", 1)[1]
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
        if _is_bmap_scp(cmd):
            return server_mod.subprocess.CompletedProcess(cmd, 1)
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
        if _is_bmap_scp(cmd):
            return server_mod.subprocess.CompletedProcess(cmd, 1)
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


def test_flash_verify_command_checksums_decompressed_stream():
    """bmaptool decompresses compressed images while writing, so the
    verify command must size and checksum the decompressed bytes, not
    the compressed file (which would never match the device)."""
    cmd = server_mod._flash_verify_command(
        "/tmp/dut-flash-x-core-image.wic.bz2", "/dev/sda1")
    assert cmd.count("bzip2 -dc") == 2  # one pass for size, one for sha
    assert "wc -c" in cmd
    assert "stat -c" not in cmd

    # Compressed tarballs are unpacked to the contained image stream
    cmd = server_mod._flash_verify_command(
        "/tmp/dut-flash-x-rootfs.tar.xz", "/dev/sda1")
    assert "xz -dc" in cmd
    assert "tar -xO" in cmd

    # Raw images keep the cheap stat/sha256sum on the file itself
    cmd = server_mod._flash_verify_command(
        "/tmp/dut-flash-x-core-image.wic", "/dev/sda1")
    assert "stat -c %s" in cmd
    assert " -dc" not in cmd


def test_bmap_path_for_image():
    """The bmap name drops the compression suffix and appends .bmap."""
    assert server_mod._bmap_path_for_image(
        "/home/dutctl/core-image-weston-wpe-raspberrypi5-0.wic.bz2"
    ) == "/home/dutctl/core-image-weston-wpe-raspberrypi5-0.wic.bmap"

    # Uncompressed images keep their name and gain .bmap
    assert server_mod._bmap_path_for_image(
        "/images/core-image.wic") == "/images/core-image.wic.bmap"

    # Other compressors behave the same
    assert server_mod._bmap_path_for_image(
        "/images/core-image.wic.gz") == "/images/core-image.wic.bmap"

    # The image inside a tarball is not derivable, so no bmap is guessed
    assert server_mod._bmap_path_for_image("/images/rootfs.tar.xz") is None


def test_flash_image_uses_bmap_when_present(monkeypatch):
    """When a .bmap sits next to the image it is staged to the node and
    passed to bmaptool, and the whole-image read-back is skipped since
    bmaptool only writes the mapped blocks."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    scp_specs = []
    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "scp":
            scp_specs.extend(a for a in cmd if "@" in a and ":" in a)
        elif cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
        return server_mod.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(
        node, dut, client, "/home/dutctl/core-image.wic.bz2")

    # The bmap was fetched from the client and pushed to the node
    bmap_specs = [s for s in scp_specs if s.endswith(".bmap")]
    assert len(bmap_specs) == 2
    assert bmap_specs[0].endswith("/home/dutctl/core-image.wic.bmap")
    node_bmap = bmap_specs[1].split(":", 1)[1]

    # bmaptool got --bmap pointing at the staged copy, not --nobmap
    bmaptool_cmds = [c for c in ssh_cmds if "bmaptool" in c]
    assert len(bmaptool_cmds) == 1
    assert f"--bmap {node_bmap}" in bmaptool_cmds[0]
    assert "--nobmap" not in bmaptool_cmds[0]

    # Read-back verification is skipped, mux is still handed back, and
    # both staged files are removed from the node
    assert not [c for c in ssh_cmds if "sha256sum" in c]
    assert "usbsdmux /dev/sg1 dut" in ssh_cmds
    rm_cmds = [c for c in ssh_cmds if c.startswith("rm -f ")]
    assert len(rm_cmds) == 1
    assert node_bmap in rm_cmds[0]
    assert "core-image.wic.bz2" in rm_cmds[0]


def test_flash_image_falls_back_to_nobmap_when_absent(monkeypatch):
    """A missing bmap must not fail the flash: bmaptool runs with
    --nobmap and the read-back verification still guards the write."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    ssh_cmds = []

    def fake_run(cmd, **kwargs):
        if _is_bmap_scp(cmd):
            return server_mod.subprocess.CompletedProcess(cmd, 1)
        if cmd[0] == "ssh":
            ssh_cmds.append(cmd[-1])
        return server_mod.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(
        node, dut, client, "/home/dutctl/core-image.wic.bz2")

    bmaptool_cmds = [c for c in ssh_cmds if "bmaptool" in c]
    assert len(bmaptool_cmds) == 1
    assert "--nobmap" in bmaptool_cmds[0]
    assert len([c for c in ssh_cmds if "sha256sum" in c]) == 1

    # Nothing bmap-related is left to clean up on the node
    rm_cmds = [c for c in ssh_cmds if c.startswith("rm -f ")]
    assert len(rm_cmds) == 1
    assert ".bmap" not in rm_cmds[0]


def test_flash_image_removes_tmpdir_after_partial_bmap_fetch(monkeypatch):
    """A failed bmap fetch can leave a partial file in the temp dir; the
    directory must still be removed rather than leaking under /tmp."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    tmpdirs = []

    def fake_run(cmd, **kwargs):
        dest = cmd[-1]
        if cmd[0] == "scp" and "@" not in dest:
            # Local destination: scp writes something even when it then
            # fails partway through the transfer
            Path(dest).touch()
            if _is_bmap_scp(cmd):
                tmpdirs.append(str(Path(dest).parent))
                return server_mod.subprocess.CompletedProcess(cmd, 1)
        return server_mod.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._flash_image(
        node, dut, client, "/home/dutctl/core-image.wic.bz2")

    assert tmpdirs, "the bmap fetch was never attempted"
    for tmpdir in tmpdirs:
        assert not Path(tmpdir).exists(), f"{tmpdir} leaked"


def test_flash_image_switch_back_failure_takes_precedence(monkeypatch):
    """When verification and the mux switch-back both fail, the error
    reports the stuck mux first (it needs operator action) but still
    mentions the verification failure."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    def fake_run(cmd, **kwargs):
        rc = 0
        if _is_bmap_scp(cmd):
            return server_mod.subprocess.CompletedProcess(cmd, 1)
        if cmd[0] == "ssh" and (
                "sha256sum" in cmd[-1] or cmd[-1].endswith(" dut")):
            rc = 1
        return server_mod.subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    with pytest.raises(
            RuntimeError,
            match="switch storage back to dut.*verification also failed"):
        server_mod._flash_image(node, dut, client, "/remote/image.wic")


def test_flash_and_verify_on_node_serializes_same_node(monkeypatch):
    """Concurrent flashes targeting the same node must not run their
    node commands at the same time; the second waits for the first."""
    node = {"name": "node-serialize-test", "ssh": {"ip": "192.0.2.20"}}
    guard = threading.Lock()
    active = 0
    max_active = 0

    def fake_run_node_command(n, cmd):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return True

    monkeypatch.setattr(
        server_mod, "_run_node_command", fake_run_node_command)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                server_mod._flash_and_verify_on_node,
                node, "/dev/sg1", "/dev/sda1", f"/tmp/img-{i}")
            for i in range(3)
        ]
        for f in futures:
            f.result(timeout=5)

    assert max_active == 1


def test_flash_and_verify_on_node_different_nodes_not_serialized(
        monkeypatch):
    """The per-node lock must not serialize flashes across different
    nodes: both must be able to make progress at the same time."""
    node_a = {"name": "node-a-concurrent", "ssh": {"ip": "192.0.2.21"}}
    node_b = {"name": "node-b-concurrent", "ssh": {"ip": "192.0.2.22"}}
    barrier = threading.Barrier(2, timeout=2)

    def fake_run_node_command(n, cmd):
        # Only succeeds if both nodes' flashes are in flight together;
        # a wrongful global serialization would starve this and time out.
        barrier.wait()
        return True

    monkeypatch.setattr(
        server_mod, "_run_node_command", fake_run_node_command)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_a = pool.submit(
            server_mod._flash_and_verify_on_node,
            node_a, "/dev/sg1", "/dev/sda1", "/tmp/img")
        f_b = pool.submit(
            server_mod._flash_and_verify_on_node,
            node_b, "/dev/sg2", "/dev/sda2", "/tmp/img")
        f_a.result(timeout=5)
        f_b.result(timeout=5)


def test_flash_node_step_failure_disables_dut(monkeypatch):
    """A node-side flash failure (step 3) disables the DUT so it drops
    out of pool lookups; a client-side scp failure does not."""
    node, dut = _make_node_dut(pool="pool-01")
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}
    client = _make_client()

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    def fail_bmaptool(cmd, **kwargs):
        rc = 1 if cmd[0] == "ssh" and "bmaptool" in cmd[-1] else 0
        return server_mod.subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(server_mod.subprocess, "run", fail_bmaptool)
    with pytest.raises(RuntimeError, match="flash command failed"):
        server_mod._flash_image(node, dut, client, "/remote/image.wic")
    assert dut["metadata"]["enabled"] is False
    assert server_mod._list_duts_in_pool("pool-01") == []

    dut["metadata"]["enabled"] = True

    def fail_scp(cmd, **kwargs):
        rc = 1 if cmd[0] == "scp" else 0
        return server_mod.subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(server_mod.subprocess, "run", fail_scp)
    with pytest.raises(RuntimeError, match="scp from client failed"):
        server_mod._flash_image(node, dut, client, "/remote/image.wic")
    assert dut["metadata"]["enabled"] is True


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


def test_conf_info_reserves_active_filter(flask_client, monkeypatch):
    """With "active": true, /conf/info/reserves returns only entries
    where valid-from <= now <= valid-until."""
    monkeypatch.setattr(server_mod, "admin_key", "test-admin-key")
    now = int(time.time())
    with server_mod.state_lock:
        server_mod.reserves[:] = [
            {"token": "expired", "valid-from": now - 100,
             "valid-until": now - 10, "client-key": "k", "dut-name": "d1"},
            {"token": "active", "valid-from": now - 10,
             "valid-until": now + 100, "client-key": "k", "dut-name": "d2"},
            {"token": "future", "valid-from": now + 50,
             "valid-until": now + 100, "client-key": "k", "dut-name": "d3"},
        ]

    resp = flask_client.post(
        "/conf/info/reserves", json={"admin-key": "test-admin-key"})
    tokens = {r["token"] for r in resp.get_json()}
    assert tokens == {"expired", "active", "future"}

    resp = flask_client.post(
        "/conf/info/reserves",
        json={"admin-key": "test-admin-key", "active": True})
    assert [r["token"] for r in resp.get_json()] == ["active"]


def test_conf_dut_enabled_toggles_dut(flask_client, monkeypatch):
    """/conf/dut/enabled disables/enables a DUT at runtime, controlling
    whether pool lookups (and therefore /reserve) can see it."""
    client = _make_client()
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
    monkeypatch.setattr(server_mod, "admin_key", "test-admin-key")

    # Wrong admin key -> HTTP 403
    resp = flask_client.post(
        "/conf/dut/enabled",
        json={"admin-key": "bad", "dut-name": dut["name"],
              "enabled": False},
    )
    assert resp.status_code == 403

    # Unknown DUT -> result -2
    resp = flask_client.post(
        "/conf/dut/enabled",
        json={"admin-key": "test-admin-key", "dut-name": "no-such-dut",
              "enabled": False},
    )
    assert resp.get_json()["result"] == -2

    # Non-boolean enabled -> result -1
    resp = flask_client.post(
        "/conf/dut/enabled",
        json={"admin-key": "test-admin-key", "dut-name": dut["name"],
              "enabled": "false"},
    )
    assert resp.get_json()["result"] == -1

    # Disable: the only DUT in the pool vanishes, so /reserve fails
    resp = flask_client.post(
        "/conf/dut/enabled",
        json={"admin-key": "test-admin-key", "dut-name": dut["name"],
              "enabled": False},
    )
    data = resp.get_json()
    assert data == {"result": 0, "dut-name": dut["name"], "enabled": False}

    resp = flask_client.post(
        "/reserve", json={"client-key": client["key"], "pool": "pool-01"})
    assert resp.get_json()["status"] == -2

    # Re-enable: reservable again
    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})
    resp = flask_client.post(
        "/conf/dut/enabled",
        json={"admin-key": "test-admin-key", "dut-name": dut["name"],
              "enabled": True},
    )
    assert resp.get_json()["result"] == 0

    resp = flask_client.post(
        "/reserve", json={"client-key": client["key"], "pool": "pool-01"})
    assert resp.get_json()["status"] == 0


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


# ---------------------------------------------------------------------------
# Multi-pool DUTs
# ---------------------------------------------------------------------------

def test_dut_pools_accepts_single_name():
    assert server_mod._dut_pools({"metadata": {"pool": "pool-01"}}) == [
        "pool-01"]
    assert server_mod._dut_pools({"metadata": {"pools": "pool-01"}}) == [
        "pool-01"]


def test_dut_pools_accepts_list_and_dedups():
    dut = {"metadata": {"pools": ["pool-01", "pool-02"], "pool": "pool-01"}}
    assert server_mod._dut_pools(dut) == ["pool-01", "pool-02"]


def test_dut_pools_reject_booleans():
    assert server_mod._as_pool_list(True) == []
    assert server_mod._as_pool_list(["pool-01", True, False]) == ["pool-01"]
    assert server_mod._dut_pools({"metadata": {"pool": True}}) == []


def test_load_nodes_ignores_boolean_pool_value(tmp_path):
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "node-01.yml").write_text(
        """
- name: node-01
  ssh:
    - ip: 192.0.2.1
  duts:
    - name: dut-01
      metadata:
        - pool: yes
        - pool: pool-01
      network:
        - ip: 192.0.2.2
"""
    )

    loaded = server_mod._load_nodes(tmp_path)
    assert loaded[0]["duts"][0]["metadata"]["pools"] == ["pool-01"]


def test_dut_pools_empty_when_missing():
    assert server_mod._dut_pools({"metadata": {}}) == []
    assert server_mod._dut_pools({}) == []


def test_load_nodes_accumulates_repeated_pool_keys(tmp_path):
    _write_node_yaml(tmp_path / "nodes", "        - pool: pool-02")

    loaded = server_mod._load_nodes(tmp_path)
    metadata = loaded[0]["duts"][0]["metadata"]
    assert metadata["pools"] == ["pool-01", "pool-02"]
    assert "pool" not in metadata


def test_load_nodes_accepts_pools_list(tmp_path):
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "node-01.yml").write_text(
        """
- name: node-01
  ssh:
    - ip: 192.0.2.1
  duts:
    - name: dut-01
      metadata:
        - pools:
            - pool-01
            - pool-02
      network:
        - ip: 192.0.2.2
"""
    )

    loaded = server_mod._load_nodes(tmp_path)
    assert loaded[0]["duts"][0]["metadata"]["pools"] == ["pool-01", "pool-02"]


def test_normalize_metadata_accepts_mapping_style_section():
    assert server_mod._normalize_metadata({"pool": "pool-01"}) == {
        "pools": ["pool-01"]}
    assert server_mod._normalize_metadata(
        {"pools": ["pool-01", "pool-02"], "enabled": False}) == {
            "pools": ["pool-01", "pool-02"], "enabled": False}


def test_normalize_metadata_does_not_mutate_its_input():
    raw = {"pool": "pool-01"}
    server_mod._normalize_metadata(raw)
    assert raw == {"pool": "pool-01"}

    raw = [{"pool": "pool-01"}]
    server_mod._normalize_metadata(raw)
    assert raw == [{"pool": "pool-01"}]


def test_load_nodes_accepts_mapping_style_metadata(tmp_path):
    nodes_dir = tmp_path / "nodes"
    nodes_dir.mkdir()
    (nodes_dir / "node-01.yml").write_text(
        """
- name: node-01
  ssh:
    - ip: 192.0.2.1
  duts:
    - name: dut-01
      metadata:
        pool: pool-01
      network:
        - ip: 192.0.2.2
"""
    )

    loaded = server_mod._load_nodes(tmp_path)
    metadata = loaded[0]["duts"][0]["metadata"]
    assert metadata["pools"] == ["pool-01"]
    assert metadata["enabled"] is True


def test_dut_in_several_pools_is_listed_in_each():
    node, dut = _make_node_dut()
    dut["metadata"] = {"pools": ["pool-01", "pool-02"]}

    with server_mod.state_lock:
        server_mod.nodes[:] = [node]

    assert server_mod._pool_exists("pool-01") is True
    assert server_mod._pool_exists("pool-02") is True
    assert server_mod._pool_exists("pool-03") is False
    assert server_mod._list_duts_in_pool("pool-01") == [(node, dut)]
    assert server_mod._list_duts_in_pool("pool-02") == [(node, dut)]


def test_reserve_from_secondary_pool(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut()
    dut["metadata"] = {"pools": ["pool-01", "pool-02"]}

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    resp = flask_client.post(
        "/reserve",
        json={"client-key": client["key"], "pool": "pool-02"},
    )
    data = resp.get_json()
    assert data["status"] == 0
    assert data["dut-name"] == dut["name"]


def test_reserve_in_one_pool_blocks_the_other_pools(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut()
    dut["metadata"] = {"pools": ["pool-01", "pool-02"]}

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    resp = flask_client.post(
        "/reserve", json={"client-key": client["key"], "pool": "pool-01"})
    assert resp.get_json()["status"] == 0

    resp = flask_client.post(
        "/reserve", json={"client-key": client["key"], "pool": "pool-02"})
    data = resp.get_json()
    assert data["status"] == -4
    assert "all duts in use already" in data["error"]


def test_lease_by_secondary_pool_releases_reserve(flask_client, monkeypatch):
    client = _make_client()
    node, dut = _make_node_dut()
    dut["metadata"] = {"pools": ["pool-01", "pool-02"]}

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    resp = flask_client.post(
        "/reserve", json={"client-key": client["key"], "pool": "pool-01"})
    token = resp.get_json()["token"]

    resp = flask_client.post(
        "/lease", json={"client-key": client["key"], "pool": "pool-02"})
    assert resp.get_json()["status"] == 0

    # Expired: valid-until pulled back to the release time
    assert server_mod._get_reserve_by_token(
        token)["valid-until"] <= int(time.time())


# ---------------------------------------------------------------------------
# Client SSH overrides
# ---------------------------------------------------------------------------

def test_parse_client_ssh_overrides_valid_values():
    assert server_mod._parse_client_ssh_overrides({}) == {}
    assert server_mod._parse_client_ssh_overrides(
        {"client-ssh-ip": " 203.0.113.9 "}) == {"ip": "203.0.113.9"}
    # A port arriving as a string (the CLI forwards the raw env var)
    assert server_mod._parse_client_ssh_overrides(
        {"client-ssh-port": "2222"}) == {"port": 2222}
    assert server_mod._parse_client_ssh_overrides(
        {"client-ssh-ip": "203.0.113.9", "client-ssh-port": 2222}
    ) == {"ip": "203.0.113.9", "port": 2222}


@pytest.mark.parametrize("value, expected", [
    ("203.0.113.9", "203.0.113.9"),
    ("  203.0.113.9  ", "203.0.113.9"),
    ("2001:db8::1", "2001:db8::1"),
    # Dynamic DNS is the usual way a NATed client is addressed
    ("client-01.dyn.example.com", "client-01.dyn.example.com"),
    ("client-01.dyn.example.com.", "client-01.dyn.example.com."),
    ("localhost", "localhost"),
])
def test_valid_ssh_host_accepts_addresses_and_names(value, expected):
    assert server_mod._valid_ssh_host(value) == expected


@pytest.mark.parametrize("value", [
    "",
    "   ",
    1234,
    None,
    True,
    ["203.0.113.9"],
    # Malformed literals must not pass as DNS names
    "203.0.113.999",
    "203.0.113",
    "010.0.113.9",
    # Anything that would not survive as an `ssh user@host` argument
    "203.0.113.9 -oProxyCommand=id",
    "root@203.0.113.9",
    "203.0.113.9:2222",
    "$(id).example.com",
    "-oProxyCommand=id",
    "client..example.com",
    "client-.example.com",
    "a" * 64 + ".example.com",
])
def test_valid_ssh_host_rejects_bad_values(value):
    with pytest.raises(ValueError):
        server_mod._valid_ssh_host(value)


@pytest.mark.parametrize("value, expected", [
    (22, 22),
    ("2222", 2222),
    (" 2222 ", 2222),
    (65535, 65535),
])
def test_valid_ssh_port_accepts_port_numbers(value, expected):
    assert server_mod._valid_ssh_port(value) == expected


@pytest.mark.parametrize("body", [
    {"client-ssh-ip": ""},
    {"client-ssh-ip": "   "},
    {"client-ssh-ip": 1234},
    {"client-ssh-port": "not-a-port"},
    {"client-ssh-port": 0},
    {"client-ssh-port": -1},
    {"client-ssh-port": 65536},
    {"client-ssh-port": True},
    {"client-ssh-port": []},
])
def test_parse_client_ssh_overrides_rejects_bad_values(body):
    with pytest.raises(ValueError):
        server_mod._parse_client_ssh_overrides(body)


def test_reserve_rejects_injection_in_client_ssh_ip(flask_client, monkeypatch):
    """A bogus address never reaches the ssh command line."""
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    def fail_popen(*args, **kwargs):
        raise AssertionError("no tunnel must be started")

    monkeypatch.setattr(server_mod.subprocess, "Popen", fail_popen)

    resp = flask_client.post(
        "/reserve",
        json={
            "client-key": client["key"],
            "pool": "pool-01",
            "client-ssh-ip": "203.0.113.9 -oProxyCommand=id",
        },
    )
    data = resp.get_json()
    assert data["status"] == -5
    assert "client-ssh-ip" in data["error"]

    with server_mod.state_lock:
        assert client["ssh"]["ip"] == "192.0.2.10"
        assert server_mod.reserves == []


def test_reserve_applies_client_ssh_overrides(flask_client, monkeypatch):
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    resp = flask_client.post(
        "/reserve",
        json={
            "client-key": client["key"],
            "pool": "pool-01",
            "client-ssh-ip": "203.0.113.9",
            "client-ssh-port": 2222,
        },
    )
    data = resp.get_json()
    assert data["status"] == 0
    assert data["client-ssh-overrides"] == ["ip", "port"]

    with server_mod.state_lock:
        assert client["ssh"]["ip"] == "203.0.113.9"
        assert client["ssh"]["port"] == 2222
        # The user is not overridable and keeps coming from the config
        assert client["ssh"]["user"] == "tester"
        assert client["ssh-overrides"] == ["ip", "port"]


def test_reserve_without_overrides_keeps_configured_ssh(
        flask_client, monkeypatch):
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")

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
    assert data["client-ssh-overrides"] == []

    with server_mod.state_lock:
        assert client["ssh"] == {"ip": "192.0.2.10", "port": 22,
                                 "user": "tester"}
        assert "ssh-overrides" not in client


def test_reserve_tunnel_uses_overridden_address(flask_client, monkeypatch):
    """The reverse tunnel must be opened to the announced address."""
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01", dut_ip="192.0.2.30")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(server_mod.subprocess, "Popen", fake_popen)

    resp = flask_client.post(
        "/reserve",
        json={
            "client-key": client["key"],
            "pool": "pool-01",
            "client-ssh-ip": "203.0.113.9",
            "client-ssh-port": "2222",
        },
    )
    data = resp.get_json()
    assert data["status"] == 0

    cmd = captured["cmd"]
    assert "tester@203.0.113.9" in cmd
    assert cmd[cmd.index("-p") + 1] == "2222"


def test_reserve_rejects_invalid_client_ssh_port(flask_client, monkeypatch):
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    resp = flask_client.post(
        "/reserve",
        json={
            "client-key": client["key"],
            "pool": "pool-01",
            "client-ssh-port": "ssh",
        },
    )
    data = resp.get_json()
    assert data["status"] == -5
    assert "client-ssh-port" in data["error"]

    # Nothing reserved and the configured address left untouched
    with server_mod.state_lock:
        assert server_mod.reserves == []
        assert client["ssh"]["port"] == 22


def test_client_ssh_override_marker_accumulates(flask_client, monkeypatch):
    """Announcing one field at a time leaves the other override in place."""
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]

    monkeypatch.setattr(
        server_mod, "_start_ssh_tunnel", lambda *a, **k: {"pid": 1})

    flask_client.post(
        "/reserve",
        json={
            "client-key": client["key"],
            "pool": "pool-01",
            "client-ssh-ip": "203.0.113.9",
        },
    )
    with server_mod.state_lock:
        assert client["ssh-overrides"] == ["ip"]

    # A later call announcing only the port keeps the ip override
    flask_client.post(
        "/pools",
        json={"client-key": client["key"], "client-ssh-port": 2222},
    )
    with server_mod.state_lock:
        assert client["ssh"] == {"ip": "203.0.113.9", "port": 2222,
                                 "user": "tester"}
        assert client["ssh-overrides"] == ["ip", "port"]


def _reserve_for_flash(client, dut, token):
    with server_mod.state_lock:
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


def test_flash_applies_client_ssh_overrides(flask_client, monkeypatch):
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-flash-override"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
    _reserve_for_flash(client, dut, token)

    seen = {}

    def fake_flash_image(node_arg, dut_arg, client_arg, client_path):
        seen["ssh"] = dict(client_arg["ssh"])

    monkeypatch.setattr(server_mod, "_flash_image", fake_flash_image)

    resp = flask_client.post(
        "/flash",
        json={
            "token": token,
            "path": "/images/image.wic",
            "client-ssh-ip": "203.0.113.9",
            "client-ssh-port": 2222,
        },
    )
    assert resp.get_json()["status"] == 0
    assert seen["ssh"]["ip"] == "203.0.113.9"
    assert seen["ssh"]["port"] == 2222

    with server_mod.state_lock:
        assert client["ssh-overrides"] == ["ip", "port"]


def test_flash_rejects_invalid_client_ssh_ip(flask_client, monkeypatch):
    client = _make_client(ip="192.0.2.10", port=22)
    node, dut = _make_node_dut(pool="pool-01")
    token = "token-flash-bad-override"

    with server_mod.state_lock:
        server_mod.clients[:] = [client]
        server_mod.nodes[:] = [node]
    _reserve_for_flash(client, dut, token)

    def fail_flash_image(*args, **kwargs):
        raise AssertionError("flash must not run with a bad override")

    monkeypatch.setattr(server_mod, "_flash_image", fail_flash_image)

    resp = flask_client.post(
        "/flash",
        json={
            "token": token,
            "path": "/images/image.wic",
            "client-ssh-ip": "",
        },
    )
    data = resp.get_json()
    assert data["status"] == -5
    assert "client-ssh-ip" in data["error"]

    with server_mod.state_lock:
        assert client["ssh"]["ip"] == "192.0.2.10"


# ---------------------------------------------------------------------------
# /wipe
# ---------------------------------------------------------------------------

def _reserved_dut(pool="pool-01", token="token-wipe"):
    """A client, a node, a DUT and a live reservation for it."""
    client = _make_client()
    node, dut = _make_node_dut(pool=pool)
    dut["storage"] = {"control": "/dev/sg1", "device": "/dev/sda1"}

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
    return node, dut, token


def test_wipe_defaults_to_the_head_of_the_storage(flask_client, monkeypatch):
    _, _, token = _reserved_dut(token="token-wipe-default")
    seen = {}

    def fake_wipe(node, dut, size_bytes):
        seen["size"] = size_bytes

    monkeypatch.setattr(server_mod, "_wipe_storage", fake_wipe)

    resp = flask_client.post("/wipe", json={"token": token})

    assert resp.get_json()["status"] == 0
    assert seen["size"] == server_mod.WIPE_DEFAULT_BYTES


@pytest.mark.parametrize("size", [0, -1, "128MiB", 1.5, True, None])
def test_wipe_refuses_a_size_that_is_not_a_count_of_bytes(
        flask_client, monkeypatch, size):
    _, _, token = _reserved_dut(token="token-wipe-badsize")
    monkeypatch.setattr(server_mod, "_wipe_storage",
                        lambda *a: pytest.fail("should not have run"))

    resp = flask_client.post("/wipe", json={"token": token, "size": size})

    data = resp.get_json()
    assert data["status"] == -99
    assert "size" in data["error"]


def test_wipe_reports_a_failure_verbatim(flask_client, monkeypatch):
    _, _, token = _reserved_dut(token="token-wipe-fail")

    def fake_wipe(node, dut, size_bytes):
        raise RuntimeError("wipe command failed on node")

    monkeypatch.setattr(server_mod, "_wipe_storage", fake_wipe)

    data = flask_client.post("/wipe", json={"token": token}).get_json()

    assert data["status"] == -99
    assert data["error"] == "wipe command failed on node"


def test_wipe_zeroes_the_device_through_the_node(monkeypatch):
    node, dut, _ = _reserved_dut(token="token-wipe-node")
    commands = []

    monkeypatch.setattr(server_mod, "_run_node_command",
                        lambda n, cmd: commands.append(cmd) or True)

    server_mod._wipe_storage(node, dut, 2 * server_mod._MIB)

    assert len(commands) == 2
    assert "usbsdmux /dev/sg1 host" in commands[0]
    assert "dd if=/dev/zero of=/dev/sda1 bs=1M count=$count" in commands[0]
    assert "oflag=direct" in commands[0]
    assert commands[1] == "usbsdmux /dev/sg1 dut"


def test_wipe_never_asks_for_more_than_the_card_holds(monkeypatch):
    """Running past the end is ENOSPC, which would read as a failure."""
    node, dut, _ = _reserved_dut(token="token-wipe-clamp")
    commands = []
    monkeypatch.setattr(server_mod, "_run_node_command",
                        lambda n, cmd: commands.append(cmd) or True)

    server_mod._wipe_storage(node, dut, 64 * 1024 ** 3)

    assert "blockdev --getsize64 /dev/sda1" in commands[0]
    assert "count=$((65536 < count ? 65536 : count))" in commands[0]


def test_wipe_rounds_a_partial_mebibyte_up(monkeypatch):
    node, dut, _ = _reserved_dut(token="token-wipe-round")
    commands = []
    monkeypatch.setattr(server_mod, "_run_node_command",
                        lambda n, cmd: commands.append(cmd) or True)

    server_mod._wipe_storage(node, dut, server_mod._MIB + 1)

    assert "(2 < count ? 2 : count)" in commands[0]


def test_a_stuck_mux_is_reported_before_anything_else(monkeypatch):
    """A mux left on host needs operator action, so it comes first."""
    node, dut, _ = _reserved_dut(token="token-wipe-stuck")

    monkeypatch.setattr(server_mod, "_run_node_command",
                        lambda n, cmd: not cmd.endswith(" dut"))

    with pytest.raises(RuntimeError, match="switch storage back to dut"):
        server_mod._wipe_storage(node, dut, server_mod._MIB)


def test_the_card_goes_back_to_the_dut_after_a_failed_wipe(monkeypatch):
    """Otherwise a bad card would also leave the mux on host."""
    node, dut, _ = _reserved_dut(token="token-wipe-back")
    commands = []

    def fake_run(n, cmd):
        commands.append(cmd)
        return cmd.endswith(" dut")

    monkeypatch.setattr(server_mod, "_run_node_command", fake_run)

    with pytest.raises(RuntimeError, match="wipe command failed on node"):
        server_mod._wipe_storage(node, dut, server_mod._MIB)

    assert commands[-1] == "usbsdmux /dev/sg1 dut"


def test_both_failing_reports_the_mux_and_mentions_the_wipe(monkeypatch):
    node, dut, _ = _reserved_dut(token="token-wipe-both")

    monkeypatch.setattr(server_mod, "_run_node_command",
                        lambda n, cmd: False)

    with pytest.raises(RuntimeError,
                       match="switch storage back to dut.*wipe also failed"):
        server_mod._wipe_storage(node, dut, server_mod._MIB)


def test_a_dut_that_cannot_be_wiped_leaves_the_pool(monkeypatch):
    """The same reasoning as a failed flash: bad card or bad mux."""
    node, dut, _ = _reserved_dut(token="token-wipe-disable")

    monkeypatch.setattr(server_mod, "_run_node_command",
                        lambda n, cmd: cmd.endswith(" dut"))

    with pytest.raises(RuntimeError, match="wipe command failed"):
        server_mod._wipe_storage(node, dut, server_mod._MIB)

    assert dut["metadata"]["enabled"] is False


def test_wipe_needs_a_dut_with_storage(monkeypatch):
    node, dut, _ = _reserved_dut(token="token-wipe-nostorage")
    dut["storage"] = {}

    with pytest.raises(RuntimeError, match="storage.control/device missing"):
        server_mod._wipe_storage(node, dut, server_mod._MIB)

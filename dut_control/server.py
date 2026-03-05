#!/usr/bin/env python3

import os
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify
import yaml

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("DUT_CONTROL_DIR", "dut-control"))

admin_key = None
nodes = []       # list[dict]
clients = []     # list[dict]
reserves = []    # list[dict]
processes = []   # list[dict]

state_lock = threading.RLock()

server = Flask(__name__)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def validate_client(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        body = request.get_json(silent=True) or {}
        client_key = body.get("client-key")

        if not client_key:
            return jsonify({"status": -1, "error": "client-key missing"}), 200

        client = _get_client_by_key(client_key)
        if client is None:
            return jsonify(
                {"status": -1, "error": "client key is not valid"}), 200

        request.client = client
        result = func(*args, **kwargs)
        return result
    return wrapper


def validate_pool(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        body = request.get_json(silent=True) or {}
        pool = body.get("pool")

        if not pool:
            return jsonify({"status": -2, "error": "pool missing"}), 200

        # Pool existence
        if not _pool_exists(pool):
            return jsonify({"status": -2, "error": "pool does not exist"}), 200

        duts_in_pool = _list_duts_in_pool(pool)
        if not duts_in_pool:
            return jsonify({"status": -3, "error": "pool is empty"}), 200

        request.pool = pool
        request.duts_in_pool = duts_in_pool

        result = func(*args, **kwargs)
        return result
    return wrapper


def validate_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        body = request.get_json(silent=True) or {}
        token = body.get("token")
        if not token:
            return jsonify({"status": -1, "error": "token missing"}), 200

        reserve_entry = _get_reserve_by_token(token)
        if not reserve_entry:
            return jsonify({"status": -1, "error": "token is not valid"}), 200

        if not _is_reserve_valid(reserve_entry):
            return jsonify({"status": -2, "error": "token expired"}), 200

        node, dut = _get_dut_and_node_by_name(reserve_entry["dut-name"])
        if not node or not dut:
            return jsonify({"status": -99, "error": "dut not found"}), 200

        result = func(*args, **kwargs)
        return result
    return wrapper


# ---------------------------------------------------------------------------
# YAML loading / normalization helpers
# ---------------------------------------------------------------------------

def _normalize_section(value):
    """
    Transform YAML like:
      ssh:
        - ip: 192.168.1.1
        - port: 22
        - user: root
    into:
      {"ip": "192.168.1.1", "port": 22, "user": "root"}
    """
    if isinstance(value, list):
        out = {}
        for item in value:
            if isinstance(item, dict):
                out.update(item)
        return out
    elif isinstance(value, dict):
        return value
    return {}


def _load_yaml_file(path: Path):
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def _load_conf(config_dir: Path):
    cfg_path = config_dir / "conf.yml"
    data = _load_yaml_file(cfg_path)

    # conf.yml can be a list or a mapping
    if isinstance(data, list):
        merged = {}
        for item in data:
            if isinstance(item, dict):
                merged.update(item)
        data = merged

    key = data.get("admin-key")
    if not key:
        raise ValueError("conf.yml must contain 'admin-key'")
    return key


def _load_nodes(config_dir: Path):
    nodes_dir = config_dir / "nodes"
    result = []

    if not nodes_dir.is_dir():
        return result

    for path in sorted(nodes_dir.glob("*.yml")):
        data = _load_yaml_file(path)
        docs = data if isinstance(data, list) else [data]

        for doc in docs:
            if not isinstance(doc, dict):
                continue

            node = {
                "name": doc["name"],
                "ssh": _normalize_section(doc.get("ssh", {})),
                "duts": [],
            }

            for dut in doc.get("duts", []):
                if not isinstance(dut, dict):
                    continue
                d = {
                    "name": dut["name"],
                    "metadata": _normalize_section(dut.get("metadata", {})),
                    "network": _normalize_section(dut.get("network", {})),
                    "storage": _normalize_section(dut.get("storage", {})),
                    "power": _normalize_section(dut.get("power", {})),
                }
                node["duts"].append(d)

            result.append(node)

    return result


def _load_clients(config_dir: Path):
    clients_dir = config_dir / "clients"
    result = []

    if not clients_dir.is_dir():
        return result

    for path in sorted(clients_dir.glob("*.yml")):
        data = _load_yaml_file(path)
        docs = data if isinstance(data, list) else [data]

        for doc in docs:
            if not isinstance(doc, dict):
                continue

            client = {
                "name": doc["name"],
                "key": doc["key"],
                "ssh": _normalize_section(doc.get("ssh", {})),
                "ports-range": _normalize_section(doc.get("ports-range", {})),
            }
            result.append(client)

    return result


def reload_config():
    """
    Reload admin_key, nodes, clients from YAML.
    Does NOT touch reserves/processes.
    """
    global admin_key, nodes, clients

    new_admin_key = _load_conf(CONFIG_DIR)
    new_nodes = _load_nodes(CONFIG_DIR)
    new_clients = _load_clients(CONFIG_DIR)

    with state_lock:
        admin_key = new_admin_key
        nodes[:] = new_nodes
        clients[:] = new_clients


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _get_client_by_key(key: str):
    with state_lock:
        for c in clients:
            if c.get("key") == key:
                return c
    return None


def _pool_exists(pool: str) -> bool:
    with state_lock:
        for node in nodes:
            for dut in node.get("duts", []):
                if dut.get("metadata", {}).get("pool") == pool:
                    return True
    return False


def _list_duts_in_pool(pool: str):
    """Return list of (node, dut) pairs for given pool."""
    result = []
    with state_lock:
        for node in nodes:
            for dut in node.get("duts", []):
                if dut.get("metadata", {}).get("pool") == pool:
                    result.append((node, dut))
    return result


def _get_dut_and_node_by_name(dut_name: str):
    with state_lock:
        for node in nodes:
            for dut in node.get("duts", []):
                if dut.get("name") == dut_name:
                    return node, dut
    return None, None


def _get_reserve_by_token(token: str):
    with state_lock:
        for r in reserves:
            if r.get("token") == token:
                return r
    return None


def _now_epoch() -> int:
    return int(time.time())


def _is_reserve_valid(reserve: dict) -> bool:
    return reserve.get("valid-until", 0) >= _now_epoch()


def _client_used_ports(client_name: str):
    used = set()
    with state_lock:
        for p in processes:
            if p.get("client-name") == client_name:
                used.update(p.get("ports-in-use", []))
    return used


def _find_free_port_for_client(client: dict) -> int | None:
    pr = client.get("ports-range", {})
    start = int(pr.get("from", 0))
    end = int(pr.get("to", -1))
    if start <= 0 or end < start:
        return None

    used = _client_used_ports(client["name"])
    for port in range(start, end + 1):
        if port not in used:
            return port
    return None


# ---------------------------------------------------------------------------
# Process management (SSH tunnels)
# ---------------------------------------------------------------------------

def _start_ssh_tunnel(
        client: dict,
        dut: dict,
        remote_port: int,
        reserve_token: str):
    """
    Start SSH port forwarding process:
      ssh -N -p <client_ssh_port> -R <remote_port>:<dut_ip>:<dut_ssh_port> \
          user@client_ip
    """
    client_ssh = client["ssh"]
    client_ip = client_ssh["ip"]
    client_ssh_port = int(client_ssh.get("port", 22))
    user = client_ssh.get("user", "root")

    dut_net = dut["network"]
    dut_ip = dut_net["ip"]
    dut_ssh_port = int(dut_net.get("ssh-port", 22))

    cmd = [
        "ssh",
        "-N",
        "-p",
        str(client_ssh_port),
        "-R",
        f"{remote_port}:{dut_ip}:{dut_ssh_port}",
        f"{user}@{client_ip}",
    ]

    # Detach process; make its own process group to kill later
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )

    entry = {
        "pid": proc.pid,
        "reserve-token": reserve_token,
        "command": " ".join(cmd),
        "process": proc,
        "client-name": client["name"],
        "ports-in-use": [remote_port],
    }

    with state_lock:
        processes.append(entry)

    return entry


def _stop_process_entry(entry: dict):
    proc = entry.get("process")
    if proc is None:
        return
    try:
        # Kill process group if possible
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Utility: admin key check
# ---------------------------------------------------------------------------

def _check_admin_key_from_body(body: dict) -> bool:
    key = body.get("admin-key")
    with state_lock:
        return key == admin_key


# ---------------------------------------------------------------------------
# Utility: simple network checks
# ---------------------------------------------------------------------------

def _ping_host(ip: str, timeout_sec: int = 1) -> bool:
    try:
        # Linux / Unix ping one packet
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout_sec), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError:
        # ping not available; best-effort "offline"
        return False


def _check_ssh(ip: str, port: int, timeout_sec: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Endpoints: configuration / info
# ---------------------------------------------------------------------------

@server.route("/conf/reload", methods=["POST", "PUT"])
def conf_reload():
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    reload_config()
    return jsonify({"result": 0})


@server.route("/conf/info/nodes", methods=["POST", "PUT"])
def conf_info_nodes():
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    with state_lock:
        print(jsonify(nodes))
        return jsonify(nodes)


@server.route("/conf/info/clients", methods=["POST", "PUT"])
def conf_info_clients():
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    with state_lock:
        return jsonify(clients)


@server.route("/conf/info/processes", methods=["POST", "PUT"])
def conf_info_processes():
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    with state_lock:
        # project internal entries to a JSON-serializable form
        serializable = [
            {
                "pid": p.get("pid"),
                "reserve-token": p.get("reserve-token"),
                "command": p.get("command"),
                "client-name": p.get("client-name"),
                "ports-in-use": p.get("ports-in-use", []),
            }
            for p in processes
        ]
    return jsonify(serializable)


@server.route("/conf/info/reserves", methods=["POST", "PUT"])
def conf_info_reserves():
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    with state_lock:
        return jsonify(reserves)


@server.route("/conf/reserves/prune", methods=["POST", "PUT"])
def conf_reserves_prune():
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    now = _now_epoch()
    with state_lock:
        before = len(reserves)
        # Keep only non-expired
        reserves[:] = [r for r in reserves if r.get("valid-until", 0) >= now]
        after = len(reserves)
    return jsonify({"result": 0, "pruned": before - after})


# ---------------------------------------------------------------------------
# /reserve
# ---------------------------------------------------------------------------

def _error_response(status_code: int, error_msg: str):
    """Helper for consistent error responses."""
    return jsonify({"status": status_code, "error": error_msg}), 200


def _rollback_reserve(token: str):
    """Remove specific reservation entry."""
    with state_lock:
        reserves[:] = [r for r in reserves if r.get("token") != token]


@server.route("/reserve", methods=["POST", "PUT"])
@validate_client
@validate_pool
def reserve():
    """Reserve a DUT with low CC: extract checks and logic."""
    duts_in_pool = request.duts_in_pool
    now = _now_epoch()

    # Early check: any available DUTs?
    with state_lock:
        in_use_duts = {r["dut-name"] for r in reserves
                       if r.get("valid-until", 0) >= now}
    available = [(node, dut) for (node, dut) in duts_in_pool
                 if dut["name"] not in in_use_duts]
    if not available:
        return _error_response(-4, "all duts in use already")

    node, dut = available[0]

    # Early check: free port?
    free_port = _find_free_port_for_client(request.client)
    if free_port is None:
        return _error_response(-4, "no free ports for client")

    # Create and store reservation
    token = secrets.token_hex(8)
    reserve_entry = {
        "token": token,
        "valid-from": now,
        "valid-until": now + 2 * 3600,  # 2 hours
        "client-key": request.client["key"],
        "dut-name": dut["name"],
    }
    with state_lock:
        reserves.append(reserve_entry)

    # Start tunnel or rollback
    try:
        _start_ssh_tunnel(request.client, dut, free_port, token)
    except Exception as e:
        _rollback_reserve(token)
        return _error_response(-99, f"internal error: {e}")

    return jsonify({
        "status": 0,
        "token": token,
        "ssh-port": free_port
    }), 200


# ---------------------------------------------------------------------------
# /lease
# ---------------------------------------------------------------------------

def _determine_lease_mode(body: dict) -> str:
    """Determine lease mode: token, pool, or all."""
    if body.get("token"):
        return "token"
    if body.get("pool"):
        return "pool"
    return "all"


def _get_tokens_to_release(body: dict, now: float) -> set[str]:
    """Collect tokens based on mode."""
    tokens = set()
    pool = body.get("pool")

    if pool:
        if not _pool_exists(pool) or not _list_duts_in_pool(pool):
            raise ValueError("pool does not exist or is empty")

    with state_lock:
        for r in reserves:
            if _matches_client_and_time(r, request.client["key"], now):
                if _matches_mode(r, body):
                    tokens.add(r["token"])
    return tokens


def _matches_client_and_time(reserve: dict, client_key: str,
                             now: float) -> bool:
    """Check client and time validity."""
    return (reserve.get("client-key") == client_key and
            reserve.get("valid-until", 0) >= now)


def _matches_mode(reserve: dict, body: dict) -> bool:
    """Check if reserve matches token/pool/all mode."""
    token = body.get("token")
    pool = body.get("pool")

    if token:
        return reserve.get("token") == token
    if pool:
        node, dut = _get_dut_and_node_by_name(reserve["dut-name"])
        return dut and dut.get("metadata", {}).get("pool") == pool
    return True  # All mode


def _terminate_processes(tokens: set[str]):
    """Stop and remove processes for tokens."""
    with state_lock:
        to_remove = [p for p in processes if p.get("reserve-token") in tokens]
        for p in to_remove:
            _stop_process_entry(p)
            processes.remove(p)


def _expire_reserves(tokens: set[str], now: float):
    """Set valid-until to now for reserves."""
    with state_lock:
        for r in reserves:
            if r.get("token") in tokens:
                r["valid-until"] = now


@server.route("/lease", methods=["POST", "PUT"])
@validate_client
def lease():
    """Lease/release reserves with low CC: dispatch by mode."""
    body = request.get_json(silent=True) or {}
    now = _now_epoch()

    try:
        mode = _determine_lease_mode(body)
        if mode == "invalid":
            return _error_response(-2, "missing token or pool")

        tokens_to_release = _get_tokens_to_release(body, now)
        if not tokens_to_release:
            return jsonify({"status": 0}), 200

        _terminate_processes(tokens_to_release)
        _expire_reserves(tokens_to_release, now)
        return jsonify({"status": 0}), 200

    except Exception as e:
        return _error_response(-99, f"internal error: {e}")


# ---------------------------------------------------------------------------
# /power/<on|off|cycle>
# ---------------------------------------------------------------------------

def _run_remote_power_script(node: dict, script: str):
    """
    Minimal implementation using system ssh to run a remote script.
    Assumes passwordless SSH (keys) from this service host to node.
    """
    ssh = node["ssh"]
    ip = ssh["ip"]
    port = int(ssh.get("port", 22))
    user = ssh.get("user", "root")

    cmd = ["ssh", "-p", str(port), f"{user}@{ip}", script]
    res = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return res.returncode == 0


@server.route("/power/<action>", methods=["POST", "PUT"])
@validate_token
def power(action):
    if action not in ("on", "off", "cycle"):
        return jsonify({"status": -99, "error": "invalid action"}), 200

    body = request.get_json(silent=True) or {}
    token = body.get("token")
    reserve_entry = _get_reserve_by_token(token)
    node, dut = _get_dut_and_node_by_name(reserve_entry["dut-name"])

    power_info = dut.get("power", {})
    script_on = power_info.get("power-on")
    script_off = power_info.get("power-off")

    ok = True
    if action == "on":
        ok = _run_remote_power_script(node, script_on)
    elif action == "off":
        ok = _run_remote_power_script(node, script_off)
    elif action == "cycle":
        ok = _run_remote_power_script(node, script_off)
        if ok:
            time.sleep(1)
            ok = _run_remote_power_script(node, script_on)

    if not ok:
        return jsonify({"status": -99, "error": "power script failed"}), 200

    return jsonify({"status": 0}), 200


# ---------------------------------------------------------------------------
# /flash
# ---------------------------------------------------------------------------

def _flash_image(node: dict, dut: dict, client: dict, client_path: str):
    """
    1. scp image from client -> service temp dir
    2. scp image from service temp dir -> node temp dir
    3. ssh to node and run usbsdmux/bmaptool commands using storage.* info

    Requires passwordless SSH/SCP from the service host to both client and
    node.
    """
    storage = dut.get("storage", {})
    control = storage.get("control")  # e.g. /dev/sg2
    device = storage.get("device")    # e.g. /dev/sdc

    if not control or not device:
        raise RuntimeError("storage.control/device missing in config")

    client_ssh = client["ssh"]
    client_ip = client_ssh["ip"]
    client_user = client_ssh.get("user", "root")
    client_port = int(client_ssh.get("port", 22))

    node_ssh = node["ssh"]
    node_ip = node_ssh["ip"]
    node_user = node_ssh.get("user", "root")
    node_port = int(node_ssh.get("port", 22))

    # Normalize basename once; we reuse it for local and remote tmp paths
    image_name = os.path.basename(client_path)

    tmpdir = tempfile.mkdtemp(prefix="dut-flash-")
    local_tmp_path = str(Path(tmpdir) / image_name)
    node_tmp_path = f"/tmp/{image_name}"

    try:
        # 1) scp from client -> local temp dir
        #    scp -P <client_port> user@client_ip:/remote/path /local/tmp/path
        scp_from_client = [
            "scp",
            "-P",
            str(client_port),
            f"{client_user}@{client_ip}:{client_path}",
            local_tmp_path,
        ]
        res = subprocess.run(
            scp_from_client,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError("scp from client failed")

        # 2) scp from local temp dir -> node temp dir
        #    scp -P <node_port> /local/tmp/path user@node_ip:/tmp/image.wic
        scp_to_node = [
            "scp",
            "-P",
            str(node_port),
            local_tmp_path,
            f"{node_user}@{node_ip}:{node_tmp_path}",
        ]
        res = subprocess.run(
            scp_to_node,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError("scp to node failed")

        # 3) ssh to node and run usbsdmux / bmaptool
        #    usbsdmux <control> host
        #    bmaptool copy --nobmap <node_tmp_path> <device>
        #    usbsdmux <control> dut
        cmd = (
            f"usbsdmux {control} host && "
            f"bmaptool copy --nobmap {node_tmp_path} {device} && "
            f"usbsdmux {control} dut"
        )

        ssh_cmd = [
            "ssh",
            "-p",
            str(node_port),
            f"{node_user}@{node_ip}",
            cmd,
        ]
        res = subprocess.run(
            ssh_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError("flash command failed on node")

    finally:
        # Best-effort cleanup of local temp file/dir
        try:
            if os.path.exists(local_tmp_path):
                os.remove(local_tmp_path)
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            # directory not empty or already removed; ignore
            pass


@server.route("/flash", methods=["POST", "PUT"])
@validate_token
def flash():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    path = body.get("path")

    if not path:
        return jsonify({"status": -99, "error": "path missing"}), 200

    reserve_entry = _get_reserve_by_token(token)

    client = _get_client_by_key(reserve_entry["client-key"])
    if client is None:
        return jsonify({"status": -99, "error": "client not found"}), 200

    node, dut = _get_dut_and_node_by_name(reserve_entry["dut-name"])
    if not node or not dut:
        return jsonify({"status": -99, "error": "dut not found"}), 200

    try:
        _flash_image(node, dut, client, path)
    except Exception as e:
        return jsonify({"status": -99, "error": f"flash failed: {e}"}), 200

    return jsonify({"status": 0}), 200


# ---------------------------------------------------------------------------
# /dut/status
# ---------------------------------------------------------------------------

@server.route("/dut/status", methods=["POST", "PUT"])
@validate_token
def dut_status():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    reserve_entry = _get_reserve_by_token(token)
    node, dut = _get_dut_and_node_by_name(reserve_entry["dut-name"])

    ip = dut["network"]["ip"]
    ssh_port = int(dut["network"].get("ssh-port", 22))

    if not _ping_host(ip):
        return jsonify({"status": "offline"}), 200

    if _check_ssh(ip, ssh_port):
        return jsonify({"status": "ssh"}), 200

    return jsonify({"status": "ping"}), 200


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------

# Initial config load at import time
reload_config()


def main():
    # You can make host/port configurable via env vars if you like
    server.run(host="0.0.0.0", port=8000, debug=True)


if __name__ == "__main__":
    main()

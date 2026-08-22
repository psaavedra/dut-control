#!/usr/bin/env python3

import os
import random
import secrets
import shlex
import shutil
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
# Global definitions
# ---------------------------------------------------------------------------

_USBSDMUX_SETTLE_DELAY = 5

_SSH_SKIP_HOST_CHECK = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]

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
                metadata = _normalize_section(dut.get("metadata", {}))
                metadata.setdefault("enabled", True)
                d = {
                    "name": dut["name"],
                    "metadata": metadata,
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


def _dut_enabled(dut: dict) -> bool:
    return bool(dut.get("metadata", {}).get("enabled", True))


def _pool_exists(pool: str) -> bool:
    with state_lock:
        for node in nodes:
            for dut in node.get("duts", []):
                if (dut.get("metadata", {}).get("pool") == pool
                        and _dut_enabled(dut)):
                    return True
    return False


def _list_duts_in_pool(pool: str):
    """Return list of (node, dut) pairs for given pool, excluding disabled
    duts."""
    result = []
    with state_lock:
        for node in nodes:
            for dut in node.get("duts", []):
                if (dut.get("metadata", {}).get("pool") == pool
                        and _dut_enabled(dut)):
                    result.append((node, dut))
    return result


def _reserved_dut_names(now: int):
    """Names of DUTs held by a reservation that has not expired yet."""
    with state_lock:
        return {r["dut-name"] for r in reserves
                if r.get("valid-until", 0) >= now}


def _list_pools():
    """
    Per-pool summary: how many DUTs are enabled and how many of those
    are not currently reserved. Only pools with at least one enabled
    DUT are listed, which is exactly what _pool_exists considers to
    exist and therefore what /reserve can hand out.
    """
    pools = {}
    with state_lock:
        # Both halves under one acquisition: reading the reservations
        # and walking the nodes under separate ones could mix two
        # states and report a count that never actually held. Nesting
        # is fine, state_lock is reentrant.
        reserved = _reserved_dut_names(_now_epoch())
        for node in nodes:
            for dut in node.get("duts", []):
                pool = dut.get("metadata", {}).get("pool")
                if not pool or not _dut_enabled(dut):
                    continue
                entry = pools.setdefault(
                    pool, {"name": pool, "enabled-duts": 0, "free-duts": 0})
                entry["enabled-duts"] += 1
                if dut.get("name") not in reserved:
                    entry["free-duts"] += 1
    return [pools[name] for name in sorted(pools)]


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
        *_SSH_SKIP_HOST_CHECK,
        "-N",
        "-p", str(client_ssh_port),
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
        result = list(reserves)

    if body.get("active"):
        now = _now_epoch()
        result = [
            r for r in result
            if r.get("valid-from", 0) <= now <= r.get("valid-until", 0)
        ]

    return jsonify(result)


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


@server.route("/conf/dut/enabled", methods=["POST", "PUT"])
def conf_dut_enabled():
    """
    Enable/disable a DUT at runtime. Disabled DUTs are excluded from pool
    lookups (so they cannot be reserved) but existing reservations keep
    working. The change lives in memory only: /conf/reload or a restart
    reverts to the YAML value.
    """
    body = request.get_json(silent=True) or {}
    if not _check_admin_key_from_body(body):
        return jsonify({"error": "invalid admin-key"}), 403

    dut_name = body.get("dut-name")
    if not dut_name:
        return jsonify({"result": -1, "error": "dut-name missing"}), 200

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(
            {"result": -1, "error": "enabled must be a boolean"}), 200

    with state_lock:
        node, dut = _get_dut_and_node_by_name(dut_name)
        if not dut:
            return jsonify({"result": -2, "error": "dut not found"}), 200
        dut.setdefault("metadata", {})["enabled"] = enabled

    return jsonify({"result": 0, "dut-name": dut_name, "enabled": enabled})


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


@server.route("/pools", methods=["POST", "PUT"])
@validate_client
def pools():
    """List reservable pools with their enabled and free DUT counts."""
    return jsonify({"status": 0, "pools": _list_pools()}), 200


@server.route("/reserve", methods=["POST", "PUT"])
@validate_client
@validate_pool
def reserve():
    """Reserve a DUT with low CC: extract checks and logic."""
    duts_in_pool = request.duts_in_pool
    now = _now_epoch()

    # Early check: any available DUTs?
    in_use_duts = _reserved_dut_names(now)
    available = [(node, dut) for (node, dut) in duts_in_pool
                 if dut["name"] not in in_use_duts]
    if not available:
        return _error_response(-4, "all duts in use already")

    node, dut = random.choice(available)

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
        "dut-name": dut["name"],
        "ip": dut["network"]["ip"],
        "ssh-port": dut["network"]["ssh-port"],
        "tunnel-ssh-port": free_port
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

def _run_node_command(node: dict, command: str) -> bool:
    """
    Run a shell command on a node over SSH; True when it exits 0.
    Assumes passwordless SSH (keys) from this service host to node.
    """
    ssh = node["ssh"]
    ip = ssh["ip"]
    port = int(ssh.get("port", 22))
    user = ssh.get("user", "root")

    cmd = ["ssh",
           *_SSH_SKIP_HOST_CHECK,
           "-p", str(port), f"{user}@{ip}", command]
    res = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return res.returncode == 0


def _run_remote_power_script(node: dict, script: str):
    return _run_node_command(node, script)


def _switch_sd_card(node: dict, dut: dict, mode: str) -> bool:
    """
    Point the DUT's SD card mux to `mode` (dut/host/off) via usbsdmux.
    No-op success for DUTs without a storage mux configured.
    """
    control = dut.get("storage", {}).get("control")
    if not control:
        return True
    return _run_node_command(node, f"usbsdmux {shlex.quote(control)} {mode}")


def _power_action_error(action: str, node: dict, dut: dict):
    """
    Run the power action, returning an error message or None on success.
    `on`/`off` also point the SD card mux to dut/off after the script.
    """
    power_info = dut.get("power", {})

    if action == "cycle":
        if not _run_remote_power_script(node, power_info.get("power-off")):
            return "power script failed"
        time.sleep(1)
        if not _run_remote_power_script(node, power_info.get("power-on")):
            return "power script failed"
        return None

    if not _run_remote_power_script(node, power_info.get(f"power-{action}")):
        return "power script failed"

    mux_mode = "dut" if action == "on" else "off"
    if not _switch_sd_card(node, dut, mux_mode):
        return f"usbsdmux switch to {mux_mode} failed"
    return None


@server.route("/power/<action>", methods=["POST", "PUT"])
@validate_token
def power(action):
    if action not in ("on", "off", "cycle"):
        return jsonify({"status": -99, "error": "invalid action"}), 200

    body = request.get_json(silent=True) or {}
    token = body.get("token")
    reserve_entry = _get_reserve_by_token(token)
    node, dut = _get_dut_and_node_by_name(reserve_entry["dut-name"])

    error = _power_action_error(action, node, dut)
    if error:
        return jsonify({"status": -99, "error": error}), 200

    return jsonify({"status": 0}), 200


# ---------------------------------------------------------------------------
# /flash
# ---------------------------------------------------------------------------

# Compressed image formats bmaptool transparently unpacks while writing;
# verification must checksum the same decompressed stream, not the file.
_IMAGE_DECOMPRESSORS = {
    ".gz": "gzip -dc",
    ".bz2": "bzip2 -dc",
    ".xz": "xz -dc",
    ".zst": "zstd -dc",
    ".lz4": "lz4 -dc",
    ".lzo": "lzop -dc",
}


def _image_stream_command(node_tmp_path: str):
    """
    Shell command writing the raw image bytes to stdout, or None when
    the file is already a raw image. Covers the compressed (and
    compressed tar) formats bmaptool decompresses on the fly.
    """
    image = shlex.quote(node_tmp_path)
    name = node_tmp_path.lower()
    for ext, tool in _IMAGE_DECOMPRESSORS.items():
        if name.endswith(ext):
            stream = f"{tool} < {image}"
            if name.endswith(".tar" + ext):
                stream += " | tar -xO"
            return stream
    return None


def _bmap_path_for_image(image_path: str):
    """
    Path of the .bmap file conventionally shipped next to an image: the
    compression suffix is dropped and .bmap appended, so
    foo.wic.bz2 -> foo.wic.bmap and foo.wic -> foo.wic.bmap.

    Returns None for compressed tarballs, where the name of the image
    inside the archive (and hence its bmap) is not derivable.
    """
    name = image_path.lower()
    for ext in _IMAGE_DECOMPRESSORS:
        if name.endswith(ext):
            if name.endswith(".tar" + ext):
                return None
            return image_path[:-len(ext)] + ".bmap"
    return image_path + ".bmap"


def _flash_verify_command(node_tmp_path: str, device: str) -> str:
    """
    Shell command run on the node to check the device actually holds the
    image: compare the image checksum against the same number of bytes
    read back from the device. Compressed images are checksummed over
    their decompressed stream (decompressing twice: size, then hash),
    since that is what bmaptool writes. dd uses direct I/O so the bytes
    come from the medium, not from the page cache still warm from the
    write.
    """
    image = shlex.quote(node_tmp_path)
    dev = shlex.quote(device)
    stream = _image_stream_command(node_tmp_path)
    if stream:
        size_cmd = f"{stream} | wc -c"
        sha_cmd = f"{stream} | sha256sum"
    else:
        size_cmd = f"stat -c %s -- {image}"
        sha_cmd = f"sha256sum -- {image}"
    return (
        f"img_size=$({size_cmd}) && "
        f"img_sha=$({sha_cmd} | awk '{{print $1}}') && "
        f"dev_sha=$(dd if={dev} iflag=direct bs=4M 2>/dev/null"
        f' | head -c "$img_size" | sha256sum | awk \'{{print $1}}\') && '
        f'[ "$img_sha" = "$dev_sha" ]'
    )


# Serializes _flash_and_verify_on_node per node name: usbsdmux/bmaptool
# operate on physical storage shared by all DUTs of a node, so a second
# flash for the same node waits for the first to finish instead of
# racing it. Keyed by name (not the node dict) so the lock survives a
# /conf/reload, which replaces node dicts wholesale.
_node_flash_locks: dict = {}
_node_flash_locks_guard = threading.Lock()


def _get_node_flash_lock(node_name: str):
    """Return the flash lock for a node name, creating it on first use."""
    with _node_flash_locks_guard:
        lock = _node_flash_locks.get(node_name)
        if lock is None:
            lock = threading.Lock()
            _node_flash_locks[node_name] = lock
        return lock


def _flash_and_verify_on_node(
        node: dict, control: str, device: str, node_tmp_path: str,
        node_bmap_path: str = None):
    """
    Expose the SD card to the node, flash the image, read the device back
    to check it holds the image, and hand the card back to the DUT.

    Only one call runs per node at a time; a concurrent call for the
    same node blocks here until the in-progress one finishes.

    With a bmap, bmaptool copies only the blocks the bmap maps and
    checksums them against it while writing. The whole-image read-back
    is then skipped: unmapped areas of the device keep whatever they
    held before, so a linear comparison would never match.
    """
    with _get_node_flash_lock(node["name"]):
        bmap_arg = ("--bmap " + shlex.quote(node_bmap_path)
                    if node_bmap_path else "--nobmap")
        flash_cmd = (
            f"usbsdmux {shlex.quote(control)} host && "
            f"sleep {_USBSDMUX_SETTLE_DELAY} && "
            f"bmaptool copy {bmap_arg} "
            f"{shlex.quote(node_tmp_path)} {shlex.quote(device)}"
        )
        if not _run_node_command(node, flash_cmd):
            raise RuntimeError("flash command failed on node")

        # Verify while the card is still attached to the node, then
        # switch the mux back to the DUT whatever the outcome so it is
        # left in a known state. A failed switch-back is reported ahead
        # of a checksum mismatch: a mux stuck on host needs operator
        # action first.
        verified = True
        if not node_bmap_path:
            verified = _run_node_command(
                node, _flash_verify_command(node_tmp_path, device))
        switched = _run_node_command(
            node, f"usbsdmux {shlex.quote(control)} dut")
        if not switched:
            detail = "" if verified else " (image verification also failed)"
            raise RuntimeError(
                "usbsdmux failed to switch storage back to dut" + detail)
        if not verified:
            raise RuntimeError(
                "flash verification failed: device content does not "
                "match the image")


def _run_scp(source: str, dest: str, port: int) -> bool:
    """Copy one file with scp; True when it exits 0."""
    cmd = ["scp", *_SSH_SKIP_HOST_CHECK, "-P", str(port), source, dest]
    res = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return res.returncode == 0


def _remote_spec(ssh: dict, path: str) -> str:
    """
    scp remote file spec (user@host:path). Remote scp paths go through
    the remote shell, so the path is quoted to keep it a single plain
    argument.
    """
    return f"{ssh.get('user', 'root')}@{ssh['ip']}:{shlex.quote(path)}"


def _stage_optional_bmap(client: dict, node: dict, client_path: str,
                         tmpdir: str, unique_suffix: str):
    """
    Best-effort staging of the .bmap file shipped next to the image
    (client -> service -> node). Returns the node-side path, or None
    when the image has no bmap or it could not be staged, in which case
    the flash falls back to --nobmap.

    A partial local copy left by a failed scp needs no handling here:
    it lives in tmpdir, which the caller removes wholesale.
    """
    bmap_client_path = _bmap_path_for_image(client_path)
    if not bmap_client_path:
        return None

    bmap_name = os.path.basename(bmap_client_path)
    local_path = str(Path(tmpdir) / bmap_name)
    node_path = f"/tmp/{unique_suffix}-{bmap_name}"

    client_ssh = client["ssh"]
    if not _run_scp(_remote_spec(client_ssh, bmap_client_path), local_path,
                    int(client_ssh.get("port", 22))):
        return None

    node_ssh = node["ssh"]
    if not _run_scp(local_path, _remote_spec(node_ssh, node_path),
                    int(node_ssh.get("port", 22))):
        # A failed push can still leave a partial copy on the node
        _run_node_command(node, f"rm -f {shlex.quote(node_path)}")
        return None

    return node_path


def _cleanup_flash_temps(node: dict, tmpdir: str, node_paths):
    """
    Best-effort removal of the staged image/bmap copies.

    The local side is swept whole rather than by known path: a failed
    scp can leave a partial file behind, which would keep the directory
    alive and leak one temp dir per flash. tmpdir always comes from
    mkdtemp() and only ever holds copies we staged there.
    """
    shutil.rmtree(tmpdir, ignore_errors=True)

    remote = " ".join(shlex.quote(p) for p in node_paths if p)
    if remote:
        _run_node_command(node, f"rm -f {remote}")


def _flash_image(node: dict, dut: dict, client: dict, client_path: str):
    """
    1. scp image from client -> service temp dir
    2. scp image from service temp dir -> node temp dir, together with
       the matching .bmap file when the image ships one
    3. ssh to node: run usbsdmux/bmaptool using storage info, then read
       the device back and compare checksums to confirm the flash took
       (read-back skipped for bmap copies, see _flash_and_verify_on_node)

    Requires passwordless SSH/SCP from the service host to both client and
    node.
    """
    storage = dut.get("storage", {})
    control = storage.get("control")  # e.g. /dev/sg2
    device = storage.get("device")    # e.g. /dev/sdc

    if not control or not device:
        raise RuntimeError("storage.control/device missing in config")

    client_ssh = client["ssh"]
    node_ssh = node["ssh"]

    # Normalize basename once; we reuse it for local and remote tmp paths
    image_name = os.path.basename(client_path)

    # tempfile.mkdtemp() guarantees a unique local directory per call, so
    # local_tmp_path never collides across concurrent flashes. The node
    # side has no such directory of its own, so reuse the same unique
    # suffix to keep node_tmp_path collision-free when the same image name
    # is flashed to the same node concurrently.
    tmpdir = tempfile.mkdtemp(prefix="dut-flash-")
    unique_suffix = Path(tmpdir).name
    local_tmp_path = str(Path(tmpdir) / image_name)
    node_tmp_path = f"/tmp/{unique_suffix}-{image_name}"
    node_bmap_path = None

    try:
        # 1) scp from client -> local temp dir
        if not _run_scp(_remote_spec(client_ssh, client_path), local_tmp_path,
                        int(client_ssh.get("port", 22))):
            raise RuntimeError("scp from client failed")

        # 2) scp from local temp dir -> node temp dir
        if not _run_scp(local_tmp_path, _remote_spec(node_ssh, node_tmp_path),
                        int(node_ssh.get("port", 22))):
            raise RuntimeError("scp to node failed")

        # 2b) Bring the .bmap along when the image has one: bmaptool then
        #     copies only the mapped blocks, which is much faster and
        #     checksums the copied data against the bmap.
        node_bmap_path = _stage_optional_bmap(
            client, node, client_path, tmpdir, unique_suffix)

        # 3) ssh to node: flash, verify device content, hand back to DUT.
        #    A failure here points at a bad card/mux on this DUT, so take
        #    it out of rotation; an operator can re-enable it via
        #    /conf/dut/enabled or a config reload.
        try:
            _flash_and_verify_on_node(
                node, control, device, node_tmp_path, node_bmap_path)
        except Exception:
            with state_lock:
                dut.setdefault("metadata", {})["enabled"] = False
            raise

    finally:
        _cleanup_flash_temps(
            node, tmpdir, (node_tmp_path, node_bmap_path))


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
        return jsonify({"status": -99, "error": str(e)}), 200

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

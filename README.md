# dut-control

Flask-based DUT (Device Under Test) reservation and control service with HTTP APIs.

## Overview

`dut-control` manages a pool of DUTs connected behind one or more lab nodes and exposes a small HTTP API to reserve a DUT, control its power, flash images, and query basic reachability status.

The service is implemented as a Flask application and loads its configuration from YAML files describing nodes, DUTs, and clients.

Core pieces:

- **Flask service**: `dut_control.server`
- **Client CLI**: `dut_control.client` (installed as `dut-control-client`)
- **Admin CLI**: `dut_control.admin` (installed as `dut-control-admin`)
- **Admin web UI**: `dut_control.webadmin` (installed as `dut-control-webadmin`)

## Features

- DUT reservation by pool with time-limited tokens
- Automatic reverse SSH tunnel setup from the service to the client to expose the DUT SSH port on a client port
- Lease/cleanup of reservations, including process and port management
- Power control (on/off/cycle) via per-DUT scripts executed over SSH on the node
- Image flashing pipeline using `scp`, `usbsdmux`, and `bmaptool` on the node
- DUT reachability status: `offline`, `ping`, or `ssh`
- Admin endpoints and CLI to inspect configuration, processes, and reservations and to prune expired entries

## Architecture

The system models three main entities:

- **Clients**: machines from which users will ultimately access reserved DUTs; each client has a key, SSH parameters, and a port range for reverse SSH tunnels
- **Nodes**: lab hosts to which DUTs are physically connected; each node has SSH parameters and a list of DUTs
- **DUTs**: devices under test with metadata (including `pool`), network parameters, storage information, and optional power control scripts

Configuration is provided as YAML files under a configuration directory (see below), which the service loads on startup and can reload on demand.

## Requirements

### Runtime

- Python ≥ 3.10
- System tools available on the service host:
  - `ssh` and `scp` (for reverse tunnels, power control, and flashing)
  - `ping` (for basic reachability checks)
- System tools available on nodes (for flashing):
  - `usbsdmux`
  - `bmaptool`

### Python dependencies

From `pyproject.toml` / `requirements.txt`:

- `flask>=2.3`
- `pyyaml>=6.0`
- `requests>=2.31`
- For testing: `pytest>=8.0`

## Installation

Clone the repository and install in a virtual environment:

```bash
git clone https://github.com/psaavedra/dut-control.git
cd dut-control
python -m venv .venv
source .venv/bin/activate
pip install .
```

To install with test extras:

```bash
pip install ".[test]"
```

This will install the `dut_control` package and the console entry points `dut-control`, `dut-control-client`, `dut-control-admin`, and `dut-control-webadmin`.

## Configuration

By default the service reads configuration from a directory named `dut-control` relative to the current working directory.

You can override this with the `DUT_CONTROL_DIR` environment variable pointing to a directory containing at least `conf.yml` and optionally `clients/` and `nodes/` subdirectories.

Example directory layout (the repository ships sample data in `dut_control/data`):

```text
dut-control/
  conf.yml
  clients/
    client-01.yml
  nodes/
    machine-01.yml
```

### conf.yml

`conf.yml` defines the admin key used to protect `/conf/...` endpoints.

```yaml
# dut-control/conf.yml
---
admin-key: 6bdb3138229b7e45
```

The value of `admin-key` must be provided by admin clients when calling admin endpoints.

### Client configuration

Clients are defined under `clients/*.yml`.

Example:

```yaml
- name: client-01
  key: fac72a9494cd132a
  ssh:
    - ip: 192.168.1.246
    - port: 22
    - user: psaavedra
  ports-range:
    - from: 5000
    - to: 5010
```

`ssh` and `ports-range` sections are normalized by the service into dictionaries.

### Node and DUT configuration

Nodes and their DUTs are defined under `nodes/*.yml`.

Example:

```yaml
---
- name: node-01
  ssh:
    - ip: 192.168.1.246
    - port: 22
    - user: runner
  duts:
    - name: rpi5-01
      metadata:
        - pool: rpi5
      network:
        - ip: 192.168.1.105
        - ssh-port: 22
      storage:
        - driver: usbsdmux
        - control: /dev/sg1
        - device: /dev/sda1
      power:
        - driver: script
        - power-on: /script.sh up raspberrypi5-01
        - power-off: /stript.sh down raspberrypi5-01
    - name: rpi5-02
      metadata:
        - pool: rpi5
      network:
        - ip: 192.168.1.12
        - ssh-port: 22
      storage:
        - driver: usbsdmux
        - control: /dev/sg2
        - device: /dev/sda2
      power:
        - driver: script
        - power-on: /script.sh up raspberrypi5-02
        - power-off: /stript.sh down raspberrypi5-02

```

Each DUT must define at least `name`, `metadata.pool`, and `network.ip`; optional sections include storage and power scripts depending on required features.

## Running the service

You can run the Flask service directly via the console script:

```bash
export DUT_CONTROL_DIR=/path/to/dut-control-config
dut-control
```

This invokes `dut_control.server:main()`, which loads configuration and starts the Flask server on `0.0.0.0:8000` with debug enabled.

Alternatively, you can run the module directly:

```bash
python -m dut_control.server
```

For production use you will typically want to run the Flask server behind a WSGI server (e.g. gunicorn) instead of using the built-in development server.

## REST API

All API endpoints use JSON bodies and responses.

### Reservation: /reserve

Reserve a DUT from a given pool.

- **Method**: `POST` (or `PUT`)
- **Path**: `/reserve`
- **Request body**:
  - `client-key` (string, required): client key from configuration
  - `pool` (string, required): pool name (from DUT `metadata.pool`)

On success, the service:

- Selects a DUT from the pool that is not currently reserved
- Finds a free port for the client in its configured `ports-range`
- Creates a reservation token valid for 2 hours
- Starts an SSH reverse tunnel process toward the client

**Success response** (HTTP 200):

```json
{
  "status": 0,
  "token": "<reserve-token>",
  "dut-name": "rpi5-01",
  "ip": "192.168.1.105",
  "ssh-port": 22,
  "tunnel-ssh-port": 5000
}
```

**Error responses** use HTTP 200 with a JSON body containing `status` and `error`:

- `status = -1`: missing or invalid `client-key`
- `status = -2`: missing `pool` or pool does not exist
- `status = -3`: pool exists but is empty
- `status = -4`: all DUTs in pool already reserved or no free ports for client
- `status = -99`: internal error when starting the SSH tunnel

### Lease / release: /lease

Release reservations associated with a client.

- **Method**: `POST` (or `PUT`)
- **Path**: `/lease`
- **Request body**:
  - `client-key` (string, required)
  - One of:
    - `token` (string): release only this reservation
    - `pool` (string): release reservations in the given pool for the client
    - Neither: release all active reservations for the client

On success the service:

- Kills SSH tunnel processes associated with the selected reservations
- Sets `valid-until` to now for those reservations

**Success response**:

```json
{ "status": 0 }
```

If no matching reservations exist, the service still returns `{"status": 0}`.

Internal errors are returned as `{"status": -99, "error": "..."}`.

### Power control: /power/<on|off|cycle>

Control DUT power for an existing reservation.

- **Method**: `POST` (or `PUT`)
- **Path**: `/power/<action>` where `<action>` is `on`, `off`, or `cycle`
- **Request body**:
  - `token` (string, required): reservation token

**Validation**:

- Missing or invalid token, or expired reservation, returns `status = -1` or `-2`
- If the action is not one of `on`, `off`, `cycle`, the service returns `{"status": -99, "error": "invalid action"}`

On success, the service:

- Locates the DUT and node associated with the reservation token
- Reads power scripts from DUT configuration (`power-on` and `power-off`)
- Runs these scripts on the node host over SSH
- For `on`, additionally runs `usbsdmux <control> dut` on the node to hand the SD card back to the DUT, when the DUT has a `storage.control` configured
- For `off`, additionally runs `usbsdmux <control> off` on the node to cut power to the SD card, when the DUT has a `storage.control` configured

If the power script execution fails, the service returns `{"status": -99, "error": "power script failed"}`. If the script succeeds but the SD mux switch fails, it returns `{"status": -99, "error": "usbsdmux switch to <dut|off> failed"}`.

On success it returns `{"status": 0}`.

### Flash image: /flash

Flash an image onto DUT storage via the node.

- **Method**: `POST` (or `PUT`)
- **Path**: `/flash`
- **Request body**:
  - `token` (string, required): reservation token
  - `path` (string, required): path to the image file as seen from the client host

The service performs the following steps:

1. Copies the image from the client to a temporary directory on the service host using `scp`
2. Copies the image from the service host to the node under `/tmp/` using `scp`, together with the image's `.bmap` file when one exists (see **Block maps** below)
3. On the node, runs:
   - `usbsdmux <control> host`
   - `bmaptool copy --bmap <bmap> <image> <device>`, or `bmaptool copy --nobmap <image> <device>` when no `.bmap` was found
4. Verifies the flash on the node by reading back the first image-size bytes of the device (direct I/O, bypassing the page cache) and comparing their SHA-256 checksum against the image's. For compressed images (`.gz`, `.bz2`, `.xz`, `.zst`, `.lz4`, `.lzo`, including compressed tarballs), which `bmaptool` decompresses while writing, the size and checksum are computed over the decompressed stream — the corresponding decompression tool must be available on the node. This step is skipped for `--bmap` copies (see **Block maps**)
5. Runs `usbsdmux <control> dut` on the node to hand the storage back to the DUT (this happens even if verification fails, so the mux is left in a known state)

Storage parameters (`control` and `device`) are read from the DUT `storage` configuration.

#### Block maps

If a `.bmap` file sits next to the image on the client, the service copies it along with the image and passes it to `bmaptool`. The bmap name is derived from the image by dropping the compression suffix and appending `.bmap`, following the usual Yocto layout:

```text
core-image-weston-wpe-raspberrypi5-0.wic.bmap
core-image-weston-wpe-raspberrypi5-0.wic.bz2   <-- path passed to /flash
```

With a bmap, `bmaptool` copies only the blocks the bmap maps (much faster than a full write) and checksums the copied data against the bmap while writing. The bmap is optional and best-effort: if it does not exist, or cannot be copied, the flash proceeds with `--nobmap` and no error is reported. No bmap is guessed for compressed tarballs, whose contained image name is not derivable.

Because a bmap copy writes only mapped blocks, the unmapped areas of the device keep whatever they held before, and the whole-image read-back of step 4 would never match. That verification is therefore skipped for `--bmap` copies, which instead rely on `bmaptool`'s own per-block checksum verification.

Step 3-5 (the node-side flash/verify/switch-back) is serialized per node: if two `/flash` requests target DUTs on the same node at the same time, the second request blocks until the first one finishes instead of racing it on the node's usbsdmux/bmaptool. Flashes to different nodes still run concurrently.

**Responses**:

- Missing `path`: `{"status": -99, "error": "path missing"}`
- Missing client or DUT in configuration: `{"status": -99, "error": "client not found"|"dut not found"}`
- Flash pipeline failure: `{"status": -99, "error": "..."}` (e.g. `"scp from client failed"`, `"flash command failed on node"`)
- Verification mismatch: `{"status": -99, "error": "flash verification failed: ..."}`
- Success: `{"status": 0}`

If the node-side step (3-5) fails; the flash command, the verification, or the mux switch-back; the service additionally marks the DUT as disabled (`metadata.enabled: false`), excluding it from new reservations; such a failure usually means a bad SD card or mux on that DUT. Existing reservations keep working. Re-enable it with `dut-control-admin dut-enable <dut-name>` (or `/conf/reload`) once fixed. Failures copying the image from the client (steps 1-2) do not disable the DUT.

### DUT status: /dut/status

Check simple reachability status for the DUT associated with a reservation.

- **Method**: `POST` (or `PUT`)
- **Path**: `/dut/status`
- **Request body**:
  - `token` (string, required): reservation token

The service:

- Looks up the DUT IP and SSH port from configuration
- Runs `ping` on the DUT IP
- If ping fails, returns `{"status": "offline"}`
- If ping succeeds and a TCP connection to the SSH port works, returns `{"status": "ssh"}`
- If ping succeeds but SSH connection fails, returns `{"status": "ping"}`

### Admin: configuration and state

All admin endpoints require `admin-key` in the JSON body.

If the key does not match the configured `admin-key`, the service returns HTTP 403 and `{"error": "invalid admin-key"}`.

**Endpoints**:

- **`POST /conf/reload`**
  Reloads YAML configuration from `DUT_CONTROL_DIR`; returns `{"result": 0}` on success

- **`POST /conf/info/nodes`**
  Returns the current in-memory list of nodes and DUTs

- **`POST /conf/info/clients`**
  Returns the current in-memory list of clients

- **`POST /conf/info/processes`**
  Returns tracked SSH tunnel processes (pid, reserve token, command, client name, ports in use)

- **`POST /conf/info/reserves`**
  Returns current reservation entries. With `"active": true` in the body, returns only currently valid entries (`valid-from <= now <= valid-until`)

- **`POST /conf/reserves/prune`**
  Prunes expired reservations and returns `{"result": 0, "pruned": <n>}`

- **`POST /conf/dut/enabled`**
  Enables or disables a DUT at runtime. Body: `dut-name` (string, required) and `enabled` (boolean, required), plus `admin-key`. Returns `{"result": 0, "dut-name": ..., "enabled": ...}` on success, `{"result": -1, "error": ...}` for a missing `dut-name` or non-boolean `enabled`, and `{"result": -2, "error": "dut not found"}` for an unknown DUT.
  Disabled DUTs are excluded from pool lookups so they cannot be reserved; existing reservations keep working. The change is in-memory only: `/conf/reload` or a service restart reverts to the `enabled` value from the YAML configuration.

## CLI tools

### dut-control-client

`dut-control-client` is a thin CLI wrapper around the main reservation, lease, power, flash, and status endpoints.

**Environment**:

- `DUT_CONTROL_URL` (optional): base URL of the service (default `http://localhost:8000`)
- `DUT_CONTROL_CLIENT_KEY` (required for `reserve` and `lease`): client key matching configuration

**Global options**:

- `-u, --url`: override base URL (default uses `DUT_CONTROL_URL` or built-in default)
- `--timeout`: HTTP timeout in seconds (default 10.0)

**Subcommands**:

- **`reserve <pool>`**
  Reserves a DUT from the given pool and prints `token`, `dut-name`, `ip`, `ssh-port`, and `tunnel-ssh-port` to stdout

- **`lease [--token TOKEN | --pool POOL | --all] [-q|--quiet]`**
  Releases reservations for the current client, filtered by token or pool, or all; prints `lease: ok` on success unless `--quiet` is used

- **`power <on|off|cycle> <token> [-q|--quiet]`**
  Calls `/power/<action>` for the given reservation token; prints `power: ok` on success unless `--quiet` is used

- **`flash <token> <path> [-q|--quiet]`**
  Calls `/flash` with the given token and image path; prints `flash: ok` on success unless `--quiet` is used

- **`status <token>`**
  Calls `/dut/status` and prints one of `offline`, `ping`, or `ssh`

**Example usage**:

```bash
export DUT_CONTROL_URL=http://lab-controller:8000
export DUT_CONTROL_CLIENT_KEY=fac72a9494cd132a

# Reserve a DUT from pool "rpi5"
dut-control-client reserve rpi5

# Release all reservations for this client
dut-control-client lease --all

# Power cycle a reserved DUT
dut-control-client power cycle "$TOKEN"

# Flash an image for a reservation token
dut-control-client flash "$TOKEN" /images/rpi5-image.wic

# Check DUT status
dut-control-client status "$TOKEN"
```

### dut-control-admin

`dut-control-admin` is the admin CLI for `/conf/...` endpoints.

**Environment**:

- `DUT_CONTROL_URL` (optional): base URL of the service
- `DUT_CONTROL_ADMIN_KEY` (required): admin key matching `conf.yml`

**Global options**:

- `-u, --url`: override base URL (default uses `DUT_CONTROL_URL` or built-in default)
- `--timeout`: HTTP timeout in seconds (default 10.0)

**Subcommands**:

- **`reload`**
  Calls `/conf/reload` and prints the JSON response

- **`nodes`**
  Calls `/conf/info/nodes` and prints nodes and DUTs as JSON

- **`clients`**
  Calls `/conf/info/clients` and prints clients as JSON

- **`processes`**
  Calls `/conf/info/processes` and prints active SSH tunnel processes

- **`reserves [-a|--active]`**
  Calls `/conf/info/reserves` and prints current reservations; with `--active`, only currently valid ones (`valid-from <= now <= valid-until`)

- **`prune`**
  Calls `/conf/reserves/prune` and prints how many reservations were pruned

- **`dut-enable <dut-name>`** / **`dut-disable <dut-name>`**
  Calls `/conf/dut/enabled` to enable or disable a DUT at runtime and prints the JSON response; exits with status 1 if the service reports an error (e.g. unknown DUT). The change lasts until the next `reload` or service restart.

**Example**:

```bash
export DUT_CONTROL_URL=http://lab-controller:8000
export DUT_CONTROL_ADMIN_KEY=6bdb3138229b7e45

# Reload configuration
dut-control-admin reload

# Inspect nodes and DUTs
dut-control-admin nodes

# Prune expired reservations
dut-control-admin prune

# Take a misbehaving DUT out of rotation, then bring it back
dut-control-admin dut-disable rpi5-03
dut-control-admin dut-enable rpi5-03
```

### dut-control-webadmin

`dut-control-webadmin` is a browser UI for the `/conf/...` admin endpoints. It is a **separate HTTP service**: it renders HTML and proxies every call to dut-control server side with `requests`, exactly as `dut-control-admin` does. It never imports the service module, so it can run on a different host.

A proxy is not a stylistic choice. Every dut-control endpoint accepts only `POST`/`PUT`, takes its secret in the JSON body, and sends no CORS headers, so a browser cannot call the API directly at all.

**Environment**:

- `DUT_CONTROL_URL` (optional): base URL of the dut-control service (default `http://localhost:8000`)
- `DUT_CONTROL_WEBADMIN_SECRET` (optional): Flask session signing key. When unset, a random one is generated per start, so logins do not survive a restart

**Options**:

- `-u, --url`: dut-control base URL (default uses `DUT_CONTROL_URL` or the built-in default)
- `--host`: address to listen on (default `0.0.0.0`)
- `--port`: port to listen on (default `8080`)
- `--timeout`: HTTP timeout in seconds for API calls (default 10.0)

**Authentication**: there is no admin key on the command line. Opening the UI shows a login form; the key is checked against the service and then held **server side**, keyed by an opaque session id. The browser cookie carries only that id, so the admin key is never sent to the browser and never crosses the network after login. Logging out, or a restart, drops it.

**Views**:

- **nodes and DUTs**: one table per node, with each DUT's pool, enabled state, and its network, storage and power sections rendered generically, so keys added to a node YAML file show up rather than being dropped
- **clients**: configured clients with SSH details and tunnel port ranges
- **ssh tunnel processes**: the tunnels the service holds open, with pid, ports in use, owning reservation token and the full ssh command

A client key is enough to reserve DUTs, so the clients view masks each one (`fac7...132a`) until you expand it. Expanding puts the full key in that page, so treat it as you would `dut-control-admin clients` output.

**Example usage**:

```bash
export DUT_CONTROL_URL=http://lab-controller:8000
export DUT_CONTROL_WEBADMIN_SECRET=$(openssl rand -hex 32)

dut-control-webadmin --port 8080
# then open http://localhost:8080/ and log in with the admin key
```

**Security notes**:

- The service is an authenticated admin proxy. It binds `0.0.0.0` by default, matching `dut-control` itself, so restrict it to a trusted network or bind `--host 127.0.0.1`.
- It runs with Flask debug off, deliberately: the Werkzeug debugger would expose an interactive console on a process holding the admin key.
- State-changing requests carry a per-session CSRF token, and the session cookie is `HttpOnly` with `SameSite=Lax`.
- A public login form is an online guessing oracle against the admin key; there is no rate limiting.

## Testing

To run the unit tests:

```bash
pip install ".[test]"
pytest
```

## License

The project is licensed under the MIT License.

## Author

Pablo Saavedra (psaavedra@igalia.com)

## Links

- **Homepage**: https://github.com/psaavedra/dut-control
- **Source**: https://github.com/psaavedra/dut-control.git

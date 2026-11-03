# dut-control

Flask-based DUT (Device Under Test) reservation and control service with HTTP APIs.

## Overview

`dut-control` manages a pool of DUTs connected behind one or more lab nodes and exposes a small HTTP API to reserve a DUT, control its power, flash images, and query basic reachability status.

The service is implemented as a Flask application and loads its configuration from YAML files describing nodes, DUTs, and clients.

Core pieces:

- **Flask service**: `dut_control.server`
- **Client CLI**: `dut_control.client` (installed as `dut-control-client`)
- **Admin CLI**: `dut_control.admin` (installed as `dut-control-admin`)

## Features

- DUT reservation by pool with time-limited tokens
- Automatic reverse SSH tunnel setup from the service to the client to expose the DUT SSH port on a client port
- Client SSH address/port overrides announced per request, for clients behind NAT or with a dynamic address
- Lease/cleanup of reservations, including process and port management
- Power control (on/off/cycle) via per-DUT scripts executed over SSH on the node
- Image flashing pipeline using `scp`, `usbsdmux`, and `bmaptool` on the node
- Quick storage wipe through the node, for handing a device back without leaving the last run on it
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

The client and admin CLIs need only `requests>=2.31`, which is what a plain install brings. The service needs `flask>=2.3` and `pyyaml>=6.0` on top, which come from the `server` extra, so a machine that only drives DUTs does not install a web framework it never starts. For testing: `pytest>=8.0`, which pulls the server extra along.

## Installation

Clone the repository and install in a virtual environment:

```bash
git clone https://github.com/psaavedra/dut-control.git
cd dut-control
python -m venv .venv
source .venv/bin/activate
pip install .
```

That installs the three console scripts, but only the dependencies the client and admin CLIs use. To run the service, ask for the `server` extra:

```bash
pip install ".[server]"
```

Starting `dut-control` without it fails on a missing `flask`. To install with test extras, which include the server:

```bash
pip install ".[test]"
```

This will install the `dut_control` package and the console entry points `dut-control`, `dut-control-client`, and `dut-control-admin`.

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
max-upload-bytes: 8589934592
```

`max-upload-bytes` is optional and caps an uploaded image; it defaults to 8 GiB.

The value of `admin-key` must be provided by admin clients when calling admin endpoints.

#### Accounting

An optional `accounting` section gives each account a budget of tokens. Tokens pay for reserved time, and a pool can cost more than another:

```yaml
accounting:
  - enabled: true
  - enforcement: observe
  - token-minutes: 120
  - default-monthly-token-limit: 40
  - default-weekly-recharge-tokens: 10
  - default-balance-cap: 40
  - recharge-weekday: 1
  - recharge-hour-utc: 0
  - database: /var/lib/dut-control/accounting.sqlite3
  - pool-costs:
      rpi4: 1
      rpi5: 2
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn accounting on. Without the section, or with `false`, nothing changes. |
| `enforcement` | `observe` | `observe` only records, `warn` lets a reservation through with a warning, `reject` refuses it. |
| `token-minutes` | `120` | Minutes of reservation that one token pays for in a pool of cost 1. |
| `default-monthly-token-limit` | `40` | Most tokens the weekly recharges give in one calendar month (UTC). |
| `default-weekly-recharge-tokens` | `10` | Tokens added at each weekly recharge. |
| `default-balance-cap` | the monthly limit of the account | Most tokens an account can hold. |
| `recharge-weekday` | `1` | ISO day of the recharge, 1 (Monday) to 7 (Sunday). |
| `recharge-hour-utc` | `0` | Hour of the recharge, 0 to 23, in UTC. |
| `database` | `accounting.sqlite3` in `DUT_CONTROL_STATE_DIR`, else in the config directory | Where the balances and the history are kept. |
| `pool-costs` | `{}` | Cost of each pool; a pool not listed costs 1. A reservation of 120 minutes in a pool of cost 2 takes 2 tokens. |

The `default-*` values apply to every client without its own `quota`; see [Client configuration](#client-configuration). An unknown key or a bad value (a negative number, `token-minutes: 0`, a cost of 0, `recharge-hour-utc: 24`) makes the configuration fail to load, since a typo here would change what a team may spend.

With accounting on, `/power`, `/flash` and `/wipe` also log each call on a valid reservation to the `activity_events` table of the database, success or failure. They cost no tokens. A row has the account, the pool and DUT of the reservation, the result and, for a failure, the error cut at its first colon, so a path that follows it is not kept. The metadata is `action` for `/power`; `mode` (`named` or `upload`), the bare `image-name` and, for an upload, `bmap` for `/flash`; `size-bytes` for `/wipe`. Neither the reservation token nor an image path is stored. `/dut/status` is not logged, so `wait` adds nothing. There is no endpoint for this table yet; read it with `sqlite3`:

```bash
sqlite3 accounting.sqlite3 "SELECT datetime(ts, 'unixepoch'), account_id, action, dut_name, result, error FROM activity_events ORDER BY id DESC LIMIT 20"
```

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

An optional `allowed-pools` list restricts which pools a client can list and reserve from:

```yaml
- name: client-02
  key: 6b0c1e4f2a7d9b83
  ssh:
    - ip: 192.168.1.247
    - port: 22
    - user: ci
  ports-range:
    - from: 5100
    - to: 5110
  allowed-pools:
    - rpi5-fast
```

A client without the key, like `client-01` above, reaches every pool, so configurations written before the key keep working; `allowed-pools: []` denies every pool. The value must be a YAML list of pool names, and a scalar, a mapping, an empty entry or a bare `yes` (which YAML reads as a boolean) makes the configuration fail to load rather than quietly changing who can reach what. Repeated names are dropped. A name is not checked against the configured pools, since a pool may be added, removed or disabled later without making the policy invalid; a permitted pool that does not exist simply never shows up.

The policy covers discovery and new reservations only: a reservation already handed out survives a `/conf/reload` that drops its pool, so its token can still be used and released. `/conf/info/clients` reports `allowed-pools` for the clients that have one.

Two optional keys tie a client to the [accounting](#accounting) budget:

```yaml
- name: client-03
  key: 0c5e7a1d9f2b4863
  account-id: team-browser
  ssh:
    - ip: 192.168.1.248
    - user: ci
  ports-range:
    - from: 5200
    - to: 5210
  quota:
    - enabled: true
    - monthly-token-limit: 80
    - weekly-recharge-tokens: 20
    - balance-cap: 80
    - max-active-reservations: 2
    - pool-concurrency:
        rpi5-fast: 1
    - reserve-rate:
        - requests: 10
        - window-seconds: 60
```

`account-id` names the account the client spends from; it defaults to the client `name`. The history is kept by account, so a new `key` with the same `account-id` keeps it. Two clients can share an account, for example the old and the new key during a rotation, but then they must have the same `quota`.

`quota` overrides the defaults of `conf.yml` for that account. `monthly-token-limit`, `weekly-recharge-tokens` and `balance-cap` take the place of the `default-*` keys. `max-active-reservations` caps the reservations held at the same time, and `pool-concurrency` does the same per pool. `reserve-rate` caps the `/reserve` calls in a time window. `enabled: false` makes the account exempt: its use is still recorded but never refused. As for `allowed-pools`, a bad value makes the configuration fail to load. `/conf/info/clients` shows both keys for the clients that have them.

`ssh.ip` and `ssh.port` are the address the service uses to reach the client back (reverse tunnel and image `scp`). A client that is not reachable at that address, typically because it sits behind NAT and only knows its public address and forwarded port at run time, can announce the right values on each request; see [Client SSH overrides](#client-ssh-overrides). Such a client still needs an `ssh` section in its YAML entry, if only for `user`; the announced values replace the configured ones in memory.

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
        - pool: rpi5-fast
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

Each DUT must define at least `name`, at least one pool, and `network.ip`; optional sections include storage and power scripts depending on required features.

A DUT can belong to several pools, so the same device can be offered both in a broad pool and in a narrower one. Any of these forms is accepted, and they can be mixed:

```yaml
      metadata:
        - pool: rpi5            # single pool
```

```yaml
      metadata:
        - pool: rpi5            # repeated key, accumulated
        - pool: rpi5-fast
```

```yaml
      metadata:
        - pools:                # list of names
            - rpi5
            - rpi5-fast
```

Unlike the rest of the configuration sections, where a repeated key keeps the last value, pool entries are accumulated. Once loaded, the DUT metadata always exposes the resolved list as `pools` (visible in `/conf/info/nodes`). A DUT is reservable through any of its pools, and reserving it through one pool makes it unavailable in all the others.

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

### Client SSH overrides

Any endpoint that takes a `client-key` (`/pools`, `/reserve`, `/lease`), plus `/flash`, accepts two optional fields overriding the SSH parameters the service uses to connect back to the client:

- `client-ssh-ip` (string): address to use instead of the configured `ssh.ip`. An IPv4 or IPv6 literal, or a DNS name (dynamic DNS is the usual way a NATed client is addressed)
- `client-ssh-port` (integer, or its decimal string form): port to use instead of the configured `ssh.port`, in the range 1-65535

This is what makes a client behind NAT usable: the client announces the address and forwarded port at which the service can actually reach it, which its static YAML entry cannot know.

The service applies the announced values to the in-memory client and records which fields no longer come from the configuration in the client attribute `ssh-overrides`, a sorted list holding `"ip"`, `"port"`, or both. The attribute is visible in `/conf/info/clients` and therefore identifies the dynamically addressed clients:

```json
{
  "name": "client-01",
  "key": "fac72a9494cd132a",
  "ssh": { "ip": "203.0.113.9", "port": 2222, "user": "psaavedra" },
  "ports-range": { "from": 5000, "to": 5010 },
  "ssh-overrides": ["ip", "port"]
}
```

Notes:

- The override persists in memory until the client announces a different value, so a reservation made with an override keeps working for later `/power`, `/flash`, and `/dut/status` calls that carry no override of their own
- A request announcing only one of the two fields leaves the other override in place, and `ssh-overrides` accumulates accordingly
- `ssh.user` is never overridable; it always comes from the configuration
- Overrides are in-memory only: `/conf/reload` or a service restart rebuilds the client list from the YAML and drops both the values and the `ssh-overrides` marker
- Both fields are validated before use and a field that is present but unusable fails the request with `status = -5` instead of silently falling back to the configured value, so a typo does not send the service connecting to the wrong host. The address ends up in an `ssh user@host` argument, so a value that is neither a well-formed address literal nor a DNS name is rejected: that covers empty or non-string values, malformed literals such as `203.0.113.999`, and anything carrying whitespace, shell metacharacters, a `user@` prefix or a `:port` suffix. The port is rejected unless it is a decimal number in the 1-65535 range (booleans included, which JSON allows where an integer is expected)

### Pool listing: /pools

List the pools a client can reserve from, with their DUT counts.

- **Method**: `POST` (or `PUT`)
- **Path**: `/pools`
- **Request body**:
  - `client-key` (string, required): client key from configuration

**Success response** (HTTP 200):

```json
{
  "status": 0,
  "pools": [
    { "name": "raspberrypi4-64bit", "enabled-duts": 1, "free-duts": 1 },
    { "name": "rpi5", "enabled-duts": 3, "free-duts": 1 }
  ]
}
```

`enabled-duts` counts the DUTs in the pool that are enabled; `free-duts` counts those of them without an unexpired reservation, so it is what `/reserve` could currently hand out. A DUT in several pools is counted in each of them. Pools are sorted by name, and a pool whose DUTs are all disabled is omitted, since it cannot be reserved from at all. The listing is limited to the pools the client is allowed to use, so a client whose `allowed-pools` is empty gets `{"status": 0, "pools": []}`; see [Client configuration](#client-configuration).

With [accounting](#accounting) on, each pool also says what a reservation in it costs, and whether the balance of the client's account covers it:

```json
{
  "name": "rpi5",
  "enabled-duts": 3,
  "free-duts": 1,
  "token-cost": 2,
  "reservation-minutes": 120,
  "reservation-tokens": 2.0,
  "affordable": true
}
```

`token-cost` is the cost of the pool in `pool-costs`, and `reservation-tokens` what one reservation of `reservation-minutes` takes from the balance. The call applies the recharges that are due before it compares.

Missing or invalid `client-key` returns `status = -1`; an invalid SSH override returns `status = -5` (see [Client SSH overrides](#client-ssh-overrides)).

### Reservation: /reserve

Reserve a DUT from a given pool.

- **Method**: `POST` (or `PUT`)
- **Path**: `/reserve`
- **Request body**:
  - `client-key` (string, required): client key from configuration
  - `pool` (string, required): pool name (from DUT `metadata.pools`)
  - `client-ssh-ip` (string, optional) and `client-ssh-port` (integer, optional): address the service must use to reach the client back, see [Client SSH overrides](#client-ssh-overrides)

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
  "tunnel-ssh-port": 5000,
  "client-ssh-overrides": ["ip", "port"]
}
```

`client-ssh-overrides` echoes back the SSH fields of the client that are currently overridden, and is an empty list for a client using its configured address.

With [accounting](#accounting) on, the reservation is charged to the account of the client and the answer has four more keys:

```json
{
  "account-id": "team-browser",
  "charged-tokens": 2.0,
  "tokens-available": 8.0,
  "valid-until": 1790623200
}
```

The price is set by the pool in the request, not by the other pools of the DUT, so a DUT in a cheap pool and an expensive pool costs the price of the pool in the request. If the SSH tunnel does not start, the reservation is dropped and the whole charge goes back.

Before it picks a DUT, `/reserve` checks the quota of the account, in this order: the `reserve-rate` of the client, its `max-active-reservations` and `pool-concurrency`, then the balance against the price. What happens when a check fails depends on `enforcement`:

| `enforcement` | Result |
|---|---|
| `observe` | The reservation goes through; the balance can go below zero. |
| `warn` | The same, and the answer lists the failed checks: `"quota-warning": ["quota_exceeded"]`. |
| `reject` | The reservation is refused with the first failed check, and nothing is charged. |

An account with `quota.enabled: false` is always in `observe`. `pool-concurrency` counts the reservations of the account on the DUTs of the pool, so a DUT reserved through another pool it belongs to counts too. A `/reserve` call counts toward `reserve-rate` whatever its result, except a call refused for the rate itself, so a client that waits gets through.

A refusal carries an `error-code` for scripts. For lack of tokens it also says when that clears:

```json
{
  "status": -6,
  "error": "quota exceeded",
  "error-code": "quota_exceeded",
  "tokens-required": 3.0,
  "tokens-available": 1.5,
  "next-recharge-at": 1790640000,
  "next-recharge-at-iso": "2026-09-29T00:00:00Z",
  "next-recharge-tokens": 10.0
}
```

**Error responses** use HTTP 200 with a JSON body containing `status` and `error`:

- `status = -1`: missing or invalid `client-key`
- `status = -2`: missing `pool`, pool not in the client's `allowed-pools`, or pool does not exist
- `status = -3`: pool exists but is empty
- `status = -4`: all DUTs in pool already reserved or no free ports for client
- `status = -5`: invalid `client-ssh-ip` or `client-ssh-port`
- `status = -6`, `error-code = quota_exceeded`: the balance does not cover the reservation; wait for `next-recharge-at` or an admin credit
- `status = -8`, `error-code = concurrency_limit`: the account holds as many reservations as `max-active-reservations`, or as `pool-concurrency` allows in the pool; release one first
- `status = -9`, `error-code = rate_limited`: too many `/reserve` calls; `retry-after` says in how many seconds to try again
- `status = -99`: internal error when starting the SSH tunnel or charging the reservation

### Lease / release: /lease

Release reservations associated with a client.

- **Method**: `POST` (or `PUT`)
- **Path**: `/lease`
- **Request body**:
  - `client-key` (string, required)
  - `client-ssh-ip` / `client-ssh-port` (optional): see [Client SSH overrides](#client-ssh-overrides)
  - One of:
    - `token` (string): release only this reservation
    - `pool` (string): release reservations in the given pool for the client
    - Neither: release all active reservations for the client

On success the service:

- Kills SSH tunnel processes associated with the selected reservations
- Sets `valid-until` to now for those reservations
- With [accounting](#accounting) on, gives back the unused whole minutes of each one; a started minute counts as used

A reservation gets one refund at most, however many times it is released. The refund can take the balance over the cap, since these are the account's own tokens, and it does not count toward the monthly limit. A reservation that runs to its end, or one removed by `/conf/reserves/prune`, gets nothing back.

**Success response**:

```json
{ "status": 0 }
```

If no matching reservations exist, the service still returns `{"status": 0}`.

Internal errors are returned as `{"status": -99, "error": "..."}`.

### Account: /account

The token balance of the client's account and its next recharge. See [Accounting](#accounting).

- **Method**: `POST` (or `PUT`)
- **Path**: `/account`
- **Request body**: `client-key` (string, required)

**Success response**:

```json
{
  "status": 0,
  "account-id": "team-browser",
  "tokens-available": 31.5,
  "monthly-token-limit": 80.0,
  "weekly-recharge-tokens": 20.0,
  "balance-cap": 80.0,
  "period-start": 1788220800,
  "period-end": 1790812800,
  "period-granted": 40.0,
  "next-recharge-at": 1791158400,
  "next-recharge-at-iso": "2026-10-05T00:00:00Z",
  "next-recharge-tokens": 20.0,
  "active-reservations": 1
}
```

The call first applies the recharges that are due. `period-start` and `period-end` bound the calendar month (UTC), and `period-granted` is what the recharges gave in it. `next-recharge-tokens` is what the next recharge will add, after the monthly limit and the cap. `active-reservations` counts the unexpired reservations of every client of the account. Times are epoch seconds.

**Error responses**:

- `status = -1`: missing or invalid `client-key`
- `status = -10`: accounting is off on this service

A service from before accounting answers with HTTP 404.

### Account history: /account/history

The charges, refunds and recharges of the client's account, newest first.

- **Method**: `POST` (or `PUT`)
- **Path**: `/account/history`
- **Request body**:
  - `client-key` (string, required)
  - `since`, `until` (integers, optional): only entries from `since` and before `until`, in epoch seconds
  - `kind` (string, optional): only one kind of entry: `recharge`, `charge`, `refund`, `reserve-rollback` or `admin`
  - `pool` (string, optional): only entries for that pool
  - `limit` (integer, optional): entries per page; 100 by default, 1000 at most
  - `before` (integer, optional): only entries older than this id, to read the next page

**Success response**:

```json
{
  "status": 0,
  "account-id": "team-browser",
  "entries": [
    {
      "id": 7,
      "ts": 1790600000,
      "ts-iso": "2026-09-27T15:33:20Z",
      "kind": "charge",
      "tokens": -2.0,
      "balance-after": 31.5,
      "pool": "rpi5",
      "dut-name": "rpi5-01",
      "minutes": 120,
      "actor": "client-01",
      "reason": null
    }
  ],
  "next-before": 7
}
```

`tokens` is negative for what leaves the balance. A full page sets `next-before` to the id to pass as `before` for the next one; otherwise it is `null`. Entries never hold the reservation token.

**Error responses**: as for `/account`, plus `status = -99` for a filter that is not a non-negative integer.

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
There are two ways in, and the content type picks between them.

**Naming the image** (`application/json`), where the service fetches it back off the client:

- **Request body**:
  - `token` (string, required): reservation token
  - `path` (string, required): path to the image file as seen from the client host
  - `client-ssh-ip` (string, optional) and `client-ssh-port` (integer, optional): address the service must use to `scp` the image off the client, see [Client SSH overrides](#client-ssh-overrides). Accepted here as well as on `/reserve` because a client with a dynamic address may have moved since it reserved

**Uploading it** (`multipart/form-data`), where the client sends it with the request:

- **Form fields**:
  - `token` (required): reservation token
  - `image` (file, required): the image itself, streamed to the service's staging directory
  - `bmap` (file, optional): its block map. The service cannot go looking for one here, so a client that has it sends it

Uploading asks nothing of the client but the request, so it works from a container, from behind NAT, and from anywhere the service has no SSH route back to. The filename is reduced to a bare name before it is used, and the request is capped by `max-upload-bytes` in `conf.yml` (8 GiB by default); over that, the reply is `{"status": -99, "error": "upload is larger than ..."}` rather than an HTTP error page.

Once the image is staged, both modes are the same. The service performs the following steps:

1. Copies the image from the client to a temporary directory on the service host using `scp`, or takes it from the upload
2. Copies the image from the service host to the node under `/tmp/` using `scp`, together with the image's `.bmap` file when one exists (see **Block maps** below)
3. On the node, runs:
   - `usbsdmux <control> host`
   - `bmaptool copy --bmap <bmap> <image> <device>`, or `bmaptool copy --nobmap <image> <device>` when no `.bmap` was found
4. Verifies the flash on the node by reading back the first image-size bytes of the device (direct I/O, bypassing the page cache) and comparing their SHA-256 checksum against the image's. For compressed images (`.gz`, `.bz2`, `.xz`, `.zst`, `.lz4`, `.lzo`, including compressed tarballs), which `bmaptool` decompresses while writing, the size and checksum are computed over the decompressed stream — the corresponding decompression tool must be available on the node. This step is skipped for `--bmap` copies (see **Block maps**)
5. Runs `usbsdmux <control> dut` on the node to hand the storage back to the DUT (this happens even if the flash or the verification fails, so the mux is left in a known state)

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
- Invalid SSH override: `{"status": -5, "error": "client-ssh-ip is not valid"|"client-ssh-port is not valid"}`
- Missing client or DUT in configuration: `{"status": -99, "error": "client not found"|"dut not found"}`
- Flash pipeline failure: `{"status": -99, "error": "..."}` (e.g. `"scp from client failed"`, `"flash command failed on node"`)
- Verification mismatch: `{"status": -99, "error": "flash verification failed: ..."}`
- Success: `{"status": 0}`

If the node-side step (3-5) fails; the flash command, the verification, or the mux switch-back; the service additionally marks the DUT as disabled (`metadata.enabled: false`), excluding it from new reservations; such a failure usually means a bad SD card or mux on that DUT. Existing reservations keep working. Re-enable it with `dut-control-admin dut-enable <dut-name>` (or `/conf/reload`) once fixed. Failures copying the image from the client (steps 1-2) do not disable the DUT.

### Wipe storage: /wipe

Overwrite the head of the DUT storage with zeros, through the node.

- **Method**: `POST` (or `PUT`)
- **Path**: `/wipe`
- **Request body**:
  - `token` (string, required): reservation token
  - `size` (integer, optional): how many bytes to overwrite, rounded up to a whole MiB. Defaults to 128 MiB

On the node, under the same per-node lock a flash takes, the service runs:

- `usbsdmux <control> host`
- `dd if=/dev/zero of=<device> bs=1M count=<N> oflag=direct conv=fsync`
- `usbsdmux <control> dut`, whatever the outcome, so the mux is left in a known state

This goes the same way a flash does rather than through the DUT itself, so it needs no SSH to the device, no credentials and no working userspace: it still runs after a test run that crashed it. And it overwrites blocks rather than unlinking files, so nothing is left in the free space for the next tenant to read back.

What the 128 MiB default reaches depends on the image layout. On the usual one it takes out the partition table, so nothing mounts by label and the device will not boot, all of `/boot`, and the head of the root filesystem including its superblock. It does not reach a data partition sitting after the root filesystem. It is a quick wipe, not a scrub; raise `size` past the root filesystem if something must not survive on the card at all.

**Responses**:

- Invalid `size`: `{"status": -99, "error": "size is not a positive number of bytes"}`
- Missing DUT in configuration: `{"status": -99, "error": "dut not found"}`
- No storage configured: `{"status": -99, "error": "storage.control/device missing in config"}`
- Wipe pipeline failure: `{"status": -99, "error": "..."}`
- Success: `{"status": 0}`

As with `/flash`, a failure on the node marks the DUT as disabled: it usually means a bad SD card or mux. Re-enable it with `dut-control-admin dut-enable <dut-name>` once fixed.

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
  Returns the current in-memory list of clients, which is where the effective `allowed-pools` policy of each one can be read

- **`POST /conf/info/processes`**
  Returns tracked SSH tunnel processes (pid, reserve token, command, client name, ports in use)

- **`POST /conf/info/reserves`**
  Returns current reservation entries. With `"active": true` in the body, returns only currently valid entries (`valid-from <= now <= valid-until`)

- **`POST /conf/reserves/prune`**
  Prunes expired reservations and returns `{"result": 0, "pruned": <n>}`

- **`POST /conf/dut/enabled`**
  Enables or disables a DUT at runtime. Body: `dut-name` (string, required) and `enabled` (boolean, required), plus `admin-key`. Returns `{"result": 0, "dut-name": ..., "enabled": ...}` on success, `{"result": -1, "error": ...}` for a missing `dut-name` or non-boolean `enabled`, and `{"result": -2, "error": "dut not found"}` for an unknown DUT.
  Disabled DUTs are excluded from pool lookups so they cannot be reserved; existing reservations keep working. The change is in-memory only: `/conf/reload` or a service restart reverts to the `enabled` value from the YAML configuration.

### Admin: accounts

These endpoints manage the [accounting](#accounting). An account is named by one of its clients, in `client`, since the quota of the account comes from the client configuration. They return `{"result": -10, "error": "accounting is off"}` when accounting is off, and `{"result": -1, "error": "client not found"}` for an unknown client.

- **`POST /conf/account/info`**
  Body: `client`. Returns what [`/account`](#account-account) returns, plus `enforcement` (the mode that applies to the account) and `clients` (every client of the account).

- **`POST /conf/account/recharge`**
  Body: `client`, `tokens` (a number; negative takes tokens away), `reason` and `idempotency-key` (both non-empty strings). Adds the tokens and returns the account as `info` does. The same key applies once per account, so a request that timed out can be sent again. An admin credit is not a recharge: it ignores the cap and the monthly limit. A missing field, or a change that would take the balance below zero, returns `result = -2`.

- **`POST /conf/account/set-balance`**
  As `recharge`, but `tokens` (zero or more) is the new balance. The ledger keeps the difference, with the reason.

- **`POST /conf/account/ledger`**
  Body: `client`, plus the filters of [`/account/history`](#account-history-accounthistory). Returns the same page; a bad filter returns `result = -2`.

- **`POST /conf/account/report`**
  Body: `since` and `until` (epoch seconds), `client` and `pool`, all optional. Returns the ledger totals by account, pool and kind:

  ```json
  {
    "result": 0,
    "rows": [
      {"account-id": "team-browser", "pool": "rpi5", "kind": "charge",
       "entries": 12, "tokens": -24.0, "minutes": 1440}
    ]
  }
  ```

  `minutes` is the reserved time of the charges. Refunds are in their own row, so the time used is the charged time less what the refunds gave back.

## CLI tools

### dut-control-client

`dut-control-client` is a thin CLI wrapper around the main reservation, lease, power, flash, and status endpoints.

**Environment**:

- `DUT_CONTROL_URL` (optional): base URL of the service (default `http://localhost:8000`)
- `DUT_CONTROL_CLIENT_KEY` (required for `pools`, `reserve`, `lease`, `account` and `account-history`): client key matching configuration
- `DUT_CONTROL_TOKEN` (optional): reservation token for `power`, `flash`, `status` and `lease`, used when none is given on the command line. Preferred over the positional argument on a shared host, where a token on a command line is readable in `ps` by every other user and ends up in any log that echoes the command
- `DUT_CONTROL_CLIENT_SSH_IP` (optional): address the service must use to reach this host back, overriding the `ssh.ip` of its configuration entry
- `DUT_CONTROL_CLIENT_SSH_PORT` (optional): port to use instead of the configured `ssh.port`

`DUT_CONTROL_CLIENT_SSH_IP` and `DUT_CONTROL_CLIENT_SSH_PORT` are sent by the `reserve` and `flash` subcommands, the two that make the service connect back to this host, and are simply omitted when unset. Both are validated by the CLI before any request is made (same rules as the service, see [Client SSH overrides](#client-ssh-overrides)); an invalid value aborts the command with `error: <VARIABLE> is not a valid value: ...` on stderr and exit status 1, so the variable at fault is named where it was set.

**Global options**:

- `-u, --url`: override base URL (default uses `DUT_CONTROL_URL` or built-in default)
- `--timeout`: HTTP timeout in seconds (default 10, or 900 for `flash` and `wipe`). Those two wait for the node to settle the mux and write to the card, which no default meant for a lookup can cover. Being a global option it goes **before** the subcommand: `dut-control-client --timeout 1800 wipe`

**Subcommands**:

- **`pools [--json]`**
  Calls `/pools` and prints one row per reservable pool with its enabled and free DUT counts (`--json` prints the service response instead, with the token prices when accounting is on):

  ```text
  POOL                ENABLED  FREE
  raspberrypi4-64bit        1     1
  rpi5                      3     1
  ```

- **`reserve <pool> [--json] [--retries N] [--retries-wait D]`**
  Reserves a DUT from the given pool and prints `token`, `dut-name`, `ip`, `ssh-port`, and `tunnel-ssh-port` to stdout, plus a `client-ssh-overrides` line when the service is using an announced address for this host. `--json` prints the service response as one JSON object instead, which is the form to parse from a script: it is the response itself, so a field added to it later needs no client change. `--retries` waits out a pool whose DUTs are all taken (`status = -4`), but not a quota refusal (`-6`, `-8`, `-9`), which no short wait clears; for one of those it prints when the refusal clears (the next recharge, or `retry-after`) on stderr. `--retries-wait` is a number of seconds, or carries an `s`, `m` or `h` suffix.

- **`lease [--token TOKEN | --pool POOL | --all] [-q|--quiet]`**
  Releases reservations for the current client, filtered by token or pool, or all; prints `lease: ok` on success unless `--quiet` is used. Given no filter at all, it releases the reservation named by `DUT_CONTROL_TOKEN` when that is set, and everything the client holds when it is not

- **`power <on|off|cycle> [token] [-q|--quiet]`**
  Calls `/power/<action>` for the given reservation token, or for `DUT_CONTROL_TOKEN`; prints `power: ok` on success unless `--quiet` is used

- **`flash [token] <path> [--upload] [-q|--quiet]`**
  Calls `/flash` with the given token, or `DUT_CONTROL_TOKEN`, and the image path; prints `flash: ok` on success unless `--quiet` is used. `--upload` sends the image with the request instead, taking `path` as a local file and bringing along the `.bmap` beside it when there is one; use it when the service has no SSH route back to this host

- **`wipe [token] [--size SIZE] [-q|--quiet]`**
  Calls `/wipe` for the given token, or `DUT_CONTROL_TOKEN`. `--size` accepts `128MiB`, `512M`, `2G` or a plain byte count, and is left to the service when not given. `K`, `M` and `G` are 1024-based, and so are the `KB`/`MB`/`GB` spellings, which `dd` would read as 1000-based. Prints `wipe: ok` on success unless `--quiet` is used

- **`status [token]`**
  Calls `/dut/status` for the given token, or `DUT_CONTROL_TOKEN`, and prints one of `offline`, `ping`, or `ssh`

- **`wait [token] [--for ping|ssh] [--retries N] [--retries-wait D]`**
  Polls `/dut/status` until the DUT is at least as reachable as `--for` asks, then prints the state it reached. Gives up after the retries with status 1. Progress goes to stderr. Unlike `reserve`, this one retries by default.

- **`account [--json]`**
  Calls `/account` and prints the balance, what the month has given so far, the next recharge and the active reservations:

  ```text
  account-id: team-browser
  tokens-available: 31.5
  period-granted: 40 of 80
  next-recharge: 20 at 2026-10-05T00:00:00Z
  active-reservations: 1
  ```

  `--json` prints the service response as one JSON object instead. A service from before accounting makes it print `error: server does not support accounting` and exit with status 2, so a script can tell that case from an error (status 1).

- **`account-history [--since TIME] [--until TIME] [--kind KIND] [--pool POOL] [--limit N] [--before ID] [--json]`**
  Calls `/account/history` and prints one row per entry, newest first. `TIME` is epoch seconds or ISO 8601, read as UTC when it has no zone, e.g. `2026-09-01` or `2026-09-01T12:00:00+02:00`. When there are more entries, the `--before` value for the next page goes to stderr. Exits as `account` does on a service without accounting.

**Example usage**:

```bash
export DUT_CONTROL_URL=http://lab-controller:8000
export DUT_CONTROL_CLIENT_KEY=fac72a9494cd132a

# Reserve a DUT from pool "rpi5"
dut-control-client reserve rpi5

# The same, for a script to parse
TOKEN=$(dut-control-client reserve rpi5 --json | jq -r .token)

# Wait up to an hour for a busy pool
dut-control-client reserve rpi5 --retries 60 --retries-wait 1m

# Same, from a host behind NAT reachable at 203.0.113.9:2222
export DUT_CONTROL_CLIENT_SSH_IP=203.0.113.9
export DUT_CONTROL_CLIENT_SSH_PORT=2222
dut-control-client reserve rpi5

# Release all reservations for this client
dut-control-client lease --all

# Keep the token out of every later command line
export DUT_CONTROL_TOKEN=$(dut-control-client reserve rpi5 --json | jq -r .token)

# Power cycle a reserved DUT
dut-control-client power cycle

# Flash an image for a reservation token
dut-control-client flash /images/rpi5-image.wic

# The same, from a host the service cannot reach back
dut-control-client flash --upload ./rpi5-image.wic.bz2

# Check DUT status
dut-control-client status

# Wait for it to finish booting after a power on
dut-control-client power on
dut-control-client wait --for ssh --retries 30 --retries-wait 10s

# Clear the card before handing the device back
dut-control-client wipe

# Hand this reservation back
dut-control-client lease

# How many tokens are left, and what was spent this month
dut-control-client account
dut-control-client account-history --since 2026-09-01 --kind charge
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

- **`account <client>`**
  Calls `/conf/account/info` and prints the account of the client as JSON

- **`recharge <client> --tokens N --reason TEXT [--idempotency-key KEY]`** / **`set-balance <client> --tokens N --reason TEXT [--idempotency-key KEY]`**
  Calls `/conf/account/recharge` or `/conf/account/set-balance`. Without `--idempotency-key`, a new UUID is used; pass your own to retry a call safely

- **`account-ledger <client> [--since TIME] [--until TIME] [--pool POOL] [--kind KIND] [--limit N] [--before ID]`**
  Calls `/conf/account/ledger` and prints the page as JSON

- **`account-report [--since TIME] [--until TIME] [--pool POOL] [--client NAME] [--format table|json|csv]`**
  Calls `/conf/account/report` and prints the totals as a table (default), JSON, or CSV with a header row

`TIME` is epoch seconds or ISO 8601, as for `dut-control-client`. The account subcommands exit with status 1, after printing the answer, when the service refuses the call.

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

# Give a team 10 more tokens, and see what was spent in September
dut-control-admin recharge client-01 --tokens 10 --reason "release week"
dut-control-admin account-report --since 2026-09-01 --until 2026-10-01 --format csv
```

## Testing

To run the unit tests:

```bash
pip install ".[test]"
pytest
```

To run the same checks CI runs, install the linters too:

```bash
pip install ".[test,lint]"
./.github/scripts/run-all-sanatizers
```

That runs radon, pycodestyle, pyflakes, shellcheck and a check on the commit messages on the branch. A linter that is not installed fails the run rather than being skipped quietly.

## License

The project is licensed under the MIT License.

## Author

Pablo Saavedra (psaavedra@igalia.com)

## Links

- **Homepage**: https://github.com/psaavedra/dut-control
- **Source**: https://github.com/psaavedra/dut-control.git

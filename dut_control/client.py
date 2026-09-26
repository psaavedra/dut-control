#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import re
import sys
import time
from typing import Any, Dict

import requests


DEFAULT_BASE_URL = os.environ.get("DUT_CONTROL_URL", "http://localhost:8000")
CLIENT_KEY_ENV = "DUT_CONTROL_CLIENT_KEY"
TOKEN_ENV = "DUT_CONTROL_TOKEN"
CLIENT_SSH_IP_ENV = "DUT_CONTROL_CLIENT_SSH_IP"
CLIENT_SSH_PORT_ENV = "DUT_CONTROL_CLIENT_SSH_PORT"

# A single DNS label, as on the service side
_HOSTNAME_LABEL = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

# Every DUT in the pool is taken, or the client has no free port. Both
# clear on their own; every other status is a mistake that will not.
BUSY_STATUS = -4

# What /dut/status can report, from least to most reachable.
REACHABILITY = ("offline", "ping", "ssh")

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smh]?)$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}

# An HTTP request the service answers as soon as it has looked at it.
DEFAULT_TIMEOUT = 10.0
# One where it first settles a mux and writes to an SD card over USB.
STORAGE_TIMEOUT = 900.0

_SIZE = re.compile(r"^(\d+)\s*([kmg])?i?b?$", re.IGNORECASE)
_UNIT_BYTES = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def _full_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _env_error_and_exit(env_name: str, value: str) -> None:
    print(
        f"error: {env_name} is not a valid value: {value!r}",
        file=sys.stderr,
    )
    sys.exit(1)


def _is_hostname(value: str) -> bool:
    """True for a plausible DNS name; mirrors the service-side check."""
    host = value.rstrip(".")
    if not host or len(host) > 253:
        return False
    labels = host.split(".")
    # An all-numeric rightmost label is a malformed address literal, not
    # a name: 192.168.1.999 is a typo, not a host to resolve.
    if labels[-1].isdigit():
        return False
    return all(_HOSTNAME_LABEL.match(label) for label in labels)


def _checked_ssh_ip(value: str) -> str:
    """
    An announced address: an IPv4/IPv6 literal or a DNS name. Checked
    here as well as on the service so a typo in the environment names
    the variable at fault instead of coming back as a rejected request.
    """
    host = value.strip()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    if not _is_hostname(host):
        _env_error_and_exit(CLIENT_SSH_IP_ENV, value)
    return host


def _checked_ssh_port(value: str) -> int:
    """An announced port: a decimal TCP port number."""
    try:
        port = int(value.strip(), 10)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        _env_error_and_exit(CLIENT_SSH_PORT_ENV, value)
    return port


def _ssh_override_payload() -> Dict[str, Any]:
    """
    Address the service has to use to reach this host back, when the one
    in its configuration is not the right one; typically because this
    host sits behind NAT and only knows its public address (and the
    forwarded port) at run time.

    Empty when neither environment variable is set, in which case the
    service keeps using the configured SSH parameters.
    """
    payload: Dict[str, Any] = {}

    ip = os.environ.get(CLIENT_SSH_IP_ENV)
    if ip:
        payload["client-ssh-ip"] = _checked_ssh_ip(ip)

    port = os.environ.get(CLIENT_SSH_PORT_ENV)
    if port:
        payload["client-ssh-port"] = _checked_ssh_port(port)

    return payload


def _required_token(args: argparse.Namespace) -> str:
    """
    The reservation token, from the argument or the environment.

    A token on the command line is readable in `ps` by every other user
    of the host, and ends up in any log that echoes the command.
    """
    token = args.token or os.environ.get(TOKEN_ENV)
    if not token:
        print(
            f"error: no token given and {TOKEN_ENV} is not set",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


def _duration(value: str) -> float:
    """A wait, in seconds unless it carries an s, m or h suffix."""
    match = _DURATION.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid duration '{value}'; examples: 30, 30s, 5m, 1h")
    amount, unit = match.groups()
    return float(amount) * _UNIT_SECONDS[unit.lower() or "s"]


def _timeout(args: argparse.Namespace,
             default: float = DEFAULT_TIMEOUT) -> float:
    """What the caller asked for, or what this request usually needs."""
    return default if args.timeout is None else args.timeout


def _size(value: str) -> int:
    """A size in bytes; K, M and G are binary, as in dd."""
    match = _SIZE.match(value.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid size '{value}'; examples: 128MiB, 512M, 2G")
    amount, unit = match.groups()
    size = int(amount) * _UNIT_BYTES[(unit or "").lower()]
    if size <= 0:
        raise argparse.ArgumentTypeError("a size of zero wipes nothing")
    return size


def _print_error_and_exit(prefix: str, data: Dict[str, Any]) -> None:
    status = data.get("status")
    err = data.get("error", "unknown error")
    print(f"{prefix}: {err} (status={status})", file=sys.stderr)
    sys.exit(1)


def cmd_pools(args: argparse.Namespace) -> None:
    client_key = os.environ.get(CLIENT_KEY_ENV)
    if not client_key:
        print(
            f"error: {CLIENT_KEY_ENV} not set; cannot list pools",
            file=sys.stderr,
        )
        sys.exit(1)

    resp = requests.post(
        _full_url(args.url, "/pools"),
        json={"client-key": client_key},
        timeout=_timeout(args),
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("pools failed", data)

    pools = data.get("pools", [])
    width = max([len("POOL")] + [len(p["name"]) for p in pools])
    print(f"{'POOL':<{width}}  ENABLED  FREE")
    for pool in pools:
        print(
            f"{pool['name']:<{width}}  "
            f"{pool['enabled-duts']:>7}  "
            f"{pool['free-duts']:>4}"
        )


def _reserve(args: argparse.Namespace, payload: Dict[str, Any]) -> Dict:
    """
    Ask for a DUT, waiting out a pool that is merely busy.

    Progress goes to stderr, so that --json leaves one object on stdout
    however many attempts it took.
    """
    for attempt in range(args.retries + 1):
        resp = requests.post(
            _full_url(args.url, "/reserve"),
            json=payload,
            timeout=_timeout(args),
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != BUSY_STATUS or attempt == args.retries:
            return data

        print(
            f"pool {args.pool} is busy, retrying in "
            f"{args.retries_wait:g}s",
            file=sys.stderr,
        )
        time.sleep(args.retries_wait)


def cmd_reserve(args: argparse.Namespace) -> None:
    client_key = os.environ.get(CLIENT_KEY_ENV)
    if not client_key:
        print(
            f"error: {CLIENT_KEY_ENV} not set; cannot reserve",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = {"client-key": client_key, "pool": args.pool}
    payload.update(_ssh_override_payload())

    data = _reserve(args, payload)

    if data.get("status") != 0:
        _print_error_and_exit("reserve failed", data)

    # One object, so a caller parses a contract instead of a log format.
    if args.json:
        print(json.dumps(data))
        return

    print(f"token: {data['token']}")
    print(f"dut-name: {data['dut-name']}")
    print(f"ip: {data['ip']}")
    print(f"ssh-port: {data['ssh-port']}")
    print(f"tunnel-ssh-port: {data['tunnel-ssh-port']}")

    overrides = data.get("client-ssh-overrides") or []
    if overrides:
        print(f"client-ssh-overrides: {', '.join(overrides)}")


def cmd_lease(args: argparse.Namespace) -> None:
    client_key = os.environ.get(CLIENT_KEY_ENV)
    if not client_key:
        print(
            f"error: {CLIENT_KEY_ENV} not set; cannot lease/release",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = args.url
    payload: Dict[str, Any] = {"client-key": client_key}

    token = args.token
    if not token and not args.pool and not args.all:
        # Nothing asked for: release just this reservation if the
        # environment names one, rather than everything this client holds.
        token = os.environ.get(TOKEN_ENV)

    if token:
        payload["token"] = token
    if args.pool:
        payload["pool"] = args.pool

    resp = requests.post(
        _full_url(base_url, "/lease"),
        json=payload,
        timeout=_timeout(args),
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("lease failed", data)

    # Success is just status 0; keep output minimal
    if not args.quiet:
        print("lease: ok")


def cmd_power(args: argparse.Namespace) -> None:
    base_url = args.url
    payload = {"token": _required_token(args)}

    resp = requests.post(
        _full_url(base_url, f"/power/{args.action}"),
        json=payload,
        timeout=_timeout(args),
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("power failed", data)

    if not args.quiet:
        print("power: ok")


def cmd_flash(args: argparse.Namespace) -> None:
    base_url = args.url
    payload = {"token": _required_token(args), "path": args.path}
    # The service scp's the image off this host, so it needs the same
    # override, which may well have changed since the reservation.
    payload.update(_ssh_override_payload())

    resp = requests.post(
        _full_url(base_url, "/flash"),
        json=payload,
        timeout=_timeout(args, STORAGE_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("flash failed", data)

    if not args.quiet:
        print("flash: ok")


def cmd_wipe(args: argparse.Namespace) -> None:
    payload: Dict[str, Any] = {"token": _required_token(args)}
    # Left out when not asked for, so the default lives in one place.
    if args.size is not None:
        payload["size"] = args.size

    resp = requests.post(
        _full_url(args.url, "/wipe"),
        json=payload,
        timeout=_timeout(args, STORAGE_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("wipe failed", data)

    if not args.quiet:
        print("wipe: ok")


def _dut_status(args: argparse.Namespace, token: str) -> str:
    """One of offline, ping or ssh, as /dut/status reports it."""
    resp = requests.post(
        _full_url(args.url, "/dut/status"),
        json={"token": token},
        timeout=_timeout(args),
    )
    resp.raise_for_status()
    data = resp.json()

    state = data.get("status")
    if state is None:
        print("error: unexpected response:", data, file=sys.stderr)
        sys.exit(1)
    return state


def cmd_status(args: argparse.Namespace) -> None:
    print(_dut_status(args, _required_token(args)))


def _reached(state: str, wanted: str) -> bool:
    """A DUT answering SSH answers ping too, so the states are a ladder."""
    if state not in REACHABILITY or wanted not in REACHABILITY:
        return False
    return REACHABILITY.index(state) >= REACHABILITY.index(wanted)


def cmd_wait(args: argparse.Namespace) -> None:
    token = _required_token(args)

    for attempt in range(args.retries + 1):
        state = _dut_status(args, token)
        if _reached(state, args.wanted):
            print(state)
            return

        if attempt == args.retries:
            break
        print(
            f"dut is {state}, waiting {args.retries_wait:g}s for "
            f"{args.wanted}",
            file=sys.stderr,
        )
        time.sleep(args.retries_wait)

    print(
        f"error: gave up waiting for {args.wanted}; dut is {state}",
        file=sys.stderr,
    )
    sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dut-control-client",
        description="Client CLI for the dut-control Flask service",
    )
    p.add_argument(
        "-u",
        "--url",
        default=DEFAULT_BASE_URL,
        help=(
            "Base URL of dut-control service "
            "(default: %(default)s or env DUT_CONTROL_URL)"
        ),
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=f"HTTP request timeout in seconds (default: "
             f"{DEFAULT_TIMEOUT:g}, or {STORAGE_TIMEOUT:g} for flash and "
             f"wipe, which wait for the node to write to the card)",
    )

    sub = p.add_subparsers(dest="command", required=True)

    # pools
    sp_pools = sub.add_parser(
        "pools",
        help="List pools with their enabled and free DUT counts",
    )
    sp_pools.set_defaults(func=cmd_pools)

    # reserve
    sp_reserve = sub.add_parser(
        "reserve",
        help="Reserve a DUT from a pool",
    )
    sp_reserve.add_argument(
        "pool",
        help="Pool name (metadata.pools in DUT config)",
    )
    sp_reserve.add_argument(
        "--json",
        action="store_true",
        help="Print the reservation as one JSON object",
    )
    sp_reserve.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Extra attempts while the pool is busy (default: %(default)s)",
    )
    sp_reserve.add_argument(
        "--retries-wait",
        type=_duration,
        default="60s",
        help="Wait between attempts, e.g. 30, 30s, 5m, 1h "
             "(default: %(default)s)",
    )
    sp_reserve.set_defaults(func=cmd_reserve)

    # lease
    sp_lease = sub.add_parser(
        "lease",
        help="Release reservations (by token, pool, or all for this client)",
    )
    g = sp_lease.add_mutually_exclusive_group()
    g.add_argument(
        "--token",
        help="Release only this reservation token",
    )
    g.add_argument(
        "--pool",
        help="Release reservations in this pool for the current client",
    )
    g.add_argument(
        "--all",
        action="store_true",
        help="Release all active reservations for the current client",
    )
    sp_lease.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not print anything on success",
    )
    sp_lease.set_defaults(func=cmd_lease)

    # power
    sp_power = sub.add_parser(
        "power",
        help="Control DUT power for a reservation token",
    )
    sp_power.add_argument(
        "action",
        choices=["on", "off", "cycle"],
        help="Power action",
    )
    sp_power.add_argument(
        "token",
        nargs="?",
        help=f"Reservation token (default: ${TOKEN_ENV})",
    )
    sp_power.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not print anything on success",
    )
    sp_power.set_defaults(func=cmd_power)

    # flash
    sp_flash = sub.add_parser(
        "flash",
        help="Flash an image onto the DUT storage via the node",
    )
    sp_flash.add_argument(
        "token",
        nargs="?",
        help=f"Reservation token (default: ${TOKEN_ENV})",
    )
    sp_flash.add_argument(
        "path",
        help="Path to image on the client host "
             "(as seen from the dut-control service)",
    )
    sp_flash.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not print anything on success",
    )
    sp_flash.set_defaults(func=cmd_flash)

    # status
    sp_status = sub.add_parser(
        "status",
        help="Get DUT reachability status for a reservation token",
    )
    sp_status.add_argument(
        "token",
        nargs="?",
        help=f"Reservation token (default: ${TOKEN_ENV})",
    )
    sp_status.set_defaults(func=cmd_status)

    # wipe
    sp_wipe = sub.add_parser(
        "wipe",
        help="Zero the head of the DUT storage",
    )
    sp_wipe.add_argument(
        "token",
        nargs="?",
        help=f"Reservation token (default: ${TOKEN_ENV})",
    )
    sp_wipe.add_argument(
        "--size",
        type=_size,
        help="How much to overwrite, e.g. 128MiB, 512M, 2G "
             "(default: whatever the service uses)",
    )
    sp_wipe.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Do not print anything on success",
    )
    sp_wipe.set_defaults(func=cmd_wipe)

    # wait
    sp_wait = sub.add_parser(
        "wait",
        help="Wait for a DUT to become reachable",
    )
    sp_wait.add_argument(
        "token",
        nargs="?",
        help=f"Reservation token (default: ${TOKEN_ENV})",
    )
    sp_wait.add_argument(
        "--for",
        dest="wanted",
        choices=["ping", "ssh"],
        default="ssh",
        help="Reachability to wait for (default: %(default)s)",
    )
    # Unlike reserve, waiting no times at all is just status.
    sp_wait.add_argument(
        "--retries",
        type=int,
        default=30,
        help="Extra attempts before giving up (default: %(default)s)",
    )
    sp_wait.add_argument(
        "--retries-wait",
        type=_duration,
        default="10s",
        help="Wait between attempts, e.g. 30, 30s, 5m, 1h "
             "(default: %(default)s)",
    )
    sp_wait.set_defaults(func=cmd_wait)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Lease-all is just lease without token/pool set
    if args.command == "lease" and args.all:
        # nothing extra to do; payload will only have client-key
        pass

    try:
        args.func(args)
    except requests.exceptions.RequestException as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

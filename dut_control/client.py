#!/usr/bin/env python3
import argparse
import os
import sys
from typing import Any, Dict

import requests


DEFAULT_BASE_URL = os.environ.get("DUT_CONTROL_URL", "http://localhost:8000")
CLIENT_KEY_ENV = "DUT_CONTROL_CLIENT_KEY"


def _full_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _print_error_and_exit(prefix: str, data: Dict[str, Any]) -> None:
    status = data.get("status")
    err = data.get("error", "unknown error")
    print(f"{prefix}: {err} (status={status})", file=sys.stderr)
    sys.exit(1)


def cmd_reserve(args: argparse.Namespace) -> None:
    client_key = os.environ.get(CLIENT_KEY_ENV)
    if not client_key:
        print(
            f"error: {CLIENT_KEY_ENV} not set; cannot reserve",
            file=sys.stderr,
        )
        sys.exit(1)

    base_url = args.url
    payload = {"client-key": client_key, "pool": args.pool}

    resp = requests.post(
        _full_url(base_url, "/reserve"),
        json=payload,
        timeout=args.timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("reserve failed", data)

    print(f"token: {data['token']}")
    print(f"ssh-port: {data['ssh-port']}")


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

    if args.token:
        payload["token"] = args.token
    if args.pool:
        payload["pool"] = args.pool

    resp = requests.post(
        _full_url(base_url, "/lease"),
        json=payload,
        timeout=args.timeout,
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
    payload = {"token": args.token}

    resp = requests.post(
        _full_url(base_url, f"/power/{args.action}"),
        json=payload,
        timeout=args.timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("power failed", data)

    if not args.quiet:
        print("power: ok")


def cmd_flash(args: argparse.Namespace) -> None:
    base_url = args.url
    payload = {"token": args.token, "path": args.path}

    resp = requests.post(
        _full_url(base_url, "/flash"),
        json=payload,
        timeout=args.timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != 0:
        _print_error_and_exit("flash failed", data)

    if not args.quiet:
        print("flash: ok")


def cmd_status(args: argparse.Namespace) -> None:
    base_url = args.url
    payload = {"token": args.token}

    resp = requests.post(
        _full_url(base_url, "/dut/status"),
        json=payload,
        timeout=args.timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    # /dut/status returns {"status": "offline"|"ping"|"ssh"}
    status = data.get("status")
    if status is None:
        print("error: unexpected response:", data, file=sys.stderr)
        sys.exit(1)

    print(status)


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
        default=10.0,
        help="HTTP request timeout in seconds (default: %(default)s)",
    )

    sub = p.add_subparsers(dest="command", required=True)

    # reserve
    sp_reserve = sub.add_parser(
        "reserve",
        help="Reserve a DUT from a pool",
    )
    sp_reserve.add_argument(
        "pool",
        help="Pool name (metadata.pool in DUT config)",
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
        help="Reservation token",
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
        help="Reservation token",
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
        help="Reservation token",
    )
    sp_status.set_defaults(func=cmd_status)

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

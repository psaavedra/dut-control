#!/usr/bin/env python3
import argparse
import json
import os
import sys
from typing import Any, Dict

import requests


DEFAULT_BASE_URL = os.environ.get("DUT_CONTROL_URL", "http://localhost:8000")
ADMIN_KEY_ENV = "DUT_CONTROL_ADMIN_KEY"


def _full_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _get_admin_key() -> str:
    key = os.environ.get(ADMIN_KEY_ENV)
    if not key:
        print(
            f"error: {ADMIN_KEY_ENV} not set; cannot call /conf endpoints",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _post_json(base_url: str, path: str,
               payload: Dict[str, Any], timeout: float):
    url = _full_url(base_url, path)
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_reload(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    resp = _post_json(base_url, "/conf/reload",
                      {"admin-key": key}, args.timeout)
    data = resp.json()
    # /conf/reload returns {"result": 0} on success
    _print_json(data)


def cmd_nodes(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    resp = _post_json(base_url, "/conf/info/nodes",
                      {"admin-key": key}, args.timeout)
    data = resp.json()
    _print_json(data)


def cmd_clients(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    resp = _post_json(base_url, "/conf/info/clients",
                      {"admin-key": key}, args.timeout)
    data = resp.json()
    _print_json(data)


def cmd_processes(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    resp = _post_json(
        base_url, "/conf/info/processes", {"admin-key": key}, args.timeout
    )
    data = resp.json()
    _print_json(data)


def cmd_reserves(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    resp = _post_json(
        base_url, "/conf/info/reserves", {"admin-key": key}, args.timeout
    )
    data = resp.json()
    _print_json(data)


def cmd_prune(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    resp = _post_json(
        base_url, "/conf/reserves/prune", {"admin-key": key}, args.timeout
    )
    data = resp.json()
    # /conf/reserves/prune returns {"result": 0, "pruned": <n>}
    _print_json(data)


# ---------------------------------------------------------------------------
# Argparse / entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dut-control-admin",
        description="Admin CLI for the dut-control service (/conf endpoints)",
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

    sp_reload = sub.add_parser("reload", help="Reload YAML configuration")
    sp_reload.set_defaults(func=cmd_reload)

    sp_nodes = sub.add_parser("nodes", help="Show configured nodes and DUTs")
    sp_nodes.set_defaults(func=cmd_nodes)

    sp_clients = sub.add_parser("clients", help="Show configured clients")
    sp_clients.set_defaults(func=cmd_clients)

    sp_processes = sub.add_parser(
        "processes",
        help="Show active ssh tunnel processes tracked by the service",
    )
    sp_processes.set_defaults(func=cmd_processes)

    sp_reserves = sub.add_parser(
        "reserves",
        help="Show current reservation entries",
    )
    sp_reserves.set_defaults(func=cmd_reserves)

    sp_prune = sub.add_parser(
        "prune",
        help="Prune expired reservation entries",
    )
    sp_prune.set_defaults(func=cmd_prune)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        args.func(args)
    except requests.exceptions.RequestException as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

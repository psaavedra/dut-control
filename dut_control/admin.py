#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys
import uuid
from typing import Any, Dict

import requests

from dut_control.client import _epoch


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
    payload: Dict[str, Any] = {"admin-key": key}
    if args.active:
        payload["active"] = True
    resp = _post_json(
        base_url, "/conf/info/reserves", payload, args.timeout
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


def cmd_dut_enabled(args: argparse.Namespace) -> None:
    key = _get_admin_key()
    base_url = args.url
    payload = {
        "admin-key": key,
        "dut-name": args.dut_name,
        "enabled": args.enabled,
    }
    resp = _post_json(base_url, "/conf/dut/enabled", payload, args.timeout)
    data = resp.json()
    _print_json(data)
    if data.get("result") != 0:
        sys.exit(1)


def _post_account(args: argparse.Namespace, path: str,
                  payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST to a /conf/account endpoint; exit 1 unless it worked."""
    payload = {"admin-key": _get_admin_key(),
               **{k: v for k, v in payload.items() if v is not None}}
    data = _post_json(args.url, path, payload, args.timeout).json()
    if data.get("result") != 0:
        _print_json(data)
        sys.exit(1)
    return data


def cmd_account(args: argparse.Namespace) -> None:
    _print_json(_post_account(args, "/conf/account/info",
                              {"client": args.client}))


def cmd_credit(args: argparse.Namespace) -> None:
    """recharge and set-balance; a retry with the same key is a no-op."""
    _print_json(_post_account(args, args.path, {
        "client": args.client,
        "tokens": args.tokens,
        "reason": args.reason,
        "idempotency-key": args.idempotency_key or str(uuid.uuid4()),
    }))


def cmd_account_ledger(args: argparse.Namespace) -> None:
    _print_json(_post_account(args, "/conf/account/ledger", {
        "client": args.client, "since": args.since, "until": args.until,
        "kind": args.kind, "pool": args.pool, "limit": args.limit,
        "before": args.before,
    }))


REPORT_COLUMNS = ("account-id", "pool", "kind", "entries", "tokens",
                  "minutes")


def cmd_account_report(args: argparse.Namespace) -> None:
    data = _post_account(args, "/conf/account/report", {
        "client": args.client, "pool": args.pool,
        "since": args.since, "until": args.until,
    })
    rows = data["rows"]
    if args.format == "json":
        _print_json(rows)
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=REPORT_COLUMNS,
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    else:
        print(f"{'ACCOUNT':<16}  {'POOL':<12}  {'KIND':<16}  {'ENTRIES':>7}  "
              f"{'TOKENS':>9}  {'MINUTES':>7}")
        for r in rows:
            print(f"{r['account-id']:<16}  {r['pool'] or '-':<12}  "
                  f"{r['kind']:<16}  {r['entries']:>7}  {r['tokens']:>+9g}  "
                  f"{r['minutes']:>7}")


def _add_time_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since", type=_epoch,
        help="Only entries from this time on (epoch or ISO 8601, UTC)")
    parser.add_argument(
        "--until", type=_epoch,
        help="Only entries before this time (epoch or ISO 8601, UTC)")
    parser.add_argument("--pool", help="Only entries for this pool")


def _add_credit_parser(sub, name: str, path: str, help_text: str) -> None:
    sp = sub.add_parser(name, help=help_text)
    sp.add_argument("client", help="A client of the account")
    sp.add_argument("--tokens", type=float, required=True,
                    help="Tokens; negative takes them away"
                    if name == "recharge" else "The new balance")
    sp.add_argument("--reason", required=True,
                    help="Why, kept in the ledger")
    sp.add_argument("--idempotency-key",
                    help="Applies once per account (default: a new UUID); "
                         "reuse it to retry safely")
    sp.set_defaults(func=cmd_credit, path=path)


def _add_account_parsers(sub) -> None:
    sp = sub.add_parser("account",
                        help="Show the account of a client and its policy")
    sp.add_argument("client", help="A client of the account")
    sp.set_defaults(func=cmd_account)

    _add_credit_parser(sub, "recharge", "/conf/account/recharge",
                       "Add tokens to the account of a client")
    _add_credit_parser(sub, "set-balance", "/conf/account/set-balance",
                       "Set the balance of the account of a client")

    sp = sub.add_parser("account-ledger",
                        help="List the ledger of the account of a client")
    sp.add_argument("client", help="A client of the account")
    _add_time_filters(sp)
    sp.add_argument("--kind",
                    help="Only this kind, e.g. charge, refund, admin")
    sp.add_argument("--limit", type=int, help="Entries per page")
    sp.add_argument("--before", type=int,
                    help="Entries older than this id, for the next page")
    sp.set_defaults(func=cmd_account_ledger)

    sp = sub.add_parser("account-report",
                        help="Totals by account, pool and kind")
    _add_time_filters(sp)
    sp.add_argument("--client", help="Only the account of this client")
    sp.add_argument("--format", choices=["table", "json", "csv"],
                    default="table", help="Output (default: %(default)s)")
    sp.set_defaults(func=cmd_account_report)


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
    sp_reserves.add_argument(
        "-a",
        "--active",
        action="store_true",
        help="Show only currently valid reservations "
             "(valid-from <= now <= valid-until)",
    )
    sp_reserves.set_defaults(func=cmd_reserves)

    sp_prune = sub.add_parser(
        "prune",
        help="Prune expired reservation entries",
    )
    sp_prune.set_defaults(func=cmd_prune)

    sp_dut_enable = sub.add_parser(
        "dut-enable",
        help="Enable a DUT so it can be reserved again",
    )
    sp_dut_enable.add_argument("dut_name", help="DUT name")
    sp_dut_enable.set_defaults(func=cmd_dut_enabled, enabled=True)

    sp_dut_disable = sub.add_parser(
        "dut-disable",
        help="Disable a DUT to exclude it from pools and new reservations",
    )
    sp_dut_disable.add_argument("dut_name", help="DUT name")
    sp_dut_disable.set_defaults(func=cmd_dut_enabled, enabled=False)

    _add_account_parsers(sub)

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

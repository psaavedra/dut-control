"""
Token accounting for dut-control, kept in SQLite.

* Amounts are integer millitokens; the API turns them into tokens.
* The ledger is append-only; every change of a balance is one row.
* Recharges are lazy: each call applies the weekly ones that are due.
* Reservation tokens are stored only as an HMAC.

Only the standard library is used, so the client package does not
grow a dependency.
"""

import calendar
import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MILLI = 1000
WEEK = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    balance_millitokens INTEGER NOT NULL,
    period_start INTEGER NOT NULL,
    period_granted_millitokens INTEGER NOT NULL,
    last_recharge_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts INTEGER NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    kind TEXT NOT NULL,
    amount_millitokens INTEGER NOT NULL,
    balance_after_millitokens INTEGER NOT NULL,
    reservation_token_hash TEXT,
    pool TEXT,
    dut_name TEXT,
    minutes INTEGER,
    actor TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS ledger_account
    ON ledger (account_id, id DESC);
CREATE TABLE IF NOT EXISTS reservation_charges (
    reservation_token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    pool TEXT NOT NULL,
    charged_millitokens INTEGER NOT NULL,
    reserved_minutes INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    refunded_millitokens INTEGER NOT NULL DEFAULT 0,
    released_at INTEGER
);
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    reservation_token_hash TEXT,
    action TEXT NOT NULL,
    pool TEXT,
    dut_name TEXT,
    result TEXT NOT NULL,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


class QuotaExceeded(Exception):
    """The balance does not cover a charge."""

    def __init__(self, required: int, available: int):
        super().__init__("quota exceeded")
        self.required = required
        self.available = available


@dataclass(frozen=True)
class Policy:
    """The recharge rules of one account, in millitokens."""
    monthly_limit: int
    weekly_recharge: int
    balance_cap: int
    recharge_weekday: int = 1    # ISO, 1 is Monday
    recharge_hour_utc: int = 0


def to_milli(tokens) -> int:
    return round(tokens * MILLI)


def to_tokens(milli: int) -> float:
    return milli / MILLI


def iso(ts: int) -> str:
    """An epoch as ISO 8601 in UTC, for people and for scripts."""
    return _utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def reservation_price(minutes: int, token_minutes: int, pool_cost) -> int:
    """Millitokens for ``minutes`` of a pool of cost ``pool_cost``."""
    return round(minutes * pool_cost * MILLI / token_minutes)


# ---------------------------------------------------------------------------
# Calendar, all in UTC
# ---------------------------------------------------------------------------

def _utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def month_start(ts: int) -> int:
    d = _utc(ts)
    return calendar.timegm((d.year, d.month, 1, 0, 0, 0))


def month_end(ts: int) -> int:
    d = _utc(ts)
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return calendar.timegm((year, month, 1, 0, 0, 0))


def next_recharge_at(after: int, policy: Policy) -> int:
    """The first recharge time strictly after ``after``."""
    d = _utc(after)
    day = d.replace(hour=policy.recharge_hour_utc, minute=0, second=0,
                    microsecond=0)
    day += timedelta(days=(policy.recharge_weekday - d.isoweekday()) % 7)
    t = int(day.timestamp())
    return t if t > after else t + WEEK


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class AccountingStore:
    """
    One SQLite file. A connection per call, so Flask threads can share
    the store; BEGIN IMMEDIATE makes the calls run one at a time.
    """

    def __init__(self, path: str):
        self.path = path
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)",
                         ("hmac-secret", secrets.token_hex(32)))
            self._secret = conn.execute(
                "SELECT value FROM meta WHERE key = 'hmac-secret'"
            ).fetchone()[0].encode()
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def token_hash(self, token: str) -> str:
        return hmac.new(self._secret, token.encode(),
                        hashlib.sha256).hexdigest()

    # -- inside a transaction ----------------------------------------------

    @staticmethod
    def _entry(conn, event_id, ts, account, kind, amount, **fields):
        """Move ``amount`` into the balance and log it; None if seen."""
        if conn.execute("SELECT 1 FROM ledger WHERE event_id = ?",
                        (event_id,)).fetchone():
            return None
        conn.execute(
            "UPDATE accounts SET balance_millitokens = "
            "balance_millitokens + ? WHERE account_id = ?", (amount, account))
        balance = conn.execute(
            "SELECT balance_millitokens FROM accounts WHERE account_id = ?",
            (account,)).fetchone()[0]
        columns = ["event_id", "ts", "account_id", "kind",
                   "amount_millitokens", "balance_after_millitokens",
                   *fields]
        conn.execute(
            f"INSERT INTO ledger ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            (event_id, ts, account, kind, amount, balance, *fields.values()))
        return balance

    def _grant(self, conn, row, policy, ts, event_id, reason):
        """
        One recharge at ``ts``: the weekly amount, less what the month
        has already given and what goes past the cap.
        """
        start, granted = row["period_start"], row["period_granted"]
        if ts >= month_end(start):
            start, granted = month_start(ts), 0
        amount = max(0, min(policy.weekly_recharge,
                            policy.monthly_limit - granted,
                            policy.balance_cap - row["balance"]))
        conn.execute(
            "UPDATE accounts SET period_start = ?, "
            "period_granted_millitokens = ?, last_recharge_at = ? "
            "WHERE account_id = ?",
            (start, granted + amount, ts, row["account_id"]))
        if amount:
            self._entry(conn, event_id, ts, row["account_id"], "recharge",
                        amount, reason=reason)

    def _row(self, conn, account):
        return conn.execute(
            "SELECT account_id, balance_millitokens AS balance, "
            "period_start, period_granted_millitokens AS period_granted, "
            "last_recharge_at FROM accounts WHERE account_id = ?",
            (account,)).fetchone()

    def _settle(self, conn, account, policy, now):
        """
        Open the account on first use with one weekly recharge, then
        apply every weekly recharge that is due by ``now``.
        """
        if self._row(conn, account) is None:
            conn.execute("INSERT INTO accounts VALUES (?, 0, ?, 0, ?)",
                         (account, month_start(now), now))
            self._grant(conn, self._row(conn, account), policy, now,
                        f"open:{account}", "opening")
        while True:
            row = self._row(conn, account)
            ts = next_recharge_at(row["last_recharge_at"], policy)
            if ts > now:
                break
            self._grant(conn, row, policy, ts, f"recharge:{account}:{ts}",
                        "weekly")
        if now >= month_end(row["period_start"]):
            conn.execute(
                "UPDATE accounts SET period_start = ?, "
                "period_granted_millitokens = 0 WHERE account_id = ?",
                (month_start(now), account))
        return self._row(conn, account)

    @staticmethod
    def _summary(row, policy, now):
        """The state of an account and what its next recharge gives."""
        ts = next_recharge_at(row["last_recharge_at"], policy)
        granted = row["period_granted"]
        if ts >= month_end(row["period_start"]):
            granted = 0
        return {
            "account-id": row["account_id"],
            "balance": row["balance"],
            "period-start": row["period_start"],
            "period-end": month_end(row["period_start"]),
            "period-granted": row["period_granted"],
            "next-recharge-at": ts,
            "next-recharge": max(0, min(policy.weekly_recharge,
                                        policy.monthly_limit - granted,
                                        policy.balance_cap - row["balance"])),
        }

    # -- public calls ------------------------------------------------------

    def settle(self, account: str, policy: Policy, now: int) -> dict:
        with self._transaction() as conn:
            return self._summary(self._settle(conn, account, policy, now),
                                 policy, now)

    def charge(self, account: str, policy: Policy, now: int, *, token: str,
               pool: str, dut_name: str, minutes: int, amount: int,
               actor: str, allow_negative: bool = False) -> dict:
        """
        Take ``amount`` for a reservation. Raises QuotaExceeded when the
        balance is short, unless ``allow_negative``, and ValueError for a
        token charged before.
        """
        token_hash = self.token_hash(token)
        with self._transaction() as conn:
            row = self._settle(conn, account, policy, now)
            if conn.execute(
                    "SELECT 1 FROM reservation_charges "
                    "WHERE reservation_token_hash = ?",
                    (token_hash,)).fetchone():
                raise ValueError("reservation already charged")
            if row["balance"] < amount and not allow_negative:
                raise QuotaExceeded(amount, row["balance"])
            conn.execute(
                "INSERT INTO reservation_charges (reservation_token_hash, "
                "account_id, pool, charged_millitokens, reserved_minutes, "
                "valid_until) VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash, account, pool, amount, minutes,
                 now + minutes * 60))
            self._entry(conn, f"charge:{token_hash}", now, account, "charge",
                        -amount, reservation_token_hash=token_hash,
                        pool=pool, dut_name=dut_name, minutes=minutes,
                        actor=actor)
            summary = self._summary(self._row(conn, account), policy, now)
            return dict(summary, charged=amount)

    def _give_back(self, token: str, now: int, kind: str, whole: bool):
        token_hash = self.token_hash(token)
        with self._transaction() as conn:
            charge = conn.execute(
                "SELECT * FROM reservation_charges "
                "WHERE reservation_token_hash = ?", (token_hash,)).fetchone()
            if charge is None or charge["released_at"] is not None:
                return 0
            amount = charge["charged_millitokens"]
            if not whole:
                # A minute that has started counts as used.
                unused = max(0, charge["valid_until"] - now) // 60
                amount = amount * min(unused, charge["reserved_minutes"]) \
                    // charge["reserved_minutes"]
            conn.execute(
                "UPDATE reservation_charges SET refunded_millitokens = ?, "
                "released_at = ? WHERE reservation_token_hash = ?",
                (amount, now, token_hash))
            if amount:
                self._entry(conn, f"{kind}:{token_hash}", now,
                            charge["account_id"], kind, amount,
                            reservation_token_hash=token_hash,
                            pool=charge["pool"])
            return amount

    def refund(self, token: str, now: int) -> int:
        """
        Give back the unused whole minutes of a reservation released
        early. Only the first call gives anything back. The cap does not
        apply: these are the account's own tokens.
        """
        return self._give_back(token, now, "refund", whole=False)

    def rollback(self, token: str, now: int) -> int:
        """Give back the whole charge of a reservation that never ran."""
        return self._give_back(token, now, "reserve-rollback", whole=True)

    def credit(self, account: str, policy: Policy, now: int, *, amount: int,
               reason: str, idempotency_key: str, actor: str) -> dict:
        """
        An admin change of the balance, up or down. The same key applies
        once. It is not a recharge, so it ignores the cap and the
        monthly limit; it cannot take the balance below zero.
        """
        with self._transaction() as conn:
            row = self._settle(conn, account, policy, now)
            event_id = f"admin:{idempotency_key}"
            seen = conn.execute("SELECT 1 FROM ledger WHERE event_id = ?",
                                (event_id,)).fetchone()
            if not seen and row["balance"] + amount < 0:
                raise ValueError("balance cannot go below zero")
            self._entry(conn, event_id, now, account, "admin", amount,
                        actor=actor, reason=reason)
            return self._summary(self._row(conn, account), policy, now)

    def history(self, account: str, *, since: int = None, until: int = None,
                kind: str = None, pool: str = None, before: int = None,
                limit: int = 100) -> list:
        """
        Ledger rows of an account, newest first. ``before`` is the id of
        the last row of the previous page. Rows are written in time
        order, so the id order is the time order.
        """
        filters = {"ts >= ?": since, "ts < ?": until, "kind = ?": kind,
                   "pool = ?": pool, "id < ?": before}
        where = ["account_id = ?"] + [k for k, v in filters.items()
                                      if v is not None]
        args = [account] + [v for v in filters.values() if v is not None]
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM ledger WHERE {' AND '.join(where)} "
                "ORDER BY id DESC LIMIT ?", (*args, limit))
            return [dict(r) for r in rows]
        finally:
            conn.close()

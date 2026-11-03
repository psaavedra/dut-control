#!/usr/bin/env python3

import calendar

import pytest

from dut_control import accounting as acc

MILLI = acc.MILLI
# 10 tokens a week, at most 40 a month, no cap in the way.
POLICY = acc.Policy(monthly_limit=40 * MILLI, weekly_recharge=10 * MILLI,
                    balance_cap=1000 * MILLI)


def _ts(*date):
    """Epoch of a UTC date: year, month, day[, hour[, minute]]."""
    return calendar.timegm((*date, 0, 0, 0)[:6])


@pytest.fixture
def store(tmp_path):
    return acc.AccountingStore(str(tmp_path / "accounting.sqlite3"))


def _charge(store, now, token="t1", amount=2 * MILLI, policy=POLICY,
            **kwargs):
    return store.charge("team", policy, now, token=token, pool="rpi5",
                        dut_name="rpi5-01", minutes=120, amount=amount,
                        actor="client-01", **kwargs)


@pytest.mark.parametrize("cost, tokens", [(1, 1), (2, 2), (3, 3), (2.5, 2.5)])
def test_reservation_price_follows_the_pool_cost(cost, tokens):
    assert acc.reservation_price(120, 120, cost) == tokens * MILLI


def test_next_recharge_is_the_next_weekday_and_hour():
    # 2026-06-03 is a Wednesday; recharges are on Mondays at 06:00.
    policy = acc.Policy(0, 0, 0, recharge_weekday=1, recharge_hour_utc=6)

    assert acc.next_recharge_at(_ts(2026, 6, 3), policy) == _ts(2026, 6, 8, 6)
    assert acc.next_recharge_at(_ts(2026, 6, 8, 6), policy) == \
        _ts(2026, 6, 15, 6)


def test_a_new_account_opens_with_one_weekly_recharge(store):
    info = store.settle("team", POLICY, _ts(2026, 6, 3))

    assert info["balance"] == 10 * MILLI
    assert info["period-granted"] == 10 * MILLI


def test_five_recharges_in_a_month_stay_within_the_monthly_limit(store):
    # June 2026 has five Mondays: 1, 8, 15, 22 and 29.
    store.settle("team", POLICY, _ts(2026, 5, 31))

    info = store.settle("team", POLICY, _ts(2026, 6, 30))

    assert info["period-granted"] == 40 * MILLI
    assert info["balance"] == (10 + 40) * MILLI
    assert info["next-recharge"] == 10 * MILLI   # July starts over


def test_a_long_downtime_catches_up_to_the_cap(store):
    policy = acc.Policy(monthly_limit=40 * MILLI, weekly_recharge=10 * MILLI,
                        balance_cap=25 * MILLI)
    store.settle("team", policy, _ts(2026, 1, 5))

    info = store.settle("team", policy, _ts(2026, 6, 3))

    assert info["balance"] == 25 * MILLI
    assert info["period-start"] == _ts(2026, 6, 1)
    assert info["next-recharge"] == 0


def test_a_charge_needs_the_balance(store):
    now = _ts(2026, 6, 3)

    with pytest.raises(acc.QuotaExceeded) as e:
        _charge(store, now, amount=11 * MILLI)
    assert (e.value.required, e.value.available) == (11 * MILLI, 10 * MILLI)

    info = _charge(store, now, amount=11 * MILLI, allow_negative=True)
    assert info["balance"] == -1 * MILLI


def test_a_reservation_is_charged_once(store):
    now = _ts(2026, 6, 3)
    _charge(store, now)

    with pytest.raises(ValueError, match="already charged"):
        _charge(store, now)
    assert store.settle("team", POLICY, now)["balance"] == 8 * MILLI


def test_a_refund_gives_back_the_unused_whole_minutes_once(store):
    now = _ts(2026, 6, 3)
    _charge(store, now)

    # 30 minutes and 10 seconds used: 89 whole minutes are left.
    assert store.refund("t1", now + 30 * 60 + 10) == 2 * MILLI * 89 // 120
    assert store.refund("t1", now + 31 * 60) == 0


def test_a_refund_goes_past_the_cap(store):
    policy = acc.Policy(monthly_limit=40 * MILLI, weekly_recharge=10 * MILLI,
                        balance_cap=10 * MILLI)
    # Reserve on Sunday at 23:00; the Monday recharge fills the account
    # back to the cap before the reservation is released at 00:30.
    _charge(store, _ts(2026, 6, 7, 23), policy=policy)
    assert store.settle("team", policy, _ts(2026, 6, 8, 0, 30))["balance"] \
        == 10 * MILLI

    assert store.refund("t1", _ts(2026, 6, 8, 0, 30)) == MILLI // 2

    info = store.settle("team", policy, _ts(2026, 6, 8, 0, 30))
    assert info["balance"] == 10 * MILLI + MILLI // 2


def test_a_rollback_gives_back_everything_once(store):
    now = _ts(2026, 6, 3)
    _charge(store, now)

    assert store.rollback("t1", now + 600) == 2 * MILLI
    assert store.refund("t1", now + 600) == 0
    assert store.settle("team", POLICY, now)["balance"] == 10 * MILLI


def test_an_admin_credit_applies_once_per_key(store):
    now = _ts(2026, 6, 3)

    for _ in range(2):
        info = store.credit("team", POLICY, now, amount=5 * MILLI,
                            reason="demo", idempotency_key="k1",
                            actor="admin")

    assert info["balance"] == 15 * MILLI
    with pytest.raises(ValueError, match="below zero"):
        store.credit("team", POLICY, now, amount=-16 * MILLI,
                     reason="demo", idempotency_key="k2", actor="admin")


def test_history_pages_newest_first(store):
    now = _ts(2026, 6, 3)
    _charge(store, now, token="t1")
    _charge(store, now + 60, token="t2")

    kinds = [r["kind"] for r in store.history("team")]
    assert kinds == ["charge", "charge", "recharge"]

    first = store.history("team", limit=1)
    rest = store.history("team", before=first[0]["id"])
    assert [r["id"] for r in first + rest] == \
        [r["id"] for r in store.history("team")]
    assert len(store.history("team", kind="charge", since=now + 60)) == 1


def test_the_database_never_holds_a_reservation_token(store, tmp_path):
    _charge(store, _ts(2026, 6, 3), token="secret-reservation-token")
    store.refund("secret-reservation-token", _ts(2026, 6, 3))

    data = (tmp_path / "accounting.sqlite3").read_bytes()
    wal = tmp_path / "accounting.sqlite3-wal"
    data += wal.read_bytes() if wal.exists() else b""
    assert b"secret-reservation-token" not in data

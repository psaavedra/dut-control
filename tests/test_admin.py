#!/usr/bin/env python3

import uuid

import pytest

import dut_control.admin as admin_mod


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.fixture
def answers(monkeypatch):
    """Reply to every POST with the given payload, recording the calls."""
    def reply_with(payload):
        calls = []

        def post(url, json=None, timeout=None):
            calls.append((url, json))
            return FakeResponse(payload)

        monkeypatch.setattr(admin_mod.requests, "post", post)
        monkeypatch.setenv(admin_mod.ADMIN_KEY_ENV, "an-admin-key")
        return calls
    return reply_with


def test_recharge_makes_up_an_idempotency_key(answers):
    calls = answers({"result": 0, "tokens-available": 15.0})

    assert admin_mod.main(["recharge", "client-01", "--tokens", "5",
                           "--reason", "demo"]) == 0

    url, payload = calls[0]
    assert url.endswith("/conf/account/recharge")
    key = payload.pop("idempotency-key")
    assert uuid.UUID(key)
    assert payload == {"admin-key": "an-admin-key", "client": "client-01",
                       "tokens": 5.0, "reason": "demo"}


def test_a_refused_credit_exits_with_status_1(answers):
    answers({"result": -2, "error": "balance cannot go below zero"})

    with pytest.raises(SystemExit) as excinfo:
        admin_mod.main(["set-balance", "client-01", "--tokens", "0",
                        "--reason", "reset"])

    assert excinfo.value.code == 1


def test_account_report_prints_csv(answers, capsys):
    answers({"result": 0, "rows": [
        {"account-id": "team", "pool": "rpi5", "kind": "charge",
         "entries": 2, "tokens": -4.0, "minutes": 240}]})

    assert admin_mod.main(["account-report", "--format", "csv",
                           "--since", "2026-09-01"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "account-id,pool,kind,entries,tokens,minutes",
        "team,rpi5,charge,2,-4.0,240"]

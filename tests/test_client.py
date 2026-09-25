#!/usr/bin/env python3

import json
import sys
from pathlib import Path

import pytest

# Ensure project root (containing dut_control/) is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dut_control.client as client_mod  # noqa: E402


@pytest.fixture(autouse=True)
def clear_override_env(monkeypatch):
    """The CLI reads the overrides from the environment; start clean."""
    monkeypatch.delenv(client_mod.CLIENT_SSH_IP_ENV, raising=False)
    monkeypatch.delenv(client_mod.CLIENT_SSH_PORT_ENV, raising=False)


def test_ssh_override_payload_empty_without_env():
    assert client_mod._ssh_override_payload() == {}


def test_ssh_override_payload_reads_both_env_vars(monkeypatch):
    monkeypatch.setenv(client_mod.CLIENT_SSH_IP_ENV, " 203.0.113.9 ")
    monkeypatch.setenv(client_mod.CLIENT_SSH_PORT_ENV, "2222")

    assert client_mod._ssh_override_payload() == {
        "client-ssh-ip": "203.0.113.9",
        "client-ssh-port": 2222,
    }


def test_ssh_override_payload_takes_either_variable_alone(monkeypatch):
    monkeypatch.setenv(client_mod.CLIENT_SSH_IP_ENV, "client.dyn.example.com")
    assert client_mod._ssh_override_payload() == {
        "client-ssh-ip": "client.dyn.example.com",
    }

    monkeypatch.delenv(client_mod.CLIENT_SSH_IP_ENV)
    monkeypatch.setenv(client_mod.CLIENT_SSH_PORT_ENV, "2222")
    assert client_mod._ssh_override_payload() == {"client-ssh-port": 2222}


@pytest.mark.parametrize("value", [
    " ",
    "203.0.113.999",
    "203.0.113.9 -oProxyCommand=id",
    "root@203.0.113.9",
    "203.0.113.9:2222",
])
def test_ssh_override_payload_rejects_bad_ip(monkeypatch, capsys, value):
    monkeypatch.setenv(client_mod.CLIENT_SSH_IP_ENV, value)

    with pytest.raises(SystemExit) as excinfo:
        client_mod._ssh_override_payload()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert client_mod.CLIENT_SSH_IP_ENV in err


@pytest.mark.parametrize("value", ["ssh", "0", "-1", "65536", "22.5", " "])
def test_ssh_override_payload_rejects_bad_port(monkeypatch, capsys, value):
    monkeypatch.setenv(client_mod.CLIENT_SSH_PORT_ENV, value)

    with pytest.raises(SystemExit) as excinfo:
        client_mod._ssh_override_payload()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert client_mod.CLIENT_SSH_PORT_ENV in err


class FakeResponse:
    """Enough of requests.Response for the subcommands."""

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


RESERVATION = {
    "status": 0,
    "token": "abc123",
    "dut-name": "rpi5-01",
    "ip": "192.168.1.40",
    "ssh-port": 22,
    "tunnel-ssh-port": 5001,
}


@pytest.fixture
def answers(monkeypatch):
    """Reply to every POST with the given payload, recording the calls."""
    def reply_with(payload):
        calls = []

        def post(url, json=None, timeout=None):
            calls.append((url, json))
            return FakeResponse(payload)

        monkeypatch.setattr(client_mod.requests, "post", post)
        monkeypatch.setenv(client_mod.CLIENT_KEY_ENV, "a-client-key")
        return calls
    return reply_with


def test_reserve_prints_one_json_object(answers, capsys):
    answers(RESERVATION)

    assert client_mod.main(["reserve", "rpi5", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == RESERVATION


def test_reserve_still_prints_its_lines_without_json(answers, capsys):
    answers(RESERVATION)

    assert client_mod.main(["reserve", "rpi5"]) == 0

    out = capsys.readouterr().out
    assert "token: abc123" in out
    assert "dut-name: rpi5-01" in out
    assert not out.startswith("{")


def test_reserve_forwards_whatever_the_service_added(answers, capsys):
    """The object is the response, so a new field needs no client change."""
    answers({**RESERVATION, "lease-expires": "2026-01-01T00:00:00Z"})

    client_mod.main(["reserve", "rpi5", "--json"])

    assert json.loads(capsys.readouterr().out)["lease-expires"]

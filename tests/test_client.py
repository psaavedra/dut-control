#!/usr/bin/env python3

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

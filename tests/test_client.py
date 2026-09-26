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
    """The CLI reads these from the environment; start clean."""
    monkeypatch.delenv(client_mod.CLIENT_SSH_IP_ENV, raising=False)
    monkeypatch.delenv(client_mod.CLIENT_SSH_PORT_ENV, raising=False)
    monkeypatch.delenv(client_mod.TOKEN_ENV, raising=False)


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


OK = {"status": 0}


@pytest.mark.parametrize("command", [
    ["power", "on"],
    ["status"],
    ["flash", "/images/rpi5.wic"],
])
def test_a_subcommand_takes_the_token_from_the_environment(answers,
                                                           monkeypatch,
                                                           command):
    calls = answers(OK)
    monkeypatch.setenv(client_mod.TOKEN_ENV, "from-the-environment")

    assert client_mod.main(command) == 0

    assert calls[0][1]["token"] == "from-the-environment"


def test_a_token_on_the_command_line_still_wins(answers, monkeypatch):
    calls = answers(OK)
    monkeypatch.setenv(client_mod.TOKEN_ENV, "from-the-environment")

    client_mod.main(["power", "on", "given-here"])

    assert calls[0][1]["token"] == "given-here"


def test_a_subcommand_with_no_token_anywhere_names_the_variable(answers,
                                                                capsys):
    answers(OK)

    with pytest.raises(SystemExit) as exit_info:
        client_mod.main(["status"])

    assert exit_info.value.code == 1
    assert client_mod.TOKEN_ENV in capsys.readouterr().err


def test_lease_releases_the_reservation_the_environment_names(answers,
                                                              monkeypatch):
    calls = answers(OK)
    monkeypatch.setenv(client_mod.TOKEN_ENV, "from-the-environment")

    client_mod.main(["lease"])

    assert calls[0][1]["token"] == "from-the-environment"


def test_lease_all_still_means_all(answers, monkeypatch):
    """The environment must not narrow what was asked for explicitly."""
    calls = answers(OK)
    monkeypatch.setenv(client_mod.TOKEN_ENV, "from-the-environment")

    client_mod.main(["lease", "--all"])

    assert "token" not in calls[0][1]


def test_lease_with_nothing_set_still_releases_everything(answers):
    calls = answers(OK)

    client_mod.main(["lease"])

    assert "token" not in calls[0][1]


BUSY = {"status": -4, "error": "no free duts for pool"}
UNKNOWN_POOL = {"status": -2, "error": "pool does not exist"}


@pytest.fixture
def replies(monkeypatch):
    """Answer each POST with the next payload; never really sleep."""
    calls, waits = [], []

    def reply_with(*payloads):
        def post(url, json=None, timeout=None):
            calls.append((url, json))
            return FakeResponse(payloads[min(len(calls) - 1,
                                             len(payloads) - 1)])

        monkeypatch.setattr(client_mod.requests, "post", post)
        monkeypatch.setattr(client_mod.time, "sleep", waits.append)
        monkeypatch.setenv(client_mod.CLIENT_KEY_ENV, "a-client-key")
        return calls, waits
    return reply_with


def test_a_busy_pool_is_waited_out(replies):
    calls, waits = replies(BUSY, BUSY, RESERVATION)

    assert client_mod.main(["reserve", "rpi5", "--retries", "5"]) == 0

    assert len(calls) == 3
    assert waits == [60.0, 60.0]


def test_a_pool_that_does_not_exist_is_not_waited_out(replies, capsys):
    """Retrying a typo for an hour helps nobody."""
    calls, waits = replies(UNKNOWN_POOL)

    with pytest.raises(SystemExit) as exit_info:
        client_mod.main(["reserve", "rpi5", "--retries", "50"])

    assert exit_info.value.code == 1
    assert len(calls) == 1
    assert waits == []
    assert "pool does not exist" in capsys.readouterr().err


def test_the_retries_run_out(replies, capsys):
    calls, waits = replies(BUSY)

    with pytest.raises(SystemExit):
        client_mod.main(["reserve", "rpi5", "--retries", "2"])

    assert len(calls) == 3
    assert len(waits) == 2
    assert "no free duts" in capsys.readouterr().err


def test_nothing_is_retried_by_default(replies):
    calls, waits = replies(BUSY)

    with pytest.raises(SystemExit):
        client_mod.main(["reserve", "rpi5"])

    assert len(calls) == 1
    assert waits == []


def test_a_retry_leaves_json_alone(replies, capsys):
    """Progress goes to stderr or it would corrupt the object."""
    replies(BUSY, RESERVATION)

    client_mod.main(["reserve", "rpi5", "--retries", "1", "--json"])

    captured = capsys.readouterr()
    assert json.loads(captured.out) == RESERVATION
    assert "busy" in captured.err


@pytest.mark.parametrize("given, seconds", [
    ("30", 30.0),
    ("30s", 30.0),
    ("5m", 300.0),
    ("1h", 3600.0),
    ("1.5m", 90.0),
    ("2 H", 7200.0),
])
def test_a_wait_may_carry_a_unit(replies, given, seconds):
    _, waits = replies(BUSY, RESERVATION)

    client_mod.main(["reserve", "rpi5", "--retries", "1",
                     "--retries-wait", given])

    assert waits == [seconds]


@pytest.mark.parametrize("given", ["soon", "5 days", "-30", "", "m"])
def test_a_wait_that_is_not_a_duration_is_refused(given):
    with pytest.raises(SystemExit) as exit_info:
        client_mod.build_parser().parse_args(
            ["reserve", "rpi5", "--retries-wait", given])

    assert exit_info.value.code == 2


def reachable(*states):
    return [{"status": state} for state in states]


def test_wait_returns_as_soon_as_the_dut_answers_ssh(replies, capsys):
    calls, waits = replies(*reachable("offline", "ping", "ssh"))

    assert client_mod.main(["wait", "a-token"]) == 0

    assert len(calls) == 3
    assert waits == [10.0, 10.0]
    assert capsys.readouterr().out == "ssh\n"


def test_waiting_for_ping_is_satisfied_by_ssh(replies):
    """A DUT answering SSH answers ping; the states are a ladder."""
    calls, _ = replies(*reachable("ssh"))

    client_mod.main(["wait", "a-token", "--for", "ping"])

    assert len(calls) == 1


def test_wait_gives_up_and_says_what_it_saw(replies, capsys):
    calls, waits = replies(*reachable("offline"))

    with pytest.raises(SystemExit) as exit_info:
        client_mod.main(["wait", "a-token", "--retries", "3",
                         "--retries-wait", "5s"])

    assert exit_info.value.code == 1
    assert len(calls) == 4
    assert waits == [5.0, 5.0, 5.0]
    assert "gave up waiting for ssh; dut is offline" in \
        capsys.readouterr().err


def test_wait_takes_the_token_from_the_environment(replies, monkeypatch):
    calls, _ = replies(*reachable("ssh"))
    monkeypatch.setenv(client_mod.TOKEN_ENV, "from-the-environment")

    client_mod.main(["wait"])

    assert calls[0][1] == {"token": "from-the-environment"}


def test_a_state_nobody_knows_is_not_the_one_we_asked_for(replies):
    calls, _ = replies(*reachable("rebooting"))

    with pytest.raises(SystemExit):
        client_mod.main(["wait", "a-token", "--retries", "0"])

    assert len(calls) == 1


def test_wipe_sends_the_size_in_bytes(answers):
    calls = answers(OK)

    assert client_mod.main(["wipe", "a-token", "--size", "256MiB"]) == 0

    assert calls[0][1] == {"token": "a-token", "size": 256 * 1024 ** 2}


def test_wipe_leaves_the_default_size_to_the_service(answers):
    calls = answers(OK)

    client_mod.main(["wipe", "a-token"])

    assert calls[0][1] == {"token": "a-token"}


@pytest.mark.parametrize("given, expected", [
    ("512", 512),
    ("512B", 512),
    ("64K", 64 * 1024),
    ("128M", 128 * 1024 ** 2),
    ("128MiB", 128 * 1024 ** 2),
    ("128MB", 128 * 1024 ** 2),
    ("2G", 2 * 1024 ** 3),
])
def test_a_size_may_carry_a_unit(answers, given, expected):
    """K, M and G are binary, as in dd."""
    calls = answers(OK)

    client_mod.main(["wipe", "a-token", "--size", given])

    assert calls[0][1]["size"] == expected


@pytest.mark.parametrize("given", ["lots", "128X", "", "-1", "1.5M", "0"])
def test_a_size_that_is_not_a_size_is_refused(given):
    with pytest.raises(SystemExit) as exit_info:
        client_mod.build_parser().parse_args(
            ["wipe", "a-token", "--size", given])

    assert exit_info.value.code == 2

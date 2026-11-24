from collections.abc import Sequence
from dataclasses import fields
from decimal import Decimal
import json
import signal
import subprocess

import pytest

from anycompute import engine
from anycompute.contract import ProviderCaps, ProviderSpec, Result, SandboxInfo, SandboxState
from tests.conformance import runner
from tests.fakes import FakeClock


class Transport:
    def __init__(self):
        self.calls = []
        self.listed = {}
        self.first_json = None
        self.gc_failure = None
        self.latest_process = None
        self.child_exit = None
        self.child_boxes = ("detached-box",)
        self.states = {}
        self.state_calls = []
        self.list_calls = []

    def list(self, run_id, timeout_s):
        assert 0 < timeout_s <= 120
        self.list_calls.append((run_id, self.latest_process.poll() if self.latest_process else None,
                                len(self.state_calls)))
        labels = {"anycompute": "1", "anycompute-run": run_id, "anycompute-ttl": "120"}
        return [SandboxInfo(sandbox, labels, None)
                for sandbox in self.listed.get(run_id, ())]

    def state(self, sandbox_id, timeout_s):
        assert self.latest_process is not None
        self.state_calls.append((sandbox_id, timeout_s, self.latest_process.poll()))
        return next(self.states[sandbox_id], SandboxState.GONE) if sandbox_id in self.states else SandboxState.GONE

    def read(self, sandbox_id, path, timeout_s):
        return b""

    def start(self, command: Sequence[str], **kwargs):
        run_id = kwargs["env"]["ANYCOMPUTE_RUN_ID"]
        self.calls.append((tuple(command), run_id))
        gc_id = next((arg.removeprefix("--run=") for arg in command if arg.startswith("--run=")), None)
        if gc_id is not None:
            if gc_id == self.gc_failure:
                return Process(1, b"unexpected")
            self.listed.pop(gc_id, None)
            return Process(0, b'{"error": null, "sandboxes": []}')
        if command[-1] == "touch ready; while :; do :; done":
            self.listed[run_id] = self.child_boxes
            self.latest_process = Process(self.child_exit, b"")
        elif tuple(command[-2:]) == ("sleep", "600"):
            self.listed[run_id] = ["expiring-box"]
            self.states["expiring-box"] = iter([SandboxState.RUNNING, SandboxState.GONE])
            report = {"provider": "docker", "run_id": run_id, "sandbox_id": "expiring-box",
                      "error": {"type": "SandboxTimeoutError"}}
            self.latest_process = Process(None, json.dumps(report).encode(), finish_code=123)
        elif self.first_json is not None:
            code, stdout = self.first_json
            self.first_json = None
            self.latest_process = Process(code, stdout)
        else:
            ttl = int(command[command.index("--ttl") + 1])
            report = dict.fromkeys(field.name for field in fields(Result)) | {
                "provider": command[command.index("--provider") + 1], "run_id": run_id,
                "effective_ttl": ttl, "sandbox_id": f"box-{len(self.calls)}", "exit_code": 125, "error": None,
            }
            self.latest_process = Process(125, json.dumps(report).encode())
        return self.latest_process


class Process:
    def __init__(self, returncode, stdout, *, finish_code=None):
        self.returncode = returncode
        self.stdout = stdout
        self.finish_code = finish_code

    def poll(self):
        return self.returncode

    def communicate(self, *, timeout):
        assert timeout > 0
        if self.returncode is None:
            self.returncode = self.finish_code
        return self.stdout, None

    def kill(self):
        self.returncode = -signal.SIGKILL

    def send_signal(self, sig):
        self.returncode = -sig

    def wait(self, *, timeout):
        assert timeout > 0
        return self.returncode


@pytest.fixture
def start_runner(monkeypatch):
    def build(provider="docker", *, run_id="round", caps=None, prices=None):
        transport, clock = Transport(), FakeClock()
        spec = ProviderSpec(provider, caps or ProviderCaps(), lambda: pytest.fail("opened a real SDK"))
        monkeypatch.setattr(engine, "_clock", clock)
        monkeypatch.setattr(subprocess, "Popen", transport.start)
        suite = runner.ConformanceRunner(spec, run_id, observer=transport, prices=prices)
        return suite, transport, clock

    return build


def test_passes_rounded_ttl_and_accounts_for_a_workload_exit_125(start_runner):
    suite, transport, clock = start_runner("daytona", caps=ProviderCaps(ttl_unit_s=60))
    code, report = suite.run(["exit-example"], ttl=61)
    assert code == report["exit_code"] == 125 and report["error"] is None
    assert report["effective_ttl"] == 120
    assert suite.reservation == runner.reserve_launch(runner.load_prices(), "daytona", runner.Reservation(), [120])
    assert transport.calls == [(('anycompute', 'run', '--json', '--provider', 'daytona', '--ttl', '120',
                                 '--', 'exit-example'), 'round')]
    suite.run(["exit-example"], ttl=60)
    assert suite.reservation.sandboxes == 2 and clock.now() == 2


@pytest.mark.parametrize("case", ["usd", "creates", "rounded-ttl"])
def test_launch_refuses_overspend_without_starting_a_process(start_runner, case):
    prices = runner.load_prices()
    if case == "usd":
        prices = {**prices, "docker": {**prices["docker"], "start_fee_usd": {"value": Decimal("2")}}}
    caps = ProviderCaps(ttl_unit_s=60) if case == "rounded-ttl" else ProviderCaps()
    suite, transport, _ = start_runner(caps=caps, prices=prices)
    allowed = {"usd": 1, "creates": 15, "rounded-ttl": 0}[case]
    for _ in range(allowed):
        suite.run(["exit-example"])
    prior = suite.reservation
    with pytest.raises(ValueError, match={"usd": r"docker.*\$4.*\$2", "creates": "docker.*15",
                                          "rounded-ttl": "effective TTL"}[case]):
        suite.run(["exit-example"], ttl=121 if case == "rounded-ttl" else 120)
    assert suite.reservation == prior and suite.stopped
    assert len(transport.calls) == allowed


@pytest.mark.parametrize(("code", "stdout"), [
    (1, b"unexpected"),
    (0, b'{"error":null} {"error":null}'),
    (0, b'{"error":null,"error":null}'),
    (0, b'{"error":null,"value":NaN}'),
    (0, b'[]'),
    (0, b'{}'),
    (-signal.SIGKILL, b'{"error":null}'),
    (124, b'{"error":{"type":"ProviderError","internal":true}}'),
    (124, b'{"error":{"type":"ProviderError","sandbox_may_exist":true}}'),
    (125, b'{"error":{"type":"CleanupError"}}'),
    (123, b'{"error":{"type":"SandboxTimeoutError","cleanup_error":{}}}'),
    (125, b'{"error":null,"cleanup_error":{}}'),
    (123, b'{"error":null,"rows":[{"error":{"type":"ProviderError","cleanup_error":{}}}]}'),
    (125, b'{"error":null,"rows":[{"error":null,"cleanup_error":{}}]}'),
])
def test_bad_launch_halts_the_session_and_attempts_all_cleanup_labels(start_runner, code, stdout):
    parent = "a" * 40
    target_ids = (parent, "a" * 37 + "-lc", "a" * 37 + "-kc")
    suite, transport, _ = start_runner(run_id=parent)
    transport.first_json = (code, stdout)
    transport.gc_failure = parent
    transport.listed = {label: [f"orphan-{i}"] for i, label in enumerate(target_ids)}
    with pytest.raises(RuntimeError, match="gc failed"):
        suite.run(["exit-example"])
    assert suite.stopped and suite.reservation.sandboxes == 1
    with pytest.raises(RuntimeError, match="stopped"):
        suite.run(["must-not-start"])
    assert suite.reservation.sandboxes == 1
    assert [cmd[-1] for cmd, _ in transport.calls[1:]] == [f"--run={label}" for label in target_ids]
    assert transport.listed == {parent: ["orphan-0"]}


@pytest.mark.parametrize("label", ["round", "round-lc", "round-kc"])
def test_post_launch_inventory_counts_leftover_duplicate_and_reported_primary(start_runner, label, monkeypatch):
    suite, transport, _ = start_runner()

    def start(command, **kwargs):
        if command[1] == "run":
            transport.listed[label] = ["leftover-duplicate"]
        return transport.start(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start)
    with pytest.raises(RuntimeError, match="more sandboxes than reserved"):
        suite.run(["exit-example"])
    assert suite.stopped and suite.reservation.sandboxes == 1
    assert not transport.listed


def test_runner_reserves_for_partial_retry_by_caps(start_runner):
    suite, transport, _ = start_runner(caps=ProviderCaps(create_retry="partial"))
    transport.listed["round"] = ["allowed-duplicate"]
    code, _ = suite.run(["exit-example"])
    assert code == 125 and not suite.stopped
    assert suite.reservation.sandboxes == 2


@pytest.mark.parametrize("lifetime", ["hard", "partial"])
def test_lifetime_observer_checks_active_sandbox_and_times_confirmed_deletion(start_runner, monkeypatch, lifetime):
    suite, transport, clock = start_runner(caps=ProviderCaps(lifetime=lifetime))
    state = transport.state

    def observe(sandbox, timeout):
        result = state(sandbox, timeout)
        if lifetime == "partial" and result == SandboxState.GONE:
            if transport.latest_process.poll() is None:
                return SandboxState.STOPPED
            clock.sleep(2)
        return result

    monkeypatch.setattr(transport, "state", observe)
    code, report, expiry = suite.lifetime()
    assert code == 123 and report["error"]["type"] == "SandboxTimeoutError"
    assert expiry.sandbox_id == "expiring-box"
    assert expiry.elapsed_s == 1
    expected_calls = [("expiring-box", 240, None), ("expiring-box", 239, None)]
    if lifetime == "hard":
        assert (expiry.status, expiry.deleted_elapsed_s) == ("pass", 1)
    else:
        assert (expiry.status, expiry.deleted_elapsed_s) == ("partial", 3)
        expected_calls.append(("expiring-box", 120, 123))
    assert transport.state_calls == expected_calls
    assert suite.reservation.sandboxes == 1 and not suite.stopped


def test_confirmed_delete_requires_provider_state_even_when_list_is_empty(start_runner):
    suite, transport, _ = start_runner()
    _, report = suite.run(["exit-example"])
    assert not transport.listed
    assert suite.confirm_deleted(report["sandbox_id"]) is None
    transport.states[report["sandbox_id"]] = iter([SandboxState.STOPPED])
    with pytest.raises(RuntimeError, match="did not confirm deletion"):
        suite.confirm_deleted(report["sandbox_id"])
    assert suite.stopped
    assert transport.state_calls == [(report["sandbox_id"], 120, 125)] * 2


def test_detached_child_reserves_before_spawn_at_boat_pace_and_accepts_expected_sigkill(start_runner, monkeypatch):
    suite, transport, clock = start_runner("boat", caps=ProviderCaps(lifetime="partial"), run_id="r" * 40)
    suite.run(["exit-example"], ttl=1)
    child_id = "r" * 37 + "-kc"

    def start(command, **kwargs):
        if kwargs.get("start_new_session"):
            assert clock.now() == 10
            assert suite.reservation.sandboxes == 2
            assert kwargs["env"]["ANYCOMPUTE_RUN_ID"] == child_id
        return transport.start(command, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start)
    result = suite.kill_client(ttl=120)
    assert (result.run_id, result.sandbox_id, result.status) == (child_id, "detached-box", "pass")
    assert transport.latest_process.returncode == -signal.SIGKILL
    assert [label for label, code, samples in transport.list_calls if code == -signal.SIGKILL and samples == 0] == [
        suite.run_id, "r" * 37 + "-lc", child_id,
    ]
    assert [command[-1] for command, _ in transport.calls if command[1] == "gc"] == [f"--run={child_id}"]
    assert not suite.stopped and not transport.listed


def test_detached_child_cannot_bypass_spend_limit(start_runner):
    prices = runner.load_prices()
    prices = {**prices, "docker": {**prices["docker"], "start_fee_usd": {"value": 2}}}
    suite, transport, _ = start_runner(prices=prices)
    suite.run(["exit-example"])
    with pytest.raises(runner.SpendLimitError):
        suite.kill_client()
    assert suite.stopped and suite.reservation.sandboxes == 1
    assert len(transport.calls) == 1


def test_detached_child_does_not_accept_a_prior_sandboxes_ready_file(start_runner):
    suite, transport, _ = start_runner(caps=ProviderCaps(create_retry="partial"))
    transport.listed["round-kc"] = ["previous-box"]
    suite.run(["exit-example"])
    transport.child_boxes = ("previous-box", "new-box")
    assert suite.kill_client().sandbox_id == "new-box"


@pytest.mark.parametrize("case", ["crash", "duplicate", "cleanup"])
def test_detached_failure_stops_run_and_sweeps_every_label(start_runner, case):
    suite, transport, _ = start_runner()
    if case == "crash":
        transport.child_exit = 1
    elif case == "duplicate":
        transport.child_boxes = ("box-a", "box-b")
    else:
        transport.gc_failure = "round-kc"
    with pytest.raises(RuntimeError, match={"crash": "exited before SIGKILL", "duplicate": "more sandboxes",
                                           "cleanup": "gc failed"}[case]):
        suite.kill_client()
    assert suite.stopped and suite.reservation.sandboxes == 1
    assert {command[-1] for command, _ in transport.calls if command[1] == "gc"} == {
        "--run=round", "--run=round-kc", "--run=round-lc",
    }
    assert bool(transport.listed) == (case == "cleanup")

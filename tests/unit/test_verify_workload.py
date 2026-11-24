from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from anycompute import engine, providers
from anycompute.contract import ProviderCaps, ProviderSpec, SandboxInfo
from tests.conformance import runner, verify_workload
from tests.fakes import FakeClock


class Transport:
    def __init__(self):
        self.events = []
        self.sweep_fails = None
        self.report_mismatch = None
        self.duplicate_boat = False

    def reserve(self, active_runner, ttl):
        self.events.append(("reserve", active_runner.spec.name))
        return self.original_reserve(active_runner, ttl)

    def list(self, name, run_id):
        self.events.append(("list", name, run_id))
        if self.duplicate_boat and name == "boat" and run_id == "42":
            return [SandboxInfo("boat-duplicate", {}, None)]
        return []

    def process(self, command, *, env, **kwargs):
        self.events.append(("launch", command))
        assert env["ANYCOMPUTE_RUN_ID"] == "42"
        assert "--lifecycle" not in command
        code = 0
        if command[1] == "gc":
            name = command[command.index("--provider") + 1]
            report = {"provider": name, "run_id": None, "dry_run": False, "sandboxes": [], "error": None}
            if name == self.sweep_fails:
                report["sandboxes"].append({"action": "failed"})
        else:
            rows = [
                {"provider": name, "sandbox_id": f"{name}-original", "run_id": "42", "effective_ttl": 120,
                 "outside_contract": False, "lifecycle": None, "error": None,
                 **dict.fromkeys(("exit_code", "files", "cleanup", "output"), "pass")}
                for name in ("modal", "boat")
            ]
            if self.report_mismatch:
                key, value = self.report_mismatch
                rows[1][key] = value
            report = {"source": "project-run", "anycompute_version": "0.1.0", "suite_version": None,
                      "rows": rows, "error": None}
        response = json.dumps(report).encode()
        return SimpleNamespace(returncode=code, communicate=lambda **_: (response, b""), poll=lambda: code)


@pytest.fixture
def context(monkeypatch):
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ID": "42",
        "ANYCOMPUTE_CONFORMANCE_LIVE": "1",
        "ANYCOMPUTE_RUN_ID": "42",
        "ANYCOMPUTE_VERIFIED_ATTEMPT": "2",
        "ANYCOMPUTE_VERIFIED_ADAPTERS": '["modal","boat"]',
        "MODAL_TOKEN_ID": "synthetic", "MODAL_TOKEN_SECRET": "synthetic", "BOAT_API_KEY": "synthetic",
    }
    transport = Transport()
    monkeypatch.setattr(engine, "_clock", FakeClock())
    monkeypatch.setattr(providers, "PROVIDERS", (
        ProviderSpec("modal", ProviderCaps(live_run=True, create_retry="partial"), lambda: None),
        ProviderSpec("boat", ProviderCaps(live_run=True, ttl_max_s=480, lifetime="partial"), lambda: None),
    ))
    monkeypatch.setattr(runner.subprocess, "Popen", transport.process)
    monkeypatch.setattr(verify_workload, "_Observer", lambda spec, _: SimpleNamespace(
        list=lambda run_id, __: transport.list(spec.name, run_id),
    ))
    transport.original_reserve = runner.ConformanceRunner._reserve
    monkeypatch.setattr(runner.ConformanceRunner, "_reserve", lambda self, ttl: transport.reserve(self, ttl))
    return env, transport


def test_verify_reserves_both_then_sweeps_then_launches_one_fixed_workload(context):
    env, transport = context
    report = verify_workload._verification(env)
    assert [row["provider"] for row in report["rows"]] == ["modal", "boat"]
    assert transport.events[:5] == [
        ("reserve", "modal"), ("reserve", "boat"),
        ("launch", ("anycompute", "gc", "--all", "--provider", "modal", "--json")),
        ("launch", ("anycompute", "gc", "--all", "--provider", "boat", "--json")),
        ("launch", ("anycompute", "verify", str(verify_workload._TASK), "--providers", "modal,boat", "--json")),
    ]
    assert [event for event in transport.events if event[0] == "list" and event[-1] == "42"] == [
        ("list", "modal", "42"), ("list", "boat", "42"),
    ]


@pytest.mark.parametrize(("key", "value"), [
    ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_RUN_ATTEMPT", "3"), ("GITHUB_REF", "refs/heads/feature"),
    ("ANYCOMPUTE_VERIFIED_ADAPTERS", '["boat","modal"]'), ("ANYCOMPUTE_RUN_ID", "43"), ("BOAT_API_KEY", ""),
])
def test_verify_refuses_before_any_calls_without_exact_counted_context(context, key, value):
    env, transport = context
    env[key] = value
    with pytest.raises((ValueError, runner.ConformanceFailure)):
        verify_workload._verification(env)
    assert transport.events == []


def test_disabled_adapter_refuses_without_sweeping_either_provider(context, monkeypatch):
    env, transport = context
    modal, boat = providers.PROVIDERS
    monkeypatch.setattr(providers, "PROVIDERS", (modal, replace(boat, caps=replace(boat.caps, live_run=False))))
    with pytest.raises(ValueError, match="not enabled"):
        verify_workload._verification(env)
    assert transport.events == []


def test_verify_refuses_before_sweeping_if_second_reservation_fails(context, monkeypatch):
    env, transport = context
    modal, boat = providers.PROVIDERS
    monkeypatch.setattr(providers, "PROVIDERS", (modal, replace(boat, caps=replace(boat.caps, ttl_max_s=60))))
    with pytest.raises(engine.InvalidTTLError):
        verify_workload._verification(env)
    assert transport.events == [("reserve", "modal"), ("reserve", "boat")]


def test_verify_sweep_failure_prevents_workload_launch(context):
    env, transport = context
    transport.sweep_fails = "boat"
    with pytest.raises(runner.ConformanceFailure, match="pre-run sweep"):
        verify_workload._verification(env)
    assert [event[0] for event in transport.events] == ["reserve", "reserve", "launch", "launch"]


def test_verify_counts_the_confirmed_primary_alongside_duplicates(context):
    env, transport = context
    transport.duplicate_boat = True
    with pytest.raises(runner.ConformanceFailure, match="boat: found more sandboxes than reserved"):
        verify_workload._verification(env)


@pytest.mark.parametrize(("key", "value"), [
    ("effective_ttl", 90), ("cleanup", "fail"), ("run_id", "43"), ("outside_contract", True),
    ("lifecycle", "pass"), ("sandbox_id", "***"), ("error", {"type": "CleanupError"}),
])
def test_verify_report_must_match_reservation_and_confirm_cleanup(context, key, value):
    env, transport = context
    transport.report_mismatch = key, value
    with pytest.raises(runner.ConformanceFailure):
        verify_workload._verification(env)

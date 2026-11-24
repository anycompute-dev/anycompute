from inspect import unwrap
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from anycompute import engine, providers
from anycompute.contract import ProviderCaps, ProviderSpec, SandboxInfo, SandboxState
from tests.fakes import FakeClock, FakeProvider


@pytest.mark.parametrize(("args", "suite"), [
    ((), "tests/unit/"),
    (("-m", "live_conformance"), "tests/conformance/"),
])
def test_live_conformance_requires_explicit_selection(args, suite):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args,
         "tests/unit/test_project_checks.py", "tests/conformance"],
        cwd=Path(__file__).resolve().parents[2], env={"PATH": os.defpath},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = [line for line in result.stdout.splitlines() if "::" in line]
    assert collected and all(nodeid.startswith(suite) for nodeid in collected), collected


def _unavailable():
    pytest.fail("selector opened an SDK")


@pytest.fixture
def registry(monkeypatch):
    specs = (
        ProviderSpec("docker", ProviderCaps(), _unavailable),
        ProviderSpec("modal", ProviderCaps(live_run=True), _unavailable),
        ProviderSpec("boat", ProviderCaps(), _unavailable),
        ProviderSpec("e2b", ProviderCaps(), _unavailable),
    )
    monkeypatch.setattr(providers, "PROVIDERS", specs)
    return specs


_OPTED_IN = {
    "ANYCOMPUTE_CONFORMANCE_PROVIDER": "modal", "ANYCOMPUTE_CONFORMANCE_LIVE": "1",
    "GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ATTEMPT": "2",
    "ANYCOMPUTE_VERIFIED_ATTEMPT": "2", "MODAL_MAX_THROTTLE_WAIT": "0",
}


@pytest.mark.parametrize(("key", "value"), [
    ("ANYCOMPUTE_CONFORMANCE_PROVIDER", ""),
    ("ANYCOMPUTE_CONFORMANCE_LIVE", "true"),
    ("GITHUB_ACTIONS", "false"),
    ("GITHUB_REF", "refs/pull/1/merge"),
    ("GITHUB_RUN_ATTEMPT", "3"),
    ("ANYCOMPUTE_VERIFIED_ATTEMPT", ""),
    ("MODAL_MAX_THROTTLE_WAIT", ""),
    ("MODAL_MAX_THROTTLE_WAIT", "30"),
    ("ANYCOMPUTE_CONFORMANCE_PROVIDER", "boat"),
])
def test_cloud_selection_fails_closed_before_factory(registry, key, value):
    from tests.conformance.runner import select_provider

    with pytest.raises(ValueError):
        select_provider(_OPTED_IN | {key: value})


def test_selects_only_opted_in_docker_or_counted_live_adapter(registry):
    from tests.conformance.runner import select_provider

    with pytest.raises(ValueError, match="ANYCOMPUTE_CONFORMANCE_PROVIDER"):
        select_provider({})
    assert select_provider({"ANYCOMPUTE_CONFORMANCE_PROVIDER": "docker"}) is registry[0]
    assert select_provider(_OPTED_IN) is registry[1]
    with pytest.raises(ValueError, match="attempt"):
        select_provider(_OPTED_IN | {"GITHUB_RUN_ATTEMPT": "0", "ANYCOMPUTE_VERIFIED_ATTEMPT": "0"})


def test_selects_e2b_trial_only_for_a_counted_manual_dispatch(registry):
    from tests.conformance.runner import select_provider

    env = _OPTED_IN | {"ANYCOMPUTE_CONFORMANCE_PROVIDER": "e2b", "GITHUB_EVENT_NAME": "workflow_dispatch"}
    assert select_provider(env) is registry[3]
    with pytest.raises(ValueError, match="not enabled"):
        select_provider(env | {"GITHUB_EVENT_NAME": "schedule"})
    with pytest.raises(ValueError, match="attempt"):
        select_provider(env | {"ANYCOMPUTE_VERIFIED_ATTEMPT": "1"})


def test_fixture_run_id_uses_exact_env_or_one_generated_id(registry, monkeypatch):
    from tests.conformance import conftest

    monkeypatch.setenv("ANYCOMPUTE_CONFORMANCE_PROVIDER", "docker")
    monkeypatch.setattr(conftest, "_Observer", lambda spec, run_id: object())
    build = unwrap(conftest.conformance)
    monkeypatch.setattr(engine, "_run_id", None)
    monkeypatch.delenv("ANYCOMPUTE_RUN_ID", raising=False)
    run_id = build().run_id
    assert re.fullmatch(r"[0-9a-f]{12}", run_id) and run_id == build().run_id
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "round-id")
    assert build().run_id == "round-id"


def test_observer_passes_only_owned_labels_and_spends_each_operations_budget(monkeypatch):
    from anycompute.lifecycle import _Observer

    clock, events = FakeClock(), []
    monkeypatch.setattr(engine, "_clock", clock)
    labels = {"anycompute": "1", "anycompute-run": "round-kc"}
    owned = SandboxInfo("box", labels, None)

    class Adapter(FakeProvider):
        def list(self, labels, *, timeout_s):
            events.append(("list", labels, timeout_s))
            return [owned, SandboxInfo("foreign", {"anycompute-run": "round-kc"}, None)]

        def read(self, sandbox_id, path, *, timeout_s):
            events.append(("read", sandbox_id, path, timeout_s))
            return b""

        def state(self, sandbox_id, *, timeout_s):
            events.append(("state", sandbox_id, timeout_s))
            return SandboxState.GONE

    spec = ProviderSpec("docker", ProviderCaps(), Adapter)
    observer = _Observer(spec, "round")
    assert list(observer.list("round-kc", 60)) == [owned]
    assert observer.read("box", "ready", 50) == b""
    assert observer.state("box", 120) == SandboxState.GONE
    assert events == [("list", labels, 60), ("read", "box", "/tmp/anycompute/ready", 50), ("state", "box", 120)]

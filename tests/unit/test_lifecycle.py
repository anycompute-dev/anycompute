from dataclasses import dataclass
import signal
import subprocess
import sys

import pytest

from anycompute import engine
from anycompute.contract import CleanupError, ProviderCaps, ProviderSpec, RateLimitError, SandboxInfo, SandboxState
from tests.fakes import FakeClock, FakeProvider


@dataclass
class Child:
    events: list
    returncode: int | None = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.returncode = -signal.SIGKILL

    def send_signal(self, sig):
        self.events.append(sig)
        self.returncode = -sig

    def wait(self, *, timeout):
        assert timeout > 0
        assert self.returncode is not None
        return self.returncode


def test_kill_client_waits_for_empty_ready_file_then_observes_expiry_and_cleans():
    from anycompute.lifecycle import _kill_client

    clock, events, reads = FakeClock(start=10), [], iter([None, b""])
    child = Child(events)
    parent = "a" * 40
    child_id = "a" * 37 + "-kc"

    class Observer:
        def list(self, run_id, timeout_s):
            events.append(("list", run_id, timeout_s))
            clock.sleep(2)
            return [SandboxInfo("box", {}, None)]

        def read(self, sandbox_id, path, timeout_s):
            marker = next(reads)
            events.append(("read", sandbox_id, path, timeout_s, marker))
            return marker

        def state(self, sandbox_id, timeout_s):
            assert child.poll() == -signal.SIGKILL
            events.append(("state", sandbox_id, timeout_s))
            return SandboxState.GONE

    def launch(run_id, ttl):
        events.append(("launch", run_id, ttl))
        return child

    result = _kill_client(
        parent, effective_ttl=10, lifetime="hard", suffix="-kc", launch=launch,
        observer=Observer(), cleanup=lambda run_id: events.append(("gc", run_id)), clock=clock,
    )
    assert (result.run_id, result.sandbox_id, result.status, result.elapsed_s) == (child_id, "box", "pass", 3)
    assert result.deleted_elapsed_s == 3
    assert events == [
        ("launch", child_id, 10), ("list", child_id, 10), ("read", "box", "ready", 8, None),
        ("read", "box", "ready", 7, b""), "kill", ("state", "box", 127), ("gc", child_id),
    ]


def test_launch_child_uses_default_marker_workload_and_child_env(monkeypatch):
    from anycompute.lifecycle import _launch_child

    def start(command, **kwargs):
        assert tuple(command) == (
            sys.executable, "-m", "anycompute", "run", "--json", "--provider", "docker", "--ttl", "120",
            "--", "sh", "-c", "touch ready; while :; do :; done",
        )
        assert kwargs["env"]["ANYCOMPUTE_RUN_ID"] == "round-lc"
        assert kwargs["start_new_session"] is True
        return Child([])

    monkeypatch.setattr(subprocess, "Popen", start)
    assert isinstance(_launch_child("docker", "round-lc", 120), Child)


def test_no_marker_by_ttl_interrupts_without_sigkill_and_runs_child_gc():
    from anycompute.lifecycle import _kill_client

    events, clock = [], FakeClock(start=100)

    class Observer:
        def list(self, run_id, timeout_s):
            assert run_id == "a" * 37 + "-lc"
            assert timeout_s == 3
            return [SandboxInfo("box", {}, None)]

        def read(self, sandbox_id, path, timeout_s):
            events.append(("missing", timeout_s))
            return None

    result = _kill_client(
        "a" * 40, effective_ttl=3, lifetime="hard", suffix="-lc", launch=lambda *_: Child(events),
        observer=Observer(), cleanup=lambda run_id: events.append(("gc", run_id)), clock=clock,
    )
    assert (result.status, result.elapsed_s, result.reason) == ("fail", 3, "no marker by the TTL")
    assert result.deleted_elapsed_s is None
    assert events == [("missing", 3), ("missing", 2), signal.SIGINT, ("gc", "a" * 37 + "-lc")]


@pytest.mark.parametrize("cleanup_state", [SandboxState.GONE, SandboxState.STOPPED])
def test_partial_expiry_requires_a_confirmed_child_delete(cleanup_state):
    from anycompute.lifecycle import _kill_client

    clock, events = FakeClock(), []

    class Observer:
        def list(self, run_id, timeout_s):
            return [SandboxInfo("box", {}, None)]

        def read(self, sandbox_id, path, timeout_s):
            return b""

        def state(self, sandbox_id, timeout_s):
            return cleanup_state if ("gc", "round-lc") in events else SandboxState.STOPPED

    def check():
        def cleanup(run_id):
            clock.sleep(2)
            events.append(("gc", run_id))

        return _kill_client(
            "round", effective_ttl=10, lifetime="partial", suffix="-lc", launch=lambda *_: Child(events),
            observer=Observer(), cleanup=cleanup, clock=clock,
        )

    if cleanup_state == SandboxState.GONE:
        outcome = check()
        assert (outcome.status, outcome.elapsed_s, outcome.deleted_elapsed_s) == ("partial", 0, 2)
    else:
        with pytest.raises(CleanupError) as failure:
            check()
        assert (failure.value.run_id, failure.value.sandbox_id) == ("round-lc", "box")
    assert events == ["kill", ("gc", "round-lc")]


@pytest.mark.parametrize(("state", "lifetime", "duration", "expected"), [
    (SandboxState.GONE, "hard", 4, SandboxState.GONE),
    (SandboxState.GONE, "partial", 6, None),
    (SandboxState.STOPPED, "partial", 4, SandboxState.STOPPED),
    (SandboxState.STOPPED, "hard", 0, None),
    (SandboxState.UNKNOWN, "hard", 0, None),
])
def test_expiry_pass_rules_reject_late_or_unconfirmed_ends(state, lifetime, duration, expected):
    from anycompute.lifecycle import _wait_for_end

    clock = FakeClock()

    class Observer:
        def state(self, sandbox_id, timeout_s):
            clock.sleep(duration)
            return state

    assert _wait_for_end(Observer(), "box", lifetime, clock=clock, deadline=5) == expected
    assert clock.now() == (duration or 5)


def test_observer_poll_uses_retry_after_and_only_one_immediate_retry():
    from anycompute.lifecycle import _wait_for_end

    clock, times = FakeClock(), []
    script = iter([0, 0, 7, None])

    class Observer:
        def state(self, sandbox_id, timeout_s):
            times.append((clock.now(), timeout_s))
            if (retry := next(script)) is not None:
                raise RateLimitError("fixture response", retry_after=retry)
            return SandboxState.GONE

    assert _wait_for_end(Observer(), "box", "hard", clock=clock, deadline=15) == SandboxState.GONE
    assert times == [(0, 15), (0, 15), (2, 13), (9, 6)]


def test_hung_marker_read_ends_at_marker_deadline_and_still_attempts_gc(monkeypatch):
    from anycompute.lifecycle import _Observer, _kill_client

    clock, events = FakeClock(), []
    monkeypatch.setattr(engine, "_clock", clock)
    caps = ProviderCaps()
    adapter = FakeProvider(clock=clock, caps=caps, script={"read": [{"hang": True}]}, sandboxes=(
        {"sandbox_id": "box", "labels": {"anycompute": "1", "anycompute-run": "round-kc"}},
    ))
    try:
        result = _kill_client(
            "round", effective_ttl=4, lifetime="hard", suffix="-kc", launch=lambda *_: Child(events),
            observer=_Observer(ProviderSpec("docker", caps, lambda: adapter), "round"),
            cleanup=lambda run_id: events.append(("gc", run_id)), clock=clock,
        )
    finally:
        adapter.release_hangs()
    assert (result.status, result.elapsed_s, result.reason) == ("fail", 4, "no marker by the TTL")
    assert events == [signal.SIGINT, ("gc", "round-kc")]
    assert [call["timeout_s"] for call in adapter.calls if call["op"] == "read"] == [4]

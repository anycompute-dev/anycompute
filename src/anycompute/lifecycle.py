"""Shared kill-client check for conformance and verify."""

from dataclasses import dataclass, replace
from functools import partial
import math
import os
import posixpath
import signal
import subprocess
import sys
from typing import Literal

from . import engine
from .contract import AnycomputeError, CleanupError, ProviderSpec, RateLimitError, SandboxState

_MARKER = "ready"


@dataclass(frozen=True)
class _LifecycleResult:
    run_id: str
    sandbox_id: str | None
    status: Literal["pass", "partial", "fail"]
    elapsed_s: float
    reason: str | None = None
    deleted_elapsed_s: float | None = None


def _child_run_id(parent: str, suffix: Literal["-kc", "-lc"]) -> str:
    return engine._check_run_id(parent)[:37] + suffix


def _launch_child(provider: str, run_id: str, ttl: int) -> subprocess.Popen:
    argv = (sys.executable, "-m", "anycompute", "run", "--json", "--provider", provider, "--ttl", str(ttl),
            "--", "sh", "-c", f"touch {_MARKER}; while :; do :; done")
    return subprocess.Popen(argv, env={**os.environ, "ANYCOMPUTE_RUN_ID": run_id},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


class _Observer:
    """Read-only adapter client independent of the workload's process."""

    def __init__(self, spec: ProviderSpec, run_id: str):
        self._ctx, keys = engine._provider_context(spec.name, engine._check_run_id(run_id))
        engine._require_auth(self._ctx, spec, keys)
        self._client = engine._build_client(self._ctx, spec)

    def list(self, run_id, timeout_s):
        labels = {"anycompute": "1", "anycompute-run": engine._check_run_id(run_id)}
        ctx = replace(self._ctx, run_id=run_id)
        items = engine._bounded_call(ctx, "list", partial(self._client.list, labels), timeout_s)
        return tuple(item for item in items if all(item.labels.get(key) == value for key, value in labels.items()))

    def read(self, sandbox_id, path, timeout_s):
        engine._validate_path(path)
        ctx = replace(self._ctx, sandbox_id=sandbox_id)
        read = partial(self._client.read, sandbox_id, f"{engine._CWD}/{posixpath.normpath(path)}")
        return engine._bounded_call(ctx, "read", read, timeout_s)

    def state(self, sandbox_id, timeout_s):
        ctx = replace(self._ctx, sandbox_id=sandbox_id)
        return engine._bounded_call(ctx, "state", partial(self._client.state, sandbox_id), timeout_s)


class _PollBackoff:
    delay = 1
    immediate_retry = True

    def sleep(self, clock, deadline, retry_after: float | None = None):
        wait: float = self.delay
        if (retry_after is not None and type(retry_after) in (float, int) and 0 <= retry_after < math.inf
                and (retry_after > 0 or self.immediate_retry)):
            wait = retry_after
            if wait == 0:
                self.immediate_retry = False
        clock.sleep(min(wait, max(0, deadline - clock.now())))
        self.delay = min(2 * self.delay, 10)


def _poll(clock, deadline, sample):
    backoff = _PollBackoff()
    while (left := deadline - clock.now()) > 0:
        try:
            result = sample(left)
        except RateLimitError as error:
            backoff.sleep(clock, deadline, error.retry_after)
        except AnycomputeError:
            if clock.now() >= deadline:
                return None
            raise
        else:
            if result is not None and clock.now() <= deadline:
                return result
            backoff.sleep(clock, deadline)
    return None


def _wait_for_end(observer, sandbox_id, lifetime, *, clock, deadline):
    def sample(left):
        state = observer.state(sandbox_id, left)
        if state == SandboxState.GONE or lifetime == "partial" and state == SandboxState.STOPPED:
            return state
        return None

    return _poll(clock, deadline, sample)


def _kill_client(parent: str, *, effective_ttl: int, lifetime: Literal["hard", "partial"],
                 suffix: Literal["-kc", "-lc"], launch, observer, cleanup,
                 clock=None, after_kill=None) -> _LifecycleResult:
    """Launch a marker workload; interrupt on failure, SIGKILL only after ready, then GC the child label."""
    clock = engine._clock if clock is None else clock
    run_id = _child_run_id(parent, suffix)
    start = clock.now()
    marker_deadline = start + effective_ttl
    expiry_deadline = marker_deadline + engine._GRACE_S
    child = sandbox_id = state = None
    candidates = ()

    def marker(left):
        nonlocal candidates
        assert child is not None
        if child.poll() is not None:
            return False
        if not candidates:
            candidates = observer.list(run_id, left)
        for item in candidates:
            left = marker_deadline - clock.now()
            if left <= 0:
                break
            if observer.read(item.sandbox_id, _MARKER, left) is not None:
                return item.sandbox_id
        return None

    try:
        child = launch(run_id, effective_ttl)
        found = _poll(clock, marker_deadline, marker)
        if found is False or child.poll() is not None:
            return _LifecycleResult(run_id, None, "fail", clock.now() - start, "child exited before SIGKILL")
        if found is None:
            return _LifecycleResult(run_id, None, "fail", clock.now() - start, "no marker by the TTL")
        sandbox_id = found
        child.kill()
        if child.wait(timeout=5) != -signal.SIGKILL:
            return _LifecycleResult(run_id, sandbox_id, "fail", clock.now() - start, "child exited before SIGKILL")
        if after_kill is not None:
            after_kill(expiry_deadline)
        state = _wait_for_end(observer, sandbox_id, lifetime, clock=clock, deadline=expiry_deadline)
        if state is None:
            return _LifecycleResult(run_id, sandbox_id, "fail", clock.now() - start, "sandbox did not end by TTL plus grace")
        ended = clock.now() - start
    finally:
        try:
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGINT)
                child.wait(timeout=engine._CLEANUP_S + 5)
        finally:
            cleanup(run_id)
            if state == SandboxState.STOPPED:
                try:
                    confirmed = observer.state(sandbox_id, engine._CLEANUP_S) == SandboxState.GONE
                except AnycomputeError:
                    confirmed = False
                if not confirmed:
                    raise CleanupError("Could not confirm lifecycle sandbox deletion",
                                       sandbox_id=sandbox_id, run_id=run_id)
    partial = state == SandboxState.STOPPED
    return _LifecycleResult(run_id, sandbox_id, "partial" if partial else "pass", ended,
                            deleted_elapsed_s=clock.now() - start if partial else ended)

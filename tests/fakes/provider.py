from copy import deepcopy
from itertools import count
from posixpath import join
from threading import Event, Lock

from anycompute.contract import (
    AuthError, KeySources, ProviderCaps, ProviderError, QuotaError, RateLimitError,
    RawExecResult, SandboxInfo, SandboxState,
)

from .clock import FakeClock

FAKE_CAPS = ProviderCaps(
    provider_options_allowlist=frozenset({"environment", "image"}),
    key_sources=KeySources(env=("ANYCOMPUTE_FAKE_KEY",)),
    isolation="in-memory",
)
_MAPPED_ERRORS = {401: AuthError, "quota": QuotaError}
_RAW_ERRORS = {"internal": TypeError, "interrupt": KeyboardInterrupt}


def _output(value):
    return bytes(value) if isinstance(value, list) else value


def _file_payloads(cwd, files):
    return {join(cwd, path): value.encode() if isinstance(value, str) else bytes(value) for path, value in files.items()}


class FakeProvider:
    """An in-memory adapter. Commands are recorded, not interpreted or executed."""

    def __init__(self, *, clock=None, caps=FAKE_CAPS, script=None, sandboxes=(), page_size=None):
        if page_size is not None and (type(page_size) is not int or page_size < 1):
            raise ValueError("page_size must be a positive integer")
        self.clock = clock if clock is not None else FakeClock()
        self.caps = caps
        self._page_size = page_size
        self._lock = Lock()
        self._release = Event()
        self._calls = []
        self._script = {op: list(steps) for op, steps in (script or {}).items()}
        self._inventory = {}
        self._files = {}
        self._states = {}
        self._ids = count(1)
        for row in sandboxes:
            sandbox_id = row["sandbox_id"]
            created_at = row.get("created_at", self.clock.wall_time()) if caps.list_create_time else None
            self._inventory[sandbox_id] = SandboxInfo(sandbox_id, dict(row["labels"]), created_at)
            self._files[sandbox_id] = _file_payloads("/tmp/anycompute", row.get("files", {}))
            self._states[sandbox_id] = SandboxState(row.get("state", "running"))

    @property
    def calls(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._calls)

    def release_hangs(self) -> None:
        self._release.set()

    def _call(self, op, timeout_s, *, script_op=None, **details):
        step = {}
        with self._lock:
            self._calls.append(dict(op=op, at=self.clock.now(), timeout_s=timeout_s, **details))
            steps = self._script.get(script_op or op, [])
            for index, candidate in enumerate(steps):
                if any(key in candidate and candidate[key] != details.get(key) for key in ("sandbox_id", "argv")):
                    continue
                step = candidate
                if not step.get("repeat"):
                    steps.pop(index)
                break
        if op != "create":
            self._respond(op, step)
        return step

    def _respond(self, op, step, *, sandbox_id=None):
        if step.get("delay", 0) > 0:
            self.clock._delay(step["delay"])
        if step.get("hang"):
            self._release.wait()
        error = step.get("error")
        if error is None or (op == "delete" and error == 404):
            return
        message = step.get("message", f"fake {op}: {error}")
        if error == 429:
            raise RateLimitError(message, retry_after=step.get("retry_after"), sandbox_id=sandbox_id)
        if error in _RAW_ERRORS:
            raise _RAW_ERRORS[error](message)
        if error in _MAPPED_ERRORS:
            raise _MAPPED_ERRORS[error](message, sandbox_id=sandbox_id)
        raise ProviderError(message, sandbox_id=sandbox_id, sandbox_may_exist=step.get("sandbox_may_exist", False))

    def prepare(self, *, options, timeout_s):
        self._call("prepare", timeout_s, options=dict(options))

    def create(self, *, ttl, labels, options, timeout_s):
        step = self._call("create", timeout_s, ttl=ttl, labels=dict(labels), options=dict(options))
        if step.get("error") is not None and not step.get("created"):
            self._respond("create", step)
        with self._lock:
            created_at = self.clock.wall_time() if self.caps.list_create_time else None
            ids = []
            for _ in range(2 if step.get("duplicate") else 1):
                sandbox_id = f"fake-{next(self._ids)}"
                while sandbox_id in self._inventory:
                    sandbox_id = f"fake-{next(self._ids)}"
                self._inventory[sandbox_id] = SandboxInfo(sandbox_id, dict(labels), created_at)
                self._files[sandbox_id] = {}
                ids.append(sandbox_id)
        self._respond("create", step, sandbox_id=ids[0])
        return ids[0]

    def exec(self, sandbox_id, argv, *, cwd, timeout_s):
        argv = list(argv)
        mkdir = len(argv) > 2 and argv[:2] == ["mkdir", "-p"] and all(
            path == "/tmp/anycompute" or path.startswith("/tmp/anycompute/") for path in argv[2:]
        )
        step = self._call(
            "exec", timeout_s, script_op="mkdir" if mkdir else "exec", sandbox_id=sandbox_id, argv=argv, cwd=cwd,
        )
        if files := step.get("files"):
            with self._lock:
                if sandbox_id in self._files:
                    self._files[sandbox_id].update(_file_payloads(cwd, files))
        return RawExecResult(_output(step.get("stdout", "")), _output(step.get("stderr", "")), step.get("exit_code", 0))

    def write(self, sandbox_id, path, data, *, timeout_s):
        self._call("write", timeout_s, sandbox_id=sandbox_id, path=path, data=data)
        with self._lock:
            self._files[sandbox_id][path] = data

    def read(self, sandbox_id, path, *, timeout_s):
        self._call("read", timeout_s, sandbox_id=sandbox_id, path=path)
        with self._lock:
            return self._files.get(sandbox_id, {}).get(path)

    def delete(self, sandbox_id, *, timeout_s):
        self._call("delete", timeout_s, sandbox_id=sandbox_id)
        with self._lock:
            self._inventory.pop(sandbox_id, None)
            self._files.pop(sandbox_id, None)
            self._states.pop(sandbox_id, None)

    def state(self, sandbox_id, *, timeout_s):
        step = self._call("state", timeout_s, sandbox_id=sandbox_id)
        if "state" in step:
            return SandboxState(step["state"])
        with self._lock:
            if sandbox_id not in self._inventory:
                return SandboxState.GONE
            return self._states.get(sandbox_id, SandboxState.RUNNING)

    def list(self, labels, *, timeout_s):
        deadline = self.clock.now() + timeout_s
        self._call("list", timeout_s, labels=dict(labels))
        with self._lock:
            rows = [
                SandboxInfo(row.sandbox_id, dict(row.labels), row.created_at)
                for row in self._inventory.values()
                if all(row.labels.get(key) == value for key, value in labels.items())
            ]
        if self._page_size is None:
            return rows
        result = []
        for page, start in enumerate(range(0, max(1, len(rows)), self._page_size), 1):
            self._call("list_page", max(0, deadline - self.clock.now()), page=page)
            result.extend(rows[start:start + self._page_size])
        return result

    def native(self, sandbox_id, *, timeout_s):
        self._call("native", timeout_s, sandbox_id=sandbox_id)
        return sandbox_id

    def api_host(self, *, timeout_s):
        self._call("api_host", timeout_s)
        return "https://fake.invalid"

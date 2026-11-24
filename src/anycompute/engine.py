"""Stateless sandbox execution and lifecycle policy."""

from __future__ import annotations

import json
import logging
import math
import os
import posixpath
import re
import secrets
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlsplit

from . import providers
from .contract import (
    AnycomputeError, AuthError, CleanupError, ConfigError, ExecResult, InvalidPathError,
    InvalidTTLError, ProviderError, QuotaError, RateLimitError, RawExecResult, Result, SandboxState,
    SandboxTimeoutError,
)

_CWD = "/tmp/anycompute"
_GRACE_S = 120
_CLEANUP_S = 120
_WORK_SLOTS = threading.BoundedSemaphore(64)
_CLEANUP_SLOTS = threading.BoundedSemaphore(64)
_logger = logging.getLogger("anycompute")
_RESERVED_OPTIONS = frozenset({"timeout", "ttl_minutes", "auto_stop_interval", "auto-stop",
                              "pause-on-timeout", "ephemeral", "ttlSeconds", "tags", "metadata", "labels"})
_CREATE_REFUSALS = (AuthError, QuotaError, ConfigError, InvalidPathError, InvalidTTLError, RateLimitError)


class _Clock:
    now = staticmethod(time.monotonic)
    wall_time = staticmethod(time.time)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def wait(event: threading.Event, timeout_s: float) -> bool:
        return event.wait(timeout_s)


_clock = _Clock()
_run_id: str | None = None
_run_id_lock = threading.Lock()
_pacers: dict[str, _Pacer] = {}
_pacer_lock = threading.Lock()
_env_rate_loaded = False
_env_rate: float | None = None


def _check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", run_id):
        raise ConfigError("Invalid run ID: expected [a-z0-9-]{1,40}")
    return run_id


def _resolve_run_id(run_id: str | None = None) -> str:
    if run_id is not None:
        return _check_run_id(run_id)
    global _run_id
    with _run_id_lock:
        if _run_id is None:
            configured = os.environ.get("ANYCOMPUTE_RUN_ID")
            _run_id = _check_run_id(configured) if configured is not None else secrets.token_hex(6)
        return _run_id


def _validate_path(path: str) -> None:
    if (not isinstance(path, str) or not path or path.startswith("/") or path.endswith("/")
            or "\x00" in path or ".." in path.split("/")):
        raise InvalidPathError("Expected a nonempty relative sandbox file path without '..', NUL or trailing '/'")


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if (not isinstance(argv, Sequence) or isinstance(argv, (str, bytes, bytearray)) or not argv
            or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv) or not argv[0]):
        raise ConfigError("argv must be a nonempty sequence of strings without NUL and start with a command")
    return tuple(argv)


def _file_inputs(files, read):
    if files is not None and not isinstance(files, Mapping):
        raise ConfigError("files must map sandbox paths to bytes")
    files = dict(files or {})
    normalized = set()
    parents = set()
    for path, data in files.items():
        _validate_path(path)
        if not isinstance(data, bytes):
            raise ConfigError("files must map sandbox paths to bytes")
        clean = posixpath.normpath(path)
        if clean == "." or clean in normalized:
            raise InvalidPathError("Uploads must name distinct files")
        normalized.add(clean)
        parent = posixpath.dirname(clean)
        if parent:
            parents.add(parent)
    if any(parent in normalized for path in parents for parent in _ancestors(path)):
        raise InvalidPathError("An upload path is also a parent folder")
    if read is not None and (not isinstance(read, Sequence) or isinstance(read, (str, bytes, bytearray))):
        raise ConfigError("read must be a sequence of sandbox paths")
    read = tuple(read or ())
    for path in read:
        _validate_path(path)
    return files, read, sorted(parents)


def _ancestors(path):
    while path:
        yield path
        path = posixpath.dirname(path)


def _effective_ttl(ctx, ttl, caps):
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
        raise InvalidTTLError(ctx.redact("ttl must be an integer of at least 1 second"))
    unit = caps.ttl_unit_s
    effective = ((ttl + unit - 1) // unit) * unit
    if caps.ttl_max_s is not None and effective > caps.ttl_max_s:
        raise InvalidTTLError(ctx.redact(f"Effective TTL {effective} exceeds provider maximum {caps.ttl_max_s} seconds"))
    return effective


def _config_values(data, fields):
    for part in fields:
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data if isinstance(data, str) and data else None


def _key_values(sources):
    values = {value for name in sources.env if (value := os.environ.get(name))}
    for config in sources.config:
        path = os.environ.get(config.path_env) if config.path_env else None
        try:
            with Path(path or config.path).expanduser().open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, ValueError, RuntimeError):
            continue
        for fields in config.fields:
            if not fields:
                continue
            roots, suffix = (data.values(), fields[1:]) if fields[0] == "*" else ((data,), fields)
            for root in roots:
                if value := _config_values(root, suffix):
                    values.add(value)
    return values


def _key_pattern(values) -> re.Pattern[str] | None:
    variants = {}
    for value in values:
        if value:
            for spelling in (value, repr(value)[1:-1], ascii(value)[1:-1],
                             json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]):
                variants[spelling] = variants.get(spelling, False) or len(value) >= 8
    alternatives = []
    for spelling in sorted(variants, key=lambda spelling: (-len(spelling), spelling)):
        pattern = re.escape(spelling)
        if not variants[spelling]:
            pattern = rf"(?<![A-Za-z0-9_-]){pattern}(?![A-Za-z0-9_-])"
        alternatives.append(pattern)
    return re.compile("|".join(alternatives)) if alternatives else None


def _redact_text(text: str, provider: str | None = None) -> str:
    values = {value for spec in providers.PROVIDERS if provider is None or spec.name == provider
              for value in _key_values(spec.caps.key_sources)}
    pattern = _key_pattern(values)
    return pattern.sub("***", text) if pattern else text


def _option_secrets(options):
    if isinstance(options, Mapping):
        for table in options.values():
            if isinstance(table, Mapping) and isinstance(environment := table.get("environment"), Mapping):
                yield from (value for value in environment.values() if isinstance(value, str) and len(value) >= 8)


def _provider_context(provider, run_id, options=None):
    keys = {spec.name: _key_values(spec.caps.key_sources) for spec in providers.PROVIDERS}
    pattern = _key_pattern({value for values in keys.values() for value in values} | set(_option_secrets(options)))
    return _Context(provider, run_id, mask=pattern), keys


def _get_spec(ctx, provider):
    if not isinstance(provider, str):
        raise ConfigError("Expected a provider name", run_id=ctx.run_id)
    try:
        return providers.get_provider(provider)
    except ConfigError:
        pass
    raise ConfigError(f"Unknown provider: {ctx.redact(provider)!r}", **ctx.error_context())


def _checked_options(ctx, options):
    active = {}
    if options is None:
        return active
    if not isinstance(options, Mapping):
        raise ConfigError("provider_options must map provider names to option mappings")
    for provider, entry in options.items():
        spec = _get_spec(ctx, provider)
        if not isinstance(entry, Mapping):
            raise ConfigError(ctx.redact(f"Options for {provider} must be a mapping"))
        entry = dict(entry)
        for key, value in entry.items():
            if not isinstance(key, str):
                raise ConfigError("provider_options keys must be strings")
            if key not in spec.caps.provider_options_allowlist or key in _RESERVED_OPTIONS:
                raise ConfigError(f"Unlisted provider option {ctx.redact(key)!r} for {ctx.redact(provider)}")
            if key == "environment":
                if not isinstance(value, Mapping):
                    raise ConfigError("environment must map variable names to strings")
                for name, secret in value.items():
                    if not isinstance(name, str) or not isinstance(secret, str):
                        raise ConfigError(ctx.redact(f"Expected a string value for environment key {name!r}"))
                entry[key] = dict(value)
        if provider == ctx.provider:
            active = entry
    return active


def _require_auth(ctx, spec, keys):
    sources = spec.caps.key_sources
    if (sources.env or sources.config) and not keys[spec.name]:
        locations = (*sources.env, *(config.path_env or config.path for config in sources.config))
        raise AuthError(ctx.redact(f"Missing {spec.name} credentials: set one of {', '.join(locations)}"),
                        run_id=ctx.run_id)


class _ErrorContext(TypedDict):
    run_id: str | None
    sandbox_id: str | None


@dataclass
class _Context:
    provider: str
    run_id: str | None
    sandbox_id: str | None = None
    clock: _Clock = field(default_factory=lambda: _clock)
    mask: re.Pattern[str] | None = field(default=None, repr=False)

    def redact(self, text: str) -> str:
        return self.mask.sub("***", text) if self.mask else text

    def log(self, step, duration):
        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(self.redact(
                f"{step} provider={self.provider} run_id={self.run_id} sandbox_id={self.sandbox_id} duration={duration:.3f}s"
            ))

    def error_context(self) -> _ErrorContext:
        return {"run_id": self.redact(self.run_id) if self.run_id else None,
                "sandbox_id": self.redact(self.sandbox_id) if self.sandbox_id else None}


@dataclass
class _Pacer:
    rate: float | None
    override: float | None
    next_slot: float = -math.inf
    last_start: float = -math.inf
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, ctx: _Context) -> None:
        with self.lock:
            if self.rate is None:
                return
            start = ctx.clock.now()
            interval = 60 / self.rate
            slot = max(self.next_slot, self.last_start + interval, start)
            self.next_slot = slot + interval
        if slot > start:
            ctx.clock.sleep(slot - start)
            ctx.log("pace wait", ctx.clock.now() - start)


def _checked_rate(value, name, *, environment=False) -> float:
    try:
        if environment or isinstance(value, (float, int)) and not isinstance(value, bool):
            rate = float(value)
            if math.isfinite(rate) and rate > 0 and math.isfinite(60 / rate):
                return rate
    except (ValueError, OverflowError):
        pass
    raise ConfigError(f"{name} must be a positive finite number")


def _pacer_for(spec, override) -> _Pacer:
    global _env_rate_loaded, _env_rate
    if override is not None:
        override = _checked_rate(override, "creates_per_minute")
    with _pacer_lock:
        if not _env_rate_loaded:
            name = "ANYCOMPUTE_CREATES_PER_MINUTE"
            value = os.environ.get(name)
            _env_rate = _checked_rate(value, name, environment=True) if value is not None else None
            _env_rate_loaded = True
        if (pacer := _pacers.get(spec.name)) is None:
            cap = spec.caps.creates_per_minute
            pacer = _pacers[spec.name] = _Pacer(override or _env_rate or (cap.value if cap else None), override)
        elif override is not None:
            with pacer.lock:
                if pacer.override is not None and pacer.override != override:
                    raise ConfigError("creates_per_minute differs from the earlier value for this provider")
                pacer.rate = pacer.override = override
    return pacer


def _safe_error(ctx, operation, error):
    try:
        message = ctx.redact(str(error))
    except Exception:
        message = ctx.redact("Provider error message unavailable")
    if isinstance(error, KeyboardInterrupt):
        return KeyboardInterrupt(message) if message else KeyboardInterrupt()
    context = ctx.error_context()
    if isinstance(error, ProviderError):
        return ProviderError(message, internal=error.internal is True, interrupted=error.interrupted is True, **context)
    if isinstance(error, RateLimitError):
        retry = getattr(error, "retry_after", None)
        return RateLimitError(message, retry_after=retry if type(retry) in (int, float) and 0 <= retry < math.inf else None,
                              **context)
    if isinstance(error, SandboxTimeoutError):
        return SandboxTimeoutError(message, stdout=ctx.redact(_decode(getattr(error, "stdout", None))),
                                   stderr=ctx.redact(_decode(getattr(error, "stderr", None))), **context)
    if type(error) in _CREATE_REFUSALS:
        return type(error)(message, **context)
    return ProviderError(ctx.redact(f"{operation}: {type(error).__name__}: {message}"), internal=True, **context)


class _CallTimeout(TimeoutError):
    pass


class _PaceUntil(Exception):
    pass


@dataclass
class _CreateAttempt:
    pacer: _Pacer
    reconcile: Callable[[str], None]
    requested_at: float | None = None


def _valid_sandbox_id(value):
    return type(value) is str and bool(value.strip()) and "\x00" not in value


def _valid_reply(operation, value):
    if operation == "exec":
        return (type(value) is RawExecResult and type(value.stdout) in (str, bytes)
                and type(value.stderr) in (str, bytes) and (value.exit_code is None or type(value.exit_code) is int))
    if operation in ("state", "confirm"):
        return type(value) is SandboxState
    if operation in ("prepare", "write", "delete"):
        return value is None
    if operation == "read":
        return value is None or type(value) is bytes
    if operation == "api_host":
        return value is None or type(value) is str
    return True


def _waited_call(ctx, operation, call, deadline, *, create=None):
    start = ctx.clock.now()
    if start >= deadline:
        raise _CallTimeout(f"{operation} has no client budget left")
    slots = _CLEANUP_SLOTS if operation in ("delete", "confirm") else _WORK_SLOTS
    done = threading.Event()
    retired = threading.Event()
    outcome = []
    lock = threading.Lock()
    started = accepted = False
    pacer = create.pacer if create is not None else None

    def failure(message, *, sandbox_may_exist=False):
        return ProviderError(ctx.redact(f"{operation}: {message}"), internal=True,
                             sandbox_may_exist=sandbox_may_exist, **ctx.error_context())

    def worker():
        try:
            with (pacer.lock if pacer is not None else nullcontext()), lock:
                if not started or retired.is_set() or (now := ctx.clock.now()) >= deadline:
                    return
                if create is not None:
                    # A delayed worker may reach dispatch after a later reservation.
                    if pacer.rate is not None and (due := pacer.last_start + 60 / pacer.rate) > now:
                        outcome.append((now, (False, _PaceUntil(due)), None))
                        return
                    create.requested_at = pacer.last_start = now
            raw_id = None
            try:
                value = call(timeout_s=deadline - now)
                if create is not None:
                    if not _valid_sandbox_id(value):
                        raise failure("invalid sandbox ID")
                    raw_id, value = value, None
                elif not _valid_reply(operation, value):
                    raise failure("invalid provider reply")
                result = True, value
            except BaseException as error:
                try:
                    reported_id = getattr(error, "sandbox_id", None) if create and isinstance(error, AnycomputeError) else None
                    if _valid_sandbox_id(reported_id):
                        raw_id = reported_id
                    safe = _safe_error(ctx, operation, error)
                    if create and raw_id is None and not isinstance(safe, KeyboardInterrupt):
                        if isinstance(safe, ProviderError):
                            safe.sandbox_may_exist = True
                        elif reported_id is not None or type(safe) not in _CREATE_REFUSALS:
                            safe = ProviderError(str(safe), sandbox_may_exist=True, **ctx.error_context())
                    result = False, safe
                except BaseException:
                    result = False, failure("invalid provider error", sandbox_may_exist=create is not None and raw_id is None)
            outcome.append((ctx.clock.now(), result, raw_id))
            if raw_id is not None:
                # Keep reconciliation on this bounded worker, including a reply already pending at timeout.
                done.set()
                retired.wait()
                if ctx.sandbox_id is None:
                    create.reconcile(raw_id)
        finally:
            if started and not accepted:
                slots.release()
            done.set()

    if operation == "exec":
        ctx.log("exec start", 0)
    try:
        try:
            # Dispatch cannot win a failed Thread.start, even if it already spawned a thread.
            with lock:
                if not slots.acquire(blocking=False):
                    raise failure("worker capacity exhausted")
                try:
                    threading.Thread(target=worker, daemon=True).start()
                    started = True
                except Exception:
                    pass
                finally:
                    if not started:
                        slots.release()
            if not started:
                raise failure("could not start worker")
            if (not ctx.clock.wait(done, max(0, deadline - ctx.clock.now()))
                    or not outcome or outcome[0][0] >= deadline):
                raise _CallTimeout(f"{operation} exceeded its client budget")
            _, (success, value), raw_id = outcome[0]
            if raw_id is not None:
                try:
                    ctx.sandbox_id = raw_id
                    accepted = True
                finally:
                    if accepted:
                        slots.release()
        finally:
            with lock:
                retired.set()
    except (KeyboardInterrupt, _CallTimeout):
        ctx.log(f"abandoned {operation}", ctx.clock.now() - start)
        raise
    if not isinstance(value, _PaceUntil):
        ctx.log("exec end" if operation == "exec" else operation, ctx.clock.now() - start)
    if not success:
        raise value
    return value


def _decode(value) -> str:
    if type(value) is bytes:
        return value.decode("utf-8", errors="replace")
    return value if type(value) is str else ""


def _cleanup(ctx, client):
    start = ctx.clock.now()
    deadline = start + _CLEANUP_S

    def poll(operation, method):
        delay = 1
        immediate_retry = True
        last = f"{operation} ran out of time"
        while deadline > ctx.clock.now():
            wait = delay
            try:
                response = _waited_call(ctx, operation, partial(method, ctx.sandbox_id), deadline)
                if operation == "delete" or response == SandboxState.GONE:
                    return None
                last = "sandbox state was not gone"
            except _CallTimeout as error:
                return str(error)
            except AnycomputeError as error:
                last = str(error) or type(error).__name__
                if isinstance(error, RateLimitError) and (retry := error.retry_after) is not None:
                    if retry > 0 or immediate_retry:
                        wait = retry
                        if retry == 0:
                            immediate_retry = False
            ctx.clock.sleep(min(wait, max(0, deadline - ctx.clock.now())))
            delay = min(2 * delay, 10)
        return last

    if last := poll("delete", client.delete) or poll("confirm", client.state):
        ctx.log(f"cleanup error: {last}", ctx.clock.now() - start)
        raise CleanupError(ctx.redact(f"Could not confirm sandbox deletion: {last}"), **ctx.error_context())


def _late_cleanup(ctx, client, sandbox_id):
    late = replace(ctx, sandbox_id=sandbox_id)
    start = late.clock.now()
    late.log("late create", 0)
    try:
        _cleanup(late, client)
    except CleanupError:
        pass
    except KeyboardInterrupt:
        late.log("late cleanup interrupted", late.clock.now() - start)


def _bounded_call(ctx, operation, call, budget):
    try:
        return _waited_call(ctx, operation, call, ctx.clock.now() + budget)
    except _CallTimeout as error:
        message = ctx.redact(f"{ctx.provider}: {error}")
    raise ProviderError(message, **ctx.error_context())


def _build_client(ctx, spec):
    try:
        return spec.factory()
    except Exception as error:
        mapped = _safe_error(ctx, "provider factory", error)
    raise mapped


class Sandbox:
    """A provider sandbox, created here and deleted on close or context exit."""

    def __init__(self, provider: str, ttl: int = 300,
                 provider_options: Mapping[str, Mapping[str, object]] | None = None,
                 creates_per_minute: float | None = None, run_id: str | None = None) -> None:
        try:
            self._open(provider, ttl, provider_options, creates_per_minute, run_id, ())
        except BaseException as error:
            self._close_if_created(error)
            raise

    def _open(self, provider, ttl, provider_options, creates_per_minute, run_id, parents):
        self.sandbox_seconds = None
        start = _clock.now()
        ctx, keys = _provider_context(provider, _check_run_id(run_id) if run_id is not None else None, provider_options)
        self._ctx = ctx
        spec = _get_spec(ctx, provider)
        self._effective_ttl = _effective_ttl(ctx, ttl, spec.caps)
        options = _checked_options(ctx, provider_options)
        _require_auth(ctx, spec, keys)
        pacer = _pacer_for(spec, creates_per_minute)
        ctx.log("validate", ctx.clock.now() - start)
        self._client = client = _build_client(ctx, spec)
        self.outside_contract = bool(options)
        self._closed = False
        self._cleanup_failure = None
        budget = self._effective_ttl + _GRACE_S
        _bounded_call(ctx, "prepare", partial(client.prepare, options=options), budget)
        pacer.wait(ctx)
        ctx.run_id = self.run_id = _resolve_run_id(run_id)
        labels = {"anycompute": "1", "anycompute-run": self.run_id, "anycompute-ttl": str(self._effective_ttl)}
        self._create(labels, options, budget, pacer)
        # On a declared pre-start window, only the reply tells us that the TTL is running.
        anchor = ctx.clock.now() if spec.caps.pre_start_window == "partial" else self._created_request
        self._work_deadline = anchor + budget
        self._mkdir([_CWD, *[f"{_CWD}/{path}" for path in parents]], cwd="/")

    def _create(self, labels, options, budget, pacer):
        ctx, client = self._ctx, self._client
        create = partial(client.create, ttl=self._effective_ttl, labels=labels, options=options)
        attempt = _CreateAttempt(pacer, partial(_late_cleanup, ctx, client))
        try:
            while True:
                start = ctx.clock.now()
                try:
                    _waited_call(ctx, "create", create, start + budget, create=attempt)
                    return
                except _PaceUntil as wait:
                    now = ctx.clock.now()
                    if wait.args[0] > now:
                        ctx.clock.sleep(wait.args[0] - now)
                        ctx.log("pace wait", ctx.clock.now() - now)
        except _CallTimeout as error:
            lost = ProviderError(ctx.redact(str(error)), sandbox_may_exist=attempt.requested_at is not None,
                                 **ctx.error_context())
        except KeyboardInterrupt:
            if attempt.requested_at is None or ctx.sandbox_id is not None:
                raise
            lost = ProviderError(ctx.redact("Interrupted while creating sandbox"), sandbox_may_exist=True,
                                 interrupted=True, **ctx.error_context())
        finally:
            self._created_request = attempt.requested_at
        raise lost

    @property
    def sandbox_id(self) -> str:
        sandbox_id = self._ctx.sandbox_id
        assert sandbox_id is not None
        return self._ctx.redact(sandbox_id)

    @property
    def native(self) -> object:
        with self._active():
            self.outside_contract = True
            return self._work("native")

    def _attach_context(self, error):
        for name, value in self._ctx.error_context().items():
            setattr(error, name, value)
        error.sandbox_seconds = self.sandbox_seconds

    @contextmanager
    def _active(self):
        try:
            if self._closed:
                raise ConfigError("Sandbox is closed")
            yield
        except (AnycomputeError, KeyboardInterrupt) as error:
            # The waited-call helper attaches an ID to provider failures, unlike local validation.
            if isinstance(error, (ConfigError, InvalidPathError, InvalidTTLError)) and error.sandbox_id is None:
                self._attach_context(error)
            else:
                self._close_after(error)
            raise

    def _work(self, operation, *args, **kwargs) -> Any:
        method = getattr(self._client, operation)
        call = method if operation == "api_host" else partial(method, self._ctx.sandbox_id, *args, **kwargs)
        try:
            return _waited_call(self._ctx, operation, call, self._work_deadline)
        except _CallTimeout:
            pass
        self._check_expiry(operation)

    def _check_expiry(self, operation, result=None):
        ctx = self._ctx
        sandbox_id = ctx.sandbox_id
        assert sandbox_id is not None
        start = ctx.clock.now()
        deadline = start + _CLEANUP_S
        inspect = partial(self._client.state, sandbox_id)
        try:
            state = _waited_call(ctx, "state", inspect, deadline)
            if (state == SandboxState.RUNNING and ctx.provider == "docker" and result is not None
                    and self._created_request is not None and start >= self._created_request + self._effective_ttl):
                # Docker can report the exec exit before it updates the container state.
                ctx.clock.sleep(min(1, max(0, deadline - ctx.clock.now())))
                state = _waited_call(ctx, "state", inspect, deadline)
            elif (ctx.provider == "daytona" and operation == "exec" and result is not None and result.exit_code is None
                  and self._created_request is not None and start >= self._created_request + self._effective_ttl):
                # Daytona can interrupt a session while its control plane is still deleting the sandbox.
                expiry_deadline = min(deadline, self._work_deadline)
                delay = 1
                while state in (SandboxState.RUNNING, SandboxState.UNKNOWN) and ctx.clock.now() + delay < expiry_deadline:
                    ctx.clock.sleep(delay)
                    state = _waited_call(ctx, "state", inspect, expiry_deadline)
                    delay = min(2 * delay, 10)
        except _CallTimeout as error:
            raise ProviderError(ctx.redact(f"{ctx.provider}: {error}"), **ctx.error_context()) from None
        if state in (SandboxState.GONE, SandboxState.STOPPED):
            raise SandboxTimeoutError(f"Sandbox ended during {operation}", **ctx.error_context(),
                                      stdout=ctx.redact(_decode(result.stdout)) if result else "",
                                      stderr=ctx.redact(_decode(result.stderr)) if result else "")
        if state != SandboxState.RUNNING or result is None or result.exit_code is None:
            raise ProviderError(ctx.redact(f"{operation} did not complete (sandbox state: {state})"), **ctx.error_context())

    def _mkdir(self, dirs, *, cwd):
        made = self._work("exec", ("mkdir", "-p", *dirs), cwd=cwd)
        if made.exit_code != 0:
            raise ProviderError(self._ctx.redact(f"Could not create sandbox working directory: {_decode(made.stderr)}"))

    def exec(self, argv: Sequence[str]) -> ExecResult:
        with self._active():
            result = self._work("exec", _validate_argv(argv), cwd=_CWD)
            if result.exit_code is None or result.exit_code == 137:
                self._check_expiry("exec", result)
            assert result.exit_code is not None
            return ExecResult(self._ctx.redact(_decode(result.stdout)), self._ctx.redact(_decode(result.stderr)), result.exit_code)

    def write(self, path: str, data: bytes) -> None:
        with self._active():
            _validate_path(path)
            if not isinstance(data, bytes):
                raise ConfigError("Sandbox.write data must be bytes")
            parent = posixpath.dirname(posixpath.normpath(path))
            if parent:
                self._mkdir([f"{_CWD}/{parent}"], cwd=_CWD)
            self._write(path, data)

    def _write(self, path, data):
        self._work("write", f"{_CWD}/{posixpath.normpath(path)}", data)

    def read(self, path: str) -> bytes | None:
        with self._active():
            _validate_path(path)
            return self._work("read", f"{_CWD}/{posixpath.normpath(path)}")

    def _runtime(self) -> dict[str, str]:
        python = self.exec(("python3", "--version"))
        arch = self.exec(("uname", "-m"))
        host = _runtime_host(self._work("api_host"))
        version = (python.stdout or python.stderr).strip().removeprefix("Python ") if python.exit_code == 0 else ""
        return {"python": version or "unknown", "arch": (arch.stdout.strip() if arch.exit_code == 0 else "") or "unknown",
                "api_host": self._ctx.redact(host)}

    def close(self) -> None:
        self._close_after(None)

    def _close_if_created(self, error):
        if (ctx := getattr(self, "_ctx", None)) is not None:
            if ctx.sandbox_id is not None:
                self._close_after(error)
            elif isinstance(error, KeyboardInterrupt):
                self._attach_context(error)

    def _close_after(self, original, *, result=None):
        interrupt = None
        if not self._closed:
            try:
                self._closed = True
                _cleanup(self._ctx, self._client)
                self.sandbox_seconds = self._ctx.clock.now() - self._created_request
            except CleanupError as failure:
                failure.__context__ = failure.__traceback__ = None
                self._cleanup_failure = failure
            except KeyboardInterrupt as error:
                interrupt = error
                ctx = self._ctx
                self._cleanup_failure = CleanupError(ctx.redact("Interrupted before sandbox deletion was confirmed"),
                                                     result=result, **ctx.error_context())
        primary = interrupt if interrupt is not None else original
        if primary is not None:
            self._attach_context(primary)
        if failure := self._cleanup_failure:
            if primary is None:
                if result is not None:
                    failure.result = result
                raise failure.with_traceback(None)
            if primary is not failure and getattr(primary, "cleanup_error", None) is not failure:
                primary.cleanup_error = failure
                primary.add_note(str(failure))
        if interrupt is not None:
            raise interrupt

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._close_after(exc)


def run(argv: Sequence[str], *, provider: str, ttl: int = 300,
        files: Mapping[str, bytes] | None = None, read: Sequence[str] | None = None,
        runtime: bool = False, provider_options: Mapping[str, Mapping[str, object]] | None = None,
        creates_per_minute: float | None = None, run_id: str | None = None) -> Result:
    """Run one command and confirm deletion before returning its result."""
    argv = _validate_argv(argv)
    files, read, parents = _file_inputs(files, read)
    if not isinstance(runtime, bool):
        raise ConfigError("runtime must be a bool")
    sb = Sandbox.__new__(Sandbox)
    try:
        sb._open(provider, ttl, provider_options, creates_per_minute, run_id, parents)
        for path, data in files.items():
            sb._write(path, data)
        output = sb.exec(argv)
        runtime_info = sb._runtime() if runtime else None
        readback, missing = {}, []
        for path in read:
            data = sb.read(path)
            if data is None:
                missing.append(path)
            else:
                readback[path] = data
        result = Result(output.stdout, output.stderr, output.exit_code, readback, missing, provider,
                        sb.sandbox_id, sb._ctx.redact(sb.run_id), sb._effective_ttl, runtime_info,
                        None, sb.outside_contract)
        sb._close_after(None, result=result)
    except BaseException as error:
        sb._close_if_created(error)
        raise
    return replace(result, sandbox_seconds=sb.sandbox_seconds)


def _runtime_host(url: str | None) -> str:
    if url == "local":
        return url
    if url:
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "unix":
                return "local"
            if parsed.scheme and (host := parsed.hostname):
                host = f"[{host}]" if ":" in host else host
                return f"{parsed.scheme}://{host}"
        except ValueError:
            pass
    return "unknown"


def _label_ttl(label):
    if isinstance(label, str) and re.fullmatch(r"[0-9]+", label):
        try:
            return int(label) or None
        except ValueError:
            pass
    return None


def _gc(provider: str, *, run_id: str | None = None, all_expired: bool = False,
        dry_run: bool = False, force: bool = False) -> tuple[list[dict], AnycomputeError | None]:
    if any(not isinstance(flag, bool) for flag in (all_expired, dry_run, force)) or (run_id is not None) == all_expired:
        raise ConfigError("gc requires exactly one of --run and --all")
    if run_id is not None:
        _check_run_id(run_id)
    ctx, keys = _provider_context(provider, run_id)
    spec = _get_spec(ctx, provider)
    _require_auth(ctx, spec, keys)
    client = _build_client(ctx, spec)
    labels = {"anycompute": "1"}
    if run_id is not None:
        labels["anycompute-run"] = run_id
    inventory = _bounded_call(ctx, "list", partial(client.list, labels), _CLEANUP_S)
    now = ctx.clock.wall_time()
    rows = []
    first_error = None
    for item in inventory:
        if any(item.labels.get(key) != value for key, value in labels.items()):
            continue
        sandbox = replace(ctx, sandbox_id=item.sandbox_id)
        ttl = _label_ttl(item.labels.get("anycompute-ttl"))
        timestamp = item.created_at
        created_at = None
        try:
            if type(timestamp) in (int, float) and -math.inf < timestamp < math.inf:
                created_at = datetime.fromtimestamp(timestamp, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
        if force:
            reason = "force"
        elif run_id is not None:
            reason = "run_match"
        elif ttl is None:
            reason = "no_ttl_label"
        elif created_at is None:
            reason = "no_create_time"
        elif now - timestamp <= ttl + _GRACE_S:
            reason = "young"
        else:
            try:
                state = _bounded_call(sandbox, "state", partial(client.state, item.sandbox_id), _CLEANUP_S)
            except AnycomputeError as error:
                first_error = first_error or error
                reason = "state_error"
            else:
                reason = "expired" if state in (SandboxState.GONE, SandboxState.STOPPED) else "running"
        action = "kept"
        if reason in ("run_match", "expired", "force"):
            action = "listed" if dry_run else "deleted"
            if not dry_run:
                try:
                    _cleanup(sandbox, client)
                except CleanupError as error:
                    if not isinstance(first_error, CleanupError):
                        first_error = error
                    action = "failed"
        label_run_id = item.labels.get("anycompute-run")
        rows.append({"sandbox_id": ctx.redact(item.sandbox_id),
                     "run_id": ctx.redact(label_run_id) if label_run_id else None,
                     "created_at": created_at, "action": action, "reason": reason})
    return rows, first_error

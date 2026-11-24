"""Boat sandbox adapter using the synchronous public SDK."""

import base64
import json
import math
import os
import shlex
import time
from collections.abc import Mapping, Sequence
from importlib.metadata import version

from ..contract import (
    AuthError, ConfigError, KeySources, ProviderCaps, ProviderError, ProviderSpec, QuotaError,
    RateLimitError, RawExecResult, SandboxInfo, SandboxState, StartRate,
)

_API = "https://boat.dev/api/v1"
_RUNNING = frozenset({"ready", "idle", "running"})
_QUOTA_CODES = frozenset({"limit_reached", "member_limit_reached", "trial_compute_limit_reached",
                          "trial_machine_class_not_allowed", "machine_class_plan_required", "org_suspended"})
_EXEC_ENDED_CODES = frozenset({"machine_not_running", "sandbox_not_ready", "sandbox_stopping",
                               "sandbox_direct_failed", "boat_direct_failed"})


def _sdk():
    try:
        import boat_sdk
        from boat_sdk.api.boat_api import BoatApi
        from urllib3.exceptions import HTTPError
    except ImportError:
        pass
    else:
        return boat_sdk, BoatApi, HTTPError
    raise ConfigError("Install the boat extra: anycompute[boat]")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("Boat request budget elapsed")
    return remaining


def _retry_after(headers) -> float | None:
    try:
        seconds = float((headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _error_code(error) -> str | None:
    try:
        body = json.loads(error.body or "{}")
    except (TypeError, ValueError):
        return None
    if isinstance(body, dict) and isinstance(code := body.get("code"), str):
        return code
    return None


class _Boat:
    def __init__(self) -> None:
        self._deletions: dict[str, str | None] = {}

    def _call(self, operation, *args, timeout_s: float, missing_ok: bool = False, **kwargs):
        sdk, api, http_error = _sdk()
        token = os.getenv("BOAT_API_KEY")
        if not token:
            raise AuthError("Missing Boat credentials: set BOAT_API_KEY")
        config = sdk.Configuration(host=_API, access_token=token, retries=False)
        client = sdk.ApiClient(config)
        client.user_agent = f"anycompute/{version('anycompute')}"
        try:
            return getattr(api(client), operation)(*args, _request_timeout=timeout_s, **kwargs)
        except sdk.exceptions.ApiException as error:
            status = error.status
            code = _error_code(error)
            if status == 404 and (missing_ok or operation == "command"):
                return None
            if operation == "command" and status in (400, 409, 502) and code in _EXEC_ENDED_CODES:
                return None  # The engine checks the current sandbox state.
            message = f"Boat {operation} failed (HTTP {status})" if type(status) is int else f"Boat {operation} failed"
            if status == 402 or (status in (403, 429) and code in _QUOTA_CODES):
                failure = QuotaError(message)
            elif status in (401, 403):
                failure = AuthError(message)
            elif status == 429:
                failure = RateLimitError(message, retry_after=_retry_after(error.headers))
            else:
                failure = ProviderError(message, sandbox_may_exist=operation == "create")
        except (OSError, http_error, ValueError):
            failure = ProviderError(f"Boat {operation} failed", sandbox_may_exist=operation == "create")
        finally:
            client.rest_client.pool_manager.clear()
        raise failure

    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None:
        _sdk()

    def create(self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float) -> str:
        sdk, _, _ = _sdk()
        request = sdk.CreateSandboxRequest(ttl_seconds=ttl, labels=dict(labels), no_env=True,
                                           monitoring=False, visibility="private", env=options.get("environment"))
        response = self._call("create", create_sandbox_request=request, timeout_s=timeout_s)
        if response is None or not response.ok:
            raise ProviderError("Boat create returned an invalid reply", sandbox_may_exist=True)
        sandbox_id = response.sandbox.id
        if response.ttl_seconds != ttl:
            raise ProviderError("Boat did not accept the requested TTL", sandbox_id=sandbox_id)
        return sandbox_id

    def exec(self, sandbox_id: str, argv: Sequence[str], *, cwd: str, timeout_s: float) -> RawExecResult:
        sdk, _, _ = _sdk()
        command = f"cd {shlex.quote(cwd)} && exec {shlex.join(argv)}"
        request = sdk.CommandRequest(command=command, timeout_seconds=min(600, math.ceil(timeout_s)))
        response = self._call("command", sandbox_id, request, timeout_s=timeout_s)
        if response is None:
            return RawExecResult("", "", None)
        result = response.actual_instance
        if not isinstance(result, sdk.CommandResponse) or not result.ok:
            raise ProviderError("Boat command returned an invalid reply")
        return RawExecResult(result.stdout, result.stderr, None if result.timed_out else result.exit_code)

    def write(self, sandbox_id: str, path: str, data: bytes, *, timeout_s: float) -> None:
        sdk, _, _ = _sdk()
        request = sdk.FileWriteRequest(path=path, content=base64.b64encode(data).decode("ascii"), encoding="base64")
        result = self._call("write_file", sandbox_id, request, timeout_s=timeout_s)
        if result is None or not result.ok or not result.success or result.size != len(data):
            raise ProviderError("Boat did not confirm the file write")

    def read(self, sandbox_id: str, path: str, *, timeout_s: float) -> bytes | None:
        deadline = time.monotonic() + timeout_s
        # Boat's file metadata cannot distinguish regular files from special files.
        probe = self.exec(sandbox_id, ("sh", "-c", 'test -f "$1" && test ! -L "$1"', "anycompute", path),
                          cwd="/", timeout_s=_remaining(deadline))
        if probe.exit_code == 1:
            return None
        if probe.exit_code != 0:
            raise ProviderError("Boat could not identify the file type")
        result = self._call("read_file", sandbox_id, path, encoding="base64", timeout_s=_remaining(deadline),
                            missing_ok=True)
        if result is None:
            return None
        if not result.ok or not result.success or result.encoding != "base64":
            raise ProviderError("Boat returned an invalid file reply")
        try:
            data = base64.b64decode(result.content, validate=True)
        except ValueError:
            pass
        else:
            if len(data) == result.size:
                return data
        raise ProviderError("Boat returned invalid binary file content")

    def delete(self, sandbox_id: str, *, timeout_s: float) -> None:
        try:
            result = self._call("delete_sandbox", sandbox_id=sandbox_id, x_ascii_confirm_delete=sandbox_id,
                                timeout_s=timeout_s, missing_ok=True)
            if result is None:
                return
            operation = getattr(result, "operation", None)
            if not result.ok or operation is None or operation.kind != "sandbox" or (
                operation.target_id != sandbox_id or not operation.id
            ):
                raise ProviderError("Boat returned an invalid deletion receipt")
        except ProviderError:
            self._deletions.setdefault(sandbox_id, None)
            raise
        self._deletions[sandbox_id] = operation.id

    def _get(self, sandbox_id: str, timeout_s: float):
        result = self._call("get", sandbox_id, timeout_s=timeout_s, missing_ok=True)
        if result is None:
            return None
        if not result.ok or result.sandbox.id != sandbox_id:
            raise ProviderError("Boat returned an invalid sandbox reply")
        return result.sandbox

    def state(self, sandbox_id: str, *, timeout_s: float) -> SandboxState:
        if sandbox_id in self._deletions:
            operation_id = self._deletions[sandbox_id]
            if operation_id is None:
                return SandboxState.UNKNOWN  # A lost receipt leaves the purge unconfirmed even after a 404.
            result = self._call("get_deletion_operation", operation_id, timeout_s=timeout_s)
            if result is None or not result.ok:
                raise ProviderError("Boat returned an invalid deletion status")
            operation = result.operation
            if operation.kind != "sandbox" or operation.id != operation_id or operation.target_id != sandbox_id:
                raise ProviderError("Boat returned an invalid deletion status")
            if operation.status == "completed":
                self._deletions.pop(sandbox_id, None)
                return SandboxState.GONE
            return SandboxState.UNKNOWN
        sandbox = self._get(sandbox_id, timeout_s)
        if sandbox is None:
            return SandboxState.GONE
        if sandbox.state in _RUNNING:
            return SandboxState.RUNNING
        if sandbox.state == "archived":
            return SandboxState.STOPPED
        return SandboxState.UNKNOWN

    def list(self, labels: Mapping[str, str], *, timeout_s: float) -> Sequence[SandboxInfo]:
        deadline = time.monotonic() + timeout_s
        cursor = None
        seen = set()
        sandboxes = []
        filters = [f"{key}={value}" for key, value in labels.items()]
        while True:
            result = self._call("sandboxes", cursor=cursor, label=filters or None, limit=200,
                                timeout_s=_remaining(deadline))
            if result is None or not result.ok or result.page_info is None:
                raise ProviderError("Boat returned an incomplete sandbox list")
            for sandbox in result.sandboxes:
                actual = sandbox.labels or {}
                if all(actual.get(key) == value for key, value in labels.items()):
                    created = sandbox.created_at
                    stamp = created.timestamp() if created is not None and created.tzinfo is not None else None
                    sandboxes.append(SandboxInfo(sandbox.id, actual, stamp))
            cursor = result.page_info.next_cursor
            if not result.page_info.has_more and cursor is None:
                return sandboxes
            if not result.page_info.has_more or not cursor or cursor in seen:
                raise ProviderError("Boat returned invalid sandbox pagination")
            seen.add(cursor)

    def native(self, sandbox_id: str, *, timeout_s: float) -> object:
        sandbox = self._get(sandbox_id, timeout_s)
        if sandbox is None:
            raise ProviderError("Boat sandbox is gone")
        return sandbox

    def api_host(self, *, timeout_s: float) -> str:
        return _API


SPEC = ProviderSpec(
    "boat",
    ProviderCaps(
        ttl_max_s=480, lifetime="partial", sdk_package="boat-sdk", isolation="Virtual machine",
        key_sources=KeySources(env=("BOAT_API_KEY",)), provider_options_allowlist=frozenset({"environment"}),
        creates_per_minute=StartRate(12, "https://docs.boat.dev/api/v1#limits-and-retries", "2026-11-24"),
    ),
    _Boat,
)

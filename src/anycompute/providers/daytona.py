"""Daytona v0 container adapter. Import the optional SDK on the first operation."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from math import ceil, isfinite
from os import getenv
from secrets import token_hex
from shlex import join, quote
from threading import Lock
from uuid import UUID

from ..contract import (
    AuthError, ConfigError, KeySources, ProviderCaps, ProviderError, ProviderSpec, QuotaError, RateLimitError,
    RawExecResult, SandboxInfo, SandboxState,
)


_ROOT_LOCK = Lock()
_roots = {}
_project_pool = None
_PROJECT_SNAPSHOT = "daytona-small"
_PROJECT_TARGET = "us"


def _sdk():
    try:
        import daytona
        from daytona import v0
        from daytona.v0 import CreateSandboxFromSnapshotParams
    except ImportError:
        pass
    else:
        return daytona, v0, CreateSandboxFromSnapshotParams
    raise ConfigError("Install the Daytona extra: anycompute[daytona]")


def _project_transport(client, api_url):
    from urllib3 import PoolManager

    global _project_pool
    try:
        pool = client._api_client.rest_client.pool_manager
        endpoint, target = client._api_url, client._target
    except AttributeError:
        raise ConfigError("Daytona conformance requires the locked v0 transport") from None
    if endpoint != api_url or target or not isinstance(pool, PoolManager):
        raise ConfigError("Daytona conformance requires the standard v0 endpoint and target")
    with _ROOT_LOCK:
        if _project_pool is pool:
            return
        send = pool.request

        def request(method, url, **kwargs):
            if method == "POST":
                kwargs.update(retries=False, redirect=False)
            return send(method, url, **kwargs)

        # Keep the guard on the owned pool even if the create waiter abandons its worker.
        pool.request = request
        _project_pool = pool


def _client(sdk, *, project=False):
    with _ROOT_LOCK:
        root = _roots.get(project)
        if root is None:
            root = _roots[project] = sdk.Daytona()
    client = root.v0
    if project:
        _project_transport(client, sdk.V0_API_URL)
    return client


def _retry_after(headers):
    for name, value in headers.items():
        if name.lower() == "retry-after":
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                return None
            return seconds if isfinite(seconds) and seconds >= 0 else None
    return None


def _call(operation, action, *, project=False):
    sdk, errors, _ = _sdk()
    error_type, details = ProviderError, {}
    try:
        return action(_client(sdk, project=project), errors)
    except errors.DaytonaError as error:
        status = error.status_code
        if status in (402, 403, 409, 429) and "quota" in (error.code or error.message).lower():
            error_type = QuotaError
        elif status == 429 or isinstance(error, errors.DaytonaRateLimitError):
            error_type = RateLimitError
            details = {"retry_after": _retry_after(error.headers)}
        elif status in (401, 403) or isinstance(error, errors.DaytonaAuthenticationError):
            error_type = AuthError
        elif operation == "create" and isinstance(error, errors.DaytonaTimeoutError):
            details = {"sandbox_id": error.sandbox_id, "sandbox_may_exist": True}
    raise error_type(f"Daytona {operation} failed", **details)


def _created_at(value):
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value)
            if date.tzinfo is not None:
                return date.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _checked_snapshot_id(snapshot):
    if (snapshot.name != _PROJECT_SNAPSHOT or snapshot.general is not True or snapshot.state != "active"
            or snapshot.sandbox_class != "container" or (snapshot.cpu, snapshot.mem, snapshot.disk, snapshot.gpu) != (1, 1, 3, 0)
            or not isinstance(snapshot.id, str)):
        raise ProviderError("The active public daytona-small snapshot does not match conformance pricing")
    try:
        return str(UUID(snapshot.id))
    except ValueError:
        raise ProviderError("Daytona conformance requires a snapshot ID") from None


class _Daytona:
    def __init__(self):
        self._sandbox = None
        self._session_id = None
        self._project_snapshot_id = None

    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None:
        if getenv("ANYCOMPUTE_CONFORMANCE_LIVE") != "1":
            return
        self._project_snapshot_id = None
        if options:
            raise ConfigError("Daytona conformance requires default options")
        if getenv("DAYTONA_V0_API_URL") or getenv("DAYTONA_TARGET"):
            raise ConfigError("Daytona conformance requires the standard v0 endpoint and target")
        try:
            locked = version("daytona") == "2.1.0" and version("daytona-api-client") == "0.233.0"
        except PackageNotFoundError:
            locked = False
        if not locked:
            raise ConfigError("Daytona conformance requires SDK 2.1.0 and API client 0.233.0")
        snapshot = _call("verify snapshot", lambda client, _: client.snapshot.get(_PROJECT_SNAPSHOT), project=True)
        self._project_snapshot_id = _checked_snapshot_id(snapshot)

    def create(self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float) -> str:
        project = getenv("ANYCOMPUTE_CONFORMANCE_LIVE") == "1"
        project_settings = {}
        if project:
            if options or type(ttl) is not int or ttl not in (60, 120):
                raise ConfigError("Daytona conformance requires default options and a TTL of 60 or 120 seconds")
            snapshot = self._project_snapshot_id
            if snapshot is None:
                raise ConfigError("Daytona conformance requires a verified snapshot before create")
            project_settings = {"target": _PROJECT_TARGET, "network_block_all": True}
        else:
            snapshot = options.get("snapshot", "daytona-small")
            if not isinstance(snapshot, str) or not snapshot or any(char.isspace() or char == "\0" for char in snapshot):
                raise ConfigError("Daytona snapshot must be a nonempty snapshot reference")
        _, _, create_params = _sdk()
        minutes = ttl // 60
        params = create_params(snapshot=snapshot, labels=dict(labels), ephemeral=True, ttl_minutes=minutes,
                               auto_stop_interval=minutes, env_vars=options.get("environment"), **project_settings)
        sandbox = _call("create", lambda client, _: client.create(params, timeout=timeout_s), project=project)
        sandbox_id = sandbox.id
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ProviderError("Daytona create did not return a sandbox ID", sandbox_may_exist=True)
        self._sandbox = sandbox
        self._session_id = None
        return sandbox_id

    def _handle(self, client, sandbox_id, timeout_s):
        sandbox = self._sandbox
        if sandbox is not None and sandbox.id == sandbox_id:
            return sandbox
        return client.get(sandbox_id, request_timeout=timeout_s)

    def exec(self, sandbox_id: str, argv: Sequence[str], *, cwd: str, timeout_s: float) -> RawExecResult:
        # V0's ordinary exec combines output; its synchronous session endpoint separates streams.
        command = join(("/bin/sh", "-c", f"cd {quote(cwd)} && {join(argv)}"))

        def execute(client, v0):
            try:
                process = self._handle(client, sandbox_id, timeout_s).process
                if self._session_id is None:
                    session_id = token_hex(12)
                    process.create_session(session_id, request_timeout=timeout_s)
                    self._session_id = session_id
                result = process.execute_session_command(
                    self._session_id, v0.SessionExecuteRequest(command=command, run_async=False, suppress_input_echo=True),
                    timeout=ceil(timeout_s),
                )
            except (v0.DaytonaNotFoundError, v0.DaytonaGoneError, v0.DaytonaTimeoutError):
                return RawExecResult("", "", None)
            return RawExecResult(result.stdout, result.stderr, result.exit_code)

        return _call("exec", execute)

    def write(self, sandbox_id: str, path: str, data: bytes, *, timeout_s: float) -> None:
        _call("write", lambda client, _: self._handle(client, sandbox_id, timeout_s).fs.upload_file(
            data, path, timeout=ceil(timeout_s),
        ))

    def read(self, sandbox_id: str, path: str, *, timeout_s: float) -> bytes | None:
        def read_file(client, v0):
            try:
                files = self._handle(client, sandbox_id, timeout_s).fs
                info = files.get_file_info(path, request_timeout=timeout_s)
                if not info.is_dir and info.mode.startswith("-"):
                    data = files.download_file(path, ceil(timeout_s))
                    if type(data) is not bytes:
                        raise ProviderError("Daytona returned an invalid file response")
                    return data
            except (v0.DaytonaNotFoundError, v0.DaytonaGoneError):
                pass
            return None

        return _call("read", read_file)

    def delete(self, sandbox_id: str, *, timeout_s: float) -> None:
        def delete_sandbox(client, v0):
            try:
                sandbox = self._handle(client, sandbox_id, timeout_s)
                client.delete(sandbox, timeout=timeout_s, wait=False)
            except (v0.DaytonaNotFoundError, v0.DaytonaGoneError):
                pass

        _call("delete", delete_sandbox)

    def state(self, sandbox_id: str, *, timeout_s: float) -> SandboxState:
        def inspect(client, v0):
            try:
                state = client.get(sandbox_id, request_timeout=timeout_s).state
            except (v0.DaytonaNotFoundError, v0.DaytonaGoneError):
                return SandboxState.GONE
            return {
                "started": SandboxState.RUNNING,
                "stopped": SandboxState.STOPPED,
                "paused": SandboxState.STOPPED,
                "archived": SandboxState.STOPPED,
                "destroyed": SandboxState.GONE,
            }.get(getattr(state, "value", state), SandboxState.UNKNOWN)

        return _call("inspect", inspect)

    def list(self, labels: Mapping[str, str], *, timeout_s: float) -> Sequence[SandboxInfo]:
        def list_sandboxes(client, v0):
            query = v0.ListSandboxesQuery(labels=dict(labels))
            return [SandboxInfo(sandbox.id, sandbox.labels, _created_at(sandbox.created_at))
                    for sandbox in client.list(query, request_timeout=timeout_s)
                    if all(sandbox.labels.get(key) == value for key, value in labels.items())]

        return _call("list", list_sandboxes)

    def native(self, sandbox_id: str, *, timeout_s: float) -> object:
        return _call("get sandbox", lambda client, _: self._handle(client, sandbox_id, timeout_s))

    def api_host(self, *, timeout_s: float) -> str:
        sdk, _, _ = _sdk()
        return (getenv("DAYTONA_V0_API_URL") or "").strip().rstrip("/") or sdk.V0_API_URL


SPEC = ProviderSpec(
    "daytona",
    ProviderCaps(
        ttl_unit_s=60,
        lifetime="partial",
        create_retry="partial",
        exec_retry="partial",
        provider_options_allowlist=frozenset({"snapshot", "environment"}),
        sdk_package="daytona",
        isolation="Linux container",
        key_sources=KeySources(env=("DAYTONA_API_KEY", "DAYTONA_JWT_TOKEN")),
    ),
    _Daytona,
)

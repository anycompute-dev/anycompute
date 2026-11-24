"""E2B microVM adapter. Import the optional SDK on the first operation."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from shlex import join

from ..contract import (
    AuthError, ConfigError, KeySources, ProviderCaps, ProviderError, ProviderSpec, QuotaError, RateLimitError,
    RawExecResult, SandboxInfo, SandboxState,
)


def _sdk():
    try:
        import e2b
        from httpx import TransportError
    except ImportError:
        pass
    else:
        return e2b, TransportError
    raise ConfigError("Install the E2B extra: anycompute[e2b]")


def _call(operation, action):
    sdk, transport_error = _sdk()
    error_type = ProviderError
    try:
        return action(sdk)
    except sdk.AuthenticationException:
        error_type = AuthError
    except sdk.RateLimitException as error:
        error_type = QuotaError if "quota" in str(error).lower() else RateLimitError
    except sdk.SandboxException as error:
        if error.status_code in (402, 403, 409) and "quota" in str(error).lower():
            error_type = QuotaError
        elif error.status_code in (401, 403):
            error_type = AuthError
    except (sdk.ServiceBusyException, transport_error):
        pass
    raise error_type(f"E2B {operation} failed")


def _attach(sdk, sandbox_id, timeout_s):
    # Sandbox.connect can resume a paused microVM. The pinned SDK's GET exposes the guest token without resuming.
    from e2b.api import handle_api_exception
    from e2b.api.client.api.sandboxes import get_sandboxes_sandbox_id
    from e2b.api.client.models.sandbox_detail import SandboxDetail
    from e2b.api.client_sync import get_api_client
    from packaging.version import Version

    config = sdk.ConnectionConfig(debug=False, retries=0, request_timeout=timeout_s)
    with get_api_client(config) as client:
        response = get_sandboxes_sandbox_id.sync_detailed(sandbox_id, client=client)
    if response.status_code == 404:
        raise sdk.SandboxNotFoundException("Sandbox not found")
    if response.status_code >= 300:
        raise handle_api_exception(response)
    detail = response.parsed
    if not isinstance(detail, SandboxDetail) or detail.sandbox_id != sandbox_id:
        raise ProviderError("E2B returned an invalid sandbox lookup")
    if str(detail.state) != "running":
        raise sdk.SandboxNotRunningException("Sandbox is not running")
    token = detail.envd_access_token
    if not isinstance(token, str) or not token:
        raise ProviderError("E2B sandbox lookup omitted the guest token")
    headers = {
        "E2b-Sandbox-Id": sandbox_id,
        "E2b-Sandbox-Port": str(sdk.ConnectionConfig.envd_port),
        "X-Access-Token": token,
    }
    return sdk.Sandbox(
        sandbox_id=sandbox_id, sandbox_domain=detail.domain if isinstance(detail.domain, str) else None,
        envd_version=Version(detail.envd_version), envd_access_token=token,
        connection_config=sdk.ConnectionConfig(debug=False, retries=0, request_timeout=timeout_s,
                                               extra_sandbox_headers=headers),
    )


def _options(timeout_s):
    return {"debug": False, "retries": 0, "request_timeout": timeout_s}


class _E2B:
    def __init__(self):
        self._sandbox = None

    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None:
        pass

    def create(self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float) -> str:
        template = options.get("template", "base")
        if not isinstance(template, str) or not template or any(char.isspace() or char == "\0" for char in template):
            raise ConfigError("E2B template must be a nonempty template reference")
        sandbox = _call("create", lambda sdk: sdk.Sandbox.create(
            template=template, timeout=ttl, metadata=dict(labels), envs=options.get("environment"),
            lifecycle={"on_timeout": "kill"}, **_options(timeout_s),
        ))
        sandbox_id = sandbox.sandbox_id
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ProviderError("E2B create did not return a sandbox ID", sandbox_may_exist=True)
        self._sandbox = sandbox
        return sandbox_id

    def _handle(self, sdk, sandbox_id, timeout_s):
        sandbox = self._sandbox
        if sandbox is not None and sandbox.sandbox_id == sandbox_id:
            return sandbox
        return _attach(sdk, sandbox_id, timeout_s)

    def exec(self, sandbox_id: str, argv: Sequence[str], *, cwd: str, timeout_s: float) -> RawExecResult:
        def execute(sdk):
            stdout, stderr = [], []
            try:
                sandbox = self._handle(sdk, sandbox_id, timeout_s)
                result = sandbox.commands.run(
                    join(argv), shell=("/bin/sh", "-c"), cwd=cwd, timeout=0, retain_output=False,
                    on_stdout=stdout.append, on_stderr=stderr.append,
                )
            except sdk.CommandExitException as error:
                result = error
            except (sdk.SandboxNotFoundException, sdk.TimeoutException):
                return RawExecResult("".join(stdout), "".join(stderr), None)
            return RawExecResult("".join(stdout), "".join(stderr), result.exit_code)

        return _call("exec", execute)

    def write(self, sandbox_id: str, path: str, data: bytes, *, timeout_s: float) -> None:
        _call("write", lambda sdk: self._handle(sdk, sandbox_id, timeout_s).files.write(
            path, data, request_timeout=timeout_s,
        ))

    def read(self, sandbox_id: str, path: str, *, timeout_s: float) -> bytes | None:
        def read_file(sdk):
            try:
                files = self._handle(sdk, sandbox_id, timeout_s).files
                if files.get_info(path, request_timeout=timeout_s).type is sdk.FileType.FILE:
                    data = files.read(path, format="bytes", request_timeout=timeout_s)
                    if not isinstance(data, (bytes, bytearray)):
                        raise ProviderError("E2B returned an invalid file response")
                    return bytes(data)
            except (sdk.FileNotFoundException, sdk.SandboxNotFoundException, sdk.SandboxNotRunningException):
                pass
            return None

        return _call("read", read_file)

    def delete(self, sandbox_id: str, *, timeout_s: float) -> None:
        _call("delete", lambda sdk: sdk.Sandbox.kill(sandbox_id, **_options(timeout_s)))

    def state(self, sandbox_id: str, *, timeout_s: float) -> SandboxState:
        def inspect(sdk):
            try:
                detail = sdk.Sandbox.get_info(sandbox_id, **_options(timeout_s))
            except sdk.SandboxNotFoundException:
                return SandboxState.GONE
            return {"running": SandboxState.RUNNING, "paused": SandboxState.STOPPED}.get(
                str(detail.state), SandboxState.UNKNOWN,
            )

        return _call("inspect", inspect)

    def list(self, labels: Mapping[str, str], *, timeout_s: float) -> Sequence[SandboxInfo]:
        def list_sandboxes(sdk):
            paginator = sdk.Sandbox.list(query=sdk.SandboxQuery(metadata=dict(labels)), **_options(timeout_s))
            matches = []
            while paginator.has_next:
                for item in paginator.next_items():
                    if all(item.metadata.get(key) == value for key, value in labels.items()):
                        start = item.started_at
                        created_at = start.timestamp() if isinstance(start, datetime) and start.tzinfo else None
                        matches.append(SandboxInfo(item.sandbox_id, item.metadata, created_at))
            return matches

        return _call("list", list_sandboxes)

    def native(self, sandbox_id: str, *, timeout_s: float) -> object:
        return _call("attach", lambda sdk: self._handle(sdk, sandbox_id, timeout_s))

    def api_host(self, *, timeout_s: float) -> str:
        sdk, _ = _sdk()
        return sdk.ConnectionConfig(debug=False).api_url


SPEC = ProviderSpec(
    "e2b",
    ProviderCaps(
        ttl_max_s=3600,
        create_retry="partial",
        exec_retry="partial",
        provider_options_allowlist=frozenset({"template", "environment"}),
        sdk_package="e2b",
        isolation="Firecracker microVM",
        key_sources=KeySources(env=("E2B_API_KEY",)),
    ),
    _E2B,
)

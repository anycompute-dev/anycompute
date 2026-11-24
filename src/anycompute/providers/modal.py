"""Modal Sandbox adapter. Import the optional SDK on the first operation."""

import asyncio
from collections.abc import Mapping, Sequence
from threading import Lock
from urllib.parse import urlsplit

from ..contract import (
    AuthError, ConfigError, ConfigKeys, KeySources, ProviderCaps, ProviderError, ProviderSpec, QuotaError,
    RateLimitError, RawExecResult, SandboxInfo, SandboxState,
)

_APP_LOCK = Lock()
_app = None
_AUTH_FIELDS = ("token_id", "token_secret", "oauth_refresh_token", "oauth_client_id", "oauth_client_secret",
                "oauth_jwt_key")


def _sdk():
    try:
        import modal
    except ImportError:
        pass
    else:
        return modal
    raise ConfigError("Install the modal extra: anycompute[modal]")


def _lookup_app(sdk):
    global _app
    with _APP_LOCK:
        app = _app
    if app is not None:
        return app
    app = sdk.App.lookup("anycompute", create_if_missing=True)
    with _APP_LOCK:
        if _app is None:
            _app = app
        return _app


def _call(operation, action):
    sdk = _sdk()
    errors = sdk.exception
    error = ProviderError
    may_exist = False
    try:
        return action(sdk)
    except (errors.AuthError, errors.PermissionDeniedError):
        error = AuthError
    except errors.ResourceExhaustedError as exhausted:
        reason = str(exhausted).lower()
        if "quota" in reason or "concurrency limit" in reason or "concurrent sandbox" in reason:
            error = QuotaError
        elif "rate limit" in reason or "too many requests" in reason:
            error = RateLimitError
    except errors.Error as rejected:
        may_exist = operation == "create" and not isinstance(rejected, (errors.InvalidError, errors.NotFoundError))
    message = f"Modal {operation} failed"
    if error is ProviderError:
        raise ProviderError(message, sandbox_may_exist=may_exist)
    raise error(message)


def _exec_ended_errors(sdk):
    errors = sdk.exception
    return errors.NotFoundError, errors.SandboxTimeoutError, errors.SandboxTerminatedError, errors.ExecTimeoutError


async def _read_process(process, ended_errors):
    stdout, stderr, code = await asyncio.gather(
        process.stdout.read.aio(), process.stderr.read.aio(), process.wait.aio(), return_exceptions=True,
    )
    results = (stdout, stderr, code)
    for value in results:
        if isinstance(value, BaseException) and not isinstance(value, ended_errors):
            raise value
    ended = any(isinstance(value, ended_errors) for value in results)
    return RawExecResult(
        b"" if isinstance(stdout, ended_errors) else stdout,
        b"" if isinstance(stderr, ended_errors) else stderr,
        None if ended or code == -1 else code,
    )


class _Modal:
    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None:
        pass

    def create(self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float) -> str:
        image = options.get("image")
        if image is not None and (not isinstance(image, str) or not image or any(c.isspace() or c == "\0" for c in image)):
            raise ConfigError("Modal image must be a nonempty image reference")
        app = _call("look up app", _lookup_app)

        def create_sandbox(sdk):
            return sdk.Sandbox.create(
                "sleep", "infinity", app=app, tags=dict(labels), timeout=ttl, runtime="gvisor",
                image=sdk.Image.from_registry(image) if image is not None else None, env=options.get("environment"),
            ).object_id

        sandbox_id = _call("create", create_sandbox)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ProviderError("Modal create did not return a sandbox ID", sandbox_may_exist=True)
        return sandbox_id

    def exec(self, sandbox_id: str, argv: Sequence[str], *, cwd: str, timeout_s: float) -> RawExecResult:
        def execute(sdk):
            ended_errors = _exec_ended_errors(sdk)
            try:
                process = sdk.Sandbox.from_id(sandbox_id).exec(*argv, workdir=cwd, text=False, timeout=None)
            except ended_errors:
                return RawExecResult(b"", b"", None)
            return asyncio.run(_read_process(process, ended_errors))

        return _call("exec", execute)

    def write(self, sandbox_id: str, path: str, data: bytes, *, timeout_s: float) -> None:
        _call("write", lambda sdk: sdk.Sandbox.from_id(sandbox_id).filesystem.write_bytes(data, path))

    def read(self, sandbox_id: str, path: str, *, timeout_s: float) -> bytes | None:
        def read_file(sdk):
            filesystem = sdk.Sandbox.from_id(sandbox_id).filesystem
            try:
                if filesystem.stat(path).is_file():
                    return filesystem.read_bytes(path)
            except (sdk.exception.SandboxFilesystemNotFoundError, sdk.exception.SandboxFilesystemIsADirectoryError,
                    sdk.exception.SandboxFilesystemNotADirectoryError):
                pass
            return None

        return _call("read", read_file)

    def delete(self, sandbox_id: str, *, timeout_s: float) -> None:
        def terminate(sdk):
            try:
                sdk.Sandbox.from_id(sandbox_id).terminate(wait=False)
            except sdk.exception.NotFoundError:
                pass

        _call("delete", terminate)

    def state(self, sandbox_id: str, *, timeout_s: float) -> SandboxState:
        def poll(sdk):
            try:
                code = sdk.Sandbox.from_id(sandbox_id).poll()
            except (sdk.exception.NotFoundError, sdk.exception.SandboxTimeoutError,
                    sdk.exception.SandboxTerminatedError):
                return SandboxState.GONE
            if code is None:
                return SandboxState.RUNNING
            # Modal retains finished handles, after discarding the sandbox and its filesystem.
            return SandboxState.GONE if type(code) is int else SandboxState.UNKNOWN

        return _call("state", poll)

    def list(self, labels: Mapping[str, str], *, timeout_s: float) -> Sequence[SandboxInfo]:
        app = _call("look up app", _lookup_app)

        def list_sandboxes(sdk):
            matches = []
            for sandbox in sdk.Sandbox.list(app_id=app.app_id, tags=dict(labels)):
                try:
                    tags = sandbox.get_tags()
                except sdk.exception.NotFoundError:
                    continue  # The sandbox ended between enumerating it and reading its tags.
                if all(tags.get(key) == value for key, value in labels.items()):
                    matches.append(SandboxInfo(sandbox.object_id, tags, None))
            return matches

        return _call("list", list_sandboxes)

    def native(self, sandbox_id: str, *, timeout_s: float) -> object:
        return _call("look up sandbox", lambda sdk: sdk.Sandbox.from_id(sandbox_id))

    def api_host(self, *, timeout_s: float) -> str | None:
        urls = _sdk().config.config["server_url"]
        return ",".join(host for url in urls.split(",") if (host := urlsplit(url.strip()).hostname)) or None


SPEC = ProviderSpec(
    "modal",
    ProviderCaps(
        ttl_max_s=86400,
        create_retry="partial",
        exec_retry="partial",
        provider_options_allowlist=frozenset({"image", "environment"}),
        sdk_package="modal",
        list_create_time=False,
        isolation="gVisor container",
        key_sources=KeySources(
            env=tuple(f"MODAL_{key.upper()}" for key in _AUTH_FIELDS),
            config=(ConfigKeys("~/.modal.toml", tuple(("*", key) for key in _AUTH_FIELDS), "MODAL_CONFIG_PATH"),),
        ),
        live_run=True,
    ),
    _Modal,
)

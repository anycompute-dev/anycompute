"""Modal Sandbox adapter. Import the optional SDK on the first operation."""

import asyncio
import os
from collections.abc import Mapping, Sequence
from threading import Lock

from ..contract import (
    AuthError, ConfigError, ConfigKeys, KeySources, ProviderCaps, ProviderError, ProviderSpec, QuotaError,
    RateLimitError, RawExecResult, SandboxInfo, SandboxState,
)

_APP_LOCK = Lock()
_app = None
_CONFORMANCE_SDK_VERSION = "1.6.4"
_CONFORMANCE_IMAGE = "anycompute-conformance:v1"
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


def _check_conformance(sdk, options):
    if options:
        raise ConfigError("Modal conformance requires default provider options")
    if getattr(sdk, "__version__", None) != _CONFORMANCE_SDK_VERSION:
        raise ConfigError(f"Modal conformance requires SDK {_CONFORMANCE_SDK_VERSION}")
    try:
        throttle_wait = sdk.config.config.get("max_throttle_wait")
    except (TypeError, ValueError):
        throttle_wait = None
    if type(throttle_wait) is not int or throttle_wait != 0:
        raise ConfigError("Modal conformance requires MODAL_MAX_THROTTLE_WAIT=0")


class _Modal:
    def __init__(self) -> None:
        self._prepared = None

    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None:
        if os.environ.get("ANYCOMPUTE_CONFORMANCE_LIVE") != "1":
            return

        def resolve(sdk):
            _check_conformance(sdk, options)
            # Sandbox.create resolves this published image reference.
            image = sdk.Image.from_name(_CONFORMANCE_IMAGE)
            return _lookup_app(sdk), image

        self._prepared = None
        self._prepared = _call("prepare conformance image and app", resolve)

    def create(self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float) -> str:
        registry_image = options.get("image")
        if registry_image is not None and (not isinstance(registry_image, str) or not registry_image
                                           or any(c.isspace() or c == "\0" for c in registry_image)):
            raise ConfigError("Modal image must be a nonempty image reference")
        project = os.environ.get("ANYCOMPUTE_CONFORMANCE_LIVE") == "1"
        if project:
            if type(ttl) is not int or not 1 <= ttl <= 120:
                raise ConfigError("Modal conformance requires a TTL between 1 and 120 seconds")
            if self._prepared is None:
                raise ConfigError("Modal conformance requires a prepared image and app")
            app, image = self._prepared
        else:
            app = _call("look up app", _lookup_app)

        def create_sandbox(sdk):
            if project:
                _check_conformance(sdk, options)
                return sdk.Sandbox.create(
                    "sleep", "infinity", app=app, tags=dict(labels), timeout=ttl, runtime="gvisor",
                    image=image, cpu=(0.125, 1), memory=(128, 512), block_network=True,
                ).object_id
            return sdk.Sandbox.create(
                "sleep", "infinity", app=app, tags=dict(labels), timeout=ttl, runtime="gvisor",
                image=sdk.Image.from_registry(registry_image) if registry_image is not None else None,
                env=options.get("environment"),
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
        return None if "," in urls else urls.strip()


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
    ),
    _Modal,
)

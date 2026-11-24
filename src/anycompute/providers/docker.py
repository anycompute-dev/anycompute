"""Docker Engine adapter; the optional SDK is loaded on the first operation."""

import io
import math
import posixpath
import tarfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from importlib.metadata import version

from ..contract import (
    AuthError, ConfigError, InvalidPathError, ProviderCaps, ProviderError, ProviderSpec, RateLimitError, RawExecResult,
    SandboxInfo, SandboxState,
)

DEFAULT_IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
# The archive stat header uses Go's os.FileMode type bits, not POSIX st_mode.
_NON_REGULAR_MODE = 0x8F280000


def _image(options: Mapping[str, object]) -> str:
    image = options.get("image", DEFAULT_IMAGE)
    if not isinstance(image, str) or not image or any(c.isspace() or c == "\0" for c in image):
        raise ConfigError("Docker image must be a nonempty image reference")
    return image


def _explanation(error) -> str:
    text = error.explanation
    return text.lower() if isinstance(text, str) else ""


def _sdk():
    try:
        import docker
        from urllib3.exceptions import HTTPError

        return docker, HTTPError
    except ImportError:
        pass
    raise ConfigError("Install the docker extra: anycompute[docker]")


class _Docker:
    def __init__(self) -> None:
        self._client = None

    def _call(self, operation, fn, *, sandbox_id=None):
        docker, http_error = _sdk()
        if self._client is None:
            try:
                # Only the engine's waited call limits the request duration.
                client = docker.from_env(timeout=None, use_context=True)
            except Exception:
                client = None
            if client is None:
                raise ProviderError("Cannot reach the Docker daemon; check that it is running and the context is correct")
            # from_env negotiates the API version before returning the configured session.
            client.api.headers["User-Agent"] = f"anycompute/{version('anycompute')}"
            # Requests can replay a POST when following a 307/308 redirect.
            client.api.max_redirects = 0
            self._client = client
        status, retry_after = None, None
        ambiguous = interrupted = internal = False
        try:
            return fn(self._client, docker.errors)
        except docker.errors.APIError as error:
            status = error.status_code
            ambiguous = status is None or status >= 500
            if status == 429:
                delay = error.response.headers.get("Retry-After", "")
                if delay.isascii() and delay.isdecimal() and math.isfinite(seconds := float(delay)):
                    retry_after = seconds
        except (http_error, OSError):
            ambiguous = True
        except docker.errors.DockerException:
            pass
        except KeyboardInterrupt:
            if operation not in ("create", "start"):
                raise
            ambiguous = interrupted = True
        except Exception:
            if operation != "start":
                raise
            internal = True
        suffix = f" (HTTP {status})" if status is not None else ""
        message = f"Interrupted during Docker {operation}" if interrupted else f"Docker {operation} failed{suffix}"
        if operation not in ("start", "prepare or pull image"):
            if status in (401, 403):
                raise AuthError(message)
            if status == 429:
                raise RateLimitError(message, retry_after=retry_after)
        raise ProviderError(message, sandbox_id=sandbox_id, sandbox_may_exist=operation == "create" and ambiguous,
                            interrupted=interrupted, internal=internal)

    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None:
        image = _image(options)

        def prepare_image(client, errors):
            try:
                client.images.get(image)
            except errors.ImageNotFound:
                pass
            else:
                return
            for reply in client.api.pull(image, stream=True, decode=True):
                if "error" in reply or "errorDetail" in reply:
                    raise ProviderError("Docker pull image failed")

        self._call("prepare or pull image", prepare_image)

    def create(self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float) -> str:
        image = _image(options)

        def create_container(client, _):
            api = client.api
            host_config = api.create_host_config(auto_remove=True, pids_limit=4096, security_opt=["no-new-privileges"])
            return api.create_container(
                image=image, command=[], entrypoint=["sleep", str(ttl)], detach=True,
                labels=dict(labels), environment=options.get("environment"), host_config=host_config,
                use_config_proxy=False,
            )

        # The SDK's high-level create inspects again before returning the ID.
        response = self._call("create", create_container)
        sandbox_id = response.get("Id") if isinstance(response, dict) else None
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ProviderError("Docker create did not return a container ID", sandbox_may_exist=True)
        self._call("start", lambda client, _: client.api.start(sandbox_id), sandbox_id=sandbox_id)
        return sandbox_id

    def exec(self, sandbox_id: str, argv: Sequence[str], *, cwd: str, timeout_s: float) -> RawExecResult:
        def execute(client, errors):
            api = client.api
            stdout = stderr = None
            try:
                exec_id = api.exec_create(sandbox_id, list(argv), workdir=cwd, tty=False)["Id"]
                stdout, stderr = api.exec_start(exec_id, tty=False, demux=True)
                exit_code = api.exec_inspect(exec_id).get("ExitCode")
            except errors.APIError as error:
                not_running = f"container {sandbox_id} is not running".lower()
                if error.status_code != 404 and not (error.status_code == 409 and _explanation(error) == not_running):
                    raise
                # Auto-remove can race exec inspection; keep the output for the engine's state check.
                exit_code = None
            return RawExecResult(stdout or b"", stderr or b"", exit_code)

        return self._call("exec", execute)

    def write(self, sandbox_id: str, path: str, data: bytes, *, timeout_s: float) -> None:
        parent, name = posixpath.split(path)
        if name in ("", ".", "..") or "\0" in name:
            raise InvalidPathError("Docker upload must name a file")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        payload = buffer.getvalue()
        if not self._call("write archive", lambda client, _: client.api.put_archive(sandbox_id, parent, payload)):
            raise ProviderError("Docker refused the file archive")

    def read(self, sandbox_id: str, path: str, *, timeout_s: float) -> bytes | None:
        def read_archive(client, errors):
            try:
                chunks, metadata = client.api.get_archive(sandbox_id, path)
            except errors.APIError as error:
                if error.status_code == 404 and _explanation(error).startswith("could not find the file "):
                    return None
                raise
            with closing(chunks):
                mode = metadata.get("mode") if isinstance(metadata, dict) else None
                if isinstance(mode, int) and mode & _NON_REGULAR_MODE:
                    return None
                buffer = io.BytesIO(b"".join(chunks))
            try:
                with tarfile.open(fileobj=buffer, mode="r:") as archive:
                    member = archive.next()
                    if (member is None or member.name != posixpath.basename(path)
                            or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.issparse()):
                        return None
                    if (file := archive.extractfile(member)) is not None:
                        with file:
                            return file.read()
            except (tarfile.TarError, EOFError, ValueError):
                pass
            raise ProviderError("Docker returned an invalid file archive")

        return self._call("read archive", read_archive)

    def delete(self, sandbox_id: str, *, timeout_s: float) -> None:
        def remove(client, errors):
            try:
                client.api.remove_container(sandbox_id, force=True)
            except errors.APIError as error:
                conflict = f"removal of container {sandbox_id} is already in progress".lower()
                if error.status_code != 404 and not (error.status_code == 409 and _explanation(error) == conflict):
                    raise

        self._call("delete", remove)

    def state(self, sandbox_id: str, *, timeout_s: float) -> SandboxState:
        def inspect(client, errors):
            try:
                detail = client.api.inspect_container(sandbox_id).get("State")
            except errors.APIError as error:
                if error.status_code == 404:
                    return SandboxState.GONE
                raise
            status = detail.get("Status") if isinstance(detail, dict) else None
            if status == "running":
                return SandboxState.RUNNING
            # Moby only reports "removing" once the container is no longer running.
            if status in ("created", "exited", "dead", "removing"):
                return SandboxState.STOPPED
            return SandboxState.UNKNOWN

        return self._call("state", inspect)

    def list(self, labels: Mapping[str, str], *, timeout_s: float) -> list[SandboxInfo]:
        filters = {"label": [f"{key}={value}" for key, value in labels.items()]} if labels else None
        rows = self._call("list", lambda client, _: client.api.containers(all=True, filters=filters))
        return [SandboxInfo(row["Id"], row.get("Labels") or {}, row.get("Created")) for row in rows]

    def native(self, sandbox_id: str, *, timeout_s: float) -> object:
        return self._call("get container", lambda client, _: client.containers.get(sandbox_id))

    def api_host(self, *, timeout_s: float) -> str | None:
        def host(client, _):
            base_url = client.api.base_url
            if base_url in ("http+docker://localhost", "http+docker://localnpipe"):
                return "local"
            if base_url == "http+docker://ssh":
                host = getattr(client.api.get_adapter(base_url), "ssh_host", None)
                return f"ssh://{host}" if host else None
            return base_url

        return self._call("get API host", host)


SPEC = ProviderSpec(
    "docker",
    ProviderCaps(
        pre_start_window="partial",
        provider_options_allowlist=frozenset({"image", "environment"}),
        sdk_package="docker",
        isolation="container on the host kernel",
        live_run=True,
    ),
    _Docker,
)

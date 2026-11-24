import io
import json
import socket
import sys
import tarfile
import threading
import traceback
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from anycompute.contract import (
    AuthError, ConfigError, InvalidPathError, ProviderError, RateLimitError, RawExecResult, SandboxInfo, SandboxState,
    SandboxTimeoutError,
)
from tests.fakes.clock import FakeClock


class _DockerException(Exception):
    pass


class _APIError(_DockerException):
    def __init__(self, status_code, explanation, headers=None):
        super().__init__(explanation)
        self.status_code = status_code
        self.explanation = explanation
        self.response = SimpleNamespace(headers=headers or {})


class _ImageNotFound(_APIError):
    pass


@pytest.fixture
def sdk(monkeypatch):
    client = Mock()
    client.api.headers = {}
    sdk = SimpleNamespace(
        from_env=Mock(return_value=client),
        errors=SimpleNamespace(DockerException=_DockerException, APIError=_APIError, ImageNotFound=_ImageNotFound),
    )
    monkeypatch.setitem(sys.modules, "docker", sdk)
    monkeypatch.setitem(sys.modules, "urllib3.exceptions", SimpleNamespace(HTTPError=OSError))
    return sdk, client


def test_docker_spec_declares_hard_lifetime_after_start_without_importing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "docker", None)
    from anycompute.providers import docker as adapter

    spec = adapter.SPEC
    assert spec.name == spec.caps.sdk_package == "docker"
    assert spec.caps.lifetime == "hard" and spec.caps.pre_start_window == "partial"
    assert spec.caps.ttl_unit_s == 1 and spec.caps.ttl_max_s is None
    assert spec.caps.labels_at_create and spec.caps.list_create_time and spec.caps.live_run
    assert spec.caps.create_retry == spec.caps.exec_retry == "off"
    assert spec.caps.creates_per_minute is None
    assert spec.caps.isolation == "container on the host kernel"
    assert not (spec.caps.key_sources.env or spec.caps.key_sources.config)
    assert spec.caps.provider_options_allowlist == {"image", "environment"}
    assert adapter.DEFAULT_IMAGE == (
        "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
    )
    assert spec.factory() is not spec.factory()


def test_missing_sdk_names_the_optional_extra(monkeypatch):
    from anycompute.providers.docker import SPEC

    monkeypatch.setitem(sys.modules, "docker", None)
    with pytest.raises(ConfigError, match=r"anycompute\[docker\]") as error:
        SPEC.factory().prepare(options={}, timeout_s=150)
    assert error.value.__context__ is error.value.__cause__ is None


def test_prepare_opens_the_client_lazily_and_reuses_a_cached_image(sdk):
    from anycompute.providers.docker import DEFAULT_IMAGE, SPEC

    module, client = sdk
    adapter = SPEC.factory()
    module.from_env.assert_not_called()
    for _ in range(2):
        adapter.prepare(options={}, timeout_s=150)
    module.from_env.assert_called_once_with(timeout=None, use_context=True)
    assert client.api.headers["User-Agent"] == f"anycompute/{version('anycompute')}"
    assert client.images.get.call_count == 2
    client.images.get.assert_called_with(DEFAULT_IMAGE)
    client.api.pull.assert_not_called()


def test_prepare_pulls_the_selected_image_only_on_an_image_cache_miss(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.images.get.side_effect = _ImageNotFound(404, "No such image: example-image")
    client.api.pull.return_value = iter(({"status": "downloaded"},))
    SPEC.factory().prepare(options={"image": "example-image:latest"}, timeout_s=150)
    client.images.get.assert_called_once_with("example-image:latest")
    client.api.pull.assert_called_once_with("example-image:latest", stream=True, decode=True)


@pytest.mark.parametrize("image", [None, "", "bad image", "bad\x00image"])
def test_prepare_rejects_an_invalid_image_reference_without_connecting(sdk, image):
    from anycompute.providers.docker import SPEC

    module, _ = sdk
    with pytest.raises(ConfigError, match="image"):
        SPEC.factory().prepare(options={"image": image}, timeout_s=150)
    module.from_env.assert_not_called()


@pytest.mark.parametrize("status", [401, 429, 502])
def test_registry_pull_failure_is_not_a_start_rate_or_auth_error(sdk, status):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.images.get.side_effect = _ImageNotFound(404, "No such image")
    client.api.pull.side_effect = _APIError(status, "unpublished daemon detail")
    with pytest.raises(ProviderError, match="pull image") as error:
        SPEC.factory().prepare(options={}, timeout_s=150)
    assert not error.value.sandbox_may_exist and not error.value.internal
    assert error.value.__context__ is error.value.__cause__ is None
    assert "unpublished daemon detail" not in "".join(traceback.format_exception(error.value))
    client.api.create_container.assert_not_called()
    assert client.api.pull.call_count == 1


@pytest.mark.parametrize("reply", [
    {"error": "unpublished daemon detail"},
    {"errorDetail": {"message": "unpublished daemon detail"}},
])
def test_registry_error_in_a_successful_http_pull_stream_is_not_silently_ignored(sdk, reply):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.images.get.side_effect = _ImageNotFound(404, "No such image")
    client.api.pull.return_value = iter(({"status": "pulling"}, reply))
    with pytest.raises(ProviderError, match="pull image") as error:
        SPEC.factory().prepare(options={}, timeout_s=150)
    assert not error.value.sandbox_may_exist and not error.value.internal
    assert error.value.__context__ is error.value.__cause__ is None
    assert "unpublished daemon detail" not in "".join(traceback.format_exception(error.value))
    client.api.create_container.assert_not_called()


@pytest.mark.parametrize("failure_type", [_DockerException, OSError, Exception])
def test_daemon_connection_failure_has_a_static_hint(sdk, failure_type):
    from anycompute.providers.docker import SPEC

    module, _ = sdk
    module.from_env.side_effect = failure_type("unpublished daemon detail")
    with pytest.raises(ProviderError, match="Docker daemon.*running") as error:
        SPEC.factory().prepare(options={}, timeout_s=150)
    assert not error.value.sandbox_may_exist and not error.value.internal
    assert error.value.__context__ is error.value.__cause__ is None
    assert "unpublished daemon detail" not in "".join(traceback.format_exception(error.value))


@pytest.fixture
def registered(monkeypatch):
    from anycompute import engine, providers
    from anycompute.providers.docker import SPEC

    monkeypatch.setattr(providers, "PROVIDERS", (SPEC,))
    monkeypatch.setattr(engine, "_pacers", {})
    monkeypatch.setattr(engine, "_env_rate_loaded", True)
    monkeypatch.setattr(engine, "_env_rate", None)
    return engine


def test_hung_version_request_uses_prepare_budget_without_creating(sdk, registered, monkeypatch):
    module, client = sdk
    entered, release = threading.Event(), threading.Event()
    waits = []

    def version_request(**kwargs):
        entered.set()
        assert release.wait(2)
        return client

    def wait(event, budget):
        assert entered.wait(2) and not event.is_set()
        waits.append(budget)
        return False

    module.from_env.side_effect = version_request
    monkeypatch.setattr(registered, "_clock", SimpleNamespace(now=lambda: 0, wait=wait))
    try:
        with pytest.raises(ProviderError, match="prepare exceeded") as error:
            registered.Sandbox("docker", ttl=30, run_id="docker-test")
        assert waits == [150]
        assert not error.value.sandbox_may_exist and error.value.sandbox_id is None
        client.api.create_container.assert_not_called()
    finally:
        release.set()


@pytest.mark.parametrize("status, explanation, ok", [
    (404, "No such container: box-id", True),
    (409, "removal of container box-id is already in progress", True),
    (409, "container is paused and must be unpaused first", False),
    (409, "removal of container other-id is already in progress", False),
    (500, "removal of container box-id is already in progress", False),
])
def test_force_delete_accepts_only_not_found_or_the_exact_removal_conflict(sdk, status, explanation, ok):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.remove_container.side_effect = _APIError(status, explanation)
    adapter = SPEC.factory()
    if ok:
        assert adapter.delete("box-id", timeout_s=120) is None
    else:
        with pytest.raises(ProviderError, match=f"HTTP {status}") as error:
            adapter.delete("box-id", timeout_s=120)
        assert error.value.__context__ is error.value.__cause__ is None
        assert explanation not in str(error.value)
    client.api.remove_container.assert_called_once_with("box-id", force=True)
    client.api.inspect_container.assert_not_called()


@pytest.mark.parametrize("status, expected", [
    ("running", SandboxState.RUNNING),
    ("created", SandboxState.STOPPED),
    ("exited", SandboxState.STOPPED),
    ("dead", SandboxState.STOPPED),
    ("paused", SandboxState.UNKNOWN),
    ("restarting", SandboxState.UNKNOWN),
    ("removing", SandboxState.STOPPED),
    ("unexpected", SandboxState.UNKNOWN),
    (None, SandboxState.UNKNOWN),
])
def test_state_translates_native_container_status(sdk, status, expected):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.inspect_container.return_value = {"State": {"Status": status}}
    assert SPEC.factory().state("box-id", timeout_s=120) == expected
    client.api.inspect_container.assert_called_once_with("box-id")


def test_state_confirms_absence_on_404_not_a_daemon_fault(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.inspect_container.side_effect = [
        _APIError(404, "No such container: box-id"), _APIError(502, "unpublished daemon detail"),
    ]
    adapter = SPEC.factory()
    assert adapter.state("box-id", timeout_s=120) == SandboxState.GONE
    with pytest.raises(ProviderError, match="HTTP 502"):
        adapter.state("box-id", timeout_s=120)


def test_create_sends_fixed_lifecycle_and_safety_settings_before_start(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.create_container.return_value = {"Id": "box-id"}
    labels = {"anycompute": "1", "anycompute-run": "docker-test", "anycompute-ttl": "61"}
    options = {"image": "example-image:latest", "environment": {"COLOR": "blue"}}
    assert SPEC.factory().create(ttl=61, labels=labels, options=options, timeout_s=181) == "box-id"
    client.api.create_host_config.assert_called_once_with(
        auto_remove=True, pids_limit=4096, security_opt=["no-new-privileges"],
    )
    client.api.create_container.assert_called_once_with(
        image="example-image:latest", command=[], entrypoint=["sleep", "61"], detach=True,
        labels=labels, environment={"COLOR": "blue"}, host_config=client.api.create_host_config.return_value,
        use_config_proxy=False,
    )
    client.api.start.assert_called_once_with("box-id")
    assert [method for method, *_ in client.api.method_calls] == [
        "create_host_config", "create_container", "start",
    ]
    client.images.get.assert_not_called()
    client.api.pull.assert_not_called()


@pytest.mark.parametrize("failure, interrupted, internal", [
    (_APIError(500, "missing sleep in private image"), False, False),
    (KeyboardInterrupt("unpublished daemon detail"), True, False),
    (TypeError("unpublished daemon detail"), False, True),
])
def test_start_failure_carries_the_created_id_and_engine_confirms_delete(sdk, registered, failure, interrupted, internal):
    _, client = sdk
    container_status = None

    def create(**kwargs):
        nonlocal container_status
        container_status = "created"
        return {"Id": "box-id"}

    def start(sandbox_id):
        assert sandbox_id == "box-id" and container_status == "created"
        raise failure

    def remove(sandbox_id, *, force):
        nonlocal container_status
        assert (sandbox_id, force, container_status) == ("box-id", True, "created")
        container_status = None

    def inspect(sandbox_id):
        assert sandbox_id == "box-id" and container_status is None
        raise _APIError(404, "No such container: box-id")

    client.api.create_container.side_effect = create
    client.api.start.side_effect = start
    client.api.remove_container.side_effect = remove
    client.api.inspect_container.side_effect = inspect
    with pytest.raises(ProviderError, match="Docker start|Interrupted") as error:
        registered.Sandbox("docker", ttl=30, run_id="docker-test")
    assert error.value.sandbox_id == "box-id" and error.value.run_id == "docker-test"
    assert error.value.sandbox_seconds is not None and not error.value.sandbox_may_exist
    assert error.value.interrupted is interrupted and error.value.internal is internal
    assert error.value.__context__ is error.value.__cause__ is None
    assert str(failure) not in "".join(traceback.format_exception(error.value))
    client.api.remove_container.assert_called_once_with("box-id", force=True)
    client.api.inspect_container.assert_called_once_with("box-id")
    client.api.exec_create.assert_not_called()


@pytest.mark.parametrize("failure, response, may_exist, interrupted", [
    (OSError("unpublished daemon detail"), None, True, False),
    (_APIError(502, "unpublished daemon detail"), None, True, False),
    (KeyboardInterrupt("unpublished daemon detail"), None, True, True),
    (None, {}, True, False),
    (_APIError(404, "No such image"), None, False, False),
])
def test_create_never_retries_and_only_flags_ambiguous_responses(sdk, failure, response, may_exist, interrupted):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.create_container.side_effect = failure
    client.api.create_container.return_value = response
    with pytest.raises(ProviderError) as error:
        SPEC.factory().create(ttl=30, labels={}, options={}, timeout_s=150)
    assert not error.value.sandbox_id and error.value.sandbox_may_exist is may_exist
    assert error.value.interrupted is interrupted
    assert error.value.__context__ is error.value.__cause__ is None
    assert client.api.create_container.call_count == 1
    client.api.start.assert_not_called()


@pytest.mark.parametrize("status, headers, error_type, retry_after", [
    (401, {}, AuthError, None),
    (403, {}, AuthError, None),
    (429, {"Retry-After": "7"}, RateLimitError, 7),
    (429, {"Retry-After": "not a delay"}, RateLimitError, None),
])
def test_explicit_daemon_auth_and_rate_errors_are_mapped_without_retry(sdk, status, headers, error_type, retry_after):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.create_container.side_effect = _APIError(status, "unpublished daemon detail", headers)
    with pytest.raises(error_type, match=f"HTTP {status}") as error:
        SPEC.factory().create(ttl=30, labels={}, options={}, timeout_s=150)
    assert getattr(error.value, "retry_after", None) == retry_after
    assert error.value.__context__ is error.value.__cause__ is None
    assert client.api.create_container.call_count == 1
    client.api.start.assert_not_called()


@pytest.mark.parametrize("code, output", [(7, (b"out\xff", b"err")), (137, (b"", b"err")), (None, (None, None))])
def test_exec_passes_literal_argv_and_returns_demultiplexed_bytes_without_state(sdk, code, output):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_start.return_value = output
    client.api.exec_inspect.return_value = {"ExitCode": code}
    argv = ("program", "two words", "$HOME; *", "'", "")
    assert SPEC.factory().exec("box-id", argv, cwd="/tmp/anycompute", timeout_s=150) == RawExecResult(
        output[0] or b"", output[1] or b"", code,
    )
    client.api.exec_create.assert_called_once_with("box-id", list(argv), workdir="/tmp/anycompute", tty=False)
    client.api.exec_start.assert_called_once_with("exec-id", tty=False, demux=True)
    client.api.exec_inspect.assert_called_once_with("exec-id")
    client.api.inspect_container.assert_not_called()


@pytest.mark.parametrize("step, response, expected", [
    ("exec_create", _APIError(409, "Container box-id is not running"), RawExecResult(b"", b"", None)),
    ("exec_start", _APIError(404, "No such container: box-id"), RawExecResult(b"", b"", None)),
    ("exec_inspect", _APIError(404, "No such exec instance: exec-id"), RawExecResult(b"out", b"err", None)),
    ("exec_create", _APIError(409, "Container box-id is paused, unpause the container before exec"), None),
    ("exec_start", OSError("unpublished daemon detail"), None),
])
def test_exec_preserves_output_if_the_exec_instance_disappears_but_not_other_errors(sdk, step, response, expected):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_start.return_value = (b"out", b"err")
    action = getattr(client.api, step)
    action.side_effect = response
    adapter = SPEC.factory()
    if expected is None:
        with pytest.raises(ProviderError, match="Docker exec failed") as error:
            adapter.exec("box-id", ["program"], cwd="/", timeout_s=150)
        assert error.value.__context__ is error.value.__cause__ is None
    else:
        assert adapter.exec("box-id", ["program"], cwd="/", timeout_s=150) == expected
    assert action.call_count == 1
    client.api.inspect_container.assert_not_called()


def test_engine_owns_the_docker_exec_expiry_check_and_confirm(sdk, registered):
    _, client = sdk
    client.api.create_container.return_value = {"Id": "box-id"}
    client.api.exec_create.side_effect = [{"Id": "mkdir-id"}, {"Id": "exec-id"}]
    client.api.exec_start.side_effect = [(None, None), (b"out\xff", b"err")]
    client.api.exec_inspect.side_effect = [{"ExitCode": 0}, _APIError(404, "No such exec instance: exec-id")]
    client.api.inspect_container.side_effect = [
        {"State": {"Status": "removing"}}, _APIError(404, "No such container: box-id"),
    ]
    with pytest.raises(SandboxTimeoutError) as error:
        registered.run(["program"], provider="docker", ttl=30, run_id="docker-test")
    assert (error.value.stdout, error.value.stderr, error.value.sandbox_id) == ("out\ufffd", "err", "box-id")
    assert [method for method, *_ in client.api.method_calls][-3:] == [
        "inspect_container", "remove_container", "inspect_container",
    ]


@pytest.mark.parametrize("exit_code, elapsed, stops, rechecks", [
    pytest.param(137, 30, True, True, id="killed-at-ttl"),
    pytest.param(None, 30, True, True, id="missing-exit-at-ttl"),
    pytest.param(137, 29, False, False, id="early-command-exit"),
    pytest.param(137, 30, False, True, id="running-after-recheck"),
])
def test_docker_ambiguous_exit_rechecks_running_state_at_ttl(sdk, registered, monkeypatch,
                                                           exit_code, elapsed, stops, rechecks):
    _, client = sdk
    clock = FakeClock()
    monkeypatch.setattr(registered, "_clock", clock)
    client.api.create_container.return_value = {"Id": "box-id"}
    client.api.start.side_effect = lambda _: clock.sleep(2)
    client.api.exec_create.side_effect = [{"Id": "mkdir-id"}, {"Id": "exec-id"}]
    client.api.exec_inspect.side_effect = [{"ExitCode": 0}, {"ExitCode": exit_code}]

    def start_exec(exec_id, **kwargs):
        if exec_id == "mkdir-id":
            return None, None
        clock.sleep(elapsed - clock.now())
        return b"part\xff", b"diagnostic"

    def state(sandbox_id):
        if client.api.remove_container.called:
            raise _APIError(404, f"No such container: {sandbox_id}")
        return {"State": {"Status": "removing" if stops and clock.now() >= 31 else "running"}}

    client.api.exec_start.side_effect = start_exec
    client.api.inspect_container.side_effect = state
    options = {"provider": "docker", "ttl": 30, "run_id": "docker-test"}
    if stops:
        with pytest.raises(SandboxTimeoutError) as caught:
            registered.run(["program"], **options)
        result = caught.value
        assert result.exit_code == 123
    else:
        result = registered.run(["program"], **options)
        assert result.exit_code == 137
    assert (result.stdout, result.stderr, result.sandbox_id, result.run_id) == (
        "part\ufffd", "diagnostic", "box-id", "docker-test",
    )
    assert clock.now() == elapsed + int(rechecks)
    assert [method for method, *_ in client.api.method_calls[-(3 + int(rechecks)):]] == (
        ["inspect_container"] * (1 + int(rechecks)) + ["remove_container", "inspect_container"]
    )
    assert client.api.exec_start.call_count == 2
    client.api.remove_container.assert_called_once_with("box-id", force=True)


def test_upload_archives_only_the_basename_and_preserves_binary_bytes(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.put_archive.return_value = True
    data = b"\x00\xffbinary"
    SPEC.factory().write("box-id", "/tmp/anycompute/sub/f name", data, timeout_s=150)
    args, kwargs = client.api.put_archive.call_args
    assert args[:2] == ("box-id", "/tmp/anycompute/sub") and not kwargs
    with tarfile.open(fileobj=io.BytesIO(args[2]), mode="r:") as archive:
        [member] = archive.getmembers()
        assert member.name == "f name" and member.isreg() and member.size == len(data)
        source = archive.extractfile(member)
        assert source is not None and source.read() == data


def test_write_does_not_archive_a_directory_name(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    with pytest.raises(InvalidPathError):
        SPEC.factory().write("box-id", "/tmp/anycompute/.", b"", timeout_s=150)
    client.api.put_archive.assert_not_called()


def test_write_reports_a_refused_archive(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.put_archive.return_value = False
    with pytest.raises(ProviderError, match="archive"):
        SPEC.factory().write("box-id", "/tmp/anycompute/in", b"", timeout_s=150)


def _archive(name, kind, data):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind == tarfile.REGTYPE:
            member.size = len(data)
        if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            member.linkname = "../elsewhere"
        archive.addfile(member, io.BytesIO(data))
    payload = buffer.getvalue()
    yield payload[:381]
    yield payload[381:]


@pytest.mark.parametrize("name, kind, expected", [
    ("out", tarfile.REGTYPE, b"\x00\xffbinary"),
    ("out", tarfile.DIRTYPE, None),
    ("out", tarfile.SYMTYPE, None),
    ("out", tarfile.LNKTYPE, None),
    ("out", tarfile.FIFOTYPE, None),
    ("../out", tarfile.REGTYPE, None),
    ("/out", tarfile.REGTYPE, None),
    ("elsewhere/out", tarfile.REGTYPE, None),
])
def test_read_only_returns_the_exact_regular_archive_member_in_memory(sdk, monkeypatch, name, kind, expected):
    from anycompute.providers.docker import SPEC

    def no_host_extraction(*args, **kwargs):
        pytest.fail("archive must not be extracted to the host")

    monkeypatch.setattr(tarfile.TarFile, "extract", no_host_extraction)
    monkeypatch.setattr(tarfile.TarFile, "extractall", no_host_extraction)
    if expected is None:
        monkeypatch.setattr(tarfile.TarFile, "extractfile", no_host_extraction)
    _, client = sdk
    client.api.get_archive.return_value = (_archive(name, kind, b"\x00\xffbinary"), None)
    assert SPEC.factory().read("box-id", "/tmp/anycompute/sub/out", timeout_s=150) == expected
    client.api.get_archive.assert_called_once_with("box-id", "/tmp/anycompute/sub/out")


@pytest.mark.parametrize("mode", [1 << 31, 1 << 27, 1 << 25], ids=["directory", "symlink", "fifo"])
def test_read_skips_a_known_non_regular_path_without_buffering_its_archive(sdk, mode):
    from anycompute.providers.docker import SPEC

    def no_body():
        pytest.fail("non-regular archive body was read")
        yield b""

    _, client = sdk
    client.api.get_archive.return_value = (no_body(), {"mode": mode | 0o755})
    assert SPEC.factory().read("box-id", "/tmp/anycompute/sub/out", timeout_s=150) is None


def test_read_maps_a_missing_file_but_not_a_missing_container(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.get_archive.side_effect = [
        _APIError(404, "Could not find the file /tmp/anycompute/out in container box-id"),
        _APIError(404, "No such container: box-id"),
    ]
    adapter = SPEC.factory()
    assert adapter.read("box-id", "/tmp/anycompute/out", timeout_s=150) is None
    with pytest.raises(ProviderError, match="HTTP 404"):
        adapter.read("box-id", "/tmp/anycompute/out", timeout_s=150)


def test_read_reports_a_malformed_archive_without_raw_error_context(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.get_archive.return_value = ((chunk for chunk in (b"not a tar",)), {"mode": 0o644})
    with pytest.raises(ProviderError, match="archive") as error:
        SPEC.factory().read("box-id", "/tmp/anycompute/out", timeout_s=150)
    assert error.value.__context__ is error.value.__cause__ is None


def test_list_filters_labels_on_the_daemon_and_keeps_create_times_and_stopped_containers(sdk):
    from anycompute.providers.docker import SPEC

    module, client = sdk
    selector = {"anycompute": "1", "anycompute-run": "docker-test"}
    labels = {**selector, "anycompute-ttl": "30"}
    client.api.containers.return_value = [
        {"Id": "created-id", "State": "created", "Created": 1_000_000, "Labels": labels},
        {"Id": "exited-id", "State": "exited", "Labels": labels},
    ]
    assert SPEC.factory().list(selector, timeout_s=120) == [
        SandboxInfo("created-id", labels, 1_000_000), SandboxInfo("exited-id", labels, None),
    ]
    module.from_env.assert_called_once_with(timeout=None, use_context=True)
    client.api.containers.assert_called_once_with(
        all=True, filters={"label": ["anycompute=1", "anycompute-run=docker-test"]},
    )
    client.api.pull.assert_not_called()
    client.api.inspect_container.assert_not_called()


def test_native_returns_the_sdk_container(sdk):
    from anycompute.providers.docker import SPEC

    _, client = sdk
    assert SPEC.factory().native("box-id", timeout_s=120) is client.containers.get.return_value
    client.containers.get.assert_called_once_with("box-id")


@pytest.mark.parametrize("base_url, host", [
    ("http+docker://localhost", "local"),
    ("http+docker://localnpipe", "local"),
    ("https://engine.invalid:2376", "https://engine.invalid"),
    ("http+docker://ssh", "ssh://engine.invalid"),
])
def test_runtime_host_reports_the_used_daemon_or_local(sdk, base_url, host):
    from anycompute import engine
    from anycompute.providers.docker import SPEC

    _, client = sdk
    client.api.base_url = base_url
    client.api.get_adapter.return_value.ssh_host = "daemon-user@engine.invalid:22"
    assert engine._runtime_host(SPEC.factory().api_host(timeout_s=120)) == host


@pytest.fixture
def transport(monkeypatch, tmp_path):
    pytest.importorskip("docker", reason="SDK transport checks require the docker extra")
    from docker.context import ContextAPI
    from urllib3.connectionpool import HTTPConnectionPool
    from urllib3.response import HTTPResponse

    def no_network(*args, **kwargs):
        pytest.fail("transport fake attempted socket access")

    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    (tmp_path / "config.json").write_text('{"currentContext":"test-context"}', encoding="utf-8")
    ContextAPI.create_context("test-context", host="tcp://context.invalid:2375")

    calls, replies = [], {"/version": (200, {"ApiVersion": "1.47"}, {})}

    def make_request(pool, connection, method, url, *args, **kwargs):
        path = urlsplit(url).path
        if path != "/version":
            assert kwargs["headers"]["User-Agent"] == f"anycompute/{version('anycompute')}"
        calls.append((pool.host, method, path, kwargs["timeout"].read_timeout))
        result = replies.get(path, (500, {"message": "unexpected test route"}, {}))
        if isinstance(result, Exception):
            raise result
        status, payload, headers = result
        raw = json.dumps(payload).encode()
        return HTTPResponse(body=io.BytesIO(raw), status=status, preload_content=False, headers={
            "Content-Type": "application/json", "Content-Length": str(len(raw)), **headers,
        })

    monkeypatch.setattr(HTTPConnectionPool, "_make_request", make_request)
    return calls, replies


@pytest.mark.parametrize("override, daemon", [(None, "context.invalid"), ("env.invalid", "env.invalid")])
def test_sdk_version_and_list_have_no_read_timeout_and_use_the_correct_daemon(transport, monkeypatch, override, daemon):
    from anycompute.providers.docker import SPEC

    calls, replies = transport
    replies["/v1.47/containers/json"] = (200, [], {})
    if override:
        monkeypatch.setenv("DOCKER_HOST", f"tcp://{override}:2375")
    assert SPEC.factory().list({"anycompute": "1"}, timeout_s=120) == []
    assert calls == [(daemon, "GET", "/version", None), (daemon, "GET", "/v1.47/containers/json", None)]


@pytest.mark.parametrize("failure", ["lost", "redirect"])
@pytest.mark.parametrize("operation, path", [
    ("create", "/v1.47/containers/create"), ("exec-create", "/v1.47/containers/box-id/exec"),
    ("exec-start", "/v1.47/exec/exec-id/start"),
])
def test_sdk_transport_never_replays_create_or_exec(transport, operation, path, failure):
    from urllib3.exceptions import ProtocolError

    from anycompute.providers.docker import SPEC

    calls, replies = transport
    expected = [("GET", "/version")]
    if operation == "exec-start":
        replies["/v1.47/containers/box-id/exec"] = (201, {"Id": "exec-id"}, {})
        expected.append(("POST", "/v1.47/containers/box-id/exec"))
    replies[path] = (ProtocolError("unpublished daemon detail") if failure == "lost" else
                     (307, {}, {"Location": "/must-not-replay"}))
    adapter = SPEC.factory()
    with pytest.raises(ProviderError) as error:
        if operation == "create":
            adapter.create(ttl=30, labels={}, options={}, timeout_s=150)
        else:
            adapter.exec("box-id", ["program"], cwd="/", timeout_s=150)
    assert [(method, route) for _, method, route, _ in calls] == [*expected, ("POST", path)]
    assert all(read_timeout is None for *_, read_timeout in calls)
    assert error.value.sandbox_may_exist is (operation == "create")
    assert error.value.__context__ is error.value.__cause__ is None

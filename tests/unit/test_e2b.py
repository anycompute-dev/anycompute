import json
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from anycompute import AuthError, ConfigError, ProviderError, QuotaError, RateLimitError
from anycompute.contract import RawExecResult, SandboxInfo, SandboxState
from anycompute.providers import e2b


LABELS = {"anycompute": "1", "anycompute-run": "demo", "anycompute-ttl": "60"}
USER_AGENT = f"anycompute/{version('anycompute')}"
SDK_OPTIONS = {"debug": False, "retries": 0, "request_timeout": 120, "api_headers": {"User-Agent": USER_AGENT}}


class ServiceError(Exception):
    def __init__(self, message="", status_code=None):
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture
def sdk(monkeypatch):
    sandbox = Mock(sandbox_id="sb-one")
    sdk = SimpleNamespace(
        Sandbox=SimpleNamespace(create=Mock(return_value=sandbox), kill=Mock(), list=Mock(), get_info=Mock()),
        SandboxQuery=lambda **values: values, FileType=SimpleNamespace(FILE="file"),
        SandboxException=ServiceError,
    )
    for name in ("AuthenticationException", "RateLimitException", "CommandExitException", "TimeoutException",
                 "SandboxNotFoundException", "SandboxNotRunningException", "FileNotFoundException",
                 "ServiceBusyException"):
        setattr(sdk, name, type(name, (ServiceError,), {}))
    monkeypatch.setattr(e2b, "_sdk", lambda: (sdk, OSError))
    return sdk


def test_create_uses_provider_ttl_and_tags_and_leaves_live_disabled(sdk):
    adapter = e2b.SPEC.factory()
    assert adapter.create(ttl=60, labels=LABELS, options={}, timeout_s=120) == "sb-one"
    sdk.Sandbox.create.assert_called_once_with(
        template="base", timeout=60, metadata=LABELS, envs=None, lifecycle={"on_timeout": "kill"}, **SDK_OPTIONS,
    )
    assert adapter.native("sb-one", timeout_s=120) is sdk.Sandbox.create.return_value
    assert e2b.SPEC.caps.ttl_max_s == 3600
    assert not e2b.SPEC.caps.live_run


@pytest.mark.parametrize(("name", "status", "text", "mapped"), [
    ("AuthenticationException", 401, "missing credential", AuthError),
    ("RateLimitException", 429, "rate limited", RateLimitError),
    ("SandboxException", 403, "sandbox quota exceeded", QuotaError),
    ("SandboxException", 500, "internal failure", ProviderError),
    ("ServiceBusyException", 503, "busy", ProviderError),
])
def test_create_maps_public_errors_without_exposing_vendor_payload(sdk, name, status, text, mapped):
    sdk.Sandbox.create.side_effect = getattr(sdk, name)(f"{text} secret-response", status)
    with pytest.raises(mapped) as exc:
        e2b.SPEC.factory().create(ttl=60, labels=LABELS, options={}, timeout_s=120)
    assert "secret-response" not in str(exc.value)
    sdk.Sandbox.create.assert_called_once()


@pytest.mark.parametrize("status", [0, 7, None])
def test_exec_preserves_streams_and_quotes_every_argument(sdk, status):
    adapter = e2b.SPEC.factory()
    adapter.create(ttl=60, labels=LABELS, options={}, timeout_s=120)
    run = sdk.Sandbox.create.return_value.commands.run

    def emit(command, *, cwd, shell, timeout, retain_output, on_stdout, on_stderr):
        assert command == "printf %s 'two words' '$HOME' ''"
        assert (cwd, shell, timeout, retain_output) == ("/tmp/anycompute", ("/bin/sh", "-c"), 0, False)
        on_stdout("out\ufffd")
        on_stderr("err")
        if status is None:
            raise sdk.TimeoutException()
        if status == 7:
            error = sdk.CommandExitException()
            error.exit_code = status
            raise error
        return SimpleNamespace(exit_code=status)

    run.side_effect = emit
    assert adapter.exec("sb-one", ("printf", "%s", "two words", "$HOME", ""), cwd="/tmp/anycompute", timeout_s=120) == (
        RawExecResult("out\ufffd", "err", status)
    )
    run.assert_called_once()


@pytest.mark.parametrize(("state", "expected"), [
    ("running", SandboxState.RUNNING), ("paused", SandboxState.STOPPED), ("starting", SandboxState.UNKNOWN),
    (None, SandboxState.GONE),
])
def test_state_only_confirms_control_plane_not_found(sdk, state, expected):
    if state is None:
        sdk.Sandbox.get_info.side_effect = sdk.SandboxNotFoundException()
    else:
        sdk.Sandbox.get_info.return_value = SimpleNamespace(state=state)
    adapter = e2b.SPEC.factory()
    assert adapter.state("sb-one", timeout_s=120) is expected
    adapter.delete("sb-one", timeout_s=120)
    sdk.Sandbox.kill.assert_called_once_with("sb-one", **SDK_OPTIONS)


def test_list_drains_every_page_and_filters_actual_metadata(sdk):
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    item = lambda sandbox_id, metadata: SimpleNamespace(sandbox_id=sandbox_id, metadata=metadata, started_at=timestamp)

    class Paginator:
        def __init__(self):
            self.pages = [[item("sb-one", LABELS), item("unowned", {})], [], [item("sb-three", LABELS)]]

        @property
        def has_next(self):
            return bool(self.pages)

        def next_items(self):
            return self.pages.pop(0)

    sdk.Sandbox.list.return_value = Paginator()
    assert e2b.SPEC.factory().list(LABELS, timeout_s=120) == [
        SandboxInfo("sb-one", LABELS, timestamp.timestamp()), SandboxInfo("sb-three", LABELS, timestamp.timestamp()),
    ]
    sdk.Sandbox.list.assert_called_once_with(query={"metadata": LABELS}, **SDK_OPTIONS)


def test_binary_files_skip_nonregular_and_missing_paths(sdk):
    adapter = e2b.SPEC.factory()
    adapter.create(ttl=60, labels=LABELS, options={}, timeout_s=120)
    files = sdk.Sandbox.create.return_value.files
    path = "/tmp/anycompute/data"
    files.get_info.return_value.type = sdk.FileType.FILE
    files.read.return_value = bytearray(b"abc\x00\xff")
    assert adapter.read("sb-one", path, timeout_s=120) == b"abc\x00\xff"
    adapter.write("sb-one", path, b"take", timeout_s=120)
    files.write.assert_called_once_with(path, b"take", request_timeout=120)
    files.get_info.return_value.type = "dir"
    assert adapter.read("sb-one", path, timeout_s=120) is None
    files.get_info.side_effect = sdk.FileNotFoundException
    assert adapter.read("sb-one", path, timeout_s=120) is None
    files.read.assert_called_once_with(path, format="bytes", request_timeout=120)


def test_missing_extra_reports_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "e2b", None)
    with pytest.raises(ConfigError, match=r"anycompute\[e2b\]"):
        e2b.SPEC.factory().state("sandbox", timeout_s=120)


def test_real_sdk_sends_one_create_and_attaches_with_get_only(monkeypatch):
    sdk = pytest.importorskip("e2b")
    import httpx
    import e2b.api.client_sync as clients
    import e2b.sandbox_sync.sandbox_api as sandbox_api
    from e2b.retry import RetryableTransport

    responses = [503, 200]
    fixture_value = "unit-fixture"
    calls = []

    def transport(request):
        assert request.headers["User-Agent"] == USER_AGENT
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            payload = json.loads(request.content)
            assert (payload["timeout"], payload["metadata"], payload["autoPause"]) == (60, LABELS, False)
            return httpx.Response(responses.pop(0), json={"code": 503, "message": "unavailable"})
        return httpx.Response(responses.pop(0), json={
            "sandboxID": "sb-one", "templateID": "base", "clientID": "unit", "envdVersion": "0.10.0",
            "startedAt": "2026-01-02T00:00:00Z", "endAt": "2026-01-02T00:01:00Z",
            "cpuCount": 2, "memoryMB": 512, "diskSizeMB": 100, "state": "running",
            "envdAccessToken": fixture_value, "domain": "e2b.app",
        })

    def client(config):
        assert config.retries == 0
        return sdk.ApiClient(config, transport=RetryableTransport(httpx.MockTransport(transport), config.retries))

    monkeypatch.setenv("E2B_API_KEY", fixture_value)
    monkeypatch.setattr(clients, "get_api_client", client)
    monkeypatch.setattr(sandbox_api, "get_api_client", client)
    adapter = e2b.SPEC.factory()
    with pytest.raises(ProviderError):
        adapter.create(ttl=60, labels=LABELS, options={}, timeout_s=120)
    sandbox = adapter.native("sb-one", timeout_s=120)
    assert sandbox.sandbox_id == "sb-one"
    assert sandbox.connection_config.sandbox_headers["User-Agent"] == USER_AGENT
    assert calls == [("POST", "/v2/sandboxes"), ("GET", "/sandboxes/sb-one")]

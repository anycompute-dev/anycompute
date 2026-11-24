import json
import sys
from datetime import UTC, datetime
from shlex import split
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from anycompute import AuthError, ConfigError, ProviderError, QuotaError, RateLimitError
from anycompute.contract import RawExecResult, SandboxInfo, SandboxState
from anycompute.providers import daytona


LABELS = {"anycompute": "1", "anycompute-run": "demo", "anycompute-ttl": "120"}


class ServiceError(Exception):
    def __init__(self, message="", status_code=None, headers=None, code=None, sandbox_id=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}
        self.code = code
        self.sandbox_id = sandbox_id


@pytest.fixture
def sdk(monkeypatch):
    client = Mock()
    client.create.return_value.id = "sb-one"
    sdk = SimpleNamespace(Daytona=Mock(return_value=SimpleNamespace(v0=client)))
    v0 = SimpleNamespace(DaytonaError=ServiceError, SessionExecuteRequest=SimpleNamespace,
                         ListSandboxesQuery=SimpleNamespace)
    for name in ("DaytonaAuthenticationError", "DaytonaRateLimitError", "DaytonaNotFoundError", "DaytonaGoneError",
                 "DaytonaTimeoutError"):
        setattr(v0, name, type(name, (ServiceError,), {}))
    monkeypatch.setattr(daytona, "_sdk", lambda: (sdk, v0, SimpleNamespace))
    monkeypatch.setattr(daytona, "_root", None)
    return sdk, v0, client


def test_create_uses_ephemeral_whole_minute_lifetime_and_labels(sdk):
    root, _, client = sdk
    adapter = daytona.SPEC.factory()
    assert adapter.create(ttl=120, labels=LABELS, options={}, timeout_s=240) == "sb-one"
    client.create.assert_called_once_with(SimpleNamespace(
        snapshot="daytona-small", env_vars=None, ephemeral=True, ttl_minutes=2, auto_stop_interval=2, labels=LABELS,
    ), timeout=240)
    assert adapter.native("sb-one", timeout_s=120) is client.create.return_value
    root.Daytona.assert_called_once()
    client.get.assert_not_called()
    assert daytona.SPEC.caps.ttl_unit_s == 60
    assert daytona.SPEC.caps.lifetime == "partial"
    assert not daytona.SPEC.caps.live_run


@pytest.mark.parametrize(("status", "code", "kind"), [
    (403, "permission_denied", AuthError), (403, "quota_exceeded", QuotaError),
    (429, "too_many_requests", RateLimitError), (503, None, ProviderError),
])
def test_create_maps_vendor_status_without_exposing_payload(sdk, status, code, kind):
    _, v0, client = sdk
    client.create.side_effect = v0.DaytonaError("secret-response", status, {"Retry-After": "3"}, code)
    with pytest.raises(kind) as exc:
        daytona.SPEC.factory().create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    assert "secret-response" not in str(exc.value)
    assert getattr(exc.value, "retry_after", None) == (3 if kind is RateLimitError else None)
    client.create.assert_called_once()


def test_create_timeout_keeps_known_id_for_engine_cleanup(sdk):
    _, v0, client = sdk
    client.create.side_effect = v0.DaytonaTimeoutError(sandbox_id="sb-maybe")
    with pytest.raises(ProviderError) as exc:
        daytona.SPEC.factory().create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    assert exc.value.sandbox_id == "sb-maybe" and exc.value.sandbox_may_exist


def test_session_exec_uses_posix_arguments_and_preserves_two_streams(sdk):
    _, v0, client = sdk
    adapter = daytona.SPEC.factory()
    adapter.create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    process = client.create.return_value.process
    process.execute_session_command.return_value = SimpleNamespace(stdout="out\ufffd", stderr="err", exit_code=7)
    argv = ("printf", "%s", "two words", "$HOME", "'", "")
    for _ in range(2):
        assert adapter.exec("sb-one", argv, cwd="/path with spaces", timeout_s=179.5) == RawExecResult("out\ufffd", "err", 7)
    process.create_session.assert_called_once()
    ((session, params), options) = process.execute_session_command.call_args
    shell, flag, command = split(params.command)
    assert (shell, flag, split(command)) == ("/bin/sh", "-c", ["cd", "/path with spaces", "&&", *argv])
    assert (params.run_async, params.suppress_input_echo, options) == (False, True, {"timeout": 180})
    assert session == process.create_session.call_args.args[0]
    process.execute_session_command.side_effect = v0.DaytonaGoneError
    assert adapter.exec("sb-one", argv, cwd="/", timeout_s=120).exit_code is None


@pytest.mark.parametrize(("state", "expected"), [
    ("started", SandboxState.RUNNING), ("stopped", SandboxState.STOPPED), ("paused", SandboxState.STOPPED),
    ("archived", SandboxState.STOPPED), ("destroyed", SandboxState.GONE), ("destroying", SandboxState.UNKNOWN),
    ("error", SandboxState.UNKNOWN), (None, SandboxState.GONE),
])
def test_state_checks_direct_lookup_and_never_counts_destroying_as_deleted(sdk, state, expected):
    _, v0, client = sdk
    if state is None:
        client.get.side_effect = v0.DaytonaNotFoundError
    else:
        client.get.return_value.state = SimpleNamespace(value=state)
    adapter = daytona.SPEC.factory()
    assert adapter.state("sb-one", timeout_s=120) is expected
    adapter.delete("sb-one", timeout_s=120)
    if state is None:
        client.delete.assert_not_called()
    else:
        client.delete.assert_called_once_with(client.get.return_value, timeout=120, wait=False)
    client.list.assert_not_called()


def test_list_uses_all_iterator_pages_and_keeps_only_matched_labels(sdk):
    _, _, client = sdk
    rows = [
        SimpleNamespace(id="sb-one", labels=LABELS, created_at="2026-01-02T00:00:00Z"),
        SimpleNamespace(id="unowned", labels={}, created_at=None),
        SimpleNamespace(id="sb-two", labels=LABELS, created_at="malformed"),
    ]
    client.list.return_value = iter(rows)
    assert daytona.SPEC.factory().list(LABELS, timeout_s=120) == [
        SandboxInfo("sb-one", LABELS, datetime(2026, 1, 2, tzinfo=UTC).timestamp()),
        SandboxInfo("sb-two", LABELS, None),
    ]
    client.list.assert_called_once_with(SimpleNamespace(labels=LABELS), request_timeout=120)


def test_files_keep_binary_content_and_skip_nonregular_paths(sdk):
    _, v0, client = sdk
    files = client.get.return_value.fs
    files.get_file_info.return_value = SimpleNamespace(is_dir=False, mode="-rw-r--r--")
    files.download_file.return_value = b"\x00\xff"
    adapter = daytona.SPEC.factory()
    path = "/tmp/anycompute/data"
    adapter.write("sb-one", path, b"\x00\xff", timeout_s=120)
    files.upload_file.assert_called_once_with(b"\x00\xff", path, timeout=120)
    assert adapter.read("sb-one", path, timeout_s=120) == b"\x00\xff"
    files.get_file_info.return_value.mode = "Lrwxrwxrwx"
    assert adapter.read("sb-one", path, timeout_s=120) is None
    files.get_file_info.side_effect = v0.DaytonaNotFoundError
    assert adapter.read("sb-one", path, timeout_s=120) is None
    files.download_file.assert_called_once_with(path, 120)


def test_missing_extra_reports_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "daytona", None)
    with pytest.raises(ConfigError, match=r"anycompute\[daytona\]"):
        daytona.SPEC.factory().state("sandbox", timeout_s=120)


def test_real_sdk_serializes_ttl_and_reports_one_failed_post(monkeypatch):
    pytest.importorskip("daytona.v0.common.daytona")
    import urllib3
    from daytona.v0._sync import daytona as v0_client

    observed = []

    def request(self, method, url, *, body, **kwargs):
        observed.append((method, urlsplit(url).path, json.loads(body)))
        return urllib3.response.HTTPResponse(
            body=b'{"code":"QUOTA_EXCEEDED","message":"quota exceeded"}', status=403,
            headers={"Content-Type": "application/json"}, preload_content=True,
        )

    monkeypatch.setattr(v0_client.SyncEventDispatcher, "ensure_connected", lambda self: None)
    monkeypatch.setattr(urllib3.PoolManager, "request", request)
    monkeypatch.setenv("DAYTONA_API_KEY", "unit-fixture")
    monkeypatch.setattr(daytona, "_root", None)
    with pytest.raises(QuotaError):
        daytona.SPEC.factory().create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    assert len(observed) == 1
    method, path, payload = observed[0]
    assert (method, path) == ("POST", "/api/sandbox")
    assert (payload["snapshot"], payload["ttlMinutes"], payload["autoStopInterval"], payload["autoDeleteInterval"]) == (
        "daytona-small", 2, 2, 0,
    )
    assert {key: payload["labels"][key] for key in LABELS} == LABELS

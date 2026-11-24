import asyncio
import json
import os
import subprocess
import sys
from importlib.util import find_spec
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlunsplit

import pytest

from anycompute import AuthError, ConfigError, ProviderError, QuotaError, RateLimitError, SandboxTimeoutError, engine
from anycompute.contract import RawExecResult, SandboxInfo, SandboxState
from anycompute.providers import modal


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.delenv("ANYCOMPUTE_CONFORMANCE_LIVE", raising=False)
    error = type("Error", (Exception,), {})
    subclasses = ("AuthError", "PermissionDeniedError", "ExecTimeoutError", "SandboxTimeoutError",
                  "SandboxTerminatedError", "ResourceExhaustedError", "InvalidError", "NotFoundError", "ServiceError",
                  "SandboxFilesystemNotFoundError", "SandboxFilesystemIsADirectoryError",
                  "SandboxFilesystemNotADirectoryError")
    errors = SimpleNamespace(Error=error, **{name: type(name, (error,), {}) for name in subclasses})
    client = SimpleNamespace(
        __version__="1.6.4",
        exception=errors,
        App=SimpleNamespace(lookup=Mock(return_value=SimpleNamespace(app_id="ap-anycompute"))),
        Sandbox=SimpleNamespace(create=Mock(return_value=SimpleNamespace(object_id="sb-one")), from_id=Mock(),
                                list=Mock()),
        Image=SimpleNamespace(from_registry=Mock(), from_name=Mock()),
        config=SimpleNamespace(config={"server_url": "https://api.modal.com,https://api.modal2.com"}),
    )
    monkeypatch.setattr(modal, "_sdk", lambda: client)
    monkeypatch.setattr(modal, "_app", None)
    return client


LABELS = {"anycompute": "1", "anycompute-run": "demo", "anycompute-ttl": "90"}


def test_create_uses_provider_ttl_and_tags_on_request_and_reuses_app(sdk):
    for _ in range(2):
        assert modal._Modal().create(ttl=90, labels=LABELS, options={
            "image": "debian:12-slim", "environment": {"LANG": "C.UTF-8"},
        }, timeout_s=210) == "sb-one"
    sdk.App.lookup.assert_called_once_with("anycompute", create_if_missing=True)
    sdk.Image.from_registry.assert_called_with("debian:12-slim")
    sdk.Sandbox.create.assert_called_with("sleep", "infinity", app=sdk.App.lookup.return_value, tags=LABELS,
                                          timeout=90, runtime="gvisor", image=sdk.Image.from_registry.return_value,
                                          env={"LANG": "C.UTF-8"})
    assert sdk.Sandbox.create.call_count == 2


def test_conformance_prepares_named_image_and_caps_resources(sdk, monkeypatch):
    monkeypatch.setenv("ANYCOMPUTE_CONFORMANCE_LIVE", "1")
    sdk.config.config["max_throttle_wait"] = 0
    adapter = modal._Modal()
    adapter.prepare(options={}, timeout_s=210)
    resolved = sdk.Image.from_name.return_value.hydrate
    sdk.Image.from_name.assert_called_once_with("anycompute-conformance:v1")
    resolved.assert_called_once_with()
    sdk.App.lookup.assert_called_once_with("anycompute", create_if_missing=True)
    sdk.Sandbox.create.assert_not_called()

    assert adapter.create(ttl=90, labels=LABELS, options={}, timeout_s=210) == "sb-one"
    sdk.Sandbox.create.assert_called_once_with(
        "sleep", "infinity", app=sdk.App.lookup.return_value, tags=LABELS, timeout=90, runtime="gvisor",
        image=resolved.return_value, cpu=(0.125, 1), memory=(128, 512), block_network=True,
    )
    assert sdk.Image.from_name.call_count == resolved.call_count == sdk.App.lookup.call_count == 1
    sdk.Image.from_registry.assert_not_called()


def test_conformance_create_requires_successful_preparation(sdk, monkeypatch):
    monkeypatch.setenv("ANYCOMPUTE_CONFORMANCE_LIVE", "1")
    sdk.config.config["max_throttle_wait"] = 0
    adapter = modal._Modal()
    with pytest.raises(ConfigError, match="prepared image and app"):
        adapter.create(ttl=90, labels=LABELS, options={}, timeout_s=210)
    sdk.Image.from_name.return_value.hydrate.side_effect = sdk.exception.NotFoundError("private response")
    with pytest.raises(ProviderError, match="prepare conformance image and app") as error:
        adapter.prepare(options={}, timeout_s=210)
    assert not error.value.sandbox_may_exist and "private response" not in str(error.value)
    with pytest.raises(ConfigError, match="prepared image and app"):
        adapter.create(ttl=90, labels=LABELS, options={}, timeout_s=210)
    sdk.App.lookup.assert_not_called()
    sdk.Sandbox.create.assert_not_called()


def test_conformance_create_rechecks_ttl_and_sdk_settings_after_preparation(sdk, monkeypatch):
    monkeypatch.setenv("ANYCOMPUTE_CONFORMANCE_LIVE", "1")
    sdk.config.config["max_throttle_wait"] = 0
    adapter = modal._Modal()
    adapter.prepare(options={}, timeout_s=210)
    with pytest.raises(ConfigError, match="TTL between 1 and 120"):
        adapter.create(ttl=121, labels=LABELS, options={}, timeout_s=210)
    sdk.config.config["max_throttle_wait"] = 1
    with pytest.raises(ConfigError, match="requires MODAL_MAX_THROTTLE_WAIT=0"):
        adapter.create(ttl=90, labels=LABELS, options={}, timeout_s=210)
    sdk.Sandbox.create.assert_not_called()


def test_byok_prepare_is_a_noop(sdk):
    modal._Modal().prepare(options={"image": "debian:12-slim"}, timeout_s=210)
    sdk.Image.from_name.assert_not_called()
    sdk.Image.from_registry.assert_not_called()
    sdk.App.lookup.assert_not_called()


@pytest.mark.parametrize(("version", "throttle_wait", "options", "reason"), [
    ("1.6.5", 0, {}, "requires SDK 1.6.4"),
    ("1.6.4", None, {}, "requires MODAL_MAX_THROTTLE_WAIT=0"),
    ("1.6.4", 1, {}, "requires MODAL_MAX_THROTTLE_WAIT=0"),
    ("1.6.4", "0", {}, "requires MODAL_MAX_THROTTLE_WAIT=0"),
    ("1.6.4", 0, {"environment": {}}, "requires default provider options"),
    ("1.6.4", 0, {"image": "debian:12-slim"}, "requires default provider options"),
])
def test_unbounded_conformance_prepare_stops_before_metadata(sdk, monkeypatch, version, throttle_wait, options, reason):
    monkeypatch.setenv("ANYCOMPUTE_CONFORMANCE_LIVE", "1")
    sdk.__version__ = version
    if throttle_wait is not None:
        sdk.config.config["max_throttle_wait"] = throttle_wait
    with pytest.raises(ConfigError, match=reason):
        modal._Modal().prepare(options=options, timeout_s=210)
    sdk.Image.from_name.assert_not_called()
    sdk.App.lookup.assert_not_called()
    sdk.Sandbox.create.assert_not_called()


def test_locked_modal_sdk_retry_bound_with_throttle_retries_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(tmp_path / "no-modal-config"))
    monkeypatch.setenv("MODAL_MAX_THROTTLE_WAIT", "0")
    if find_spec("modal") is None:
        pytest.importorskip("modal")
    import modal as sdk
    from grpclib import GRPCError, Status
    from modal._grpc_client import UnaryUnaryWrapper
    from modal._utils import grpc_utils

    assert sdk.__version__ == modal._CONFORMANCE_SDK_VERSION
    assert sdk.config.config.get("max_throttle_wait") == 0

    class CreateStub(UnaryUnaryWrapper):
        name = "SandboxCreateV2"

        def __init__(self, status):
            self.status = status
            self.calls = []

        async def direct(self, request, *, timeout, metadata):
            self.calls.append(dict(metadata))
            raise GRPCError(self.status, "synthetic")

    def unexpected_throttle_policy(_):
        pytest.fail("Disabled throttle retries must not inspect server retry policies")

    monkeypatch.setattr(grpc_utils, "get_server_retry_policy", unexpected_throttle_policy)
    retry = grpc_utils.Retry(base_delay=0)

    async def check():
        for status, attempts in ((Status.INTERNAL, 4), (Status.RESOURCE_EXHAUSTED, 1)):
            stub = CreateStub(status)
            with pytest.raises(GRPCError):
                await grpc_utils._retry_transient_errors(stub, None, retry=retry, metadata=None)
            assert len(stub.calls) == attempts
            assert len({call["x-idempotency-key"] for call in stub.calls}) == 1

    asyncio.run(check())


def test_invalid_image_fails_before_looking_up_app(sdk):
    with pytest.raises(ConfigError, match="image"):
        modal._Modal().create(ttl=90, labels=LABELS, options={"image": " "}, timeout_s=210)
    sdk.App.lookup.assert_not_called()


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "modal", None)
    with pytest.raises(ConfigError, match="modal extra"):
        modal._Modal().native("sb-one", timeout_s=120)


@pytest.mark.parametrize(("sdk_kind", "message", "kind", "may_exist"), [
    ("AuthError", "invalid", AuthError, False), ("PermissionDeniedError", "denied", AuthError, False),
    ("ResourceExhaustedError", "rate limit exceeded", RateLimitError, False),
    ("ResourceExhaustedError", "concurrency quota exceeded", QuotaError, False),
    ("ResourceExhaustedError", "unclassified", ProviderError, False),
    ("InvalidError", "bad arg", ProviderError, False), ("ServiceError", "lost response", ProviderError, True),
])
def test_create_maps_sdk_errors_without_retry_or_raw_message(sdk, sdk_kind, message, kind, may_exist):
    sdk.Sandbox.create.side_effect = getattr(sdk.exception, sdk_kind)(f"{message} private-key")
    with pytest.raises(kind) as raised:
        modal._Modal().create(ttl=90, labels=LABELS, options={}, timeout_s=210)
    assert getattr(raised.value, "sandbox_may_exist", False) is may_exist
    assert "private-key" not in str(raised.value)
    sdk.Sandbox.create.assert_called_once()


@pytest.mark.parametrize(("result", "code"), [(7, 7), (137, 137), (-1, None), ("SandboxTimeoutError", None)])
def test_exec_preserves_literal_args_and_binary_streams(sdk, result, code):
    call = sdk.Sandbox.from_id.return_value.exec
    stdout = SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=b"out\xff")))
    stderr = SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=b"err\xfe")))
    wait = AsyncMock(side_effect=getattr(sdk.exception, result)) if isinstance(result, str) else AsyncMock(
        return_value=result,
    )
    call.return_value = SimpleNamespace(stdout=stdout, stderr=stderr, wait=SimpleNamespace(aio=wait))
    assert modal._Modal().exec("sb-one", ("echo", "a b", "$HOME", ""), cwd="/tmp/anycompute", timeout_s=210) == (
        RawExecResult(b"out\xff", b"err\xfe", code)
    )
    call.assert_called_once_with("echo", "a b", "$HOME", "", workdir="/tmp/anycompute", text=False, timeout=None)


def test_exec_on_terminated_sandbox_leaves_expiry_classification_to_engine(sdk):
    sdk.Sandbox.from_id.return_value.exec.side_effect = sdk.exception.SandboxTerminatedError
    assert modal._Modal().exec("sb-one", ("true",), cwd="/", timeout_s=120) == RawExecResult(b"", b"", None)


def test_files_use_absolute_binary_paths_and_skip_missing_or_nonregular_files(sdk):
    adapter = modal._Modal()
    path = "/tmp/anycompute/folder/data"
    filesystem = sdk.Sandbox.from_id.return_value.filesystem
    payload = b"\x00\xff"
    adapter.write("sb-one", path, payload, timeout_s=120)
    filesystem.write_bytes.assert_called_once_with(payload, path)
    filesystem.read_bytes.return_value = payload
    filesystem.stat.return_value.is_file.return_value = True
    assert adapter.read("sb-one", path, timeout_s=120) == payload
    filesystem.stat.return_value.is_file.return_value = False
    assert adapter.read("sb-one", path, timeout_s=120) is None
    filesystem.stat.side_effect = sdk.exception.SandboxFilesystemNotFoundError
    assert adapter.read("sb-one", path, timeout_s=120) is None
    filesystem.read_bytes.assert_called_once_with(path)


@pytest.mark.parametrize(("code", "state"), [(None, SandboxState.RUNNING), (0, SandboxState.GONE),
                                             (124, SandboxState.GONE), (137, SandboxState.GONE),
                                             ("unknown", SandboxState.UNKNOWN)])
def test_state_confirms_from_a_direct_sdk_lookup(sdk, code, state):
    sandbox = sdk.Sandbox.from_id.return_value
    sandbox.poll.return_value = code
    adapter = modal._Modal()
    adapter.delete("sb-one", timeout_s=120)
    sandbox.terminate.assert_called_once_with(wait=False)
    assert adapter.state("sb-one", timeout_s=120) is state
    sdk.Sandbox.list.assert_not_called()


def test_missing_sandbox_is_gone_and_delete_is_idempotent(sdk):
    sdk.Sandbox.from_id.side_effect = sdk.exception.NotFoundError
    adapter = modal._Modal()
    adapter.delete("sb-one", timeout_s=120)
    assert adapter.state("sb-one", timeout_s=120) is SandboxState.GONE


def test_list_drains_every_page_with_app_and_label_filter(sdk):
    def pages():
        yield SimpleNamespace(object_id="sb-first", get_tags=lambda: LABELS)
        yield SimpleNamespace(object_id="sb-other", get_tags=lambda: {})
        yield SimpleNamespace(object_id="sb-last", get_tags=lambda: LABELS | {"other": "label"})

    sdk.Sandbox.list.return_value = pages()
    entries = modal._Modal().list({"anycompute": "1"}, timeout_s=120)
    assert entries == [SandboxInfo("sb-first", LABELS, None), SandboxInfo("sb-last", LABELS | {"other": "label"}, None)]
    sdk.Sandbox.list.assert_called_once_with(app_id="ap-anycompute", tags={"anycompute": "1"})


@pytest.mark.parametrize(("configured", "reported"), [
    (urlunsplit(("https", "example-user:example-password@api.modal.com", "/control", "debug=1", "")),
     "https://api.modal.com"),
    ("https://api.modal.com, https://api.modal2.com", "unknown"),
])
def test_runtime_host_reports_one_endpoint_without_userinfo(sdk, configured, reported):
    sdk.config.config["server_url"] = configured
    assert engine._runtime_host(modal._Modal().api_host(timeout_s=120)) == reported


def test_profile_keys_redact_even_when_inactive(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[inactive]\ntoken_id = "inactive-test-key"\ntoken_secret = "inactive-test-secret"\n'
                    '[secondary]\noauth_client_secret = "inactive-oauth-secret"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", os.fspath(path))
    assert engine._redact_text("inactive-test-key inactive-test-secret inactive-oauth-secret", "modal") == "*** *** ***"


def test_missing_keys_fail_before_sdk_lookup(sdk, monkeypatch, tmp_path):
    monkeypatch.setenv("MODAL_CONFIG_PATH", os.fspath(tmp_path / "missing.toml"))
    for name in modal.SPEC.caps.key_sources.env:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AuthError, match="Missing modal credentials"):
        engine.run(["true"], provider="modal")
    sdk.App.lookup.assert_not_called()


def test_run_reports_provider_expiry_and_confirms_cleanup(sdk, monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "synthetic-token")
    sandbox = sdk.Sandbox.from_id.return_value
    sandbox.poll.return_value = 124

    def process(code, stdout=b""):
        return SimpleNamespace(
            stdout=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=stdout))),
            stderr=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value=b""))),
            wait=SimpleNamespace(aio=AsyncMock(return_value=code)),
        )

    sandbox.exec.side_effect = [process(0), process(137, b"before expiry")]
    with pytest.raises(SandboxTimeoutError) as raised:
        engine.run(["sleep", "600"], provider="modal", ttl=90, run_id="demo")
    assert raised.value.exit_code == 123
    assert raised.value.stdout == "before expiry"
    assert raised.value.sandbox_id == "sb-one"
    assert raised.value.sandbox_seconds is not None
    sandbox.terminate.assert_called_once_with(wait=False)


def test_cli_reports_modal_as_excluded_and_redacts_configured_credentials(tmp_path, monkeypatch):
    config = tmp_path / "modal.toml"
    config.write_text('[inactive]\noauth_client_secret = "synthetic-oauth-secret"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", os.fspath(config))
    result = subprocess.run([sys.executable, "-m", "anycompute", "providers", "--json"],
                            capture_output=True, text=True, check=True)
    assert "synthetic-oauth-secret" not in result.stdout
    row = next(row for row in json.loads(result.stdout)["providers"] if row["provider"] == "modal")
    assert row["live_run"] is False
    assert row["keys"]["~/.modal.toml:*.oauth_client_secret"] == "set"

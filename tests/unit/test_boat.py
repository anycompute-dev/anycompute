import io
import json
import shlex
import sys
from datetime import UTC, datetime
from types import SimpleNamespace as Reply
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from anycompute import AuthError, ConfigError, ProviderError, QuotaError, RateLimitError, SandboxTimeoutError
from anycompute import run
from anycompute.contract import RawExecResult, SandboxInfo, SandboxState
from anycompute.providers import boat


BOX = "bx_23456789"
LABELS = {"anycompute": "1", "anycompute-run": "test", "anycompute-ttl": "120"}


class HTTPFailure(Exception):
    def __init__(self, status, code="", headers=None):
        self.status = status
        self.body = json.dumps({"code": code, "message": "synthetic-key-value"})
        self.headers = headers


class CommandResponse(Reply):
    pass


def command_reply(exit_code=0, *, timed_out=False):
    return Reply(actual_instance=CommandResponse(ok=True, exit_code=exit_code, stdout="out\ufffd", stderr="err",
                                                 timed_out=timed_out))


@pytest.fixture
def sdk(monkeypatch):
    client, api = Mock(), Mock()
    library = Reply(Configuration=Mock(), ApiClient=Mock(return_value=client), CreateSandboxRequest=Reply,
                    CommandRequest=Reply, CommandResponse=CommandResponse, FileWriteRequest=Reply,
                    exceptions=Reply(ApiException=HTTPFailure))
    monkeypatch.setattr(boat, "_sdk", lambda: (library, lambda _: api, OSError))
    monkeypatch.setenv("BOAT_API_KEY", "synthetic-key-value")
    return library, client, api


def test_create_sends_labels_and_ttl_and_withholds_account_secrets(sdk):
    library, client, api = sdk
    api.create.return_value = Reply(ok=True, ttl_seconds=120, sandbox=Reply(id=BOX))
    assert boat._Boat().create(ttl=120, labels=LABELS, options={"environment": {"LANG": "C"}}, timeout_s=240) == BOX
    request = api.create.call_args.kwargs["create_sandbox_request"]
    assert request == Reply(ttl_seconds=120, labels=LABELS, no_env=True, monitoring=False, visibility="private",
                            env={"LANG": "C"})
    api.create.assert_called_once_with(create_sandbox_request=request, _request_timeout=240)
    library.Configuration.assert_called_once_with(host="https://boat.dev/api/v1", access_token="synthetic-key-value",
                                                  retries=False)
    assert client.user_agent.startswith("anycompute/")
    client.rest_client.pool_manager.clear.assert_called_once_with()


def test_create_rejects_provider_ttl_clamp_and_keeps_cleanup_id(sdk):
    sdk[2].create.return_value = Reply(ok=True, ttl_seconds=60, sandbox=Reply(id=BOX))
    with pytest.raises(ProviderError, match="requested TTL") as error:
        boat._Boat().create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    assert error.value.sandbox_id == BOX


@pytest.mark.parametrize(("failure", "error", "retry_after"), [
    (HTTPFailure(401), AuthError, None),
    (HTTPFailure(402, "billing_required"), QuotaError, None),
    (HTTPFailure(429, "limit_reached"), QuotaError, None),
    (HTTPFailure(429, "rate_limited", {"Retry-After": "4"}), RateLimitError, 4),
    (HTTPFailure(429, "rate_limited", {"Retry-After": "NaN"}), RateLimitError, None),
    (HTTPFailure(502), ProviderError, None),
    (OSError("synthetic-key-value"), ProviderError, None),
])
def test_create_maps_failure_without_replay_or_raw_response(sdk, failure, error, retry_after):
    api = sdk[2]
    api.create.side_effect = failure
    with pytest.raises(error) as caught:
        boat._Boat().create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    assert caught.value.__context__ is None
    assert "synthetic-key-value" not in str(caught.value)
    assert getattr(caught.value, "retry_after", None) == retry_after
    assert getattr(caught.value, "sandbox_may_exist", False) is (error is ProviderError)
    assert api.create.call_count == 1


def test_command_quotes_arguments_and_uses_budget(sdk):
    api = sdk[2]
    api.command.return_value = command_reply(7)
    argv = ("echo", "a b", "$(literal)", "'", "")
    output = boat._Boat().exec(BOX, argv, cwd="/tmp/abc def", timeout_s=599.1)
    assert output == RawExecResult("out\ufffd", "err", 7)
    sandbox_id, request = api.command.call_args.args
    assert sandbox_id == BOX
    assert shlex.split(request.command) == ["cd", "/tmp/abc def", "&&", "exec", *argv]
    assert request.timeout_seconds == 600
    assert api.command.call_args.kwargs == {"_request_timeout": 599.1}
    assert api.command.call_count == 1


def test_engine_classifies_archived_sandbox_after_command_disconnect(sdk):
    api = sdk[2]
    api.create.return_value = Reply(ok=True, ttl_seconds=120, sandbox=Reply(id=BOX))
    api.command.side_effect = [command_reply(), HTTPFailure(502, "sandbox_direct_failed")]
    api.get.return_value = Reply(ok=True, sandbox=Reply(id=BOX, state="archived"))
    receipt = Reply(id="operation", target_id=BOX, kind="sandbox", status="completed")
    api.delete_sandbox.return_value = Reply(ok=True, operation=receipt)
    api.get_deletion_operation.return_value = Reply(ok=True, operation=receipt)
    with pytest.raises(SandboxTimeoutError) as error:
        run(["sleep", "600"], provider="boat", ttl=120, run_id="test")
    assert error.value.sandbox_id == BOX and error.value.cleanup_error is None
    assert api.command.call_count == 2
    assert api.delete_sandbox.call_args.kwargs["x_ascii_confirm_delete"] == BOX


def test_timed_out_command_preserves_partial_output_for_engine(sdk):
    sdk[2].command.return_value = command_reply(137, timed_out=True)
    assert boat._Boat().exec(BOX, ("sleep", "2"), cwd="/", timeout_s=4) == RawExecResult("out\ufffd", "err", None)


def test_regular_binary_file_round_trip_and_nonregular_refused(sdk):
    api = sdk[2]
    api.command.side_effect = [command_reply(), command_reply(1), command_reply()]
    api.write_file.return_value = Reply(ok=True, success=True, size=2)
    api.read_file.side_effect = [Reply(ok=True, success=True, content="AP8=", encoding="base64", size=2),
                                 Reply(ok=True, success=True, content="?!", encoding="base64", size=2)]
    adapter = boat._Boat()
    path = "/tmp/anycompute/a; $(literal)"
    adapter.write(BOX, path, b"\x00\xff", timeout_s=120)
    assert api.write_file.call_args.args == (BOX, Reply(path=path, content="AP8=", encoding="base64"))
    assert adapter.read(BOX, path, timeout_s=120) == b"\x00\xff"
    assert shlex.split(api.command.call_args.args[1].command)[-1] == path
    assert adapter.read(BOX, path, timeout_s=120) is None
    with pytest.raises(ProviderError, match="binary file"):
        adapter.read(BOX, path, timeout_s=120)
    assert api.read_file.call_count == 2


def test_cleanup_waits_for_the_deletion_receipt_to_complete(sdk):
    api = sdk[2]
    receipt = Reply(id="operation", target_id=BOX, kind="sandbox", status="pending")
    api.delete_sandbox.return_value = Reply(ok=True, operation=receipt)
    api.get_deletion_operation.return_value = Reply(ok=True, operation=receipt)
    adapter = boat._Boat()
    adapter.delete(BOX, timeout_s=120)
    api.delete_sandbox.assert_called_once_with(x_ascii_confirm_delete=BOX, sandbox_id=BOX, _request_timeout=120)
    assert adapter.state(BOX, timeout_s=119.4) == SandboxState.UNKNOWN
    api.get_deletion_operation.assert_called_once_with("operation", _request_timeout=119.4)
    receipt.status = "completed"
    assert adapter.state(BOX, timeout_s=118.4) == SandboxState.GONE
    api.get.assert_not_called()


@pytest.mark.parametrize(("state", "expected"), [("ready", SandboxState.RUNNING), ("idle", SandboxState.RUNNING),
    ("running", SandboxState.RUNNING), ("archiving", SandboxState.UNKNOWN), ("archived", SandboxState.STOPPED),
    ("provisioned", SandboxState.UNKNOWN), (None, SandboxState.GONE)])
def test_state_uses_independent_get(sdk, state, expected):
    api = sdk[2]
    if state is None:
        api.get.side_effect = HTTPFailure(404)
    else:
        api.get.return_value = Reply(ok=True, sandbox=Reply(id=BOX, state=state))
    assert boat._Boat().state(BOX, timeout_s=120) == expected


def test_list_reads_all_pages_and_rechecks_labels(sdk):
    api = sdk[2]
    created = datetime(2026, 11, 24, tzinfo=UTC)
    api.sandboxes.side_effect = [
        Reply(ok=True, sandboxes=[Reply(id=BOX, labels=LABELS, created_at=created)],
              page_info=Reply(has_more=True, next_cursor="next")),
        Reply(ok=True, sandboxes=[Reply(id="unrelated", labels={}), Reply(id="second", labels=LABELS, created_at=None)],
              page_info=Reply(has_more=False, next_cursor=None)),
    ]
    requested = {"anycompute": "1", "anycompute-run": "test"}
    assert boat._Boat().list(requested, timeout_s=120) == [SandboxInfo(BOX, LABELS, created.timestamp()),
                                                        SandboxInfo("second", LABELS, None)]
    assert [call.kwargs["cursor"] for call in api.sandboxes.call_args_list] == [None, "next"]
    assert all(call.kwargs["label"] == ["anycompute=1", "anycompute-run=test"] for call in api.sandboxes.call_args_list)


def test_list_fails_closed_on_repeated_cursor(sdk):
    sdk[2].sandboxes.return_value = Reply(ok=True, sandboxes=[], page_info=Reply(has_more=True, next_cursor="repeat"))
    with pytest.raises(ProviderError, match="pagination"):
        boat._Boat().list(LABELS, timeout_s=120)
    assert sdk[2].sandboxes.call_count == 2


def test_keyless_caps_import_and_live_opt_out(monkeypatch):
    monkeypatch.setitem(sys.modules, "boat_sdk", None)
    from anycompute.providers import get_provider

    spec = get_provider("boat")
    assert spec.caps.ttl_max_s == 480 and spec.caps.lifetime == "partial"
    assert spec.caps.labels_at_create and not spec.caps.live_run
    assert spec.caps.creates_per_minute.value == 12
    with pytest.raises(ConfigError, match=r"anycompute\[boat\]"):
        boat._Boat().prepare(options={}, timeout_s=120)


def test_installed_sdk_transport_serializes_and_does_not_follow_create_redirect(monkeypatch):
    pytest.importorskip("boat_sdk")
    urllib3 = pytest.importorskip("urllib3")
    monkeypatch.setenv("BOAT_API_KEY", "synthetic-key-value")
    sent = []

    def request(pool, method, url, **kwargs):
        sent.append((pool, method, url, kwargs))
        return urllib3.HTTPResponse(status=307, body=io.BytesIO(), headers={"Location": "https://example.invalid/redirect"},
                                    retries=kwargs.get("retries", pool.retries))

    monkeypatch.setattr(urllib3.connectionpool.HTTPSConnectionPool, "urlopen", request)
    with pytest.raises(ProviderError, match="HTTP 307"):
        boat._Boat().create(ttl=120, labels=LABELS, options={}, timeout_s=240)
    assert len(sent) == 1
    pool, method, url, request = sent[0]
    assert pool.host == "boat.dev" and method == "POST" and url == "/api/v1/sandboxes"
    assert request["headers"]["User-Agent"].startswith("anycompute/")
    assert json.loads(request["body"])["labels"] == LABELS
    assert json.loads(request["body"])["noEnv"] is True


def test_installed_sdk_transport_uses_real_documented_models(monkeypatch):
    pytest.importorskip("boat_sdk")
    urllib3 = pytest.importorskip("urllib3")
    monkeypatch.setenv("BOAT_API_KEY", "synthetic-key-value")
    recorded = []
    sandbox = {"id": BOX, "name": "Sandbox", "state": "archived", "labels": LABELS,
               "createdAt": "2026-11-24T00:00:00Z", "desktopAvailable": False, "snapshotAvailable": False}
    operation = {"id": "bdop_" + "0" * 32, "targetId": BOX, "status": "completed", "kind": "sandbox",
                 "reason": "explicit", "attemptCount": 1, "requestedAt": "2026-11-24T00:00:00Z"}
    envelopes = iter([
        (202, {"ok": True, "type": "sandbox.created", "status": "provisioning", "ttlSeconds": 120, "sandbox": sandbox}),
        (200, {"ok": True, "type": "sandbox.list", "sandboxes": [sandbox],
               "pageInfo": {"nextCursor": None, "hasMore": False, "limit": 200}}),
        (200, {"ok": True, "type": "command.finished", "success": True, "exitCode": 0,
               "stdout": "hi", "stderr": "", "timedOut": False}),
        (202, {"ok": True, "type": "deletion.operation", "operation": operation}),
        (200, {"ok": True, "type": "deletion.operation", "operation": operation}),
    ])

    def request(pool, method, url, **kwargs):
        assert pool.host == "boat.dev"
        recorded.append((method, url, kwargs))
        status, payload = next(envelopes)
        return urllib3.HTTPResponse(status=status, body=io.BytesIO(json.dumps(payload).encode()),
                                    headers={"Content-Type": "application/json"})

    monkeypatch.setattr(urllib3.connectionpool.HTTPSConnectionPool, "urlopen", request)
    adapter = boat._Boat()
    assert adapter.create(ttl=120, labels=LABELS, options={}, timeout_s=240) == BOX
    assert adapter.list(LABELS, timeout_s=120)[0].labels == LABELS
    assert adapter.exec(BOX, ("echo", "hi"), cwd="/tmp/anycompute", timeout_s=180) == RawExecResult("hi", "", 0)
    adapter.delete(BOX, timeout_s=120)
    assert adapter.state(BOX, timeout_s=119) == SandboxState.GONE
    assert [(method, urlsplit(url).path) for method, url, _ in recorded] == [
        ("POST", "/api/v1/sandboxes"), ("GET", "/api/v1/sandboxes"),
        ("POST", f"/api/v1/sandboxes/{BOX}/commands"), ("DELETE", f"/api/v1/sandboxes/{BOX}"),
        ("GET", f"/api/v1/deletion-operations/{operation['id']}"),
    ]
    assert parse_qs(urlsplit(recorded[1][1]).query)["label"] == [f"{key}={value}" for key, value in LABELS.items()]
    assert recorded[3][2]["headers"]["X-Ascii-Confirm-Delete"] == BOX

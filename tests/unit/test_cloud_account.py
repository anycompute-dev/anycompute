import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.conformance import cloud_account


@pytest.mark.parametrize(("provider", "host", "route", "auth"), [
    ("boat", "boat.dev", "/api/v1/limits", {"Authorization": "Bearer synthetic"}),
    ("e2b", "api.e2b.app", "/default-templates", {"X-API-Key": "synthetic"}),
])
def test_account_http_checks_use_only_the_fixed_get_destination(monkeypatch, provider, host, route, auth):
    connection = Mock()
    connection.getresponse.return_value.status = 302
    connector = Mock(return_value=connection)
    monkeypatch.setattr(cloud_account, "HTTPSConnection", connector)

    assert cloud_account._inspect(provider, "synthetic") == {"access": "unverified", "http_status": 302}
    connector.assert_called_once_with(host, timeout=15)
    connection.request.assert_called_once_with("GET", route, headers={**auth, "Accept": "application/json"})
    connection.getresponse.return_value.read.assert_not_called()
    connection.close.assert_called_once_with()


def test_boat_output_contains_status_without_billing_details(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["account-check", "boat"])
    monkeypatch.setenv("BOAT_API_KEY", "synthetic")
    monkeypatch.setattr(cloud_account, "_metadata", lambda provider, key: (200, {
        "ok": True, "type": "limits.info", "canStart": True, "starts": {"day": {"remaining": 22}},
        "planIncluded": {"vmHoursRemaining": 0.5}, "billingContact": "private",
    }))
    assert cloud_account.main() == 0
    assert json.loads(capsys.readouterr().out.removeprefix("Cloud account result: ")) == {
        "provider": "boat", "access": "verified", "can_start": True, "daily_starts_cover_suite": True,
        "included_vm_time_covers_suite": False,
    }


@pytest.mark.parametrize(("memory", "status"), [(512, "within reservation"), (8192, "exceeds reservation")])
def test_e2b_checks_the_published_base_resources_only(monkeypatch, memory, status):
    monkeypatch.setattr(cloud_account, "_metadata", lambda provider, key: (200, [
        {"aliases": ["other"], "cpuCount": 8, "memoryMB": 8192},
        {"names": ["base"], "cpuCount": 2, "memoryMB": memory, "id": "private"},
    ]))
    assert cloud_account._inspect("e2b", "synthetic") == {
        "access": "verified", "base_template": status, "vcpus": 2, "memory_mb": memory,
    }


@pytest.mark.parametrize(("templates", "exit_code"), [
    ([{"names": ["base"], "cpuCount": 2, "memoryMB": 512}], 0),
    ([{"names": ["base"], "cpuCount": 2, "memoryMB": 8192}], 1),
    ([], 1),
])
def test_e2b_trial_requires_verified_base_resources(monkeypatch, templates, exit_code):
    monkeypatch.setattr(sys, "argv", ["account-check", "e2b", "--require-e2b-base-size"])
    monkeypatch.setenv("E2B_API_KEY", "synthetic")
    monkeypatch.setattr(cloud_account, "_metadata", lambda provider, key: (200, templates))
    assert cloud_account.main() == exit_code


@pytest.fixture
def daytona_client(monkeypatch):
    snapshot = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111", name="daytona-small", state="active",
        general=True, sandbox_class="container", cpu=1, mem=1, disk=3, gpu=0,
    )
    client = SimpleNamespace(
        snapshot=SimpleNamespace(get=Mock(return_value=snapshot)),
        warm_pool=SimpleNamespace(list=Mock()),
    )
    monkeypatch.setattr(cloud_account.daytona, "_call", lambda operation, action, project: action(client, None))
    return client


@pytest.mark.parametrize(("target", "warm_pool_status"), [
    ("us", "present"),
    ("eu", "absent"),
    (None, "unverified"),
])
def test_daytona_checks_the_exact_snapshot_and_warm_pool_region(daytona_client, target, warm_pool_status):
    snapshot_id = daytona_client.snapshot.get.return_value.id
    daytona_client.warm_pool.list.return_value = [
        SimpleNamespace(target=target, snapshot=snapshot_id, current_size=0, env={"private": "data"}),
    ]

    assert cloud_account._inspect("daytona", "synthetic") == {
        "access": "verified", "snapshot_matches": True, "matching_warm_pool": warm_pool_status,
    }
    daytona_client.snapshot.get.assert_called_once_with("daytona-small")
    daytona_client.warm_pool.list.assert_called_once_with()


@pytest.mark.parametrize(("code", "status"), [(404, "feature unavailable"), (403, "unverified")])
def test_daytona_reports_only_the_warm_pool_error_status(daytona_client, code, status):
    exceptions = pytest.importorskip("daytona_api_client.exceptions")
    daytona_client.warm_pool.list.side_effect = exceptions.ApiException(status=code, body="private response")

    assert cloud_account._inspect("daytona", "synthetic") == {
        "access": "verified", "snapshot_matches": True, "matching_warm_pool": status, "warm_pool_http_status": code,
    }


def test_cloud_account_workflow_scopes_each_key_to_its_manual_main_job():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/cloud-account-check.yml").read_text()
    setup, check = workflow.split("      - name: Read provider metadata\n")
    assert "on:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n" in setup
    assert "if: ${{ github.ref == 'refs/heads/main' }}\n    environment: conformance" in setup
    assert "provider: [boat, e2b, daytona]" in setup
    assert "secrets." not in setup
    for provider, name in (("boat", "BOAT_API_KEY"), ("e2b", "E2B_API_KEY"), ("daytona", "DAYTONA_API_KEY")):
        assert f"${{{{ matrix.provider == '{provider}' && secrets.{name} || '' }}}}" in check
    assert 'DAYTONA_USE_DEPRECATED_POLLING: "true"' in check
    assert 'uv run --locked --no-sync python -m tests.conformance.cloud_account "$ACCOUNT_PROVIDER"' in check

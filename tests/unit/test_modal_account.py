import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conformance import modal_account


@pytest.mark.parametrize(("workspace_ok", "reply", "status", "image", "exit_code"), [
    (False, None, "unverified", "unchecked", 1),
    (True, b'[{"tag":"anycompute-conformance:v10"}]', "verified", "not found", 0),
    (True, b'{"private":"response"}', "verified", "unverified", 0),
    (True, None, "verified", "unverified", 0),
    (True, b'[{"tag":"shared/anycompute-conformance:v1"}]', "verified", "available", 0),
])
def test_modal_account_emits_only_read_only_status(monkeypatch, capsys, workspace_ok, reply,
                                                 status, image, exit_code):
    calls = []

    def hydrate_workspace():
        calls.append("workspace")
        if not workspace_ok:
            raise RuntimeError("private provider response")
        return SimpleNamespace(name="private-workspace")

    def list_images(argv, **options):
        calls.append("images")
        assert argv == [
            sys.executable, "-m", "modal", "image", "names", "list", "--json", "--prefix",
            "anycompute-conformance:v1",
        ]
        assert options == {"capture_output": True, "check": True, "timeout": 30}
        if reply is None:
            raise subprocess.CalledProcessError(1, argv, stderr=b"private provider response")
        return SimpleNamespace(stdout=reply)

    sdk = SimpleNamespace(Workspace=SimpleNamespace(from_context=lambda: SimpleNamespace(hydrate=hydrate_workspace)))
    monkeypatch.setitem(sys.modules, "modal", sdk)
    monkeypatch.setattr(modal_account.subprocess, "run", list_images)
    monkeypatch.setenv("MODAL_TOKEN_ID", "synthetic-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "synthetic-secret")

    assert modal_account.main() == exit_code
    assert calls == (["workspace", "images"] if workspace_ok else ["workspace"])
    assert capsys.readouterr().out.splitlines() == [
        f"Modal workspace authentication: {status}",
        f"Modal named conformance image: {image}",
        "Active workspace usage cap: verify in Usage & Billing",
    ]


def test_modal_account_requires_both_secrets(monkeypatch, capsys):
    monkeypatch.setenv("MODAL_TOKEN_ID", "synthetic-id")
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    monkeypatch.setitem(sys.modules, "modal", None)
    assert modal_account.main() == 1
    assert capsys.readouterr().out == "The Modal token ID and secret are required.\n"


def test_modal_account_workflow_is_manual_main_only_and_scopes_secrets_to_the_check():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/modal-account-check.yml").read_text()
    setup, check = workflow.split("      - name: Verify the workspace and named image\n")
    assert "on:\n  workflow_dispatch:\n\npermissions:\n  contents: read\n" in setup
    assert "if: ${{ github.ref == 'refs/heads/main' }}\n    environment: conformance" in setup
    assert "secrets." not in setup
    assert set(re.findall(r"secrets\.(\w+)", check)) == {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"}
    assert "uv run --locked --no-sync python -m tests.conformance.modal_account" in check

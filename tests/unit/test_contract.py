import importlib
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import distribution

import pytest


def test_installed_distribution_exposes_cli():
    entry_points = distribution("anycompute").entry_points.select(
        group="console_scripts", name="anycompute",
    )
    assert [entry.value for entry in entry_points] == ["anycompute.cli:main"]


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("AuthError", 120), ("RateLimitError", 121), ("QuotaError", 122),
        ("SandboxTimeoutError", 123), ("ProviderError", 124), ("CleanupError", 125),
        ("InvalidTTLError", 126), ("InvalidPathError", 126), ("ConfigError", 126),
    ],
)
def test_error_has_exit_code_and_sandbox_context(name, code):
    contract = importlib.import_module("anycompute.contract")
    error = getattr(contract, name)("message", sandbox_id="sb", run_id="run", sandbox_seconds=2.0)
    assert isinstance(error, contract.AnycomputeError)
    assert (error.exit_code, error.sandbox_id, error.run_id, error.sandbox_seconds) == (code, "sb", "run", 2.0)
    assert isinstance(error, ValueError) == (code == 126)


def test_contract_import_does_not_load_engine():
    subprocess.run(
        [sys.executable, "-c", "import sys; import anycompute.contract; assert 'anycompute.engine' not in sys.modules"],
        check=True,
    )


def test_public_api_lists_only_supported_names():
    anycompute = importlib.import_module("anycompute")
    assert set(anycompute.__all__) == {
        "run", "Sandbox", "Result", "ExecResult", "AnycomputeError", "AuthError",
        "RateLimitError", "QuotaError", "SandboxTimeoutError", "ProviderError",
        "CleanupError", "InvalidTTLError", "InvalidPathError", "ConfigError",
    }


def test_capability_table_uses_caps_not_the_factory(monkeypatch):
    from anycompute import providers
    from anycompute.contract import KeySources, ProviderCaps, ProviderSpec

    def unused_factory():
        pytest.fail("reading caps must not build a provider")

    caps = replace(ProviderCaps(isolation="test VM"), key_sources=KeySources(env=("TEST_KEY",)))
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("sample", caps, unused_factory),))
    [row] = providers.capability_table()
    assert row["provider"] == "sample"
    assert row["isolation"] == "test VM"
    assert row["live_run"] is False
    assert "key_sources" not in row


def test_unknown_provider_raises_config_error():
    from anycompute.contract import ConfigError
    from anycompute.providers import get_provider

    with pytest.raises(ConfigError, match="absent"):
        get_provider("absent")

import subprocess
import sys
from pathlib import Path

import pytest

import anycompute
from anycompute import CleanupError, ExecResult, ProviderError
from tests import release_smoke


def test_provider_cli_failure_stops_before_docker(monkeypatch, capsys):
    calls = []

    def fail_cli(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(release_smoke.subprocess, "run", fail_cli)
    monkeypatch.setattr(anycompute, "run", lambda *a, **kw: pytest.fail("Docker ran after CLI failure"))

    assert release_smoke.main() == 1
    assert capsys.readouterr().out == "package failure in anycompute providers\n"
    assert calls == [(
        [Path(sys.executable).with_name("anycompute"), "providers"],
        {"check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 30},
    )]


@pytest.mark.parametrize(("outcome", "code", "message"), [
    (ExecResult("anycompute release smoke\n", "", 0), 0, "release smoke passed"),
    (ExecResult("wrong output", "", 0), 1, "package failure in Docker smoke job"),
    (ExecResult("anycompute release smoke\n", "", 1), 1, "package failure in Docker smoke job"),
    (ProviderError("daemon unreachable"), 2, "infrastructure failure in Docker smoke job"),
    (ProviderError("internal failure", internal=True), 1, "package failure in Docker smoke job"),
    (CleanupError("deletion unconfirmed"), 1, "package failure in Docker smoke job"),
])
def test_docker_smoke_result(monkeypatch, capsys, outcome, code, message):
    calls = []

    def run_docker(argv, *, provider, ttl):
        calls.append((argv, provider, ttl))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(release_smoke.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(anycompute, "run", run_docker)

    assert release_smoke.main() == code
    assert capsys.readouterr().out == f"{message}\n"
    assert calls == [(("printf", "%s", "anycompute release smoke\n"), "docker", 60)]

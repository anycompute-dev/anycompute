import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text()


def _step(name):
    step = WORKFLOW.split(f"      - name: {name}\n", 1)[1].split("\n      - ", 1)[0]
    return dedent(step.split("        run: |\n", 1)[1])


def test_only_tag_releases_use_attested_trusted_publishing():
    assert "on:\n  push:\n    tags: ['v*']\n\npermissions: {}\n" in WORKFLOW
    actions = re.findall(r"(?m)^\s+- uses:\s+(\S+)", WORKFLOW)
    assert actions and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)
    assert "secrets." not in WORKFLOW
    assert WORKFLOW.count("id-token: write") == 1

    build, following = WORKFLOW.split("\n  publish:\n", 1)
    publish, smoke = following.split("\n  smoke:\n", 1)
    assert "uv build --no-sources" in build
    assert "dist/*.tar.gz\n            dist/*.whl" in build
    assert "needs: build" in publish and "environment: pypi" in publish
    assert "id-token: write" in publish and "attestations: true" in publish
    assert "artifact-ids: ${{ needs.build.outputs.artifact-id }}" in publish
    assert "actions/checkout" not in publish
    assert "needs: [build, publish]" in smoke
    assert "RELEASE_VERSION: ${{ needs.build.outputs.version }}" in smoke
    assert '"$RUNNER_TEMP/release-venv/bin/python" tests/release_smoke.py' in smoke


@pytest.mark.parametrize("matches", [True, False])
def test_release_tag_must_match_package_version(tmp_path, matches):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    output = tmp_path / "output"
    env = {
        **os.environ,
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ["PATH"])),
        "GITHUB_REF": f"refs/tags/v{version}" if matches else f"refs/tags/v{version}.1",
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", _step("Verify the release version")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=5,
    )
    if matches:
        assert result.returncode == 0
        assert output.read_text() == f"version={version}\n"
    else:
        assert result.returncode == 1 and "Release tag must equal" in result.stderr
        assert not output.exists()


@pytest.mark.parametrize("timed_out", [False, True])
def test_published_install_retries_or_reports_install_failure(tmp_path, timed_out):
    python = tmp_path / "release-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(dedent("""\
        #!/bin/bash
        printf '%s\\n' "$*"
        if [[ ! -f "$RUNNER_TEMP/attempted" ]]; then
          touch "$RUNNER_TEMP/attempted"
          exit 1
        fi
        """))
    python.chmod(0o755)
    shell = dedent("""\
        python() { [[ "$*" == "-m venv $RUNNER_TEMP/release-venv" ]]; }
        timeout() {
          [[ "$1" == --kill-after=5s && "$2" == 600s ]] || exit 99
          [[ "$TIMED_OUT" == 0 ]] || return 124
          shift 2
          "$@"
        }
        sleep() { printf 'delay=%s\\n' "$1"; }
        export -f sleep
        """)
    result = subprocess.run(
        ["bash", "-e", "-c", shell + _step("Install the published package")],
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "RELEASE_VERSION": "0.1.0",
             "TIMED_OUT": str(int(timed_out))},
        capture_output=True, text=True, timeout=5,
    )
    if timed_out:
        assert result.returncode == 1
        assert "::error title=install failed::" in result.stdout
    else:
        assert result.returncode == 0
        install = ("-m pip --isolated install --disable-pip-version-check --no-input --no-cache-dir "
                   "--only-binary=anycompute --index-url https://pypi.org/simple anycompute[docker]==0.1.0")
        assert result.stdout.splitlines() == [install, "delay=20", install]

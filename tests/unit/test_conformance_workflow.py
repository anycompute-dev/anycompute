from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
from textwrap import dedent
from typing import NoReturn
from urllib.parse import parse_qs, urlsplit

import pytest

from anycompute import providers
from anycompute.contract import ProviderCaps, ProviderSpec
from tests.conformance.check_run_limit import main as run_gate

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
CONFORMANCE = Path(__file__).resolve().parents[2] / ".github/workflows/conformance.yml"
WORKFLOWS = CONFORMANCE.parent


@pytest.fixture(autouse=True)
def history(monkeypatch):
    replies, requests = [], []

    class Connection:
        def __init__(self, host, *, timeout):
            assert host == "api.github.com"
            assert 0 < timeout <= 15

        def request(self, method, path, *, headers):
            assert method == "GET"
            assert headers["Authorization"] == "Bearer synthetic"
            requests.append(path)

        def getresponse(self):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return replies, requests


class Reply:
    def __init__(self, *runs, total=None):
        self.status = 200
        self.body = json.dumps({
            "total_count": len(runs) if total is None else total, "workflow_runs": runs,
        }).encode()
        self.content_length: str | None = None

    def getheader(self, name):
        assert name == "Content-Length"
        return self.content_length

    def read(self, count):
        assert count > 0
        return self.body[:count]


def run(run_id=42, *, attempt=2, event="workflow_dispatch", title="adapters=modal,boat"):
    return {
        "id": run_id, "event": event, "display_title": title, "run_attempt": attempt,
        "created_at": "2026-11-23T00:00:00Z",
    }


def no_client() -> NoReturn:
    raise AssertionError("The keyless gate must not build a provider client")


@pytest.fixture
def gate(monkeypatch, tmp_path):
    env = {
        "GITHUB_REPOSITORY": "example/compute",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_TOKEN": "synthetic",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "REQUESTED_ADAPTERS": "modal,boat",
    }
    monkeypatch.setattr(providers, "PROVIDERS", tuple(
        ProviderSpec(name, ProviderCaps(live_run=enabled), no_client)
        for name, enabled in [("docker", True), ("modal", True), ("e2b", False), ("boat", True)]
    ))
    return env, tmp_path / "output"


def main(env):
    return run_gate(env, now=NOW)


def test_dispatch_passes_same_named_month_to_history_and_outputs_counted_attempt(gate, history):
    env, output = gate
    replies, requests = history
    replies.append(Reply(run()))

    assert main(env) == 0
    assert output.read_text() == 'verified_attempt=2\nadapters=["modal","boat"]\n'
    (request,) = requests
    parts = urlsplit(request)
    assert parts.path == "/repos/example/compute/actions/workflows/conformance.yml/runs"
    assert parse_qs(parts.query) == {
        "created": ["2026-11-01..2026-11-30"], "page": ["1"], "per_page": ["100"],
    }


@pytest.mark.parametrize("enabled, expected", [(("docker", "boat", "modal"), '["boat","modal"]'), (("docker",), "[]")])
def test_weekly_matrix_uses_only_live_cloud_caps(gate, history, monkeypatch, capsys, enabled, expected):
    env, output = gate
    env.update(GITHUB_EVENT_NAME="schedule", REQUESTED_ADAPTERS="")
    monkeypatch.setattr(providers, "PROVIDERS", tuple(
        spec for spec in providers.PROVIDERS if spec.name in enabled or not spec.caps.live_run
    ))
    history[0].append(Reply(run(event="schedule", title="Weekly conformance")))

    assert main(env) == 0
    assert output.read_text() == f"verified_attempt=2\nadapters={expected}\n"
    assert ("no conformance tests ran" in capsys.readouterr().out) == (expected == "[]")


@pytest.mark.parametrize("value", ["", "docker", "e2b", "Modal", "modal,modal", "modal,", "modal, boat",
                                   "modal\n", "modal;boat", "modal adapters=boat"])
def test_dispatch_rejects_invalid_adapter_input_before_history(gate, history, capsys, value):
    env, output = gate
    env["REQUESTED_ADAPTERS"] = value

    assert main(env) == 1
    assert "adapters input" in capsys.readouterr().err
    assert not history[1]
    assert not output.exists()


@pytest.mark.parametrize("name, value", [
    ("GITHUB_REF", "refs/heads/Main"), ("GITHUB_REF", "refs/tags/main"),
    ("GITHUB_EVENT_NAME", "push"), ("GITHUB_RUN_ID", "042"), ("GITHUB_RUN_ATTEMPT", "2\n"),
    ("GITHUB_REPOSITORY", "example/.."), ("GITHUB_REPOSITORY", "example/compute?event=schedule"),
    ("GITHUB_TOKEN", ""), ("GITHUB_OUTPUT", ""), ("GITHUB_TOKEN", None),
])
def test_invalid_actions_context_fails_before_history(gate, history, name, value):
    env, output = gate
    if value is None:
        del env[name]
    else:
        env[name] = value

    assert main(env) == 1
    assert not history[1]
    assert not output.exists()


@pytest.mark.parametrize("current", [
    run(attempt=1), run(attempt=3), run(run_id=41),
    run(title="adapters=modal"), run(event="schedule", title="Weekly conformance"),
])
def test_gate_outputs_nothing_for_wrong_current_attempt_or_selection(gate, history, current):
    env, output = gate
    history[0].append(Reply(current))

    assert main(env) == 1
    assert not output.exists()


def test_later_page_is_required_and_shared_runner_refuses_fifth_manual_attempt(gate, history, capsys):
    env, output = gate
    replies, requests = history
    archived = [run(i, attempt=1, event="schedule", title="Weekly conformance") for i in range(100, 197)]
    archived += [run(i, attempt=1, title="adapters=modal") for i in range(1, 4)]
    replies.extend((Reply(*archived, total=101), Reply(run(), total=101)))

    assert main(env) == 1
    assert "modal: 5 extra runs exceed 4" in capsys.readouterr().err
    assert [parse_qs(urlsplit(path).query)["page"] for path in requests] == [["1"], ["2"]]
    assert not output.exists()


@pytest.mark.parametrize("failure, message", [
    ("redirect", "HTTP 302"), ("denied", "HTTP 403"), ("malformed", "JSON"), ("non-JSON number", "JSON"),
    ("incomplete", "page size"), ("search cap", "search cap"), ("oversized", "too large"),
    ("truncated body", "incomplete"),
    ("broken HTTP", "request failed"), ("timeout", "request failed"),
])
def test_bad_history_never_writes_outputs_or_logs_raw_payload(gate, history, capsys, failure, message):
    env, output = gate
    replies, requests = history
    reply = Reply(run())
    raw = "raw server payload"
    if failure == "redirect":
        reply.status = 302
    elif failure == "denied":
        reply.status = 403
    elif failure == "malformed":
        reply.body = raw.encode()
    elif failure == "non-JSON number":
        reply.body = reply.body.replace(b'"total_count": 1', b'"invalid": NaN, "total_count": 1')
    elif failure == "incomplete":
        reply = Reply(run(), total=2)
    elif failure == "search cap":
        reply = Reply(run(), total=1000)
    elif failure == "oversized":
        reply.body += b" " * (8 * 1024 * 1024)
    elif failure == "truncated body":
        reply.content_length = str(len(reply.body) + 1)
    elif failure == "broken HTTP":
        reply = http.client.BadStatusLine(raw)
    else:
        reply = TimeoutError(raw)
    replies.append(reply)

    assert main(env) == 1
    captured = capsys.readouterr()
    assert message in captured.err
    assert raw not in captured.out + captured.err
    assert len(requests) == 1
    assert not output.exists()


def job(workflow, name):
    text = (WORKFLOWS / f"{workflow}.yml").read_text()
    _, marker, body = text.partition(f"\n  {name}:\n")
    assert marker, f"Missing {name} job"
    return re.split(r"^  [a-z-]+:\s*$", body, maxsplit=1, flags=re.MULTILINE)[0]


def step(job_body, name):
    _, marker, body = job_body.partition(f"      - name: {name}\n")
    assert marker, f"Missing {name} step"
    return body.split("\n      - ", 1)[0]


def script(step_body):
    return dedent(step_body.split("        run: |\n", 1)[1])


def bash(code, **env):
    shell = shutil.which("bash")
    if shell is None:
        pytest.skip("Workflow shell requires bash")
    assert shell is not None
    return subprocess.run([shell, "--noprofile", "--norc", "-eo", "pipefail", "-c", code],
                          env={"PATH": "", **env}, capture_output=True, text=True, timeout=5)


def test_every_pull_request_runs_keyless_docker_conformance():
    workflow = (WORKFLOWS / "ci.yml").read_text()
    docker = job("ci", "docker")
    assert "\n  pull_request:\n" in workflow
    assert not re.search(r"^    (if|environment|needs):", docker, re.MULTILINE)
    assert "timeout-minutes: 30" in docker
    assert "ANYCOMPUTE_CONFORMANCE_PROVIDER: docker" in docker
    assert "ANYCOMPUTE_RUN_ID: ${{ github.run_id }}" in docker
    assert "secrets." not in docker and "ANYCOMPUTE_CONFORMANCE_LIVE" not in docker
    assert "uv sync --locked --python 3.12 --extra docker" in docker
    assert "persist-credentials: false" in docker
    assert "continue-on-error" not in docker
    assert docker.rstrip().endswith("- run: uv run --locked pytest -q -m live_conformance tests/conformance")


def test_docker_pre_pull_uses_adapter_default_not_a_copied_tag():
    pull = script(step(job("ci", "docker"), "Pull the pinned Docker image"))
    result = bash(dedent(r'''
        uv() {
          [[ "$*" == *"from anycompute.providers.docker import DEFAULT_IMAGE; print(DEFAULT_IMAGE)" ]] || return 1
          printf '%s\n' synthetic-image-ref
        }
        docker() { printf '%s\n' "$@"; }
    ''') + pull)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["pull", "synthetic-image-ref"]


def test_workflow_keeps_the_history_gate_keyless_and_passes_only_its_matrix():
    workflow = CONFORMANCE.read_text()
    limit, cloud = job("conformance", "limit"), job("conformance", "cloud")
    assert "\n  workflow_dispatch:\n" in workflow and "\n  schedule:\n" in workflow
    assert "format('adapters={0}', inputs.adapters)" in workflow
    assert "\npermissions: {}\n" in workflow
    assert re.search(r"(?m)^    permissions:\n      actions: read\n    \w", limit)
    assert not re.search(r"^    (environment|needs):", limit, re.MULTILINE)
    assert "secrets." not in limit and "uses: actions/checkout" not in limit
    assert "adapters: ${{ steps.history.outputs.adapters }}" in limit
    assert "verified_attempt: ${{ steps.history.outputs.verified_attempt }}" in limit
    assert "REQUESTED_ADAPTERS: ${{ inputs.adapters }}" in limit
    assert "uv run --locked python -m tests.conformance.check_run_limit" in limit
    assert limit.count("inputs.") == 1 and "inputs." not in cloud
    assert "needs: limit" in cloud and "environment: conformance" in cloud
    assert "github.ref == 'refs/heads/main'" in cloud
    assert "needs.limit.result == 'success'" in cloud
    assert re.search(r"(?m)^    permissions:\n      contents: read\n    \w", cloud)
    assert "needs.limit.outputs.adapters != '' && needs.limit.outputs.adapters != '[]'" in cloud
    assert "provider: ${{ fromJSON(needs.limit.outputs.adapters) }}" in cloud
    assert "ANYCOMPUTE_RUN_ID: ${{ github.run_id }}" in cloud
    assert "ANYCOMPUTE_CONFORMANCE_PROVIDER: ${{ matrix.provider }}" in cloud
    assert "timeout-minutes: 90" in cloud and "fail-fast: false" in cloud
    assert "uv sync --locked --python 3.12 --extra \"$ANYCOMPUTE_CONFORMANCE_PROVIDER\"" in cloud
    baseline = set(re.findall(r"uses: (\S+)", job("ci", "logic-tests")))
    assert set(re.findall(r"uses: (\S+)", cloud)) == baseline
    assert set(re.findall(r"uses: (\S+)", limit)) == {action for action in baseline if "checkout" not in action}


def test_cloud_jobs_are_not_part_of_pull_request_checks():
    workflow = CONFORMANCE.read_text()
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", triggers) == ["schedule", "workflow_dispatch"]
    cloud = job("conformance", "cloud")
    condition = cloud.split("    if: >-\n", 1)[1].split("    environment:", 1)[0]
    assert "(github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')" in condition


@pytest.mark.parametrize("ref, expected", [("refs/heads/main", 0), ("refs/heads/Main", 1)])
def test_gate_fetches_exact_main_sha_without_checkout_credentials(ref, expected):
    checkout = step(job("conformance", "limit"), "Check out public code")
    assert 'GIT_TERMINAL_PROMPT: "0"' in checkout
    sha = "a" * 40
    result = bash("git() { printf '%s\\n' \"$*\"; }\n" + script(checkout),
                  GITHUB_REF=ref, GITHUB_REPOSITORY="example/compute", GITHUB_SHA=sha)
    assert result.returncode == expected, result.stderr
    assert result.stdout.splitlines() == ([] if expected else [
        "init --quiet",
        f"-c credential.helper= -c http.extraHeader= fetch --no-tags --depth=1 https://github.com/example/compute.git {sha}",
        "checkout --quiet --detach FETCH_HEAD",
    ])


@pytest.mark.parametrize("ref, actual, verified, expected", [
    ("refs/heads/main", "2", "2", 0), ("refs/heads/main", "3", "2", 1),
    ("refs/heads/main", "1", "2", 1), ("refs/heads/main", "", "", 1),
    ("refs/heads/Main", "2", "2", 1),
])
def test_cloud_attempt_verification_must_be_exact_before_keys(ref, actual, verified, expected):
    cloud = job("conformance", "cloud")
    attempt = step(cloud, "Verify the counted attempt")
    assert "id: attempt" in attempt
    assert "VERIFIED_ATTEMPT: ${{ needs.limit.outputs.verified_attempt }}" in attempt
    assert "secrets." not in cloud.split("      - name: Sweep and run conformance", 1)[0]
    result = bash(script(attempt), GITHUB_REF=ref, GITHUB_RUN_ATTEMPT=actual, VERIFIED_ATTEMPT=verified)
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("sweep, suite, expected", [(0, 0, 0), (125, 0, 125), (0, 5, 5)])
def test_cloud_sweeps_first_and_never_treats_zero_collected_tests_as_success(sweep, suite, expected):
    test_step = step(job("conformance", "cloud"), "Sweep and run conformance")
    assert 'if: ${{ steps.attempt.outcome == \'success\' }}' in test_step
    assert "continue-on-error" not in test_step
    assert "timeout-minutes: 60" in test_step
    assert 'ANYCOMPUTE_CONFORMANCE_LIVE: "1"' in test_step
    assert "ANYCOMPUTE_VERIFIED_ATTEMPT: ${{ needs.limit.outputs.verified_attempt }}" in test_step
    assert dict(re.findall(r"(\w+): \$\{\{ matrix\.provider == '(\w+)' && secrets\.\1", test_step)) == {
        "MODAL_TOKEN_ID": "modal", "MODAL_TOKEN_SECRET": "modal", "BOAT_API_KEY": "boat",
        "E2B_API_KEY": "e2b", "DAYTONA_API_KEY": "daytona",
    }
    fake_uv = r'''
      uv() {
        printf '%s\n' "$*"
        if [[ "$3" == pytest ]]; then return "$SUITE"; fi
        return "$SWEEP"
      }
    '''
    result = bash(fake_uv + script(test_step), ANYCOMPUTE_CONFORMANCE_PROVIDER="modal",
                  SWEEP=str(sweep), SUITE=str(suite))
    calls = ["run --locked anycompute gc --all --provider modal --json"]
    if not sweep:
        calls.append("run --locked pytest -q -m live_conformance tests/conformance")
    assert result.returncode == expected, result.stderr
    assert result.stdout.splitlines() == calls


def test_cloud_always_cleanup_requires_verified_attempt_and_tries_only_three_labels():
    cleanup = step(job("conformance", "cloud"), "Clean up this run")
    assert "if: ${{ always() && steps.attempt.outcome == 'success' && steps.install.outcome == 'success' }}" in cleanup
    assert "timeout-minutes: 20" in cleanup
    assert "env: *provider_env" in cleanup
    fake_uv = r'''
      uv() {
        printf '%s\n' "$*"
        [[ "$*" != *-lc* ]]
      }
    '''
    parent = "abcdefghij" * 4
    result = bash(fake_uv + script(cleanup), ANYCOMPUTE_CONFORMANCE_PROVIDER="boat", ANYCOMPUTE_RUN_ID=parent)
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == [
        f"run --locked anycompute gc --provider boat --run={run_id} --json"
        for run_id in (parent, parent[:37] + "-lc", parent[:37] + "-kc")
    ]

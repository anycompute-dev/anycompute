from dataclasses import replace
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
        for name, enabled in [("docker", True), ("modal", True), ("e2b", False), ("daytona", False), ("boat", True)]
    ))
    return env, tmp_path / "output"


def main(env):
    return run_gate(env, now=NOW)


@pytest.fixture
def trial_gate(gate, monkeypatch):
    monkeypatch.setattr(providers, "PROVIDERS", tuple(
        replace(spec, caps=replace(spec.caps, live_run=False)) if spec.name in ("modal", "boat") else spec
        for spec in providers.PROVIDERS
    ))
    return gate


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
    env.update(GITHUB_EVENT_NAME="schedule", REQUESTED_ADAPTERS="",
               ANYCOMPUTE_MODAL_TRIAL_MONTH="2026-11", ANYCOMPUTE_BOAT_TRIAL_MONTH="2026-11")
    monkeypatch.setattr(providers, "PROVIDERS", tuple(
        replace(spec, caps=replace(spec.caps, live_run=spec.name in enabled)) for spec in providers.PROVIDERS
    ))
    history[0].append(Reply(run(event="schedule", title="Weekly conformance")))

    assert main(env) == 0
    assert output.read_text() == f"verified_attempt=2\nadapters={expected}\n"
    assert ("no conformance tests ran" in capsys.readouterr().out) == (expected == "[]")


@pytest.mark.parametrize("value", ["", "docker", "unknown", "Modal", "modal,modal", "modal,", "modal, boat",
                                   "modal\n", "modal;boat", "modal adapters=boat"])
def test_dispatch_rejects_invalid_adapter_input_before_history(gate, history, capsys, value):
    env, output = gate
    env["REQUESTED_ADAPTERS"] = value

    assert main(env) == 1
    assert "adapters input" in capsys.readouterr().err
    assert not history[1]
    assert not output.exists()


def test_manual_e2b_counts_only_named_dispatches(gate, history, capsys):
    env, output = gate
    env["REQUESTED_ADAPTERS"] = "e2b"
    weekly = run(1, attempt=5, event="schedule", title="Weekly conformance")
    trial = run(title="adapters=e2b")
    history[0].append(Reply(weekly, trial))
    assert main(env) == 0
    assert output.read_text() == 'verified_attempt=2\nadapters=["e2b"]\n'
    output.unlink()

    history[0].append(Reply(weekly, run(2, attempt=3, title="adapters=e2b"), trial))
    assert main(env) == 1
    assert "e2b: 5 extra runs exceed 4" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(("provider", "other"), [("modal", "boat"), ("boat", "modal")])
def test_approved_trial_allows_one_dispatch_per_provider(trial_gate, history, provider, other):
    env, output = trial_gate
    env.update(REQUESTED_ADAPTERS=provider, GITHUB_RUN_ATTEMPT="1")
    env[f"ANYCOMPUTE_{provider.upper()}_TRIAL_MONTH"] = "2026-11"
    history[0].append(Reply(
        run(1, event="schedule", title="Weekly conformance"),
        run(2, attempt=1, title=f"adapters={other}"),
        run(attempt=1, title=f"adapters={provider}"),
    ))

    assert main(env) == 0
    assert output.read_text() == f'verified_attempt=1\nadapters=["{provider}"]\n'


@pytest.mark.parametrize(("provider", "approval"), [("modal", None), ("boat", "2026-10"), ("modal", "2026-11\n")])
def test_trial_requires_its_own_current_month_approval(trial_gate, history, capsys, provider, approval):
    env, output = trial_gate
    env.update(REQUESTED_ADAPTERS=provider, GITHUB_RUN_ATTEMPT="1",
               ANYCOMPUTE_MODAL_TRIAL_MONTH="2026-11", ANYCOMPUTE_BOAT_TRIAL_MONTH="2026-11")
    variable = f"ANYCOMPUTE_{provider.upper()}_TRIAL_MONTH"
    if approval is None:
        del env[variable]
    else:
        env[variable] = approval

    assert main(env) == 1
    assert f"{provider}: trial has no approval for this UTC month" in capsys.readouterr().err
    assert not history[1] and not output.exists()


@pytest.mark.parametrize(("adapters", "attempt", "prior", "message"), [
    ("modal", 2, None, "one first attempt"),
    ("boat", 1, "adapters=boat", "one first attempt"),
    ("modal,boat", 1, None, "must name one provider"),
    ("boat,e2b", 1, None, "must name one provider"),
])
def test_approved_trial_refuses_reruns_extra_runs_and_mixed_dispatches(trial_gate, history, capsys,
                                                                      adapters, attempt, prior, message):
    env, output = trial_gate
    env.update(REQUESTED_ADAPTERS=adapters, GITHUB_RUN_ATTEMPT=str(attempt),
               ANYCOMPUTE_MODAL_TRIAL_MONTH="2026-11", ANYCOMPUTE_BOAT_TRIAL_MONTH="2026-11")
    attempts = [run(1, attempt=1, title=prior)] if prior else []
    history[0].append(Reply(*attempts, run(attempt=attempt, title=f"adapters={adapters}")))

    assert main(env) == 1
    assert message in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(("adapters", "attempt", "prior_title", "extra", "month", "error"), [
    ("daytona", 1, "adapters=daytona", False, 11, None),
    ("daytona", 1, None, False, 11, "requires the original trial and one fresh dispatch"),
    ("daytona", 1, "adapters=boat", False, 11, "requires the original trial and one fresh dispatch"),
    ("daytona", 1, "adapters=daytona", True, 11, "requires the original trial and one fresh dispatch"),
    ("daytona", 2, "adapters=daytona", False, 11, "requires the original trial and one fresh dispatch"),
    ("daytona,e2b", 1, "adapters=daytona", False, 11, "naming only Daytona"),
    ("daytona", 1, "adapters=daytona", False, 12, "limited to November 2026 UTC"),
])
def test_daytona_allows_one_fresh_verification_after_the_trial(gate, history, capsys, adapters, attempt, prior_title, extra, month, error):
    env, output = gate
    env.update(REQUESTED_ADAPTERS=adapters, GITHUB_RUN_ATTEMPT=str(attempt))
    date = f"2026-{month:02d}-23T00:00:00Z"
    weekly = run(1, attempt=5, event="schedule", title="Weekly conformance") | {"created_at": date}
    attempts = [weekly]
    if prior_title:
        attempts.append(run(45435204082, attempt=1, title=prior_title) | {"created_at": date})
    if extra:
        attempts.append(run(2, attempt=1, title="adapters=daytona") | {"created_at": date})
    attempts.append(run(attempt=attempt, title=f"adapters={adapters}") | {"created_at": date})
    history[0].append(Reply(*attempts))

    assert run_gate(env, now=NOW.replace(month=month)) == (1 if error else 0)
    if error:
        assert error in capsys.readouterr().err
        assert not output.exists()
    else:
        assert output.read_text() == 'verified_attempt=1\nadapters=["daytona"]\n'


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
    suite = step(docker, "Run Docker conformance")
    assert "run: uv run --locked pytest -q -m live_conformance tests/conformance" in suite
    assert "github.event_name == 'push' && github.ref == 'refs/heads/main' && 'project-run' || 'user-run'" in suite


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
    for provider in ("MODAL", "BOAT"):
        variable = f"ANYCOMPUTE_{provider}_TRIAL_MONTH"
        assert variable + ": ${{ vars." + variable + " }}" in limit
        assert variable not in cloud
    assert "uv run --locked python -m tests.conformance.check_run_limit" in limit
    assert limit.count("inputs.") == 2 and "inputs." not in cloud
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
    artifact = step(cloud, "Upload the conformance report")
    assert set(re.findall(r"uses: (\S+)", cloud)) == baseline | set(re.findall(r"uses: (\S+)", artifact))
    assert set(re.findall(r"uses: (\S+)", limit)) == {action for action in baseline if "checkout" not in action}


def test_cloud_jobs_are_not_part_of_pull_request_checks():
    workflow = CONFORMANCE.read_text()
    triggers = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"(?m)^  (\w+):", triggers) == ["schedule", "workflow_dispatch"]
    cloud = job("conformance", "cloud")
    condition = cloud.split("    if: >-\n", 1)[1].split("    environment:", 1)[0]
    assert "(github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')" in condition


@pytest.mark.parametrize(("workflow", "job_name", "provider"), [
    ("ci", "docker", "docker"), ("conformance", "cloud", "${{ matrix.provider }}"),
])
def test_workflow_uploads_only_the_conformance_report(workflow, job_name, provider):
    upload = step(job(workflow, job_name), "Upload the conformance report")
    assert "steps.suite.outcome == 'success' || steps.suite.outcome == 'failure'" in upload
    assert "if-no-files-found: ${{ steps.suite.outcome == 'success' && 'error' || 'ignore' }}" in upload
    assert f"name: conformance-{provider}-${{{{ github.run_attempt }}}}" in upload
    assert "path: ${{ runner.temp }}/conformance-report.json" in upload
    assert "secrets." not in upload


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
    assert 'MODAL_MAX_THROTTLE_WAIT: "0"' in test_step
    assert "ANYCOMPUTE_VERIFIED_ATTEMPT: ${{ needs.limit.outputs.verified_attempt }}" in test_step
    assert dict(re.findall(r"(\w+): \$\{\{ matrix\.provider == '(\w+)' && secrets\.\1", test_step)) == {
        "MODAL_TOKEN_ID": "modal", "MODAL_TOKEN_SECRET": "modal", "BOAT_API_KEY": "boat",
        "E2B_API_KEY": "e2b", "DAYTONA_API_KEY": "daytona",
    }
    fake_uv = r'''
      uv() {
        printf '%s\n' "$*"
        if [[ "$3" == pytest ]]; then
          [[ "$ANYCOMPUTE_REPORT_SOURCE" == project-run ]] || return 1
          [[ "$ANYCOMPUTE_CONFORMANCE_REPORT" == "$RUNNER_TEMP/conformance-report.json" ]] || return 1
          return "$SUITE"
        fi
        return "$SWEEP"
      }
    '''
    result = bash(fake_uv + script(test_step), ANYCOMPUTE_CONFORMANCE_PROVIDER="modal",
                  SWEEP=str(sweep), SUITE=str(suite), RUNNER_TEMP="/tmp")
    calls = ["run --locked anycompute gc --all --provider modal --json"]
    if not sweep:
        calls.append("run --locked pytest -q -m live_conformance tests/conformance")
    assert result.returncode == expected, result.stderr
    assert result.stdout.splitlines() == calls


@pytest.mark.parametrize(("provider", "flag"), [
    ("e2b", "--require-e2b-base-size"), ("daytona", "--require-daytona-readiness"),
])
@pytest.mark.parametrize("preflight", [0, 1])
def test_cloud_workflow_checks_readiness_before_the_sweep_or_suite(provider, flag, preflight):
    test_step = step(job("conformance", "cloud"), "Sweep and run conformance")
    fake_uv = r'''
      uv() {
        printf '%s\n' "$*"
        if [[ "$3" == --no-sync ]]; then
          return "$PREFLIGHT"
        fi
      }
    '''
    result = bash(fake_uv + script(test_step), ANYCOMPUTE_CONFORMANCE_PROVIDER=provider,
                  PREFLIGHT=str(preflight), RUNNER_TEMP="/tmp")
    calls = [f"run --locked --no-sync python -m tests.conformance.cloud_account {provider} {flag}"]
    if not preflight:
        calls.extend([f"run --locked anycompute gc --all --provider {provider} --json",
                      "run --locked pytest -q -m live_conformance tests/conformance"])
    assert result.returncode == preflight, result.stderr
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


def test_verify_dispatch_uses_the_same_counted_adapters(gate, history):
    env, output = gate
    env["REQUESTED_VERIFY"] = "1"
    history[0].append(Reply(run()))
    assert main(env) == 0
    assert output.read_text() == 'verified_attempt=2\nadapters=["modal","boat"]\nverify=true\n'


def test_verify_stays_disabled_with_both_individual_trials_approved(trial_gate, history, capsys):
    env, output = trial_gate
    env.update(REQUESTED_VERIFY="1", GITHUB_RUN_ATTEMPT="1",
               ANYCOMPUTE_MODAL_TRIAL_MONTH="2026-11", ANYCOMPUTE_BOAT_TRIAL_MONTH="2026-11")

    assert main(env) == 1
    assert "Verify requires both adapters enabled for live conformance" in capsys.readouterr().err
    assert not history[1] and not output.exists()


@pytest.mark.parametrize("provider", ["modal", "boat"])
def test_verify_dispatch_refuses_when_either_provider_exceeds_its_count(gate, history, capsys, provider):
    env, output = gate
    env["REQUESTED_VERIFY"] = "1"
    previous = [run(i, attempt=1, title=f"adapters={provider}") for i in range(1, 4)]
    history[0].append(Reply(*previous, run()))
    assert main(env) == 1
    assert not output.exists()
    assert f"{provider}: 5 extra runs exceed 4" in capsys.readouterr().err


@pytest.mark.parametrize(("event", "adapters", "mode"), [
    ("schedule", "", "1"),
    ("workflow_dispatch", "modal", "1"),
    ("workflow_dispatch", "boat,modal", "1"),
    ("workflow_dispatch", "modal,boat", "arbitrary"),
])
def test_verify_gate_rejects_other_events_or_adapters_before_history(gate, history, event, adapters, mode):
    env, output = gate
    env.update(GITHUB_EVENT_NAME=event, REQUESTED_ADAPTERS=adapters, REQUESTED_VERIFY=mode)
    assert main(env) == 1
    assert not history[1]
    assert not output.exists()


def test_verify_job_replaces_the_provider_matrix_and_gates_access_to_both_secrets():
    limit, cloud, verify = (job("conformance", name) for name in ("limit", "cloud", "verify"))
    assert "verify: ${{ steps.history.outputs.verify }}" in limit
    assert "needs.limit.outputs.verify != 'true'" in cloud
    assert "needs: limit" in verify and "environment: conformance" in verify
    assert "github.ref == 'refs/heads/main'" in verify and "github.event_name == 'workflow_dispatch'" in verify
    assert "needs.limit.result == 'success'" in verify and "needs.limit.outputs.verify == 'true'" in verify
    assert """needs.limit.outputs.adapters == '["modal","boat"]'""" in verify
    assert "inputs." not in verify and "persist-credentials: false" in verify
    assert "secrets." not in verify.split("      - name: Sweep and run the comparison", 1)[0]
    attempt = step(verify, "Verify the counted attempt")
    assert script(attempt) == script(step(cloud, "Verify the counted attempt"))
    test_step = step(verify, "Sweep and run the comparison")
    assert "steps.attempt.outcome == 'success'" in test_step
    assert "ANYCOMPUTE_VERIFIED_ADAPTERS: ${{ needs.limit.outputs.adapters }}" in test_step
    assert "ANYCOMPUTE_VERIFIED_ATTEMPT: ${{ needs.limit.outputs.verified_attempt }}" in test_step
    assert set(re.findall(r"secrets\.(\w+)", test_step)) == {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "BOAT_API_KEY"}
    assert 'MODAL_MAX_THROTTLE_WAIT: "0"' in test_step
    assert "uv run --locked --no-sync python -m tests.conformance.verify_workload" in test_step
    upload = step(cloud, "Upload the conformance report")
    assert set(re.findall(r"uses: (\S+)", verify)) == (
        set(re.findall(r"uses: (\S+)", cloud)) - set(re.findall(r"uses: (\S+)", upload))
    )


@pytest.mark.parametrize("provider", ["modal", "boat"])
def test_verify_cleanup_of_each_provider_runs_even_after_the_other_fails(provider):
    verify = job("conformance", "verify")
    modal_clean = step(verify, "Clean up the Modal run")
    boat_clean = step(verify, "Clean up the boat.dev run")
    for cleanup in (modal_clean, boat_clean):
        assert "always() && steps.attempt.outcome == 'success' && steps.install.outcome == 'success'" in cleanup
        assert "timeout-minutes: 20" in cleanup
    assert "secrets.BOAT_API_KEY" not in modal_clean and "secrets.MODAL_TOKEN" not in boat_clean
    assert "run: *verify_cleanup" in boat_clean
    command = dedent(modal_clean.split("        run: &verify_cleanup |\n", 1)[1])
    fake_uv = r'''uv() { printf '%s\n' "$*"; [[ "$*" != *-lc* ]]; }'''
    run_id = "1234567890" * 4
    result = bash(fake_uv + "\n" + command, PROVIDER=provider, ANYCOMPUTE_RUN_ID=run_id)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"run --locked --no-sync anycompute gc --provider {provider} --run={label} --json"
        for label in (run_id, run_id[:37] + "-lc", run_id[:37] + "-kc")
    ]

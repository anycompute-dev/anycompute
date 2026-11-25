"""Run one fixed, cost-counted cross-provider comparison in GitHub Actions."""

import json
import os
from pathlib import Path
import sys
import tomllib

from anycompute import engine
from anycompute.lifecycle import _Observer
from tests.conformance.runner import (
    TEST_TIMEOUT_S, VERIFY_ADAPTERS, ConformanceFailure, ConformanceRunner, _discard_process, _read_report,
    load_prices, select_provider,
)

_TASK = Path(__file__).with_name("verify_task.toml")


def _sweep(runner):
    child = runner._start(("gc", "--all", "--provider", runner.spec.name, "--json"))
    try:
        code, report = _read_report(child, TEST_TIMEOUT_S)
    finally:
        _discard_process(child)
    rows = report.get("sandboxes")
    if code or report["error"] is not None or report.get("provider") != runner.spec.name or (
        report.get("run_id") is not None or report.get("dry_run") is not False or not isinstance(rows, list)
        or any(not isinstance(row, dict) or row.get("action") not in ("deleted", "kept") for row in rows)
    ):
        raise ConformanceFailure("Could not confirm the pre-run sweep")


def _verification(env):
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or (
        json.loads(env.get("ANYCOMPUTE_VERIFIED_ADAPTERS", "null")) != list(VERIFY_ADAPTERS)
    ):
        raise ConformanceFailure("Verify requires a counted manual dispatch for modal,boat")
    run_id = engine._check_run_id(env["ANYCOMPUTE_RUN_ID"])
    if run_id != env.get("GITHUB_RUN_ID"):
        raise ConformanceFailure("Verify run ID did not match the current GitHub run")
    specs = [select_provider({**env, "ANYCOMPUTE_CONFORMANCE_PROVIDER": name}) for name in VERIFY_ADAPTERS]
    if any(not spec.caps.live_run for spec in specs):
        raise ConformanceFailure("Verify requires both adapters enabled for live conformance")
    if not all(env.get(name) for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "BOAT_API_KEY")):
        raise ConformanceFailure("Verify requires both providers' conformance credentials")
    with _TASK.open("rb") as file:
        task = tomllib.load(file)
    if "providers" in task:
        raise ConformanceFailure("The conformance workload must use the provider defaults")

    prices = load_prices()
    runners = [ConformanceRunner(spec, run_id, observer=_Observer(spec, run_id), prices=prices) for spec in specs]
    effective_ttls = [runner._reserve(task["ttl"]) for runner in runners]
    for runner in runners:
        _sweep(runner)
    child = runners[0]._start(("verify", str(_TASK), "--providers", ",".join(VERIFY_ADAPTERS), "--json"))
    try:
        code, report = _read_report(child, TEST_TIMEOUT_S * len(runners))
    finally:
        _discard_process(child)
    rows = report.get("rows")
    if code or report["error"] is not None or report.get("source") != "project-run" or (
        not isinstance(report.get("anycompute_version"), str) or not report["anycompute_version"]
        or "suite_version" not in report or report["suite_version"] is not None
        or not isinstance(rows, list) or [row.get("provider") for row in rows] != list(VERIFY_ADAPTERS)
    ):
        raise ConformanceFailure("Cross-provider verification failed or returned an unexpected report")
    for runner, row, ttl in zip(runners, rows, effective_ttls, strict=True):
        sandbox_id = row.get("sandbox_id")
        if row["error"] is not None or row.get("run_id") != run_id or row.get("effective_ttl") != ttl or (
            not engine._valid_sandbox_id(sandbox_id) or "***" in sandbox_id
            or "lifecycle" not in row or row["lifecycle"] is not None or row.get("outside_contract") is not False
            or any(row.get(field) != "pass" for field in ("exit_code", "output", "files", "cleanup"))
        ):
            raise ConformanceFailure("Verify report did not match the reserved workload or confirm cleanup")
        runner._record((sandbox_id,))
        runner._scan(engine._clock.now() + engine._CLEANUP_S)
    return report


def main() -> int:
    try:
        print(json.dumps(_verification(os.environ), allow_nan=False))
    except ConformanceFailure as error:
        # The runner checks used here raise fixed messages without provider or subprocess output.
        reason = str(error)
    except Exception as error:
        reason = type(error).__name__
    else:
        return 0
    print(f"Cross-provider verification stopped. {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

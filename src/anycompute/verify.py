"""Compare one workload across providers using the engine's verified results."""

import os
from dataclasses import dataclass
from functools import partial
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
from subprocess import SubprocessError

from . import engine, lifecycle
from .contract import AnycomputeError, CleanupError, ProviderError, Result

_PARTIAL_RULES = ("lifetime", "pre_start_window", "create_retry", "exec_retry", "labels_at_create")


@dataclass(frozen=True)
class _Task:
    command: list[str]
    inputs: dict[str, bytes]
    read: list[str]
    ttl: int
    exit_code: int
    compare: str
    options: dict


@dataclass
class _Outcome:
    row: dict
    result: Result | None
    error: AnycomputeError | KeyboardInterrupt | None


def _version(package):
    if package:
        try:
            return version(package)
        except PackageNotFoundError:
            pass
    return None


def _initial_report():
    source = "project-run" if os.environ.get("ANYCOMPUTE_REPORT_SOURCE") == "project-run" else "user-run"
    return {"rows": [], "summary": None, "anycompute_version": _version("anycompute"),
            "source": source, "suite_version": None}


def _lifecycle_check(spec, run_id, ttl, ctx):
    child_id = lifecycle._child_run_id(run_id, "-lc")

    def cleanup(child_run_id):
        try:
            _, error = engine._gc(spec.name, run_id=child_run_id)
        except AnycomputeError as failed:
            error = failed
        if isinstance(error, CleanupError):
            raise error
        if error is not None:
            raise CleanupError(ctx.redact(f"Could not clean up lifecycle sandbox: {error}"),
                               run_id=child_run_id, sandbox_id=error.sandbox_id)

    observer = lifecycle._Observer(spec, child_id)
    engine._pacer_for(spec, None).wait(ctx)
    try:
        return lifecycle._kill_client(
            run_id, effective_ttl=ttl, lifetime=spec.caps.lifetime, suffix="-lc",
            launch=partial(lifecycle._launch_child, spec.name), observer=observer, cleanup=cleanup, clock=ctx.clock,
        )
    except (OSError, SubprocessError) as error:
        message = ctx.redact(f"Lifecycle child failed: {error}")
    raise ProviderError(message, run_id=child_id)


def _run_one(task, spec, run_id, ctx, check_lifecycle):
    name = spec.name
    result = error = None
    cleanup = "fail"
    lifecycle_status = None
    details = {}
    try:
        result = engine.run(task.command, provider=name, files=task.inputs, read=task.read,
                            ttl=task.ttl, runtime=True, provider_options=task.options, run_id=run_id)
        cleanup = "pass"
        if check_lifecycle:
            if result.outside_contract:
                details["lifecycle_options"] = "default options"
            checked = _lifecycle_check(spec, run_id, result.effective_ttl, ctx)
            lifecycle_status = checked.status
            if checked.reason:
                details["lifecycle_reason"] = ctx.redact(checked.reason)
    except (AnycomputeError, KeyboardInterrupt) as failure:
        error = failure
        if isinstance(error, CleanupError) and error.result is not None:
            result = error.result
        if result is None and getattr(error, "sandbox_seconds", None) is not None:
            cleanup = "pass"
        if check_lifecycle:
            lifecycle_status = "fail"
    runtime = {key: ctx.redact(result.runtime[key]) for key in ("python", "arch", "api_host")
               if key in result.runtime} if result and result.runtime else None
    sdk_version = _version(spec.caps.sdk_package)
    seconds = result.sandbox_seconds if result else getattr(error, "sandbox_seconds", None)
    row = {
        "provider": ctx.redact(name),
        "sandbox_id": ctx.redact(result.sandbox_id) if result else None,
        "run_id": ctx.redact(result.run_id) if result else None,
        "effective_ttl": result.effective_ttl if result else None,
        "exit_code": "pass" if result and result.exit_code == task.exit_code else "fail",
        "output": "pass",
        "files": "pass",
        "cleanup": cleanup,
        "lifecycle": lifecycle_status,
        "partial_caps": [name for name in _PARTIAL_RULES if getattr(spec.caps, name) in ("partial", False)],
        "runtime": runtime,
        "error": None,
        "sandbox_seconds": None if isinstance(seconds, float) and not isfinite(seconds) else seconds,
        "outside_contract": result.outside_contract if result else bool(task.options.get(name)),
        "provider_sdk_version": ctx.redact(sdk_version) if sdk_version else None,
        "details": details,
    }
    return _Outcome(row, result, error)


def _hash(data):
    return sha256(data).hexdigest() if data is not None else None


def _compare(outcome, reference, read, redact):
    row, result = outcome.row, outcome.result
    other = reference.result
    assert result is not None and other is not None
    details = row["details"]
    for name in ("stdout", "stderr"):
        first, actual = getattr(other, name), getattr(result, name)
        if first != actual:
            row["output"] = "differs"
            details[name] = {"reference": redact(first), "actual": redact(actual)}
    files = []
    for path in dict.fromkeys(read):
        first, actual = _hash(other.files.get(path)), _hash(result.files.get(path))
        if first != actual:
            files.append({"path": redact(path), "reference_sha256": redact(first) if first else None,
                          "sha256": redact(actual) if actual else None})
    if files:
        row["files"] = "differs"
        details["files"] = files
    if row["output"] == "differs" or files:
        details["reference_provider"] = reference.row["provider"]


def _summary(outcomes):
    errors, failures, differences, runtime, partial_lifetime = [], [], [], [], []
    for outcome in outcomes:
        row = outcome.row
        name = row["provider"]
        if outcome.error is not None:
            errors.append(f"error on {name}: {type(outcome.error).__name__}")
        if outcome.result is not None:
            checks = {"Exit code": row["exit_code"], "Cleanup": row["cleanup"], "Lifecycle": row["lifecycle"]}
            if failed := [field for field, status in checks.items() if status == "fail"]:
                failures.append(f"fails on {name}: {', '.join(failed)}")
            if fields := [field for field in ("stdout", "stderr", "files") if field in row["details"]]:
                differences.append(f"differs on {name}: {', '.join(fields)}")
        if row["runtime"]:
            values = row["runtime"]
            runtime.append((name, values.get("python", "unknown"), values.get("arch", "unknown")))
        if "lifetime" in row["partial_caps"]:
            partial_lifetime.append(name)
    clauses = errors + failures + differences
    failed = bool(clauses)
    if not failed:
        clauses.append("portable on " + ", ".join(item.row["provider"] for item in outcomes))
    if len({(python, arch) for _, python, arch in runtime}) > 1:
        clauses.append("runtime differs: " + ", ".join(f"{name} ({python}, {arch})" for name, python, arch in runtime))
    if partial_lifetime:
        clauses.append("partial lifetime on " + ", ".join(partial_lifetime))
    return "; ".join(clauses), int(failed)


def _run(task, specs, ctx, *, check_lifecycle=False):
    run_id = ctx.run_id
    engine._checked_options(ctx, task.options)
    engine._file_inputs(task.inputs, task.read)
    engine._validate_argv(task.command)
    outcomes = []
    for spec in specs:
        outcome = _run_one(task, spec, run_id, ctx, check_lifecycle)
        outcomes.append(outcome)
        if isinstance(outcome.error, KeyboardInterrupt) or getattr(outcome.error, "interrupted", False):
            break
    reference = next((item for item in outcomes if item.error is None), None)
    if reference is not None and task.compare == "all":
        for outcome in outcomes:
            if outcome.result is not None and outcome is not reference:
                _compare(outcome, reference, task.read, ctx.redact)
    summary, code = _summary(outcomes)
    return outcomes, summary, code

"""Spend checks and subprocess orchestration for conformance tests."""

from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_CEILING, localcontext
from functools import partial
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import NoReturn

from anycompute import engine, providers
from anycompute.contract import AnycomputeError, CleanupError, ConfigError, ProviderCaps, ProviderSpec, SandboxState
from anycompute.lifecycle import _LifecycleResult, _child_run_id, _kill_client, _launch_child, _poll, _wait_for_end

MAX_CREATES = 15
MAX_TTL_S = 120
# Project adapter guards enforce these submission limits on the locked SDKs.
_PROJECT_CREATE_ATTEMPTS = {"modal": 4, "daytona": 1}
_MODAL_MAX_CREATES = 7  # Stay below the previous 30-allocation ceiling.
VERIFY_ADAPTERS = ("modal", "boat")
MANUAL_TRIAL_ADAPTERS = frozenset({"e2b", "daytona"})
_DAYTONA_TRIAL_MONTH = (2026, 11)
GRACE_S = engine._GRACE_S
TEST_TIMEOUT_S = 540
BUDGET_USD = Decimal("2")
SECONDS_PER_HOUR = 3600
EXTRA_RUNS_PER_MONTH = 4
_PAGE_SIZE = 100
_SEARCH_CAP = 1000
_ADAPTER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_USD_CONTEXT = Context(prec=28, rounding=ROUND_CEILING)

PriceTable = Mapping[str, Mapping[str, Mapping[str, object]]]


class SpendLimitError(ValueError):
    """A launch would exceed its adapter's spend or sandbox allowance."""


class RunLimitError(ValueError):
    """This month's run history is incomplete or does not allow the current attempt."""


@dataclass(frozen=True)
class Reservation:
    sandboxes: int = 0
    # Accumulate dollars times 3600 so $2 is compared before division.
    usd_seconds: Decimal = Decimal(0)

    @property
    def usd(self) -> Decimal:
        with localcontext(_USD_CONTEXT):
            return self.usd_seconds / SECONDS_PER_HOUR


def load_prices(path: Path = Path(__file__).with_name("prices.toml")) -> PriceTable:
    with path.open("rb") as prices:
        return tomllib.load(prices, parse_float=Decimal)


def _integer(
    value: object, label: str, minimum: int = 0, *, error: type[ValueError] = ValueError,
) -> int:
    if type(value) is not int or value < minimum:
        raise error(f"{label} must be an integer >= {minimum}")
    return value


def _money(value: object, label: str) -> Decimal:
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be a finite nonnegative USD amount")
    return value


def reserve_launch(
    prices: PriceTable, provider: str, prior: Reservation, effective_ttls: Sequence[int],
    *, caps: ProviderCaps | None = None,
) -> Reservation:
    """Return cumulative reservation for one adapter; pass one TTL per possible create."""
    sandboxes = _integer(prior.sandboxes, "reserved sandbox count")
    total = _money(prior.usd_seconds, "reserved cost")
    ttls = tuple(effective_ttls)
    partial_retry = caps is not None and caps.create_retry == "partial"
    partial_lifetime = caps.lifetime == "partial" if caps is not None else provider in {"boat", "daytona"}
    multiplier = _PROJECT_CREATE_ATTEMPTS.get(provider, 2 if partial_retry else 1)
    max_creates = _MODAL_MAX_CREATES if provider == "modal" else MAX_CREATES
    sandboxes += multiplier * len(ttls)
    if sandboxes > multiplier * max_creates:
        raise SpendLimitError(f"{provider}: launch would exceed {max_creates} creates")

    try:
        terms = prices[provider]
        rate = _money(terms["compute_usd_per_hour"]["value"], "compute_usd_per_hour")
        fee = _money(terms["start_fee_usd"]["value"], "start_fee_usd")
        storage = _money(terms["stopped_storage_usd_per_week"]["value"], "stopped_storage_usd_per_week")
        granularity = _integer(terms["granularity_s"]["value"], "granularity_s", 1)
        minimum = _integer(terms["min_charge_s"]["value"], "min_charge_s")
    except (KeyError, TypeError):
        raise ValueError(f"{provider}: missing or invalid price terms") from None

    with localcontext(_USD_CONTEXT):
        if partial_lifetime:
            fee += storage
        for ttl in ttls:
            seconds = _integer(ttl, "effective TTL") + GRACE_S
            if ttl > MAX_TTL_S:
                raise ValueError(f"effective TTL must be <= {MAX_TTL_S} seconds")
            billed = max(minimum, (seconds + granularity - 1) // granularity * granularity)
            total += multiplier * (rate * billed + fee * SECONDS_PER_HOUR)
        result = Reservation(sandboxes, total)
        if total > BUDGET_USD * SECONDS_PER_HOUR:
            raise SpendLimitError(f"{provider}: reservation ${result.usd} exceeds ${BUDGET_USD}")
    return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate key")
    return result


def _dispatch_adapters(title: str) -> set[str]:
    words = title.split()
    tokens = [i for i, word in enumerate(words) if word.startswith("adapters=")]
    if len(tokens) == 1:
        i = tokens[0]
        names = words[i].removeprefix("adapters=").split(",")
        if (i + 1 == len(words) or not words[i + 1].startswith(",")) and all(
            _ADAPTER_NAME.fullmatch(name) for name in names
        ) and len(names) == len(set(names)):
            return set(names)
    raise RunLimitError("Invalid adapters= token in dispatch display_title")


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RunLimitError(f"{label} must have a timezone")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise RunLimitError(f"Invalid {label} timestamp") from None


def check_run_limit(
    fetch_page: Callable[[datetime, int, int], tuple[int, str | bytes]],
    *,
    current_run_id: int,
    current_attempt: int,
    now: datetime,
    live_adapters: Collection[str],
) -> int:
    """Return the counted attempt, or refuse before any provider calls.

    fetch_page(month_start_utc, page, per_page) returns HTTP status and raw JSON
    for this UTC month's conformance.yml runs, with no event/status filter.
    Pass the same live_run adapter names used for the weekly jobs. Count trial
    adapters only on manual dispatches that name them.
    """
    _integer(current_run_id, "current run ID", 1, error=RunLimitError)
    _integer(current_attempt, "current run attempt", 1, error=RunLimitError)
    if isinstance(live_adapters, str) or not all(
        isinstance(name, str) and _ADAPTER_NAME.fullmatch(name) for name in live_adapters
    ):
        raise RunLimitError("Invalid live adapter names")
    live = set(live_adapters) - {"docker"}
    manual = live | MANUAL_TRIAL_ADAPTERS
    month = _utc(now, "now").replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    counts: Counter[str] = Counter()
    current: set[str] | None = None
    seen: set[int] = set()
    page, total = 1, None
    while True:
        try:
            status, body = fetch_page(month, page, _PAGE_SIZE)
        except OSError:
            raise RunLimitError("GitHub run history request failed") from None
        if type(status) is not int:
            raise RunLimitError("Invalid GitHub run history HTTP status")
        if not 200 <= status < 300:
            raise RunLimitError(f"GitHub run history returned HTTP {status}")
        try:
            payload = json.loads(body, object_pairs_hook=_unique_json_object)
        except (TypeError, ValueError, RecursionError):
            raise RunLimitError("Invalid GitHub run history JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise RunLimitError("Missing or invalid workflow_runs list")
        declared, entries = payload.get("total_count"), payload["workflow_runs"]
        # Filtered GitHub searches can truncate at 1,000 results.
        if type(declared) is not int or not 0 <= declared < _SEARCH_CAP:
            raise RunLimitError("Invalid total_count or GitHub search cap reached")
        if total is not None and declared != total:
            raise RunLimitError("GitHub total_count changed during pagination")
        total = declared
        if len(entries) != min(_PAGE_SIZE, total - len(seen)):
            raise RunLimitError("GitHub page size disagrees with total_count")

        for entry in entries:
            if not isinstance(entry, dict):
                raise RunLimitError("Invalid workflow_runs entry")
            run_id = _integer(entry.get("id"), "id", 1, error=RunLimitError)
            attempt = _integer(entry.get("run_attempt"), "run_attempt", 1, error=RunLimitError)
            stamp = entry.get("created_at")
            if not isinstance(stamp, str):
                raise RunLimitError("Missing or invalid created_at")
            try:
                created_at = datetime.fromisoformat(stamp)
            except ValueError:
                raise RunLimitError("Invalid created_at timestamp") from None
            if run_id in seen:
                raise RunLimitError("GitHub run history contains a duplicate run ID")
            seen.add(run_id)
            date = _utc(created_at, "created_at")
            if (date.year, date.month) != (month.year, month.month):
                raise RunLimitError("GitHub run was not created this UTC month")
            title, event = entry.get("display_title"), entry.get("event")
            if not isinstance(title, str) or not title.strip():
                raise RunLimitError("Missing or invalid display_title")
            if event == "schedule":
                targets, extra = live, attempt - 1
            elif event == "workflow_dispatch":
                targets, extra = _dispatch_adapters(title) - {"docker"}, attempt
            else:
                raise RunLimitError("Missing or invalid conformance run event")
            if run_id == current_run_id:
                if attempt != current_attempt:
                    raise RunLimitError("GitHub history does not match the current run attempt")
                if targets - manual:
                    raise RunLimitError("Current dispatch names a non-live adapter")
                current = targets
            for target in targets & manual:
                counts[target] += extra

        if len(seen) == total:
            break
        page += 1

    if current is None:
        raise RunLimitError("Current run not found in this UTC month's GitHub history")
    for adapter in sorted(current):
        limit = EXTRA_RUNS_PER_MONTH
        if adapter == "daytona" and adapter not in live:
            if (month.year, month.month) != _DAYTONA_TRIAL_MONTH:
                raise RunLimitError("The one-time Daytona trial is limited to November 2026 UTC")
            limit = 1
        if counts[adapter] > limit:
            raise RunLimitError(f"{adapter}: {counts[adapter]} extra runs exceed {limit} this UTC month")
    return current_attempt


class ConformanceFailure(RuntimeError):
    """The session cannot safely make another sandbox."""


def select_provider(env: Mapping[str, str]) -> ProviderSpec:
    name = env.get("ANYCOMPUTE_CONFORMANCE_PROVIDER")
    if not name:
        raise ValueError("Set ANYCOMPUTE_CONFORMANCE_PROVIDER to opt in to conformance")
    if name != "docker":
        attempt = env.get("GITHUB_RUN_ATTEMPT", "")
        if (env.get("ANYCOMPUTE_CONFORMANCE_LIVE") != "1" or env.get("GITHUB_ACTIONS") != "true"
                or env.get("GITHUB_REF") != "refs/heads/main" or not re.fullmatch(r"[1-9][0-9]*", attempt)
                or env.get("ANYCOMPUTE_VERIFIED_ATTEMPT") != attempt):
            raise ValueError("Cloud conformance requires a verified main-branch GitHub Actions attempt")
    try:
        spec = providers.get_provider(name)
    except ConfigError:
        raise ValueError("ANYCOMPUTE_CONFORMANCE_PROVIDER does not name a registered adapter") from None
    manual_trial = name in MANUAL_TRIAL_ADAPTERS and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if name != "docker" and not spec.caps.live_run and not manual_trial:
        raise ValueError("Selected adapter is not enabled for live conformance")
    if name == "modal" and env.get("MODAL_MAX_THROTTLE_WAIT") != "0":
        raise ValueError("Modal conformance requires MODAL_MAX_THROTTLE_WAIT=0")
    return spec


def _reject_constant(_):
    raise ValueError("Non-JSON constant")


def _read_report(child, timeout):
    stdout, _ = child.communicate(timeout=timeout)
    code = child.returncode
    if type(code) is not int or code < 0:
        raise ConformanceFailure("CLI subprocess crashed")
    try:
        report = json.loads(stdout, object_pairs_hook=_unique_json_object, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError):
        raise ConformanceFailure("CLI stdout was not one JSON document") from None
    if not isinstance(report, dict) or not isinstance(rows := report.get("rows", []), list):
        raise ConformanceFailure("Invalid CLI JSON report")
    for row in (report, *rows):
        if not isinstance(row, dict) or "error" not in row:
            raise ConformanceFailure("Invalid CLI JSON report")
        error = row["error"]
        if error is not None and (not isinstance(error, dict) or not isinstance(error.get("type"), str)):
            raise ConformanceFailure("Invalid CLI JSON error")
        if row.get("cleanup_error") is not None or error and (
            error["type"] == "CleanupError" or error.get("cleanup_error") is not None
            or error.get("internal") is True or error.get("sandbox_may_exist") is True
        ):
            raise ConformanceFailure("CLI reported failed or uncertain cleanup")
    return code, report


def _discard_process(child):
    if child is not None and child.poll() is None:
        with suppress(OSError, subprocess.SubprocessError):
            child.kill()
            child.communicate(timeout=5)


class ConformanceRunner:
    """One sequential test session's subprocesses and reservation, for one adapter."""

    def __init__(self, spec: ProviderSpec, run_id: str, *, observer, prices: PriceTable | None = None):
        self.spec = spec
        self.run_id = engine._check_run_id(run_id)
        self.observer = observer
        self.prices = load_prices() if prices is None else prices
        self.reservation = Reservation()
        self.stopped = False
        self._seen: set[str] = set()
        self._prior: set[str] = set()
        self._launch_limit = 0
        self.run_ids = tuple(dict.fromkeys((self.run_id, _child_run_id(self.run_id, "-lc"),
                                           _child_run_id(self.run_id, "-kc"))))
        self._ctx = engine._Context(spec.name, self.run_id)
        self._pacer = engine._Pacer(6 if spec.name == "boat" else 30, None)

    def _reserve(self, ttl):
        if self.stopped:
            raise ConformanceFailure("Conformance runner stopped; no further creates")
        previous = self.reservation.sandboxes
        try:
            effective = engine._effective_ttl(self._ctx, ttl, self.spec.caps)
            self.reservation = reserve_launch(self.prices, self.spec.name, self.reservation, (effective,),
                                              caps=self.spec.caps)
        except ValueError:
            self.stopped = True
            raise
        self._prior = self._seen.copy()
        self._launch_limit = len(self._prior) + self.reservation.sandboxes - previous
        self._pacer.wait(self._ctx)
        return effective

    def _start(self, command):
        return subprocess.Popen(
            ("anycompute", *command), env={**os.environ, "ANYCOMPUTE_RUN_ID": self.run_id},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def _record(self, sandbox_ids):
        self._seen.update(sandbox_ids)
        if len(self._seen) > self._launch_limit:
            raise ConformanceFailure(f"{self.spec.name}: found more sandboxes than reserved")

    def list(self, run_id, timeout_s):
        items = self.observer.list(run_id, timeout_s)
        self._record(item.sandbox_id for item in items)
        return tuple(item for item in items if item.sandbox_id not in self._prior)

    def read(self, sandbox_id, path, timeout_s):
        return self.observer.read(sandbox_id, path, timeout_s)

    def state(self, sandbox_id, timeout_s):
        return self.observer.state(sandbox_id, timeout_s)

    def _scan(self, deadline):
        self._record(())
        for run_id in self.run_ids:
            left = deadline - self._ctx.clock.now()
            if left <= 0:
                raise ConformanceFailure("Duplicate check ran out of time")
            self.list(run_id, left)

    def _gc(self, run_id):
        child = None
        try:
            child = self._start(("gc", "--json", "--provider", self.spec.name, f"--run={run_id}"))
            code, report = _read_report(child, 2 * engine._CLEANUP_S + 10)
            rows = report.get("sandboxes")
            if code == 0 and report["error"] is None and isinstance(rows, list) and all(
                isinstance(row, dict) and row.get("action") == "deleted" for row in rows
            ):
                return
        except (ConformanceFailure, OSError, subprocess.SubprocessError):
            pass
        finally:
            _discard_process(child)
        raise CleanupError("Could not confirm gc cleanup", run_id=run_id)

    def _abort(self, error, *, already_cleaned=None) -> NoReturn:
        self.stopped = True
        failures = []
        for run_id in self.run_ids:
            if run_id == already_cleaned:
                continue
            try:
                self._gc(run_id)
            except CleanupError:
                failures.append(run_id)
        message = str(error) if isinstance(error, (ConformanceFailure, AnycomputeError)) else "CLI or observer failed"
        if failures:
            message += f"; gc failed for {', '.join(failures)}"
        if not isinstance(error, Exception):
            raise error
        raise ConformanceFailure(message) from None

    def run(self, argv: Sequence[str], *, ttl: int = MAX_TTL_S,
            files: Sequence[Path] = (), read: Sequence[str] = (), out: Path | None = None):
        code, report, _ = self._run(argv, ttl=ttl, files=files, read=read, out=out)
        return code, report

    def lifetime(self):
        return self._run(("sleep", "600"), ttl=MAX_TTL_S, observe_lifetime=True)

    def _run(self, argv, *, ttl, files=(), read=(), out=None, observe_lifetime=False):
        effective = self._reserve(ttl)
        opts = [flag for path in files for flag in ("--file", str(path))]
        opts += [flag for path in read for flag in ("--read", path)]
        if out is not None:
            opts += ["--out", str(out)]
        clock = self._ctx.clock
        start = clock.now()
        child = None
        try:
            child = self._start(("run", "--json", "--provider", self.spec.name, "--ttl", str(effective),
                                 *opts, "--", *argv))
            expiry = self._observe_lifetime(child, start, effective) if observe_lifetime else None
            code, report = _read_report(child, start + TEST_TIMEOUT_S - 60 - clock.now())
            sandbox_id = report.get("sandbox_id")
            if report.get("provider") != self.spec.name or report.get("run_id") != self.run_id:
                raise ConformanceFailure("CLI did not report the reserved provider and run ID")
            if report["error"] is None and (
                not isinstance(sandbox_id, str) or not sandbox_id or report.get("effective_ttl") != effective
                or type(report.get("exit_code")) is not int or report["exit_code"] != code
            ):
                raise ConformanceFailure("CLI did not report the reserved TTL, sandbox and exit code")
            if sandbox_id:
                self._record((sandbox_id,))
            self._scan(min(clock.now() + engine._CLEANUP_S, start + TEST_TIMEOUT_S - 5))
        except BaseException as error:
            _discard_process(child)
            self._abort(error)
        if expiry is not None and expiry.status == "partial":
            self.confirm_deleted(expiry.sandbox_id)
            expiry = replace(expiry, deleted_elapsed_s=clock.now() - start)
        return code, report, expiry

    def _observe_lifetime(self, child, start, ttl):
        clock = self._ctx.clock

        def find(left):
            if child.poll() is not None:
                return False
            return next((item.sandbox_id for item in self.list(self.run_id, left)
                         if item.labels.get("anycompute-ttl") == str(ttl)), None)

        sandbox = _poll(clock, start + ttl, find)
        if not sandbox:
            return _LifecycleResult(self.run_id, None, "fail", clock.now() - start, "no sandbox observed by the TTL")
        state = _wait_for_end(self, sandbox, self.spec.caps.lifetime, clock=clock,
                              deadline=start + ttl + GRACE_S)
        elapsed = clock.now() - start
        if state is None:
            return _LifecycleResult(self.run_id, sandbox, "fail", elapsed, "sandbox did not end by TTL plus grace")
        if state == SandboxState.STOPPED:
            return _LifecycleResult(self.run_id, sandbox, "partial", elapsed)
        return _LifecycleResult(self.run_id, sandbox, "pass", elapsed, deleted_elapsed_s=elapsed)

    def confirm_deleted(self, sandbox_id):
        try:
            if self.state(sandbox_id, engine._CLEANUP_S) != SandboxState.GONE:
                raise ConformanceFailure("Observer did not confirm deletion")
        except BaseException as error:
            self._abort(error)

    def kill_client(self, *, ttl: int = MAX_TTL_S) -> _LifecycleResult:
        effective = self._reserve(ttl)
        cleaned = None

        def cleanup(run_id):
            nonlocal cleaned
            self._gc(run_id)
            cleaned = run_id

        def check_duplicates(expiry_deadline):
            self._scan(min(expiry_deadline, self._ctx.clock.now() + engine._CLEANUP_S))

        try:
            result = _kill_client(self.run_id, effective_ttl=effective, lifetime=self.spec.caps.lifetime,
                                  suffix="-kc", launch=partial(_launch_child, self.spec.name), observer=self,
                                  cleanup=cleanup, clock=self._ctx.clock, after_kill=check_duplicates)
            if result.status == "fail":
                raise ConformanceFailure(f"{result.reason} ({result.elapsed_s:.3f}s)")
            return result
        except BaseException as error:
            self._abort(error, already_cleaned=None if isinstance(error, CleanupError) else cleaned)

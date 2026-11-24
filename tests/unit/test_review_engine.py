import json
import threading
import traceback
from queue import Queue

import pytest

from anycompute.contract import (
    CleanupError, ConfigError, KeySources, ProviderError, QuotaError, RateLimitError, RawExecResult,
    SandboxState, SandboxTimeoutError,
)
from tests.unit.test_engine import failure, gc_row, on_job, set_caps, setup


def test_gc_all_does_not_delete_running_or_unknown_state(setup):
    engine, adapter, clock = setup
    before = clock.wall_time() - 181
    states = {"active": SandboxState.RUNNING, "uncertain": SandboxState.UNKNOWN, "ended": SandboxState.STOPPED}
    adapter.handlers["list"] = lambda *args, **kwargs: [gc_row(name, before) for name in states]
    adapter.handlers["state"] = lambda name, **kwargs: SandboxState.GONE if name in adapter.gone else states[name]

    rows, error = engine._gc("fake", all_expired=True)

    assert [args for args, _ in adapter.ops("delete")] == [("ended",)]
    assert [(row["action"], row["reason"]) for row in rows] == [
        ("kept", "running"), ("kept", "running"), ("deleted", "expired"),
    ]
    assert error is None


@pytest.mark.parametrize("created_at", [True, False, float("nan"), float("inf"), float("-inf"), "100", 10**100])
def test_gc_all_keeps_invalid_create_time(setup, created_at):
    engine, adapter, _ = setup
    adapter.handlers["list"] = lambda *args, **kwargs: [gc_row("invalid-time", created_at)]
    adapter.handlers["state"] = lambda *args, **kwargs: SandboxState.GONE

    rows, error = engine._gc("fake", all_expired=True)

    assert error is None
    assert (rows[0]["action"], rows[0]["reason"], rows[0]["created_at"]) == ("kept", "no_create_time", None)
    assert not adapter.ops("state") and not adapter.ops("delete")


@pytest.mark.parametrize("names", [("delete-error", "state-error", "ended"), ("state-error", "delete-error", "ended")])
def test_gc_prefers_cleanup_failure_and_preserves_rows(setup, monkeypatch, names):
    engine, adapter, clock = setup
    set_caps(monkeypatch, pre_start_window="partial")
    before = clock.wall_time() - 181
    adapter.handlers["list"] = lambda *args, **kwargs: [gc_row(name, before) for name in names]

    def state(name, **kwargs):
        if name == "state-error":
            raise ProviderError("state unavailable")
        return SandboxState.GONE if name in adapter.gone else SandboxState.STOPPED

    def delete(name, **kwargs):
        if name == "delete-error":
            raise ProviderError("delete unavailable")
        adapter.gone.add(name)

    adapter.handlers.update(state=state, delete=delete)
    rows, error = engine._gc("fake", all_expired=True)

    assert isinstance(error, CleanupError) and error.sandbox_id == "delete-error"
    actions = {"delete-error": ("failed", "expired"), "state-error": ("kept", "state_error"),
               "ended": ("deleted", "expired")}
    assert [(row["sandbox_id"], row["action"], row["reason"]) for row in rows] == [
        (name, *actions[name]) for name in names
    ]
    assert adapter.gone == {"ended"}


@pytest.mark.parametrize("dry_run", [False, True])
def test_gc_exact_run_checks_unfiltered_inventory_even_with_force(setup, dry_run):
    engine, adapter, _ = setup
    adapter.handlers["list"] = lambda *args, **kwargs: [
        gc_row("matched", None), gc_row("not-ours", None, anycompute="0"),
        gc_row("other-run", None, **{"anycompute-run": "other"}),
    ]
    rows, error = engine._gc("fake", run_id="sweep", dry_run=dry_run, force=True)

    assert error is None
    assert [(row["sandbox_id"], row["action"], row["reason"]) for row in rows] == [
        ("matched", "listed" if dry_run else "deleted", "force")
    ]
    assert [args for args, _ in adapter.ops("delete")] == ([] if dry_run else [("matched",)])


def test_cleanup_worker_start_failure_preserves_completed_result(setup, monkeypatch):
    engine, adapter, clock = setup
    job_done = threading.Event()
    start = threading.Thread.start
    monkeypatch.setattr(engine, "_CLEANUP_SLOTS", threading.BoundedSemaphore(1), raising=False)

    def finish():
        job_done.set()
        return RawExecResult("complete", "", 7)

    def start_or_fail(thread):
        if job_done.is_set():
            raise RuntimeError("synthetic-fixture-only")
        return start(thread)

    adapter.handlers["exec"] = on_job(finish)
    with monkeypatch.context() as scoped:
        scoped.setattr(threading.Thread, "start", start_or_fail)
        with pytest.raises(CleanupError, match="could not start") as caught:
            engine.run(["script"], provider="fake", run_id="start-failure")
    error = caught.value
    assert (error.sandbox_id, error.run_id) == ("box-1", "start-failure")
    assert (error.result.stdout, error.result.exit_code, error.result.sandbox_seconds) == ("complete", 7, None)
    assert error.__context__ is error.__cause__ is None
    assert not adapter.ops("delete")
    assert clock.now() == 120
    assert engine.run(["script"], provider="fake").sandbox_seconds == 0


def time_out_after_start(clock, monkeypatch, started, *, skip=0):
    wait = clock.wait
    remaining = skip

    def timeout_once(event, budget):
        nonlocal remaining
        remaining -= 1
        if remaining == -1:
            assert started.wait(2)
            clock.t += budget
            return False
        return wait(event, budget)

    monkeypatch.setattr(clock, "wait", timeout_once)


def hold_call(started, release, workers):
    workers.append(threading.current_thread())
    started.set()
    release.wait(3)


def test_abandoned_exec_keeps_work_slot_and_reserves_delete_and_confirm(setup, monkeypatch):
    engine, adapter, clock = setup
    monkeypatch.setattr(engine, "_WORK_SLOTS", threading.BoundedSemaphore(1), raising=False)
    monkeypatch.setattr(engine, "_CLEANUP_SLOTS", threading.BoundedSemaphore(1), raising=False)
    sb = engine.Sandbox("fake")
    started, release, workers = threading.Event(), threading.Event(), []

    def hang():
        hold_call(started, release, workers)
        return RawExecResult("", "", 0)

    adapter.handlers["exec"] = on_job(hang)
    time_out_after_start(clock, monkeypatch, started)
    try:
        with pytest.raises(ProviderError):
            sb.exec(["script"])
        assert len(adapter.ops("delete")) == len(adapter.ops("state")) == 1
        assert sb.sandbox_seconds == 420
        for _ in range(3):
            with pytest.raises(ProviderError, match="worker capacity") as denied:
                engine.run(["script"], provider="fake", run_id="at-capacity")
            assert denied.value.run_id == "at-capacity" and not denied.value.sandbox_may_exist
        assert len(adapter.ops("prepare")) == len(adapter.ops("create")) == 1
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
    adapter.handlers.pop("exec")
    assert engine.run(["script"], provider="fake").exit_code == 7


@pytest.mark.parametrize("operation", ["delete", "state"])
def test_abandoned_cleanup_keeps_slot_without_blocking_work(setup, monkeypatch, operation):
    engine, adapter, clock = setup
    monkeypatch.setattr(engine, "_CLEANUP_SLOTS", threading.BoundedSemaphore(1), raising=False)
    sb, second = engine.Sandbox("fake"), engine.Sandbox("fake")
    started, release, workers = threading.Event(), threading.Event(), []

    def hang(sandbox_id, **kwargs):
        hold_call(started, release, workers)
        return SandboxState.GONE if operation == "state" else None

    adapter.handlers[operation] = hang
    time_out_after_start(clock, monkeypatch, started, skip=int(operation == "state"))
    try:
        with pytest.raises(CleanupError, match="client budget"):
            sb.close()
        third = engine.Sandbox("fake")
        with pytest.raises(CleanupError, match="worker capacity"):
            second.close()
        assert len(adapter.ops("create")) == 3
        assert len(adapter.ops("delete")) == 1
        assert len(adapter.ops("state")) == (operation == "state")
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
    adapter.handlers.pop(operation)
    third.close()


@pytest.mark.parametrize("operation,step", [("delete", "delete"), ("state", "confirm")])
@pytest.mark.parametrize("late_by", [0, 1])
def test_cleanup_rejects_reply_at_or_past_original_deadline(setup, monkeypatch, operation, step, late_by):
    engine, adapter, clock = setup

    def late(*args, timeout_s):
        clock.t += timeout_s + late_by
        return SandboxState.GONE if operation == "state" else None

    adapter.handlers[operation] = late
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    with pytest.raises(CleanupError, match=f"{step} exceeded its client budget") as caught:
        engine.run(["script"], provider="fake")
    assert caught.value.result.exit_code == 7
    assert caught.value.result.sandbox_seconds is None
    assert len(adapter.ops("state")) == (operation == "state")


def test_provider_error_masks_raw_and_normally_escaped_text(setup, monkeypatch, caplog):
    engine, adapter, _ = setup
    key = 'fixture"key\\é\nnext'
    value = 'fixture"env\\é\nnext'
    short = 'a"b'
    monkeypatch.setenv("SYNTHETIC_ENGINE_AUTH", key)
    monkeypatch.setenv("SYNTHETIC_SHORT_AUTH", short)
    set_caps(monkeypatch, provider_options_allowlist=frozenset({"environment"}),
             key_sources=KeySources(env=("SYNTHETIC_ENGINE_AUTH", "SYNTHETIC_SHORT_AUTH")))
    escaped = json.dumps(short)[1:-1]
    renderings = (key, repr(key)[1:-1], ascii(key)[1:-1], json.dumps(key)[1:-1],
                  json.dumps(key, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1])
    message = " | ".join((*renderings, escaped, "x" + escaped, "yes"))
    adapter.handlers["exec"] = on_job(failure(QuotaError(message)))
    adapter.handlers["delete"] = failure(ProviderError(json.dumps(key)))
    options = {"fake": {"environment": {"VALUE": value, "FLAG": "yes"}}}

    with caplog.at_level("DEBUG", logger="anycompute"), pytest.raises(QuotaError) as caught:
        engine.run(["script"], provider="fake", provider_options=options)

    assert str(caught.value) == " | ".join(["***"] * 7 + ["x" + escaped, "yes"])
    assert all(term not in str(caught.value.cleanup_error) for term in renderings)
    assert all(record.exc_info is None and all(term not in record.getMessage() for term in renderings)
               for record in caplog.records)
    assert caught.value.__context__ is caught.value.__cause__ is None


def test_malformed_partial_output_still_delivers_mapped_timeout(setup, monkeypatch):
    engine, adapter, _ = setup
    sb = engine.Sandbox("fake", run_id="partial-output")
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    adapter.handlers["exec"] = on_job(failure(SandboxTimeoutError(
        "ended", stdout={"message": "synthetic-fixture-only"}, stderr=b"partial\xff",
    )))

    with pytest.raises(SandboxTimeoutError) as caught:
        sb.exec(["script"])

    error = caught.value
    assert (error.stdout, error.stderr, error.sandbox_id, error.run_id) == ("", "partial\ufffd", "box-1", "partial-output")
    assert error.__cause__ is error.__context__ is None
    assert not uncaught and len(adapter.ops("delete")) == 1


def test_broken_exception_formatter_cannot_escape_worker(setup, monkeypatch):
    class UnprintableError(Exception):
        def __str__(self):
            raise KeyboardInterrupt("synthetic-fixture-only")

    engine, adapter, _ = setup
    sb = engine.Sandbox("fake", run_id="formatter")
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    adapter.handlers["exec"] = on_job(failure(UnprintableError()))

    with pytest.raises(ProviderError) as caught:
        sb.exec(["script"])

    error = caught.value
    assert error.internal and error.run_id == "formatter" and error.sandbox_id == "box-1"
    assert error.__cause__ is error.__context__ is None
    assert "synthetic-fixture-only" not in "".join(traceback.format_exception(error))
    assert not uncaught and len(adapter.ops("delete")) == 1


@pytest.mark.parametrize("operation,value", [
    ("exec", None), ("exec", RawExecResult([], "", 0)), ("exec", RawExecResult("", 1, 0)),
    ("exec", RawExecResult("", "", True)), ("exec", RawExecResult("", "", "0")),
    ("read", "not bytes"), ("write", False),
])
def test_malformed_work_reply_closes_unmanaged_sandbox(setup, operation, value):
    engine, adapter, _ = setup
    sb = engine.Sandbox("fake", run_id="invalid-reply")
    adapter.handlers[operation] = lambda *args, **kwargs: value
    args = {"exec": (["script"],), "read": ("file",), "write": ("file", b"")}

    with pytest.raises(ProviderError, match="invalid provider reply") as caught:
        getattr(sb, operation)(*args[operation])

    error = caught.value
    assert error.internal and (error.run_id, error.sandbox_id, error.sandbox_seconds) == ("invalid-reply", "box-1", 0)
    assert len(adapter.ops("delete")) == len(adapter.ops("state")) == 1
    with pytest.raises(ConfigError, match="closed"):
        sb.exec(["script"])


@pytest.mark.parametrize("retry,expected", [
    ("7", None), (True, None), (float("nan"), None), (float("inf"), None), (float("-inf"), None),
    (-1, None), (None, None), (0, 0), (7, 7), (0.25, 0.25),
])
def test_provider_boundary_normalizes_rate_limit_delay(setup, retry, expected):
    engine, adapter, _ = setup
    adapter.handlers["exec"] = on_job(failure(RateLimitError("rate limited", retry_after=retry)))
    with pytest.raises(RateLimitError) as caught:
        engine.run(["script"], provider="fake")
    assert caught.value.retry_after == expected


def test_gc_does_not_treat_malformed_state_as_gone(setup):
    engine, adapter, clock = setup
    adapter.handlers["list"] = lambda *args, **kwargs: [gc_row("not-confirmed", clock.wall_time() - 181)]
    adapter.handlers["state"] = lambda *args, **kwargs: "gone"

    rows, error = engine._gc("fake", all_expired=True)

    assert isinstance(error, ProviderError) and error.internal and error.sandbox_id == "not-confirmed"
    assert (rows[0]["action"], rows[0]["reason"]) == ("kept", "state_error")
    assert not adapter.ops("delete")


def open_or_run(engine, entry, **kwargs):
    if entry == "sandbox":
        return engine.Sandbox("fake", **kwargs)
    return engine.run(["script"], provider="fake", **kwargs)


@pytest.mark.parametrize("entry", ["sandbox", "run"])
def test_dispatched_create_error_without_id_has_recovery_context(setup, entry):
    engine, adapter, _ = setup
    adapter.handlers["create"] = failure(TypeError("unexpected create failure"))
    with pytest.raises(ProviderError) as caught:
        open_or_run(engine, entry, run_id="create-recovery")
    error = caught.value
    assert error.internal and error.sandbox_may_exist
    assert error.run_id == "create-recovery" and error.sandbox_id is None
    assert len(adapter.ops("create")) == 1 and not adapter.ops("delete")


@pytest.mark.parametrize("value", [None, "", "  ", "bad\x00id", 42])
def test_create_invalid_id_never_reaches_sandbox_operations(setup, value):
    engine, adapter, _ = setup
    adapter.handlers["create"] = lambda **kw: value
    with pytest.raises(ProviderError, match="invalid sandbox ID") as caught:
        engine.Sandbox("fake", run_id="invalid-id")
    assert caught.value.sandbox_may_exist and caught.value.sandbox_id is None
    assert not adapter.ops("exec") and not adapter.ops("delete")


@pytest.mark.parametrize("error_type", [ProviderError, QuotaError])
def test_partial_create_keeps_raw_id_only_for_adapter(setup, error_type):
    engine, adapter, _ = setup
    raw_id = "synthetic-fixture-only"
    adapter.handlers["create"] = failure(error_type(f"create failed: {raw_id}", sandbox_id=raw_id))

    with pytest.raises(error_type, match=r"create failed: \*\*\*") as caught:
        engine.run(["script"], provider="fake", run_id="partial-create")

    error = caught.value
    assert (error.sandbox_id, error.run_id, error.sandbox_seconds) == ("***", "partial-create", 0)
    assert not getattr(error, "sandbox_may_exist", False)
    assert error.__cause__ is error.__context__ is None
    for operation in ("delete", "state"):
        assert [args for args, _ in adapter.ops(operation)] == [(raw_id,)]


def test_sandbox_display_id_does_not_change_adapter_identity(setup):
    engine, adapter, _ = setup
    raw_id = "synthetic-fixture-only"
    adapter.handlers["create"] = lambda **kw: raw_id
    with engine.Sandbox("fake") as sb:
        assert sb.sandbox_id == "***"
        assert sb.exec(["script"]).exit_code == 7
    assert [args[0] for args, _ in adapter.ops("exec")] == [raw_id, raw_id]
    assert [args for args, _ in adapter.ops("delete")] == [(raw_id,)]


@pytest.mark.parametrize("entry", ["sandbox", "run"])
@pytest.mark.parametrize("where", ["_create", "_open", "create_log"])
def test_interrupt_after_create_id_is_known_confirms_delete(setup, monkeypatch, entry, where):
    engine, adapter, _ = setup
    if where != "create_log":
        finish = getattr(engine.Sandbox, where)

        def finish_then_interrupt(sb, *args, **kwargs):
            finish(sb, *args, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(engine.Sandbox, where, finish_then_interrupt)
    else:
        log = engine._Context.log

        def interrupt_create_log(ctx, step, duration):
            if step == "create" and ctx.sandbox_id is not None:
                raise KeyboardInterrupt
            log(ctx, step, duration)

        monkeypatch.setattr(engine._Context, "log", interrupt_create_log)

    with pytest.raises(KeyboardInterrupt) as caught:
        open_or_run(engine, entry, run_id="after-create")

    assert (caught.value.sandbox_id, caught.value.run_id, caught.value.sandbox_seconds) == ("box-1", "after-create", 0)
    for operation in ("delete", "state"):
        assert [args for args, _ in adapter.ops(operation)] == [("box-1",)]


@pytest.mark.parametrize("interrupt", [False, True])
def test_delayed_create_worker_does_not_dispatch_after_waiter_cancels(setup, monkeypatch, interrupt):
    engine, adapter, clock = setup
    monkeypatch.setattr(engine, "_WORK_SLOTS", threading.BoundedSemaphore(1))
    arrived, release, held = threading.Event(), threading.Event(), []
    start, wait = threading.Thread.start, clock.wait
    launches, waits = 0, 0

    def hold_second_thread(thread):
        nonlocal launches
        launches += 1
        if launches == 2:
            run = thread.run
            held.append(thread)

            def run_after_release():
                arrived.set()
                if release.wait(3):
                    run()

            thread.run = run_after_release
        return start(thread)

    def cancel_second_wait(event, budget):
        nonlocal waits
        waits += 1
        if waits == 2:
            assert arrived.wait(2)
            if interrupt:
                raise KeyboardInterrupt
            clock.t += budget
            return False
        return wait(event, budget)

    with monkeypatch.context() as scoped:
        scoped.setattr(threading.Thread, "start", hold_second_thread)
        scoped.setattr(clock, "wait", cancel_second_wait)
        try:
            expected = KeyboardInterrupt if interrupt else ProviderError
            with pytest.raises(expected) as caught:
                engine.Sandbox("fake", run_id="undispatched")
            assert caught.value.run_id == "undispatched" and not getattr(caught.value, "sandbox_may_exist", False)
            with pytest.raises(ProviderError, match="worker capacity"):
                engine.Sandbox("fake")
        finally:
            release.set()
            for worker in held:
                worker.join(2)
    assert not adapter.ops("create")
    assert engine.run(["script"], provider="fake").exit_code == 7


@pytest.mark.parametrize("spawn_first", [False, True])
def test_create_worker_start_failure_leaves_no_uncertainty_or_capacity_leak(setup, monkeypatch, spawn_first):
    engine, adapter, _ = setup
    monkeypatch.setattr(engine, "_WORK_SLOTS", threading.BoundedSemaphore(1))
    uncaught, spawned = [], []
    monkeypatch.setattr(threading, "excepthook", uncaught.append)
    start = threading.Thread.start
    launches = 0

    def fail_create_start(thread):
        nonlocal launches
        launches += 1
        if launches != 2:
            return start(thread)
        if spawn_first:
            start(thread)
            spawned.append(thread)
        raise RuntimeError("synthetic-fixture-only")

    with monkeypatch.context() as scoped:
        scoped.setattr(threading.Thread, "start", fail_create_start)
        with pytest.raises(ProviderError, match="could not start worker") as caught:
            engine.Sandbox("fake", run_id="create-start-failure")
        for worker in spawned:
            worker.join(2)

    error = caught.value
    assert error.run_id == "create-start-failure" and not error.sandbox_may_exist
    assert error.__context__ is error.__cause__ is None
    assert not uncaught and not adapter.ops("create")
    assert engine.run(["script"], provider="fake").exit_code == 7


def test_late_partial_create_with_ready_event_reconciles_raw_id(setup, monkeypatch):
    engine, adapter, clock = setup
    workers = []
    raw_id = "synthetic-fixture-only"

    def late(timeout_s, **kw):
        workers.append(threading.current_thread())
        clock.t += timeout_s
        raise QuotaError("failed after allocation", sandbox_id=raw_id)

    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    adapter.handlers["create"] = late
    with pytest.raises(ProviderError, match="client budget") as caught:
        engine.Sandbox("fake", run_id="late-partial")
    assert caught.value.sandbox_may_exist and caught.value.sandbox_id is None
    for worker in workers:
        worker.join(2)
    for operation in ("delete", "state"):
        assert adapter.ops(operation) == [((raw_id,), {"timeout_s": 120})]


def test_pacing_rechecks_spacing_at_dispatch_when_earlier_worker_is_delayed(setup, monkeypatch):
    engine, adapter, clock = setup
    events = Queue()
    wake = threading.Condition()
    arrived, release = threading.Event(), threading.Event()
    start = threading.Thread.start
    first_launches = 0

    def hold_first_create_worker(thread):
        nonlocal first_launches
        if threading.current_thread().name == "first-caller":
            first_launches += 1
            if first_launches == 2:
                run = thread.run

                def delayed():
                    arrived.set()
                    if release.wait(3):
                        run()

                thread.run = delayed
        return start(thread)

    def sleep(seconds):
        with wake:
            due = clock.t + seconds
            events.put(("sleep", due))
            assert wake.wait_for(lambda: clock.t >= due, timeout=3)

    def advance(instant):
        with wake:
            clock.t = instant
            wake.notify_all()

    def create(*, labels, timeout_s, **kw):
        events.put(("create", labels["anycompute-run"], clock.now(), timeout_s))
        return f"box-{next(adapter.ids)}"

    def caller(name):
        try:
            result = engine.run(["script"], provider="fake", run_id=name, ttl=61, creates_per_minute=12)
        except BaseException as error:
            events.put(("error", name, error))
        else:
            events.put(("result", name, result.sandbox_seconds))

    adapter.handlers["create"] = create
    monkeypatch.setattr(threading.Thread, "start", hold_first_create_worker)
    monkeypatch.setattr(clock, "sleep", sleep)
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    first = threading.Thread(target=caller, args=("first",), name="first-caller", daemon=True)
    second = threading.Thread(target=caller, args=("second",), daemon=True)
    first.start()
    try:
        assert arrived.wait(2)
        second.start()
        assert events.get(timeout=2) == ("sleep", 5)
        advance(5)
        assert events.get(timeout=2) == ("create", "second", 5, 240)
        assert events.get(timeout=2) == ("result", "second", 0)
        release.set()
        assert events.get(timeout=2) == ("sleep", 10)
        advance(10)
        assert events.get(timeout=2) == ("create", "first", 10, 240)
        assert events.get(timeout=2) == ("result", "first", 0)
    finally:
        release.set()
        advance(40)
        for caller_thread in (first, second):
            if caller_thread.ident is not None:
                caller_thread.join(2)


def test_partial_pre_start_preserves_work_window_but_measures_from_request(setup, monkeypatch):
    engine, adapter, clock = setup
    set_caps(monkeypatch, pre_start_window="partial")

    def create(**kwargs):
        clock.t += 200
        return "pre-start-box"

    def execute(box, argv, **kwargs):
        if argv[0] != "mkdir":
            clock.t += 110
        return RawExecResult("done", "", 0)

    adapter.handlers.update(create=create, exec=execute)
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    result = engine.run(["script"], provider="fake", ttl=61)

    assert [(op, kw["timeout_s"]) for op, _, kw in adapter.calls if op in ("create", "exec")] == [
        ("create", 240), ("exec", 240), ("exec", 240),
    ]
    assert result.stdout == "done" and result.sandbox_seconds == 310


@pytest.mark.parametrize("interrupt", [False, True])
def test_repeated_close_reports_incomplete_cleanup_without_another_delete(setup, monkeypatch, interrupt):
    engine, adapter, clock = setup
    sb = engine.Sandbox("fake", run_id="incomplete-close")

    def delete(*args, timeout_s):
        if interrupt:
            raise KeyboardInterrupt("stopped cleanup")
        clock.t += timeout_s

    adapter.handlers["delete"] = delete
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    with pytest.raises(KeyboardInterrupt if interrupt else CleanupError) as first:
        with sb:
            sb.close()
    with pytest.raises(CleanupError) as repeated:
        sb.close()

    error = first.value
    assert (error.sandbox_id, error.run_id, error.sandbox_seconds) == ("box-1", "incomplete-close", None)
    if interrupt:
        assert str(error.cleanup_error) == str(repeated.value)
        assert error.__notes__ == [str(error.cleanup_error)]
    else:
        assert str(error) == str(repeated.value) and error.cleanup_error is None
    assert len(adapter.ops("delete")) == 1 and not adapter.ops("state")
    with pytest.raises(ConfigError, match="closed"):
        sb.read("x")


@pytest.mark.parametrize("entry", ["context", "run"])
def test_automatic_close_reentry_keeps_one_cleanup_note(setup, monkeypatch, entry):
    engine, adapter, clock = setup
    original = QuotaError("original operation failed")

    def late_delete(*args, timeout_s):
        clock.t += timeout_s

    adapter.handlers.update(exec=on_job(failure(original)), delete=late_delete)
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    with pytest.raises(QuotaError, match="original operation failed") as caught:
        if entry == "context":
            with engine.Sandbox("fake") as sb:
                sb.exec(["script"])
        else:
            engine.run(["script"], provider="fake")

    error = caught.value
    assert isinstance(error.cleanup_error, CleanupError)
    assert error.__notes__ == [str(error.cleanup_error)]
    assert error.__context__ is error.__cause__ is error.cleanup_error.__context__ is None
    assert len(adapter.ops("delete")) == 1 and not adapter.ops("state")


@pytest.mark.parametrize("interrupt", [False, True])
def test_late_cleanup_reports_unconfirmed_delete_at_its_own_budget(setup, monkeypatch, caplog, interrupt):
    engine, adapter, clock = setup
    workers = []

    def late_create(*, timeout_s, **kwargs):
        workers.append(threading.current_thread())
        clock.t += timeout_s
        return "late-box"

    def delete(*args, timeout_s):
        if interrupt:
            raise KeyboardInterrupt("late cleanup stopped")
        clock.t += timeout_s

    adapter.handlers.update(create=late_create, delete=delete)
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))
    with caplog.at_level("DEBUG", logger="anycompute"):
        with pytest.raises(ProviderError) as caught:
            engine.Sandbox("fake", run_id="late-cleanup", ttl=61)
        for worker in workers:
            worker.join(2)

    assert caught.value.sandbox_may_exist
    assert clock.now() == (240 if interrupt else 360)
    assert adapter.ops("delete") == [(("late-box",), {"timeout_s": 120})] and not adapter.ops("state")
    assert ("late cleanup interrupted" if interrupt else "cleanup error") in caplog.text
    assert "confirm provider=" not in caplog.text

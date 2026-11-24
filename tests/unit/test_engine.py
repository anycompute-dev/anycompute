import json
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import count
from queue import Queue

import pytest

from anycompute import providers
from anycompute.contract import (
    AuthError, CleanupError, ConfigError, ConfigKeys, InvalidPathError, InvalidTTLError, KeySources,
    ProviderCaps, ProviderError, ProviderSpec, QuotaError, RateLimitError, RawExecResult,
    SandboxInfo, SandboxState, SandboxTimeoutError, StartRate,
)


class Clock:
    def __init__(self):
        self.t = 0
        self.sleeps = []
        self.waits = []

    def now(self):
        return self.t

    def wall_time(self):
        return 1_000_000 + self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def wait(self, event, timeout_s):
        self.waits.append(timeout_s)
        if not event.wait(0.1):
            self.t += timeout_s
            return False
        return True


class Adapter:
    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.ids = count(1)
        self.uploads = {}
        self.gone = set()

    def __getattr__(self, name):
        return lambda *args, **kw: self.call(name, *args, **kw)

    def call(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if name in self.handlers:
            return self.handlers[name](*args, **kw)
        if name == "create":
            return f"box-{next(self.ids)}"
        if name == "exec":
            return RawExecResult("", "", 0) if args[1][0] == "mkdir" else RawExecResult(b"out\xff", b"err", 7)
        if name == "write":
            self.uploads[args[:2]] = args[2]
        if name == "read":
            return self.uploads.get(args)
        if name == "delete":
            self.gone.add(args[0])
        if name == "state":
            return SandboxState.GONE if args[0] in self.gone else SandboxState.RUNNING

    def ops(self, name):
        return [(args, kw) for call, args, kw in self.calls if call == name]


@pytest.fixture
def setup(monkeypatch):
    from anycompute import engine

    adapter, clock = Adapter(), Clock()
    caps = ProviderCaps(ttl_unit_s=60, ttl_max_s=420, key_sources=KeySources(env=("SYNTHETIC_ENGINE_AUTH",)))
    monkeypatch.setenv("SYNTHETIC_ENGINE_AUTH", "synthetic-fixture-only")
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", caps, lambda: adapter),))
    monkeypatch.setattr(engine, "_clock", clock)
    monkeypatch.setattr(engine, "_run_id", None)
    monkeypatch.setattr(engine, "_pacers", {}, raising=False)
    monkeypatch.setattr(engine, "_env_rate_loaded", False, raising=False)
    monkeypatch.delenv("ANYCOMPUTE_RUN_ID", raising=False)
    monkeypatch.delenv("ANYCOMPUTE_CREATES_PER_MINUTE", raising=False)
    return engine, adapter, clock


def test_run_rounds_ttl_and_leaves_confirmed_result(setup):
    engine, adapter, _ = setup
    argv = ["script", "quoted argument", "$VAR"]
    result = engine.run(argv, provider="fake", ttl=61, files={"sub/a..b": b"\xff"},
                        read=["sub/a..b", "sub"], run_id="custom-run")
    assert (result.stdout, result.stderr, result.exit_code) == ("out\ufffd", "err", 7)
    assert (result.files, result.missing, result.runtime) == ({"sub/a..b": b"\xff"}, ["sub"], None)
    assert (result.effective_ttl, result.run_id, result.sandbox_seconds) == (120, "custom-run", 0)
    assert adapter.ops("create") == [((), {"ttl": 120, "labels": {
        "anycompute": "1", "anycompute-run": "custom-run", "anycompute-ttl": "120",
    }, "options": {}, "timeout_s": 240})]
    (mkdir, mkdir_kw), (job, job_kw) = adapter.ops("exec")
    assert tuple(mkdir[1]) == ("mkdir", "-p", "/tmp/anycompute", "/tmp/anycompute/sub")
    assert mkdir_kw["cwd"] == "/"
    assert list(job[1]) == argv
    assert job_kw["cwd"] == "/tmp/anycompute"
    assert [op for op, *_ in adapter.calls][-2:] == ["delete", "state"]
    assert all(len(args[1]) < 3 or args[1][0] != "python3" for args, _ in adapter.ops("exec"))
    assert not adapter.ops("api_host")


def test_public_api_exports_the_engine(setup):
    from anycompute import Sandbox, run

    engine, _, _ = setup
    assert Sandbox is engine.Sandbox and run is engine.run


@pytest.mark.parametrize("kwargs,error", [
    ({"ttl": True}, InvalidTTLError), ({"ttl": 0}, InvalidTTLError),
    ({"ttl": "60"}, InvalidTTLError), ({"ttl": 60.0}, InvalidTTLError),
    ({"ttl": 421}, InvalidTTLError), ({"run_id": "invalid\n"}, ConfigError),
    ({"runtime": 1}, ConfigError), ({"read": "x"}, ConfigError),
    ({"files": {"a": "not bytes"}}, ConfigError), ({"files": []}, ConfigError),
    ({"files": {"a": b"", "./a/b": b""}}, InvalidPathError),
    ({"files": {"a/b": b"", "a//b": b""}}, InvalidPathError),
    *[({"read": [path]}, InvalidPathError) for path in ("", "/a", "a/../b", "a/", "\x00", 1)],
])
def test_invalid_input_never_touches_adapter(setup, kwargs, error):
    engine, adapter, _ = setup
    with pytest.raises(error):
        engine.run(["script"], provider="fake", **kwargs)
    assert not adapter.calls


@pytest.mark.parametrize("argv", ["script", [], [1], ["a\x00b"], [""]])
def test_invalid_argv_never_touches_adapter(setup, argv):
    engine, adapter, _ = setup
    with pytest.raises(ConfigError):
        engine.run(argv, provider="fake")
    assert not adapter.calls


def test_missing_auth_stops_before_factory(setup, monkeypatch):
    engine, _, _ = setup
    monkeypatch.delenv("SYNTHETIC_ENGINE_AUTH")
    spec = providers.get_provider("fake")
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", spec.caps, lambda: pytest.fail("factory called")),))
    with pytest.raises(AuthError, match="SYNTHETIC_ENGINE_AUTH"):
        engine.run(["script"], provider="fake")


def test_run_id_precedence(setup, monkeypatch):
    engine, _, _ = setup
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "process-env")
    assert engine._resolve_run_id("explicit") == "explicit"
    assert engine._resolve_run_id() == "process-env"
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "later")
    assert engine._resolve_run_id() == "process-env"
    monkeypatch.delenv("ANYCOMPUTE_RUN_ID")
    monkeypatch.setattr(engine, "_run_id", None)
    generated = engine._resolve_run_id()
    assert re.fullmatch("[a-f0-9]{12}", generated)
    assert engine._resolve_run_id() == generated


def test_sandbox_validates_paths_and_closes_once(setup):
    engine, adapter, _ = setup
    with engine.Sandbox("fake") as sb:
        sb.write("sub/in", b"data")
        assert sb.read("sub/in") == b"data"
        assert sb.exec(["script"]).exit_code == 7
        before = len(adapter.calls)
        for call in (lambda: sb.read("../x"), lambda: sb.write("../x", b"")):
            with pytest.raises(InvalidPathError):
                call()
        assert len(adapter.calls) == before
    sb.close()
    assert sb.sandbox_seconds == 0
    assert len(adapter.ops("delete")) == len(adapter.ops("state")) == 1
    before = len(adapter.calls)
    for call in (lambda: sb.read("x"), lambda: sb.write("x", b""), lambda: sb.exec(["script"])):
        with pytest.raises(ConfigError):
            call()
    assert len(adapter.calls) == before


def failure(error):
    def fail(*args, **kw):
        raise error
    return fail


def on_job(result):
    def exec_job(sandbox_id, argv, **kw):
        return RawExecResult("", "", 0) if argv[0] == "mkdir" else result()
    return exec_job


@pytest.mark.parametrize("state, status, error", [
    (SandboxState.STOPPED, 137, SandboxTimeoutError), (SandboxState.GONE, None, SandboxTimeoutError),
    (SandboxState.RUNNING, None, ProviderError), (SandboxState.UNKNOWN, 137, ProviderError),
    (SandboxState.RUNNING, 137, None),
])
def test_ambiguous_exit_checks_state(setup, state, status, error):
    engine, adapter, _ = setup
    adapter.handlers["exec"] = on_job(lambda: RawExecResult(b"par\xff", b"err", status))
    states = iter((state, SandboxState.GONE))
    adapter.handlers["state"] = lambda *args, **kw: next(states)
    if error is None:
        assert engine.run(["script"], provider="fake").exit_code == 137
    else:
        with pytest.raises(error) as caught:
            engine.run(["script"], provider="fake", run_id="expiry")
        assert (caught.value.sandbox_id, caught.value.run_id, caught.value.sandbox_seconds) == ("box-1", "expiry", 0)
        if error is SandboxTimeoutError:
            assert (caught.value.stdout, caught.value.stderr) == ("par\ufffd", "err")
    assert [kw["timeout_s"] for _, kw in adapter.ops("state")] == [120, 120]
    assert len(adapter.ops("delete")) == 1


@pytest.mark.parametrize("state, error", [
    (SandboxState.GONE, SandboxTimeoutError), (SandboxState.RUNNING, ProviderError),
    (SandboxState.UNKNOWN, ProviderError), (None, ProviderError),
])
def test_hung_exec_checks_state_under_independent_budget(setup, caplog, state, error):
    engine, adapter, _ = setup
    release = threading.Event()

    def hang():
        assert threading.current_thread().daemon
        release.wait(2)
        return RawExecResult("", "", 0)

    def check(sandbox_id, **kw):
        if sandbox_id in adapter.gone:
            return SandboxState.GONE
        if state is None:
            hang()
        return state

    adapter.handlers.update(exec=on_job(hang), state=check)
    try:
        with caplog.at_level("DEBUG", logger="anycompute"), pytest.raises(error) as caught:
            engine.run(["script"], provider="fake", ttl=61)
        assert caught.value.sandbox_seconds == (360 if state is None else 240)
        assert [kw["timeout_s"] for _, kw in adapter.ops("state")] == [120, 120]
        assert len(adapter.ops("delete")) == len(adapter.ops("create")) == 1
        assert "abandoned" in caplog.text and "exec" in caplog.text
    finally:
        release.set()


def test_prepare_timeout_leaves_no_create(setup):
    engine, adapter, clock = setup
    release = threading.Event()
    adapter.handlers["prepare"] = lambda **kw: release.wait(2)
    try:
        with pytest.raises(ProviderError) as caught:
            engine.run(["script"], provider="fake", ttl=61)
        assert clock.now() == 240 and not caught.value.sandbox_may_exist
        assert not adapter.ops("create")
    finally:
        release.set()


# Generated by /ship coverage audit
# Value: protects=An ID attached to a late failed create is reconciled after create timeout;
#   fails_when=Reconciliation ignores IDs carried by failed create replies;
#   why_new=The previous late-reply cases only returned successful IDs; seam=none;
#   no_claim=The adapter and clock are scripted, so this does not establish provider deletion.
@pytest.mark.parametrize("interrupt,partial_failure", [(False, False), (True, False), (False, True)])
def test_lost_create_reconciles_when_late_id_arrives(setup, monkeypatch, caplog, interrupt, partial_failure):
    engine, adapter, clock = setup
    started, release, confirmed = threading.Event(), threading.Event(), threading.Event()
    wait = clock.wait

    def create(**kw):
        assert threading.current_thread().daemon
        started.set()
        assert release.wait(2)
        if partial_failure:
            raise ProviderError("start failed", sandbox_id="late-box")
        return "late-box"

    def state(sandbox_id, **kw):
        if sandbox_id in adapter.gone:
            confirmed.set()
            return SandboxState.GONE
        return SandboxState.RUNNING

    def interrupt_wait(event, timeout):
        if len(clock.waits) == 1:
            assert started.wait(1)
            clock.waits.append(timeout)
            raise KeyboardInterrupt
        return wait(event, timeout)

    if interrupt:
        monkeypatch.setattr(clock, "wait", interrupt_wait)
    adapter.handlers.update(create=create, state=state)
    try:
        with caplog.at_level("DEBUG", logger="anycompute"):
            with pytest.raises(ProviderError) as caught:
                engine.run(["script"], provider="fake", ttl=61, run_id="recover")
            assert caught.value.sandbox_may_exist and caught.value.interrupted is interrupt
            assert caught.value.run_id == "recover"
            release.set()
            assert confirmed.wait(1)
        assert len(adapter.ops("create")) == len(adapter.ops("delete")) == 1
        assert adapter.ops("delete")[0] == (("late-box",), {"timeout_s": 120})
        assert "late create" in caplog.text
    finally:
        release.set()


@pytest.mark.parametrize("error", [AuthError("denied"), RateLimitError("429", retry_after=7), QuotaError("quota")])
def test_mapped_create_error_is_not_retried(setup, error):
    engine, adapter, _ = setup
    adapter.handlers["create"] = failure(error)
    with pytest.raises(type(error)) as caught:
        engine.run(["script"], provider="fake", run_id="create-error")
    assert caught.value.run_id == "create-error"
    if isinstance(error, RateLimitError):
        assert caught.value.retry_after == 7
    assert len(adapter.ops("create")) == 1 and not adapter.ops("delete")


@pytest.mark.parametrize("step", ["create", "mkdir", "write", "exec", "read"])
def test_partial_create_and_post_create_errors_still_delete(setup, step):
    engine, adapter, clock = setup

    def fail(*args, **kw):
        clock.sleep(3)
        raise ProviderError("start failed", sandbox_id="box-1") if step == "create" else TypeError("broken adapter")

    if step == "mkdir":
        adapter.handlers["exec"] = lambda *args, **kw: RawExecResult("", "bad directory", 1)
    elif step == "exec":
        adapter.handlers["exec"] = on_job(fail)
    else:
        adapter.handlers[step] = fail
    with pytest.raises(ProviderError) as caught:
        engine.run(["script"], provider="fake", files={"x": b""}, read=["x"], run_id="failure")
    error = caught.value
    assert (error.sandbox_id, error.run_id, error.sandbox_seconds) == ("box-1", "failure", 0 if step == "mkdir" else 3)
    assert error.internal is (step not in {"create", "mkdir"})
    assert error.__context__ is error.__cause__ is None
    assert [args for args, _ in adapter.ops("delete")] == [("box-1",)]
    assert len(adapter.ops("state")) == 1


@pytest.mark.parametrize("outcome", ["result", "run-error", "sandbox-error", "interrupt"])
def test_failed_cleanup_preserves_result_or_original_exception(setup, outcome):
    engine, adapter, clock = setup
    adapter.handlers["delete"] = failure(ProviderError("cannot delete"))
    original = ValueError("user exception")
    if outcome == "run-error":
        adapter.handlers["exec"] = on_job(failure(QuotaError("original")))
    if outcome == "interrupt":
        adapter.handlers["exec"] = on_job(failure(KeyboardInterrupt()))

    error_type = {"result": CleanupError, "run-error": QuotaError,
                  "sandbox-error": ValueError, "interrupt": KeyboardInterrupt}[outcome]
    with pytest.raises(error_type) as caught:
        if outcome == "sandbox-error":
            with engine.Sandbox("fake", run_id="keep"):
                raise original
        else:
            engine.run(["script"], provider="fake", files={"x": b"\xff"}, read=["x"], run_id="keep")
    cleanup = caught.value if outcome == "result" else caught.value.cleanup_error
    assert (cleanup.sandbox_id, cleanup.run_id, cleanup.sandbox_seconds) == ("box-1", "keep", None)
    assert clock.now() == 120
    if outcome == "result":
        assert cleanup.result.stdout == "out\ufffd"
        assert cleanup.result.files == {"x": b"\xff"} and cleanup.result.sandbox_seconds is None
    else:
        assert caught.value.__cause__ is None
        assert str(cleanup) in caught.value.__notes__
        assert (caught.value.sandbox_id, caught.value.run_id, caught.value.sandbox_seconds) == ("box-1", "keep", None)
        if outcome == "sandbox-error":
            assert caught.value is original


def test_second_interrupt_escapes_cleanup_with_context(setup):
    engine, adapter, _ = setup
    adapter.handlers.update(exec=on_job(failure(KeyboardInterrupt("first"))), delete=failure(KeyboardInterrupt("second")))
    with pytest.raises(KeyboardInterrupt, match="second") as caught:
        engine.run(["script"], provider="fake", run_id="interrupt")
    assert (caught.value.sandbox_id, caught.value.run_id) == ("box-1", "interrupt")
    assert not adapter.ops("state")


def test_hung_delete_does_not_start_confirm(setup):
    engine, adapter, clock = setup
    release = threading.Event()
    adapter.handlers["delete"] = lambda *args, **kw: release.wait(2)
    try:
        with pytest.raises(CleanupError) as caught:
            engine.run(["script"], provider="fake", ttl=61)
        assert clock.now() == 120 and caught.value.result.exit_code == 7
        assert len(adapter.ops("delete")) == 1 and not adapter.ops("state")
    finally:
        release.set()


def test_cleanup_retries_and_counts_backoff_towards_deadline(setup):
    engine, adapter, clock = setup

    def delete(*args, **kw):
        if len(adapter.ops("delete")) == 1:
            raise ProviderError("502")

    def confirm(*args, **kw):
        if len(adapter.ops("state")) == 1:
            raise RateLimitError("429", retry_after=7)
        return SandboxState.GONE

    adapter.handlers.update(delete=delete, state=confirm)
    assert engine.run(["script"], provider="fake").sandbox_seconds == 8
    assert clock.sleeps == [1, 7]
    assert [kw["timeout_s"] for _, kw in adapter.ops("delete")] == [120, 119]
    assert [kw["timeout_s"] for _, kw in adapter.ops("state")] == [119, 112]


def test_confirm_unknown_follows_capped_poll_schedule(setup):
    engine, adapter, clock = setup
    adapter.handlers["state"] = lambda *args, **kw: SandboxState.UNKNOWN
    with pytest.raises(CleanupError):
        engine.run(["script"], provider="fake")
    assert clock.now() == 120
    assert [120 - kw["timeout_s"] for _, kw in adapter.ops("state")] == [0, 1, 3, 7, 15, *range(25, 120, 10)]


def set_caps(monkeypatch, **caps):
    spec = providers.get_provider("fake")
    monkeypatch.setattr(providers, "PROVIDERS", (replace(spec, caps=replace(spec.caps, **caps)),))


def test_redaction_handles_config_keys_short_tokens_and_escaped_json(setup, tmp_path, monkeypatch):
    engine, adapter, _ = setup
    key = 'synthetic"config-key'
    settings = tmp_path / "config.toml"
    settings.write_text(f"[example]\nkey = {json.dumps(key)}\n[other]\nlabel = 'keep-me'\n")
    monkeypatch.setenv("SYNTHETIC_ENGINE_CONFIG", str(settings))
    monkeypatch.setenv("SYNTHETIC_ENGINE_AUTH", "ab12")
    monkeypatch.setenv("SYNTHETIC_EMPTY", "")
    set_caps(monkeypatch, key_sources=KeySources(
        env=("SYNTHETIC_ENGINE_AUTH", "SYNTHETIC_EMPTY"),
        config=(ConfigKeys("no-such-file", (("*", "key"),), "SYNTHETIC_ENGINE_CONFIG"),),
    ))
    raw = f"ab12 ab12cd _ab12 ab12- (ab12) {key} keep-me"
    masked = "*** ab12cd _ab12 ab12- (***) *** keep-me"
    assert engine._redact_text(raw) == masked
    assert engine._redact_text(raw, "fake") == masked
    assert engine._redact_text(raw, "unknown") == raw
    monkeypatch.delenv("SYNTHETIC_ENGINE_AUTH")
    adapter.handlers["exec"] = on_job(lambda: RawExecResult(raw, key, 0))
    result = engine.run(["script"], provider="fake")
    assert json.loads(json.dumps({"stdout": result.stdout, "stderr": result.stderr})) == {
        "stdout": raw.replace(key, "***"), "stderr": "***",
    }


def test_provider_exception_and_cleanup_log_never_contain_keys(setup, monkeypatch, caplog):
    engine, adapter, _ = setup
    synthetic = "synthetic-fixture-only"
    env_secret = 'synthetic"job-secret'
    short = "true"
    set_caps(monkeypatch, provider_options_allowlist=frozenset({"environment"}))

    def fail():
        try:
            raise TypeError(synthetic)
        except TypeError as raw:
            raise QuotaError(f"{synthetic} {env_secret} {short}") from raw

    adapter.handlers.update(exec=on_job(fail), delete=failure(ProviderError(f"{synthetic} {env_secret}")))
    with caplog.at_level("DEBUG", logger="anycompute"), pytest.raises(QuotaError) as caught:
        engine.run(["script"], provider="fake", provider_options={"fake": {"environment": {"VALUE": env_secret, "FLAG": short}}})
    error = caught.value
    assert str(error) == "*** *** true"
    assert error.__context__ is error.__cause__ is error.cleanup_error.__context__ is None
    exposed = (str(error), repr(error), str(error.cleanup_error), *error.__notes__, *traceback.format_exception(error))
    assert all(synthetic not in text and env_secret not in text for text in exposed)
    assert any("cleanup error" in rec.message and "***" in rec.message for rec in caplog.records)
    assert all(synthetic not in repr((rec.msg, rec.args, rec.exc_info)) and env_secret not in repr((rec.msg, rec.args, rec.exc_info))
               and rec.exc_info is None for rec in caplog.records)


@pytest.mark.parametrize("opts", [{"unknown": {}}, {"fake": {"ttl_minutes": 3}}, {"fake": {"environment": {"KEY": 1}}}, []])
def test_invalid_provider_options_do_not_touch_provider(setup, monkeypatch, opts):
    engine, adapter, _ = setup
    set_caps(monkeypatch, provider_options_allowlist=frozenset({"environment", "ttl_minutes"}))
    with pytest.raises(ConfigError):
        engine.run(["script"], provider="fake", provider_options=opts)
    assert not adapter.calls


def test_options_only_reach_active_provider_and_native_marks_sandbox(setup, monkeypatch):
    engine, adapter, _ = setup
    set_caps(monkeypatch, provider_options_allowlist=frozenset({"environment"}))
    spec = providers.get_provider("fake")
    monkeypatch.setattr(providers, "PROVIDERS", (*providers.PROVIDERS, replace(spec, name="other")))
    active = {"environment": {"VALUE": "synthetic-option"}}
    options = {"other": {"environment": {"OTHER": "synthetic-other"}}, "fake": active}
    assert engine.run(["script"], provider="fake", provider_options=options).outside_contract
    assert adapter.ops("create")[-1][1]["options"] == active
    adapter.handlers["native"] = lambda *args, **kw: adapter
    with engine.Sandbox("fake", provider_options={"other": options["other"]}) as sb:
        assert not sb.outside_contract
        assert sb.native is adapter and sb.outside_contract


@pytest.mark.parametrize("version_status,version,host,expected_host", [
    (0, "3.11.0", "https://" "name:pass@" "provider.example/path?q=raw", "https://provider.example"),
    (127, "unknown", "unix:///private/socket", "local"),
])
def test_runtime_detects_in_sandbox_only_when_requested(setup, version_status, version, host, expected_host):
    engine, adapter, _ = setup

    def execute(sandbox_id, argv, **kw):
        if argv[0] == "python3":
            return RawExecResult("Python 3.11.0\n", "", version_status)
        if argv[0] == "uname":
            return RawExecResult("aarch64\n", "", 0)
        return RawExecResult("", "", 0)

    adapter.handlers.update(exec=execute, api_host=lambda **kw: host)
    result = engine.run(["script"], provider="fake", runtime=True)
    assert result.runtime == {"python": version, "arch": "aarch64", "api_host": expected_host}
    assert all(kw["cwd"] == "/tmp/anycompute" for args, kw in adapter.ops("exec") if args[1][0] != "mkdir")
    assert len(adapter.ops("api_host")) == 1


def test_runtime_failure_still_deletes(setup):
    engine, adapter, _ = setup
    adapter.handlers["api_host"] = failure(TypeError("runtime failed"))
    with pytest.raises(ProviderError, match="runtime failed") as caught:
        engine.run(["script"], provider="fake", runtime=True)
    assert caught.value.sandbox_id == "box-1" and caught.value.sandbox_seconds == 0
    assert len(adapter.ops("delete")) == len(adapter.ops("state")) == 1


def test_50_concurrent_runs_use_distinct_sandboxes_and_confirm_all(setup):
    engine, adapter, _ = setup
    barrier = threading.Barrier(50, timeout=5)

    def one(i):
        barrier.wait()
        return engine.run(["script"], provider="fake", ttl=i + 1)

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(one, range(50)))
    ids = {result.sandbox_id for result in results}
    assert len(ids) == 50
    run_ids = {result.run_id for result in results}
    assert len(run_ids) == 1
    run_id, = run_ids
    assert [kw["labels"] for _, kw in adapter.ops("create")] == [
        {"anycompute": "1", "anycompute-ttl": "60", "anycompute-run": run_id},
    ] * 50
    for operation in ("delete", "state"):
        assert len(adapter.ops(operation)) == 50
        assert {args[0] for args, _ in adapter.ops(operation)} == ids


def gc_row(ident, created, **labels):
    return SandboxInfo(ident, {"anycompute": "1", "anycompute-ttl": "60", "anycompute-run": "sweep", **labels}, created)


def test_gc_filters_age_labels_state_and_force(setup, monkeypatch):
    engine, adapter, clock = setup
    set_caps(monkeypatch, pre_start_window="partial")
    now = clock.wall_time()
    adapter.handlers["list"] = lambda *args, **kw: [
        gc_row("expired", now - 181), gc_row("young", now - 180),
        gc_row("no-ttl", now - 200, **{"anycompute-ttl": ""}),
        gc_row("malformed", now - 200, **{"anycompute-ttl": "-1"}),
        gc_row("no-time", None), gc_row("running", now - 181), gc_row("unknown", now - 181),
        gc_row("not-ours", now - 181, anycompute="0"),
    ]
    adapter.handlers["state"] = lambda ident, **kw: (
        SandboxState.STOPPED if ident == "expired" else SandboxState.RUNNING if ident == "running" else SandboxState.UNKNOWN
    )
    rows, error = engine._gc("fake", all_expired=True, dry_run=True)
    assert error is None and not adapter.ops("delete")
    assert {row["sandbox_id"]: (row["action"], row["reason"]) for row in rows} == {
        "expired": ("listed", "expired"), "young": ("kept", "young"),
        "no-ttl": ("kept", "no_ttl_label"), "malformed": ("kept", "no_ttl_label"),
        "no-time": ("kept", "no_create_time"), "running": ("kept", "running"),
        "unknown": ("kept", "running"),
    }
    assert rows[4]["created_at"] is None and rows[0]["created_at"].endswith(("Z", "+00:00"))
    adapter.handlers["state"] = lambda ident, **kw: SandboxState.GONE
    rows, error = engine._gc("fake", all_expired=True, force=True)
    assert error is None and len(adapter.ops("delete")) == 7
    assert {row["action"] for row in rows} == {"deleted"}
    assert {row["reason"] for row in rows} == {"force"}


def test_gc_run_continues_after_failed_delete_from_first_page(setup, monkeypatch):
    from tests.fakes import FakeProvider

    engine, _, clock = setup
    spec = providers.get_provider("fake")
    items = [gc_row("bad", None), gc_row("not-ours", None, anycompute="0"),
             gc_row("other", None, **{"anycompute-run": "not-sweep"}), gc_row("next-page", None)]
    fake = FakeProvider(clock=clock, caps=spec.caps, page_size=1,
                        sandboxes=[{"sandbox_id": row.sandbox_id, "labels": row.labels} for row in items],
                        script={"delete": [{"sandbox_id": "bad", "error": 429, "retry_after": 999},
                                           {"sandbox_id": "next-page", "error": 404}]})
    monkeypatch.setattr(providers, "PROVIDERS", (replace(spec, factory=lambda: fake),))
    rows, error = engine._gc("fake", run_id="sweep")
    assert [call["page"] for call in fake.calls if call["op"] == "list_page"] == [1, 2]
    list_call = next(call for call in fake.calls if call["op"] == "list")
    assert list_call["timeout_s"] == 120 and list_call["labels"] == {"anycompute": "1", "anycompute-run": "sweep"}
    assert [(call["sandbox_id"], call["at"], call["timeout_s"]) for call in fake.calls if call["op"] == "delete"] == [
        ("bad", 0, 120), ("next-page", 120, 120),
    ]
    assert isinstance(error, CleanupError) and error.sandbox_id == "bad"
    assert [(r["sandbox_id"], r["action"], r["reason"]) for r in rows] == [
        ("bad", "failed", "run_match"), ("next-page", "deleted", "run_match"),
    ]


@pytest.mark.parametrize("operation,error", [("list", ProviderError), ("state", ProviderError), ("delete", CleanupError)])
def test_gc_waits_under_independent_120_second_budgets(setup, monkeypatch, operation, error):
    engine, adapter, clock = setup
    set_caps(monkeypatch, pre_start_window="partial")
    release = threading.Event()
    adapter.handlers.update(list=lambda *args, **kw: [gc_row("expired", clock.wall_time() - 181)],
                            state=lambda *args, **kw: SandboxState.STOPPED)
    adapter.handlers[operation] = lambda *args, **kw: release.wait(2)
    try:
        if operation == "list":
            with pytest.raises(ProviderError, match="fake"):
                engine._gc("fake", all_expired=True)
        else:
            rows, failed = engine._gc("fake", all_expired=True)
            assert isinstance(failed, error) and failed.sandbox_id == "expired"
            expected = ("kept", "state_error") if operation == "state" else ("failed", "expired")
            assert [(row["action"], row["reason"]) for row in rows] == [expected]
            if operation == "state":
                assert not adapter.ops("delete")
        assert clock.now() == 120
        assert adapter.ops(operation)[0][1]["timeout_s"] == 120
    finally:
        release.set()


@pytest.mark.parametrize("rate", [True, 0, -1, float("nan"), float("inf"), "12"])
def test_invalid_keyword_pacing_rate_stops_before_calls(setup, rate):
    engine, adapter, _ = setup
    with pytest.raises(ConfigError, match="creates_per_minute"):
        engine.run(["script"], provider="fake", creates_per_minute=rate)
    assert not adapter.calls


# Generated by /ship coverage audit
# Value: protects=A second provider without a documented rate uses the cached environment rate independently;
#   fails_when=The scheduler ignores the environment fallback or shares another provider's override;
#   why_new=The original case used one provider with a keyword override; seam=none;
#   no_claim=This checks dispatch on a fake clock, not provider rate-limit acceptance.
def test_env_pacing_is_read_once_and_keyword_wins(setup, monkeypatch):
    engine, adapter, clock = setup
    monkeypatch.setenv("ANYCOMPUTE_CREATES_PER_MINUTE", "10")
    engine.run(["script"], provider="fake", creates_per_minute=12)
    monkeypatch.setenv("ANYCOMPUTE_CREATES_PER_MINUTE", "bad")
    engine.run(["script"], provider="fake")
    assert clock.sleeps == [5]
    with pytest.raises(ConfigError, match="creates_per_minute"):
        engine.Sandbox("fake", creates_per_minute=6)
    assert len(adapter.ops("create")) == 2

    spec = providers.get_provider("fake")
    monkeypatch.setattr(providers, "PROVIDERS", (spec, replace(spec, name="other")))
    engine.run(["script"], provider="other")
    assert clock.sleeps == [5]
    engine.run(["script"], provider="other")
    assert clock.sleeps == [5, 6]


def test_invalid_environment_pacing_rate_stops_before_calls(setup, monkeypatch):
    engine, adapter, _ = setup
    monkeypatch.setenv("ANYCOMPUTE_CREATES_PER_MINUTE", "not-a-rate")
    with pytest.raises(ConfigError, match="ANYCOMPUTE_CREATES_PER_MINUTE"):
        engine.run(["script"], provider="fake")
    assert not adapter.calls


def test_50_paced_threads_create_at_five_second_intervals(setup, monkeypatch):
    engine, adapter, clock = setup
    set_caps(monkeypatch, creates_per_minute=StartRate(12, "https://example.com/", "fixture"))
    ready = threading.Barrier(50, timeout=5)
    initiated = Queue()
    wake = threading.Condition()

    def sleep(seconds):
        with wake:
            due = clock.t + seconds
            clock.sleeps.append(due)
            wake.notify_all()
            assert wake.wait_for(lambda: clock.t >= due, timeout=5)

    monkeypatch.setattr(clock, "sleep", sleep)
    monkeypatch.setattr(clock, "wait", lambda event, timeout: event.wait(2))

    def create(**kw):
        initiated.put(clock.now())
        return f"paced-{next(adapter.ids)}"

    def caller():
        ready.wait()
        return engine.run(["script"], provider="fake")

    adapter.handlers["create"] = create
    with ThreadPoolExecutor(max_workers=50) as pool:
        jobs = [pool.submit(caller) for _ in range(50)]
        assert initiated.get(timeout=2) == 0
        with wake:
            assert wake.wait_for(lambda: len(clock.sleeps) == 49, timeout=2)
            assert sorted(clock.sleeps) == list(range(5, 250, 5))
        for instant in range(5, 250, 5):
            with wake:
                clock.t = instant
                wake.notify_all()
            assert initiated.get(timeout=2) == instant
        results = [job.result(timeout=2) for job in jobs]
    assert len({result.sandbox_id for result in results}) == len(adapter.ops("delete")) == 50


def test_validation_does_not_freeze_env_run_id_before_create(setup, monkeypatch):
    engine, _, _ = setup
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "before")
    with pytest.raises(InvalidTTLError):
        engine.run(["script"], provider="fake", ttl=False)
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "after")
    assert engine.run(["script"], provider="fake").run_id == "after"


def test_unmanaged_sandbox_cleans_up_when_exec_expires(setup):
    engine, adapter, _ = setup
    sb = engine.Sandbox("fake")
    adapter.handlers["exec"] = on_job(lambda: RawExecResult("partial", "", 137))
    adapter.handlers["state"] = lambda ident, **kw: SandboxState.GONE if ident in adapter.gone else SandboxState.STOPPED
    with pytest.raises(SandboxTimeoutError) as caught:
        sb.exec(["script"])
    assert caught.value.sandbox_seconds == sb.sandbox_seconds == 0
    assert len(adapter.ops("delete")) == 1
    sb.close()
    assert len(adapter.ops("delete")) == 1


# Generated by /ship coverage audit
# Value: protects=A provider-side config error after create still closes a standalone Sandbox;
#   fails_when=The engine mistakes an adapter failure for local validation and skips cleanup;
#   why_new=Existing cases cover local validation and standalone expiry only; seam=none;
#   no_claim=The adapter is scripted, so this does not establish provider error mappings or deletion.
def test_unmanaged_sandbox_cleans_up_after_provider_config_error(setup):
    engine, adapter, _ = setup
    sb = engine.Sandbox("fake", run_id="config-error")
    adapter.handlers["exec"] = on_job(failure(ConfigError("adapter rejected configuration")))

    with pytest.raises(ConfigError, match="adapter rejected configuration") as caught:
        sb.exec(["script"])

    assert [args for args, _ in adapter.ops("delete")] == [(sb.sandbox_id,)]
    assert [args for args, _ in adapter.ops("state")] == [(sb.sandbox_id,)]
    assert caught.value.sandbox_seconds == sb.sandbox_seconds == 0


def test_work_budgets_subtract_elapsed_create_and_work_time(setup):
    engine, adapter, clock = setup

    def create(**kw):
        clock.sleep(20)
        return "box"

    def upload(*args, **kw):
        clock.sleep(5)

    adapter.handlers.update(prepare=lambda **kw: clock.sleep(30), create=create, write=upload)
    result = engine.run(["script"], provider="fake", ttl=61, files={"in": b""}, read=["out"])
    assert [kw["timeout_s"] for _, _, kw in adapter.calls] == [240, 240, 220, 220, 215, 215, 120, 120]
    assert result.sandbox_seconds == 25


def test_ttl_that_rounds_above_the_provider_max_fails_before_create(setup, monkeypatch):
    engine, adapter, _ = setup
    set_caps(monkeypatch, ttl_max_s=90)
    with pytest.raises(InvalidTTLError, match="maximum"):
        engine.run(["script"], provider="fake", ttl=61)
    assert not adapter.calls


def test_invalid_ttl_message_masks_a_matching_short_provider_value(setup, monkeypatch):
    engine, adapter, _ = setup
    set_caps(monkeypatch, ttl_max_s=90)
    monkeypatch.setenv("SYNTHETIC_ENGINE_AUTH", str(90))
    with pytest.raises(InvalidTTLError, match=r"maximum \*\*\* seconds"):
        engine.run(["script"], provider="fake", ttl=61)
    assert not adapter.calls


def test_prepare_failure_does_not_freeze_run_id(setup, monkeypatch):
    engine, adapter, _ = setup
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "not-created")
    adapter.handlers["prepare"] = failure(ProviderError("image unavailable"))
    with pytest.raises(ProviderError, match="image unavailable"):
        engine.run(["script"], provider="fake")
    assert not adapter.ops("create")
    adapter.handlers.pop("prepare")
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "first-create")
    assert engine.run(["script"], provider="fake").run_id == "first-create"


@pytest.mark.parametrize("retry_after", [0, 6])
def test_delete_retries_once_at_provider_delay(setup, retry_after):
    engine, adapter, clock = setup

    def delete(sandbox_id, **kw):
        if len(adapter.ops("delete")) == 1:
            raise RateLimitError("429", retry_after=retry_after)
        adapter.gone.add(sandbox_id)

    adapter.handlers["delete"] = delete
    assert engine.run(["script"], provider="fake").sandbox_seconds == retry_after
    assert clock.now() == retry_after
    assert len(adapter.ops("delete")) == 2 and len(adapter.ops("state")) == 1


def test_confirm_repeated_zero_delay_falls_back_to_a_bounded_poll(setup):
    engine, adapter, clock = setup
    adapter.handlers["state"] = failure(RateLimitError("429", retry_after=0))
    with pytest.raises(CleanupError):
        engine.run(["script"], provider="fake")
    assert clock.now() == 120
    budgets = [kw["timeout_s"] for _, kw in adapter.ops("state")]
    assert budgets[:2] == [120, 120]
    assert budgets[2] < 120


def test_constructor_mkdir_failure_deletes_and_confirms(setup):
    engine, adapter, _ = setup
    adapter.handlers["exec"] = lambda *args, **kw: RawExecResult("", "bad directory", 1)
    with pytest.raises(ProviderError, match="bad directory") as caught:
        engine.Sandbox("fake", run_id="failed-open")
    assert (caught.value.sandbox_id, caught.value.run_id, caught.value.sandbox_seconds) == ("box-1", "failed-open", 0)
    assert len(adapter.ops("delete")) == len(adapter.ops("state")) == 1

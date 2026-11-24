import json
from pathlib import Path
import subprocess
import sys
from hashlib import sha256
from threading import Event, Thread

import pytest

from anycompute.contract import (
    AuthError, ProviderError, QuotaError, RateLimitError, RawExecResult, SandboxInfo, SandboxState,
)
from tests.fakes import FakeClock, FakeProvider


def test_in_memory_sandbox_round_trip(tmp_path):
    clock = FakeClock()
    fake = FakeProvider(clock=clock)
    labels = {"anycompute": "1", "anycompute-run": "unit", "anycompute-ttl": "5"}
    fake.prepare(options={}, timeout_s=3)
    sandbox = fake.create(ttl=5, labels=labels, options={}, timeout_s=2)
    marker = tmp_path / "must-not-exist"
    # An argv that would leave a visible host side effect if executed.
    argv = ["touch", str(marker)]
    assert fake.exec(sandbox, argv, cwd="/tmp/anycompute", timeout_s=1) == RawExecResult("", "", 0)
    assert not marker.exists()
    assert [c["argv"] for c in fake.calls if c["op"] == "exec"] == [argv]
    assert [c["ttl"] for c in fake.calls if c["op"] == "create"] == [5]
    assert fake.state(sandbox, timeout_s=1) == SandboxState.RUNNING
    assert fake.native(sandbox, timeout_s=1) == sandbox
    assert fake.api_host(timeout_s=1) == "https://fake.invalid"

    fake.write(sandbox, "/tmp/anycompute/sub/in.txt", b"\x00\xff", timeout_s=1)
    assert fake.read(sandbox, "/tmp/anycompute/sub/in.txt", timeout_s=1) == b"\x00\xff"
    assert fake.read(sandbox, "/tmp/anycompute/sub", timeout_s=1) is None
    assert fake.list({"anycompute-run": "unit"}, timeout_s=1) == [
        SandboxInfo(sandbox, labels, clock.wall_time())
    ]
    assert fake.list({"anycompute-run": "other"}, timeout_s=1) == []
    fake.delete(sandbox, timeout_s=1)
    fake.delete(sandbox, timeout_s=1)
    assert fake.state(sandbox, timeout_s=1) == SandboxState.GONE
    assert fake.list({}, timeout_s=1) == []


def _create(fake):
    return fake.create(ttl=5, labels={"anycompute": "1"}, options={}, timeout_s=4)


def test_call_log_returns_a_snapshot():
    fake = FakeProvider()
    _create(fake)
    fake.calls[0]["labels"]["anycompute"] = "changed"
    assert fake.calls[0]["labels"] == {"anycompute": "1"}


@pytest.mark.parametrize(("response", "kind"), [
    (401, AuthError), (429, RateLimitError), ("quota", QuotaError), (502, ProviderError),
    ("internal", TypeError), ("interrupt", KeyboardInterrupt),
])
def test_scripted_create_errors_do_not_create_or_repeat(response, kind):
    fake = FakeProvider(script={"create": [
        {"error": response, "message": "synthetic failure", "retry_after": 7},
    ]})
    with pytest.raises(kind, match="synthetic failure") as raised:
        _create(fake)
    if kind is RateLimitError:
        assert raised.value.retry_after == 7
    assert fake.list({}, timeout_s=1) == []
    assert _create(fake) == "fake-1"


def test_duplicate_and_failed_after_create_leave_labeled_inventory():
    fake = FakeProvider(script={"create": [
        {"duplicate": True}, {"error": 502, "created": True},
    ]})
    primary = _create(fake)
    with pytest.raises(ProviderError) as raised:
        _create(fake)
    ids = [s.sandbox_id for s in fake.list({"anycompute": "1"}, timeout_s=1)]
    assert ids == [primary, "fake-2", raised.value.sandbox_id]
    assert len(set(ids)) == 3


def test_mkdir_is_independent_from_scripted_workload():
    fake = FakeProvider(script={"exec": [
        {"stdout": [255], "stderr": "partial", "exit_code": 137, "files": {"out.bin": [0, 255]}},
        {"argv": ["exact"], "exit_code": None},
    ]})
    sandbox = _create(fake)
    assert fake.exec(sandbox, ["mkdir", "-p", "/tmp/anycompute"], cwd="/", timeout_s=1).exit_code == 0
    assert fake.exec(sandbox, ["implicit"], cwd="/tmp/anycompute", timeout_s=1) == RawExecResult(
        b"\xff", "partial", 137,
    )
    assert fake.exec(sandbox, ["unmatched"], cwd="/tmp/anycompute", timeout_s=1).exit_code == 0
    assert fake.exec(sandbox, ["exact"], cwd="/tmp/anycompute", timeout_s=1).exit_code is None
    assert fake.read(sandbox, "/tmp/anycompute/out.bin", timeout_s=1) == b"\x00\xff"


def test_delete_failure_can_be_pinned_to_one_sandbox():
    fake = FakeProvider(script={
        "delete": [{"sandbox_id": "fake-1", "error": 502, "repeat": True}, {"error": 404}],
        "state": [{"sandbox_id": "fake-1", "state": "unknown"}, {"state": "stopped"}],
    })
    first, second = _create(fake), _create(fake)
    for _ in range(2):
        with pytest.raises(ProviderError):
            fake.delete(first, timeout_s=1)
    fake.delete(second, timeout_s=1)
    assert fake.state(first, timeout_s=1) == SandboxState.UNKNOWN
    assert fake.state(first, timeout_s=1) == SandboxState.STOPPED
    assert fake.state(first, timeout_s=1) == SandboxState.RUNNING
    assert fake.state(second, timeout_s=1) == SandboxState.GONE


def _background(operation):
    done = Event()
    results = []

    def run():
        try:
            results.append(operation())
        finally:
            done.set()

    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread, done, results


def test_clock_wait_advances_only_to_delayed_reply():
    clock = FakeClock(start=10, wall_start=20)
    fake = FakeProvider(clock=clock, script={"prepare": [{"delay": 3}]})
    thread, done, results = _background(lambda: fake.prepare(options={}, timeout_s=5))
    try:
        assert clock.wait(done, 5)
        assert results == [None]
        assert clock.now() == 13
        assert clock.wall_time() == 23
    finally:
        clock.sleep(5)
        thread.join(1)


def test_late_create_outlives_wait_and_keeps_wall_time():
    clock = FakeClock(wall_start=100)
    fake = FakeProvider(clock=clock, script={"create": [{"delay": 6}]})
    thread, done, results = _background(lambda: _create(fake))
    try:
        assert not clock.wait(done, 4)
        assert clock.now() == 4
        assert [row.created_at for row in fake.list({}, timeout_s=1)] == [100]
        clock.sleep(2)
        assert done.wait(1)
        assert results == ["fake-1"]
    finally:
        clock.sleep(6)
        thread.join(1)


def test_hung_exec_releases_provider_lock_and_can_be_unblocked():
    clock = FakeClock()
    fake = FakeProvider(clock=clock, script={"exec": [{"hang": True}]})
    sandbox = _create(fake)
    thread, done, _ = _background(lambda: fake.exec(sandbox, ["fake"], cwd="/tmp/anycompute", timeout_s=5))
    try:
        assert not clock.wait(done, 5)
        assert clock.now() == 5
        assert [call["sandbox_id"] for call in fake.calls if call["op"] == "exec"] == [sandbox]
        fake.delete(sandbox, timeout_s=1)
        assert fake.state(sandbox, timeout_s=1) == SandboxState.GONE
    finally:
        fake.release_hangs()
        thread.join(1)
    assert done.is_set()


def test_paged_list_keeps_labels_create_times_and_one_budget():
    clock = FakeClock(wall_start=200)
    labels = {"anycompute": "1", "anycompute-run": "same"}
    fake = FakeProvider(clock=clock, page_size=1, sandboxes=[
        {"sandbox_id": "fake-1", "labels": labels, "created_at": None, "state": "unknown"},
        {"sandbox_id": "other", "labels": {"anycompute-run": "same"}},
        {"sandbox_id": "second-page", "labels": labels, "created_at": 50, "files": {"ready": "yes"}},
    ], script={"list_page": [{"delay": 2}, {}]})
    thread, done, results = _background(lambda: fake.list(labels, timeout_s=4))
    try:
        assert clock.wait(done, 4)
        assert results == [[SandboxInfo("fake-1", labels, None), SandboxInfo("second-page", labels, 50)]]
        pages = [(call["page"], call["timeout_s"]) for call in fake.calls if call["op"] == "list_page"]
        assert pages == [(1, 4), (2, 2)]
        assert fake.state("fake-1", timeout_s=1) == SandboxState.UNKNOWN
        assert fake.read("second-page", "/tmp/anycompute/ready", timeout_s=1) == b"yes"
        assert _create(fake) != "fake-1"
    finally:
        clock.sleep(4)
        thread.join(1)


def test_launcher_installs_only_the_configured_fake(monkeypatch, tmp_path):
    from anycompute import cli, engine, providers
    from tests.fakes import launcher

    script = tmp_path / "script.json"
    script.write_text(json.dumps({
        "clock": {"start": 6, "wall_start": 100},
        "caps": {"ttl_unit_s": 5},
        "script": {"exec": [{"exit_code": 7}]},
    }), encoding="utf-8")
    monkeypatch.setenv("ANYCOMPUTE_FAKE_SCRIPT", str(script))
    monkeypatch.setattr(providers, "PROVIDERS", providers.PROVIDERS)
    monkeypatch.setattr(engine, "_clock", engine._clock)

    def check_cli_wiring():
        (spec,) = providers.PROVIDERS
        assert spec.name == "fake"
        assert spec.caps.ttl_unit_s == 5
        assert spec.caps.key_sources.env == ("ANYCOMPUTE_FAKE_KEY",)
        fake = spec.factory()
        assert engine._clock is fake.clock
        assert (fake.clock.now(), fake.clock.wall_time()) == (6, 100)
        return fake.exec(_create(fake), ["not-a-real-command"], cwd="/tmp/anycompute", timeout_s=1).exit_code

    monkeypatch.setattr(cli, "main", check_cli_wiring)
    assert launcher.main() == 7


# Generated by /ship coverage audit
# Value: protects=The real CLI saves binary readback under its default output root and reports missing files;
#   fails_when=Readback is dropped, written elsewhere, or put in JSON as bytes instead of metadata;
#   why_new=Existing subprocess checks covered only text and status; save tests stubbed the engine; seam=none;
#   no_claim=The launcher uses an in-memory provider, not a real sandbox filesystem.
@pytest.mark.parametrize(("step", "code", "error"), [
    ({"delay": 2, "stdout": [255], "stderr": "scripted", "exit_code": 7,
      "files": {"sub/out.bin": [0, 255]}}, 7, None),
    ({"hang": True}, 124, "ProviderError"),
])
def test_launcher_runs_real_cli_with_scripted_worker(tmp_path, step, code, error):
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"caps": {"ttl_unit_s": 5}, "script": {"exec": [step]}}), encoding="utf-8")
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "tests.fakes.launcher",
         "run", "--json", "--provider", "fake", "--ttl", "3", "--read", "sub/out.bin",
         "--read", "missing.bin", "--", "not-a-real-command"],
        cwd=tmp_path,
        env={"PYTHONPATH": f"{repo / 'src'}:{repo}", "PYTHONDONTWRITEBYTECODE": "1",
             "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "ANYCOMPUTE_RUN_ID": "test-launcher",
             "ANYCOMPUTE_FAKE_SCRIPT": str(script), "ANYCOMPUTE_FAKE_KEY": "synthetic-key"},
        encoding="utf-8", capture_output=True, timeout=5,
    )
    assert result.returncode == code, result.stderr
    report = json.loads(result.stdout)
    assert report["provider"] == "fake"
    assert report["run_id"] == "test-launcher"
    if error is None:
        assert report["error"] is None
        assert report["stdout"] == "\ufffd"
        assert report["stderr"] == "scripted"
        assert report["effective_ttl"] == 5
        assert report["sandbox_seconds"] == 2
        saved = Path("anycompute-out/test-launcher/sub/out.bin")
        payload = b"\x00\xff"
        assert (tmp_path / saved).read_bytes() == payload
        assert report["files"] == {"sub/out.bin": {
            "saved_path": str(saved), "size": 2, "sha256": sha256(payload).hexdigest(),
        }}
        assert report["missing"] == ["missing.bin"]
    else:
        assert report["error"]["type"] == error

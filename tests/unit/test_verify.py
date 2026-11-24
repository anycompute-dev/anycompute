import json
import signal
from dataclasses import replace
from hashlib import sha256
from importlib.metadata import version

import pytest

from anycompute import cli, engine, lifecycle, providers
from anycompute.contract import ProviderSpec, StartRate
from tests.fakes import FAKE_CAPS, FakeClock, FakeProvider


@pytest.fixture
def register(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(engine, "_clock", clock)
    monkeypatch.setattr(engine, "_pacers", {})
    monkeypatch.setattr(engine, "_env_rate_loaded", False)
    monkeypatch.setattr(engine, "_env_rate", None)
    monkeypatch.setattr(engine, "_run_id", None)
    monkeypatch.delenv("ANYCOMPUTE_CREATES_PER_MINUTE", raising=False)
    monkeypatch.delenv("ANYCOMPUTE_REPORT_SOURCE", raising=False)
    monkeypatch.setenv("ANYCOMPUTE_RUN_ID", "a" * 40)
    monkeypatch.setenv("ANYCOMPUTE_FAKE_KEY", "fixture-only-key")

    def install(*specs):
        adapters = {}
        for item in specs:
            name, options = (item, {}) if isinstance(item, str) else item
            adapters[name] = FakeProvider(clock=clock, **options)
        monkeypatch.setattr(providers, "PROVIDERS", tuple(
            ProviderSpec(name, fake.caps, lambda fake=fake: fake) for name, fake in adapters.items()
        ))
        return adapters, clock

    return install


def task_file(tmp_path, settings=""):
    task = tmp_path / "task.toml"
    task.write_text('command = ["job", "literal arg"]\n' + settings)
    return task


def run_json(task, capsys, *, names="first,second", extra=()):
    code = cli.main(["verify", str(task), "--providers", names, "--json", *extra])
    output = capsys.readouterr()
    return code, json.loads(output.out), output.err


def test_verify_compares_output_and_files_with_runtime_but_keeps_metadata(tmp_path, register, capsys, monkeypatch):
    caps = replace(FAKE_CAPS, sdk_package="pytest")
    adapters, _ = register(
        ("first", {"caps": caps, "script": {"exec": [
            {"argv": ["job", "literal arg"], "stdout": [111, 255], "files": {"out": "first-value"}},
            {"argv": ["python3", "--version"], "stdout": "Python 3.12.0\n"},
            {"argv": ["uname", "-m"], "stdout": "aarch64\n"},
        ]}}),
        ("second", {"script": {"exec": [
            {"argv": ["job", "literal arg"], "stdout": "second-value"},
            {"argv": ["python3", "--version"], "exit_code": 127},
            {"argv": ["uname", "-m"], "stdout": "x86_64\n"},
        ]}}),
    )
    task = task_file(tmp_path, 'inputs = ["input.txt"]\nread = ["out"]\nttl = 30\n')
    (tmp_path / "input.txt").write_bytes(b"input")
    started = adapters["second"].create

    def after_first(*args, **kwargs):
        assert adapters["first"].calls[-1]["op"] == "state"
        return started(*args, **kwargs)

    monkeypatch.setattr(adapters["second"], "create", after_first)
    code, report, err = run_json(task, capsys)
    assert code == 1
    first, second = report["rows"]
    assert report["summary"] == (
        "differs on second: stdout, files; runtime differs: first (3.12.0, aarch64), second (unknown, x86_64)"
    )
    assert report["anycompute_version"] == version("anycompute")
    assert report["source"] == "user-run"
    assert report["suite_version"] is report["error"] is None
    assert first["provider_sdk_version"]
    assert second["provider_sdk_version"] is None
    assert first["runtime"] == {"python": "3.12.0", "arch": "aarch64", "api_host": "https://fake.invalid"}
    assert first["exit_code"] == first["cleanup"] == first["files"] == first["output"] == "pass"
    assert second["files"] == second["output"] == "differs"
    assert first["sandbox_seconds"] == second["sandbox_seconds"] == 0
    assert second["details"] == {
        "reference_provider": "first",
        "stdout": {"reference": "o\ufffd", "actual": "second-value"},
        "files": [{"path": "out", "reference_sha256": sha256(b"first-value").hexdigest(), "sha256": None}],
    }
    assert "first-value" not in json.dumps(report)
    assert err == f"run_id: {'a' * 40}\n"
    for adapter in adapters.values():
        assert [(call["path"], call["data"]) for call in adapter.calls if call["op"] == "write"] == [
            ("/tmp/anycompute/input.txt", b"input"),
        ]
        assert [call["ttl"] for call in adapter.calls if call["op"] == "create"] == [30]


def test_exit_code_only_still_reports_runtime_and_partial_caps(tmp_path, register, capsys, monkeypatch):
    caps = replace(FAKE_CAPS, lifetime="partial", labels_at_create=False, create_retry="partial")
    register(
        ("first", {"script": {"exec": [
            {"argv": ["job", "literal arg"], "stdout": "different"},
            {"argv": ["uname", "-m"], "stdout": "x86_64", "repeat": True},
        ]}}),
        ("second", {"caps": caps}),
    )
    task = task_file(tmp_path, 'compare = "exit_code"\n')
    monkeypatch.setenv("ANYCOMPUTE_REPORT_SOURCE", "project-run")
    code, report, _ = run_json(task, capsys)
    assert code == 0
    assert report["source"] == "project-run"
    assert report["rows"][1]["partial_caps"] == ["lifetime", "create_retry", "labels_at_create"]
    assert report["rows"][1]["lifecycle"] is None
    assert report["rows"][1]["output"] == "pass"
    assert report["summary"] == (
        "portable on first, second; runtime differs: first (unknown, x86_64), second (unknown, unknown); "
        "partial lifetime on second"
    )
    assert cli.main(["verify", str(task), "--providers", "first,second"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == report["summary"]


def test_first_error_kind_wins_and_every_provider_is_attempted(tmp_path, register, capsys):
    adapters, _ = register(
        ("first", {"script": {"create": [{"error": 429, "retry_after": 3}]}}),
        ("second", {"script": {"create": [{"error": "quota"}]}}),
    )
    code, report, err = run_json(task_file(tmp_path), capsys)
    assert code == 121
    assert report["error"] == report["rows"][0]["error"]
    assert report["summary"] == "error on first: RateLimitError; error on second: QuotaError"
    assert "retry_after=3s" in err and "QuotaError" in err
    assert all([call["op"] for call in adapter.calls] == ["prepare", "create"] for adapter in adapters.values())


def test_cleanup_error_keeps_result_and_takes_precedence_over_mismatch(tmp_path, register, capsys):
    caps = replace(FAKE_CAPS, lifetime="partial")
    register(
        ("first", {"script": {"state": [{"state": "stopped", "repeat": True}]}}),
        ("second", {"caps": caps, "script": {"exec": [{"argv": ["job", "literal arg"], "exit_code": 7}]}}),
    )
    code, report, _ = run_json(task_file(tmp_path), capsys)
    assert code == 125
    first, second = report["rows"]
    assert first["exit_code"] == "pass"
    assert first["cleanup"] == "fail" and first["sandbox_seconds"] is None
    assert first["error"]["gc_command"] == f"anycompute gc --run {'a' * 40} --provider first --dry-run"
    assert second["exit_code"] == "fail" and second["cleanup"] == "pass"
    assert report["summary"] == (
        "error on first: CleanupError; fails on first: Cleanup; fails on second: Exit code; partial lifetime on second"
    )


@pytest.mark.parametrize(("settings", "kind"), [
    ('same = "unknown"\n', "ConfigError"), ('ttl = "30"\n', "InvalidTTLError"),
    ('inputs = ["/absolute"]\n', "InvalidPathError"), ('read = ["../secret"]\n', "InvalidPathError"),
    ('[providers.first]\nenvironment = ["UNSET_VERIFY_TEST_VARIABLE"]\n', "ConfigError"),
    ('[providers.first]\nenvironment = { TOKEN = "literal" }\n', "ConfigError"),
    ('[providers.first]\ntimeout = 20\n', "ConfigError"),
])
def test_invalid_tasks_fail_before_any_sandbox(tmp_path, register, capsys, monkeypatch, settings, kind):
    adapters, _ = register("first", "second")
    monkeypatch.delenv("UNSET_VERIFY_TEST_VARIABLE", raising=False)
    code, report, _ = run_json(task_file(tmp_path, settings), capsys)
    assert code == 126
    assert report["error"]["type"] == kind and report["rows"] == []
    assert all(not adapter.calls for adapter in adapters.values())


def test_registered_short_key_masks_dynamic_text_and_keeps_verify_statuses(tmp_path, register, capsys, monkeypatch):
    register(("first", {"script": {"exec": [{"stdout": "pass text"}]}}), "second")
    monkeypatch.setenv("ANYCOMPUTE_FAKE_KEY", "pass")
    code, report, _ = run_json(task_file(tmp_path), capsys)
    assert code == 1
    first, second = report["rows"]
    assert first["cleanup"] == second["exit_code"] == "pass"
    assert second["details"]["stdout"]["reference"] == "*** text"


def test_provider_options_are_local_and_secret_stays_out_of_report(tmp_path, register, capsys, monkeypatch):
    secret = "private-task-environment"
    monkeypatch.setenv("VERIFY_TEST_TOKEN", secret)
    adapters, _ = register(
        ("first", {"script": {"exec": [{"stdout": secret}]}}),
        ("second", {"script": {"exec": [{"stdout": "output"}]}}),
    )
    task = task_file(tmp_path, '[providers.first]\nenvironment = ["VERIFY_TEST_TOKEN"]\n')
    code, report, err = run_json(task, capsys)
    assert code == 1
    assert [row["outside_contract"] for row in report["rows"]] == [True, False]
    assert report["rows"][1]["details"]["stdout"]["reference"] == "***"
    assert secret not in json.dumps(report) + err
    assert [call["options"] for call in adapters["first"].calls if call["op"] == "create"] == [
        {"environment": {"VERIFY_TEST_TOKEN": secret}},
    ]


class Child:
    returncode = None

    def __init__(self, adapter, sandbox_id):
        self.adapter = adapter
        self.sandbox_id = sandbox_id

    def poll(self):
        return self.returncode

    def kill(self):
        assert self.adapter.calls[-1]["op"] == "read"
        self.returncode = -signal.SIGKILL

    def send_signal(self, sig):
        self.returncode = -sig

    def wait(self, *, timeout):
        return self.returncode


@pytest.mark.parametrize(("case", "status", "exit_code"), [
    ("partial", "partial", 0), ("no-marker", "fail", 1), ("failed-delete", "fail", 125),
])
def test_lifecycle_with_fake_child_uses_pacer_and_child_gc(tmp_path, register, capsys, monkeypatch,
                                                           case, status, exit_code):
    caps = replace(FAKE_CAPS, lifetime="partial", creates_per_minute=StartRate(12, "https://example.invalid", "2026-11-23"))
    script = {"state": [{"sandbox_id": "fake-2", "state": "stopped"}]}
    if case == "failed-delete":
        script["delete"] = [{"sandbox_id": "fake-2", "error": 500, "repeat": True}]
    adapters, clock = register(("first", {"caps": caps, "script": script}), "second")
    starts = []
    child_id = "a" * 37 + "-lc"

    def launch(name, run_id, ttl):
        assert run_id == child_id and ttl == 10
        starts.append((name, clock.now()))
        adapter = adapters[name]
        sandbox_id = adapter.create(ttl=ttl, labels={
            "anycompute": "1", "anycompute-run": run_id, "anycompute-ttl": str(ttl),
        }, options={}, timeout_s=ttl)
        if case != "no-marker" or name == "second":
            adapter.write(sandbox_id, "/tmp/anycompute/ready", b"", timeout_s=ttl)
        if name == "second":
            child = Child(adapter, sandbox_id)

            def kill():
                Child.kill(child)
                adapter.delete(sandbox_id, timeout_s=ttl)

            child.kill = kill
            return child
        return Child(adapter, sandbox_id)

    monkeypatch.setattr(lifecycle, "_launch_child", launch)
    task = task_file(tmp_path, 'ttl = 10\n[providers.first]\nimage = "test-image"\n')
    code, report, _ = run_json(task, capsys, extra=("--lifecycle",))
    assert code == exit_code
    assert report["rows"][0]["lifecycle"] == status
    assert report["rows"][0]["details"]["lifecycle_options"] == "default options"
    assert report["rows"][1]["lifecycle"] == "pass"
    assert starts[0] == ("first", 5)
    assert [call["options"] for call in adapters["first"].calls if call["op"] == "create"] == [{"image": "test-image"}, {}]
    assert {"anycompute": "1", "anycompute-run": child_id} in [
        call["labels"] for call in adapters["first"].calls if call["op"] == "list"
    ]
    if case == "no-marker":
        assert report["rows"][0]["details"]["lifecycle_reason"] == "no marker by the TTL"
    if case == "failed-delete":
        assert report["error"]["run_id"] == child_id
        assert report["error"]["gc_command"] == f"anycompute gc --run {child_id} --provider first --dry-run"


def test_verify_usage_error_keeps_report_shape(capsys):
    assert cli.main(["verify", "missing.toml", "--json"]) == 126
    report = json.loads(capsys.readouterr().out)
    assert report["rows"] == [] and report["error"]["type"] == "ConfigError"

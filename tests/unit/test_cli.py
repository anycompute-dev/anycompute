import importlib
import io
import json
import logging
import os
import shlex
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from anycompute import providers
from anycompute.contract import (
    CleanupError, ConfigError, ConfigKeys, KeySources, ProviderCaps, ProviderError, ProviderSpec,
    RateLimitError, Result, SandboxTimeoutError, StartRate,
)
from tests.fakes import FAKE_CAPS, FakeClock, FakeProvider


@pytest.fixture
def cli():
    return importlib.import_module("anycompute.cli")


@pytest.fixture
def engine(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unexpected engine call")

    # Replace only the lifecycle boundary; no adapter or workload is executed.
    import anycompute

    stub = SimpleNamespace(
        run=unexpected, _gc=unexpected, _resolve_run_id=lambda run_id=None: run_id or "test-run",
        _redact_text=lambda text, provider=None: text, _validate_path=lambda path: None,
    )
    monkeypatch.setattr(anycompute, "engine", stub, raising=False)
    return stub


ERROR_KEYS = {
    "type", "message", "sandbox_id", "run_id", "retry_after", "sandbox_may_exist",
    "internal", "gc_command", "cleanup_error",
}


@pytest.mark.parametrize("args", [
    [], ["run", "--bogus", "--provider", "fake", "--", "job"],
    ["run", "--", "job"], ["run", "--provider", "fake"],
    ["run", "--provider", "fake", "--ttl", "abc", "--", "job"],
    ["gc", "--provider", "fake", "--run", "test-run", "--all"],
    ["gc", "--provider", "fake"], ["gc", "--all"],
    ["gc", "--provider", "fake", "--all", "--bogus"], ["providers", "--bogus"],
])
def test_usage_errors_are_config_errors_in_json(cli, engine, capsys, args):
    assert cli.main(["--json", *args]) == ConfigError.exit_code
    out = capsys.readouterr()
    error = json.loads(out.out)["error"]
    assert set(error) == ERROR_KEYS
    assert error["type"] == "ConfigError"
    assert out.err.startswith("ConfigError: ")


def test_module_help_warns_scripts_about_exit_collisions():
    proc = subprocess.run(
        [sys.executable, "-m", "anycompute", "run", "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "collide" in proc.stdout and "error" in proc.stdout and "--json" in proc.stdout
    assert proc.stderr == ""


RESULT = Result("fake stdout\n", "fake stderr\n", 121, {}, ["missing"], "fake", "sb",
                "test-run", 60, None, 1.5, False)


def test_run_preserves_argv_upload_and_exit_code_collision(cli, engine, capsys, tmp_path):
    upload = tmp_path / "in.txt"
    upload.write_bytes(b"input")

    def fake_run(argv, **kwargs):
        assert capsys.readouterr() == ("", "run_id: test-run\n")
        assert argv == ["job", "--json", "a b", "--", "$x;"]
        assert kwargs == {"provider": "fake", "ttl": 60, "run_id": "test-run",
                          "files": {"in.txt": b"input"}, "read": []}
        return RESULT

    engine.run = fake_run
    assert cli.main(["run", "--json", "--provider", "fake", "--ttl", "60", "--file",
                     str(upload), "--", "job", "--json", "a b", "--", "$x;"]) == 121
    out = capsys.readouterr()
    assert out.err == ""
    assert json.loads(out.out) == {
        "stdout": RESULT.stdout, "stderr": RESULT.stderr, "exit_code": 121, "files": {},
        "missing": ["missing"], "provider": "fake", "sandbox_id": "sb", "run_id": "test-run",
        "effective_ttl": 60, "runtime": None, "sandbox_seconds": 1.5,
        "outside_contract": False, "error": None,
    }


@pytest.mark.parametrize("as_json", [True, False])
def test_cleanup_failure_keeps_completed_output(cli, engine, capsys, tmp_path, as_json):
    binary = bytes.fromhex("00ff")
    completed = replace(RESULT, files={"sub/out.bin": binary}, sandbox_seconds=None)

    def fake_run(*args, **kwargs):
        assert kwargs["read"] == ["sub/out.bin"]
        raise CleanupError("not gone", result=completed, run_id="test-run", sandbox_id="sb")

    engine.run = fake_run
    flags = ["--json"] if as_json else []
    assert cli.main(["run", *flags, "--provider", "fake", "--out", str(tmp_path),
                     "--read", "sub/out.bin", "--", "job"]) == 125
    saved_path = tmp_path / "test-run" / "sub" / "out.bin"
    assert saved_path.read_bytes() == binary
    out = capsys.readouterr()
    gc = "anycompute gc --run test-run --provider fake --dry-run"
    assert gc in out.err and "dropping --dry-run" in out.err
    assert "CleanupError: not gone" in out.err
    if as_json:
        report = json.loads(out.out)
        assert report["files"] == {"sub/out.bin": {"size": 2, "sha256": (
            "06eb7d6a69ee19e5fbdf749018d3d2abfa04bcbd1365db312eb86dc7169389b8"
        ), "saved_path": str(saved_path)}}
        assert report["sandbox_seconds"] is None
        assert report["exit_code"] == 121 and report["stdout"] == RESULT.stdout
        assert report["error"]["gc_command"] == gc
    else:
        assert out.out == RESULT.stdout and RESULT.stderr in out.err


@pytest.mark.parametrize("missing", [True, False])
def test_run_rejects_missing_inputs_and_duplicate_basenames(cli, engine, capsys, tmp_path, missing):
    first = tmp_path / "in.txt"
    if not missing:
        first.write_bytes(b"a")
    second = tmp_path / "other" / "in.txt"
    second.parent.mkdir()
    second.write_bytes(b"b")
    flags = ["--file", str(first)] + ([] if missing else ["--file", str(second)])
    assert cli.main(["run", "--provider", "fake", "--json", *flags, "--", "job"]) == 126
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["type"] == "InvalidPathError"
    assert report["provider"] == "fake" and report["run_id"] == "test-run"


@pytest.mark.parametrize("block", ["file", "root symlink", "run symlink"])
def test_run_rejects_unusable_output_folder_before_engine(cli, engine, capsys, tmp_path, block):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    if block == "file":
        root.touch()
    elif block == "root symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / "test-run").symlink_to(outside, target_is_directory=True)
    assert cli.main(["run", "--provider", "fake", "--json", "--out", str(root),
                     "--read", "out.bin", "--", "job"]) == 126
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["type"] == "ConfigError"
    assert list(outside.iterdir()) == []


def test_run_rejects_symlink_in_output_ancestors_before_write_probe(cli, engine, capsys, monkeypatch, tmp_path):
    actual = tmp_path / "actual"
    target = actual / "nested" / "test-run"
    target.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(cli, "_temporary_file", engine.run)
    assert cli.main(["run", "--provider", "fake", "--json", "--out", str(alias / "nested"),
                     "--read", "out.bin", "--", "job"]) == 126
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "ConfigError"
    assert not list(target.iterdir())


def test_run_rejects_unsupported_directory_operations_before_engine(cli, engine, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "os", SimpleNamespace(name="nt"))
    root = tmp_path / "unavailable"
    assert cli.main(["run", "--provider", "fake", "--json", "--out", str(root),
                     "--read", "out.bin", "--", "job"]) == 126
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "ConfigError"
    assert not root.exists()


@pytest.mark.parametrize("out", [None, "relative/nested", "absolute"])
def test_run_writes_readback_in_supported_output_directories(cli, engine, monkeypatch, tmp_path, out):
    monkeypatch.chdir(tmp_path)
    if out == "absolute":
        out = str(Path.cwd() / "absolute" / "nested")
    flags = [] if out is None else ["--out", out]
    engine.run = lambda *args, **kwargs: replace(RESULT, files={"sub/out.bin": b"\x00\xff"})
    assert cli.main(["run", "--provider", "fake", *flags, "--read", "sub/out.bin", "--", "job"]) == 121
    assert (Path(out or "anycompute-out") / "test-run" / "sub" / "out.bin").read_bytes() == b"\x00\xff"


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin filesystem aliases")
@pytest.mark.parametrize("alias", ["tmp", "var"])
def test_output_directory_walk_uses_physical_darwin_alias_target(cli, monkeypatch, alias):
    root = Path(f"/{alias}")
    if root.resolve() != Path(f"/private/{alias}"):
        pytest.skip("system uses a nonstandard root path")
    walked = []
    real_directory = cli._directory

    @contextmanager
    def observe_directory(path, parent=None):
        walked.append(path)
        if parent is None:
            with real_directory(path) as fd:
                yield fd
        else:
            yield parent  # Observe the alias translation without making any directories here.

    monkeypatch.setattr(cli, "_directory", observe_directory)
    with ExitStack() as stack:
        cli._output_directory(root / "nested", "test-run", stack)
    assert walked == ["/", "private", alias, "nested", "test-run"]


@pytest.mark.parametrize("link", ["subdir", "file", "hardlink"])
def test_run_refuses_linked_output_destinations(cli, engine, tmp_path, link):
    run_dir = tmp_path / "root" / "test-run"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "out.bin"
    target.write_bytes(b"keep")
    local = run_dir / "sub" / "out.bin"
    if link == "subdir":
        (run_dir / "sub").symlink_to(outside, target_is_directory=True)
    else:
        (run_dir / "sub").mkdir()
        if link == "file":
            local.symlink_to(target)
        else:
            local.hardlink_to(target)
    engine.run = lambda *args, **kwargs: replace(RESULT, files={"sub/out.bin": b"new"})
    assert cli.main(["run", "--provider", "fake", "--out", str(run_dir.parent),
                     "--read", "sub/out.bin", "--", "job"]) == 126
    assert target.read_bytes() == b"keep"
    if link == "hardlink":
        assert local.samefile(target)


@pytest.mark.parametrize(("first", "second"), [
    ("sub/item", "sub//./item"), ("sub/Item", "SUB/item"),
    ("sub/café", "sub/cafe\u0301"), ("sub/one", "sub/two"),
])
def test_run_saves_each_distinct_local_destination_once(cli, engine, capsys, tmp_path, first, second):
    run_dir = tmp_path / "test-run"
    first_saved, second_saved = run_dir / first, run_dir / second
    first_saved.parent.mkdir(parents=True)
    first_saved.write_bytes(b"preexisting regular file")
    same_destination = second_saved.exists()
    first_bytes, second_bytes = b"\x00\xff", b"second"
    engine.run = lambda *args, **kwargs: replace(RESULT, files={first: first_bytes, second: second_bytes})
    assert cli.main(["run", "--provider", "fake", "--json", "--out", str(tmp_path),
                     "--read", first, "--read", second, "--", "job"]) == (126 if same_destination else 121)
    report = json.loads(capsys.readouterr().out)
    assert first_saved.read_bytes() == first_bytes
    expected = {first: {"size": len(first_bytes), "sha256": sha256(first_bytes).hexdigest(),
                        "saved_path": str(first_saved)}}
    if same_destination:
        assert report["error"]["type"] == "ConfigError"
        assert second_saved.read_bytes() == first_bytes
    else:
        assert report["error"] is None
        assert second_saved.read_bytes() == second_bytes
        expected[second] = {"size": len(second_bytes), "sha256": sha256(second_bytes).hexdigest(),
                            "saved_path": str(second_saved)}
    assert report["files"] == expected


def test_cleanup_remains_primary_when_one_artifact_cannot_be_saved(cli, engine, capsys, tmp_path):
    run_dir = tmp_path / "test-run"
    (run_dir / "blocked").mkdir(parents=True)
    completed = replace(RESULT, files={"blocked": b"blocked", "safe": b"saved"})

    def fake_run(*args, **kwargs):
        raise CleanupError("still running", result=completed)

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--out", str(tmp_path),
                     "--read", "blocked", "--read", "safe", "--", "job"]) == 125
    assert (run_dir / "safe").read_bytes() == b"saved"
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert set(report["files"]) == {"safe"}
    assert report["error"]["type"] == "CleanupError"
    assert "Cannot save" in out.err


@pytest.mark.parametrize("cleanup_failed", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("operation", ["write", "flush"])
def test_completed_result_preserves_files_and_typed_exit_when_stdout_fails(
    cli, engine, capsys, monkeypatch, tmp_path, cleanup_failed, as_json, operation,
):
    class BrokenOutput:
        def write(self, text):
            if operation == "write":
                raise BrokenPipeError("synthetic pipe detail")
            return len(text)

        def flush(self):
            raise BrokenPipeError("synthetic pipe detail")

    def fake_run(*args, **kwargs):
        result = replace(RESULT, files={"bytes.bin": b"\x00\xff"},
                         sandbox_seconds=None if cleanup_failed else RESULT.sandbox_seconds)
        if cleanup_failed:
            raise CleanupError("still running", result=result)
        return result

    engine._redact_text = lambda text: text.replace("synthetic pipe detail", "***")
    engine.run = fake_run
    flags = ["--json"] if as_json else []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", BrokenOutput())
        assert cli.main(["run", *flags, "--provider", "fake", "--out", str(tmp_path),
                         "--read", "bytes.bin", "--", "job"]) == (125 if cleanup_failed else 126)
    assert (tmp_path / "test-run" / "bytes.bin").read_bytes() == b"\x00\xff"
    err = capsys.readouterr().err
    assert ("CleanupError: still running" if cleanup_failed else "ConfigError:") in err
    assert ("Cannot write JSON to stdout: ***" if as_json else "Cannot write workload stdout: ***") in err
    assert "synthetic pipe detail" not in err
    if not as_json:
        assert RESULT.stderr in err


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX pipe")
def test_cleanup_exit_survives_cpython_shutdown_flush_on_closed_pipe(monkeypatch):
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    script = """
from types import SimpleNamespace
import anycompute
from anycompute.cli import main
from anycompute.contract import CleanupError, Result

def fake_run(*args, **kwargs):
    raise CleanupError('still running', result=Result('workload output', '', 0, {}, [],
                       'fake', 'sb', 'test-run', 60, None, None, False))

anycompute.engine = SimpleNamespace(run=fake_run, _resolve_run_id=lambda: 'test-run', _redact_text=lambda text: text)
raise SystemExit(main(['run', '--provider', 'fake', '--', 'job']))
"""
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "wb") as output:
        proc = subprocess.run([sys.executable, "-c", script], stdout=output,
                              stderr=subprocess.PIPE, text=True, timeout=5)
    assert proc.returncode == 125, proc.stderr
    assert "CleanupError: still running; Cannot write workload stdout:" in proc.stderr


def test_error_diagnostic_has_ascii_fallback_without_losing_json(cli, engine, capsys, monkeypatch):
    class AsciiStderr(io.StringIO):
        def write(self, text):
            text.encode("ascii")
            return super().write(text)

    def fake_run(*args, **kwargs):
        raise RateLimitError("slow down ☃")

    engine.run = fake_run
    stream = AsciiStderr()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", stream)
        assert cli.main(["run", "--json", "--provider", "fake", "--", "job"]) == 121
    assert "RateLimitError: slow down \\u2603" in stream.getvalue()
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "slow down ☃"


@pytest.mark.parametrize("cleanup_failed", [False, True])
def test_redacted_readback_key_collisions_do_not_report_ambiguous_files(cli, engine, capsys, tmp_path, cleanup_failed):
    first, second = "synthetic-first", "synthetic-second"
    root = tmp_path / first
    files = {first: b"first", second: b"other", "kept": b"\x00\xff"}

    def fake_run(*args, **kwargs):
        result = replace(RESULT, files=files, stdout=first, stderr=second, missing=[f"sub/{second}"],
                         runtime={"python": f"{first} version", "arch": "arch", "api_host": "host"},
                         sandbox_seconds=None if cleanup_failed else RESULT.sandbox_seconds)
        if cleanup_failed:
            raise CleanupError(f"not gone: {first}", result=result)
        return result

    engine._redact_text = lambda text: text.replace(first, "***").replace(second, "***")
    engine.run = fake_run
    flags = [value for path in files for value in ("--read", path)]
    assert cli.main(["run", "--json", "--provider", "fake", "--out", str(root), *flags, "--", "job"]) == (
        125 if cleanup_failed else 126
    )
    out = capsys.readouterr()
    assert first not in out.out + out.err and second not in out.out + out.err
    for path, data in files.items():
        assert (root / "test-run" / path).read_bytes() == data
    report = json.loads(out.out)
    assert report["error"]["type"] == ("CleanupError" if cleanup_failed else "ConfigError")
    assert "after redaction" in report["error"]["message"]
    assert (report["stdout"], report["stderr"], report["missing"]) == ("***", "***", ["sub/***"])
    assert report["runtime"]["python"] == "*** version"
    assert report["files"] == {"kept": {
        "size": 2, "sha256": sha256(files["kept"]).hexdigest(),
        "saved_path": str(tmp_path / "***" / "test-run" / "kept"),
    }}


def test_rate_limit_uses_error_to_distinguish_same_workload_exit(cli, engine, capsys, monkeypatch):
    source = "https://example.com/limits"
    caps = ProviderCaps(creates_per_minute=StartRate(12, source, "2026-11-23"))
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", caps, lambda: None),))

    def fake_run(*args, **kwargs):
        raise RateLimitError("slow down", retry_after=2.5)

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 121
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report == {
        **dict.fromkeys(("stdout", "stderr", "exit_code", "files", "missing", "sandbox_id",
                         "effective_ttl", "runtime", "sandbox_seconds", "outside_contract")),
        "provider": "fake", "run_id": "test-run",
        "error": {**dict.fromkeys(ERROR_KEYS), "type": "RateLimitError", "message": "slow down",
                  "retry_after": 2.5},
    }
    assert "retry_after=2.5" in out.err and "12" in out.err and source in out.err


@pytest.mark.parametrize("retry_after", [
    float("nan"), float("inf"), float("-inf"), "later", True, -1, pytest.param(10**500, id="too-large"),
])
def test_rate_limit_invalid_retry_after_is_nullable_in_strict_json(cli, engine, capsys, retry_after):
    def fake_run(*args, **kwargs):
        raise RateLimitError("slow down", retry_after=retry_after)

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 121
    out = capsys.readouterr()
    report = json.loads(out.out, parse_constant=lambda value: pytest.fail(f"invalid JSON number: {value}"))
    assert report["error"]["type"] == "RateLimitError" and report["error"]["retry_after"] is None
    assert "retry_after=" not in out.err


def test_timeout_omits_nonfinite_sandbox_seconds_without_losing_error(cli, engine, capsys):
    def fake_run(*args, **kwargs):
        raise SandboxTimeoutError("too slow", sandbox_seconds=float("nan"))

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 123
    report = json.loads(capsys.readouterr().out, parse_constant=lambda value: pytest.fail(f"invalid JSON number: {value}"))
    assert report["sandbox_seconds"] is None and report["error"]["type"] == "SandboxTimeoutError"


def test_timeout_reports_partial_output_and_its_own_cleanup_failure(cli, engine, capsys):
    def fake_run(*args, **kwargs):
        error = SandboxTimeoutError("too slow", stdout="partial out", stderr="partial err",
                                    sandbox_id="sb", run_id="test-run", sandbox_seconds=121.0)
        error.cleanup_error = CleanupError("still running")
        raise error

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 123
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report["stdout"] == "partial out" and report["stderr"] == "partial err"
    assert (report["sandbox_id"], report["run_id"], report["sandbox_seconds"]) == ("sb", "test-run", 121)
    assert report["exit_code"] is None
    assert report["error"]["cleanup_error"] == {"type": "CleanupError", "message": "still running"}
    assert report["error"]["gc_command"] in out.err


@pytest.mark.parametrize("kind", ["confirmed", "cleanup failed", "cleanup interrupted", "lost create"])
def test_interrupt_reports_context_and_exit_130(cli, engine, capsys, kind):
    def fake_run(*args, **kwargs):
        if kind == "lost create":
            raise ProviderError("interrupted", sandbox_may_exist=True, interrupted=True,
                                run_id="test-run")
        error = KeyboardInterrupt()
        setattr(error, "sandbox_id", "sb")
        setattr(error, "run_id", "test-run")
        if kind == "cleanup failed":
            setattr(error, "cleanup_error", CleanupError("not gone"))
        if kind == "confirmed":
            setattr(error, "sandbox_seconds", 0.0)
        raise error

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 130
    out = capsys.readouterr()
    report = json.loads(out.out)
    error = report["error"]
    assert error["type"] == ("ProviderError" if kind == "lost create" else "KeyboardInterrupt")
    assert error["run_id"] == "test-run"
    assert report["sandbox_id"] == (None if kind == "lost create" else "sb")
    if kind == "confirmed":
        assert error["gc_command"] is None
    else:
        assert error["gc_command"] == "anycompute gc --run test-run --provider fake --dry-run"
        assert error["gc_command"] in out.err


def test_unexpected_exception_is_redacted_before_json_serialization(cli, engine, capsys):
    synthetic = 'fake-"key"-not-real'
    engine._redact_text = lambda text: text.replace(synthetic, "***")

    def fake_run(*args, **kwargs):
        raise TypeError(f"failed with {synthetic}\nextra detail")

    engine.run = fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 124
    out = capsys.readouterr()
    error = json.loads(out.out)["error"]
    assert error["type"] == "ProviderError" and error["internal"] is True
    assert error["message"] == "TypeError: failed with ***\nextra detail"
    assert out.err.splitlines()[-1] == "ProviderError: TypeError: failed with *** extra detail"


def test_redaction_keeps_error_types_but_masks_messages(cli, engine, capsys):
    def redact(text):
        for value in ("ConfigError", "CleanupError", "--dry-run"):
            text = text.replace(value, "***")
        return text

    def fake_run(*args, **kwargs):
        error = ConfigError("ConfigError detail")
        error.cleanup_error = CleanupError("CleanupError detail")
        raise error

    engine._redact_text, engine.run = redact, fake_run
    assert cli.main(["run", "--provider", "fake", "--json", "--", "job"]) == 126
    out = capsys.readouterr()
    error = json.loads(out.out)["error"]
    assert (error["type"], error["message"]) == ("ConfigError", "*** detail")
    assert error["cleanup_error"] == {"type": "CleanupError", "message": "*** detail"}
    assert error["gc_command"] == "anycompute gc --run test-run --provider fake --dry-run"
    assert error["gc_command"] in out.err and "ConfigError: *** detail" in out.err


@pytest.mark.parametrize(("provider", "target", "masked"), [
    ("fake", "hidden-run", "hidden-run"), ("hidden-provider", "test-run", "hidden-provider"),
    ("fake", "hidden-***", "unrelated"),
])
def test_redacted_gc_target_suppresses_copyable_command(cli, engine, capsys, provider, target, masked):
    def fake_run(*args, **kwargs):
        raise CleanupError(f"not gone: {masked}", run_id=target)

    engine._redact_text, engine.run = lambda text: text.replace(masked, "***"), fake_run
    assert cli.main(["run", "--provider", provider, "--json", "--", "job"]) == 125
    out = capsys.readouterr()
    error = json.loads(out.out)["error"]
    assert error["type"] == "CleanupError" and error["gc_command"] is None
    assert "gc command unavailable" in out.err and "anycompute gc --run" not in out.err
    assert masked not in out.out + out.err


def test_run_usage_error_preserves_known_context(cli, engine, capsys):
    assert cli.main(["run", "--provider", "fake", "--json", "--ttl", "abc", "--", "job"]) == 126
    report = json.loads(capsys.readouterr().out)
    assert set(report) == set(RESULT.__dataclass_fields__) | {"error"}
    assert report["run_id"] == "test-run" and report["provider"] == "fake"
    assert report["stdout"] is None and report["files"] is None


def test_gc_usage_error_keeps_report_shape(cli, engine, capsys):
    assert cli.main(["gc", "--provider", "fake", "--json", "--dry-run", "--bogus"]) == 126
    report = json.loads(capsys.readouterr().out)
    assert report == {"provider": "fake", "run_id": None, "dry_run": True, "sandboxes": [],
                      "error": report["error"]}


def test_gc_serializes_rows_and_preserves_partial_failure(cli, engine, capsys):
    rows = [
        {"sandbox_id": "one", "run_id": "test-run", "created_at": "1970-01-01T00:00:00+00:00",
         "action": "failed", "reason": "run_match"},
        {"sandbox_id": "two", "run_id": "test-run", "created_at": None,
         "action": "deleted", "reason": "run_match"},
    ]

    def fake_gc(provider, **kwargs):
        assert provider == "fake"
        assert kwargs == {"run_id": "test-run", "all_expired": False, "dry_run": False, "force": False}
        return rows, CleanupError("not gone", run_id="test-run", sandbox_id="one")

    engine._gc = fake_gc
    assert cli.main(["gc", "--json", "--provider", "fake", "--run", "test-run"]) == 125
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report == {
        "provider": "fake", "run_id": "test-run", "dry_run": False,
        "sandboxes": rows,
        "error": {**dict.fromkeys(ERROR_KEYS), "type": "CleanupError", "message": "not gone",
                  "run_id": "test-run", "sandbox_id": "one",
                  "gc_command": "anycompute gc --run test-run --provider fake --dry-run"},
    }
    assert report["error"]["gc_command"] in out.err


def test_gc_all_cleanup_hint_uses_first_failed_rows_run_id(cli, engine, capsys):
    rows = [{"sandbox_id": "one", "run_id": "failed-run", "created_at": None,
             "action": "failed", "reason": "force"}]
    engine._gc = lambda *args, **kwargs: (rows, CleanupError("still running", sandbox_id="one"))
    assert cli.main(["gc", "--json", "--provider", "fake", "--all", "--force"]) == 125
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report["run_id"] is None
    hint = "anycompute gc --run failed-run --provider fake --dry-run"
    assert report["error"]["gc_command"] == hint and hint in out.err


@pytest.mark.parametrize("failure", ["write", "flush"])
@pytest.mark.parametrize("cleanup_failed", [True, False])
def test_plain_gc_reports_stdout_failure_without_losing_cleanup_error(
    cli, engine, capsys, monkeypatch, cleanup_failed, failure,
):
    row = {"sandbox_id": "one", "run_id": "test-run", "created_at": None,
           "action": "failed" if cleanup_failed else "deleted", "reason": "run_match"}
    error = CleanupError("not gone", sandbox_id="one", run_id="test-run") if cleanup_failed else None
    engine._gc = lambda *args, **kwargs: ([row], error)

    def unavailable(*args):
        raise BrokenPipeError("output unavailable")

    stream = io.StringIO()
    monkeypatch.setattr(stream, failure, unavailable)
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "stdout", stream)
        exit_code = cli.main(["gc", "--provider", "fake", "--run", "test-run"])

    message = capsys.readouterr().err
    hint = "anycompute gc --run test-run --provider fake --dry-run"
    assert exit_code == (CleanupError.exit_code if cleanup_failed else ConfigError.exit_code)
    assert "output unavailable" in message
    assert (hint in message) is cleanup_failed
    if cleanup_failed:
        assert "CleanupError: not gone" in message


def test_gc_all_failure_without_target_keeps_null_ids_and_explains_recovery(cli, engine, capsys):
    rows = [{"sandbox_id": "one", "run_id": None, "created_at": None,
             "action": "failed", "reason": "force"}]
    engine._gc = lambda *args, **kwargs: (rows, CleanupError("still running", sandbox_id="one"))
    assert cli.main(["gc", "--json", "--provider", "fake", "--all", "--force"]) == 125
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert report["run_id"] is None and report["sandboxes"] == rows
    assert report["error"]["run_id"] is None and report["error"]["gc_command"] is None
    assert "gc command unavailable" in out.err


# Generated by /ship coverage audit
# Value: protects=The printed dry-run recovery command can be copied for any valid run ID;
#   fails_when=A leading hyphen is parsed as a flag instead of part of the run ID;
#   why_new=Other recovery tests compare text for IDs that do not resemble flags; seam=none;
#   no_claim=The engine is scripted, so this only proves CLI argument round-tripping.
def test_gc_recovery_hint_round_trips_leading_hyphen_run_id(cli, engine, capsys):
    def fake_gc(provider, *, run_id, all_expired, dry_run, force):
        assert provider == "fake"
        if all_expired:
            return [{"sandbox_id": "one", "run_id": "-retry", "created_at": None,
                     "action": "failed", "reason": "force"}], CleanupError("still running", sandbox_id="one")
        assert (run_id, dry_run, force) == ("-retry", True, False)
        return [], None

    engine._gc = fake_gc
    assert cli.main(["gc", "--json", "--provider", "fake", "--all", "--force"]) == 125
    hint = json.loads(capsys.readouterr().out)["error"]["gc_command"]
    assert cli.main(shlex.split(hint)[1:]) == 0, capsys.readouterr().err


@pytest.mark.parametrize("as_json", [True, False])
def test_gc_all_dry_run_shows_listed_rows(cli, engine, capsys, as_json):
    row = {"sandbox_id": "one", "run_id": "test-run", "created_at": "2026-11-23T00:00:00Z",
           "action": "listed", "reason": "force"}

    def fake_gc(provider, **kwargs):
        assert provider == "fake"
        assert kwargs == {"run_id": None, "all_expired": True, "dry_run": True, "force": True}
        return [row], None

    engine._gc = fake_gc
    flags = ["--json"] if as_json else []
    assert cli.main(["gc", *flags, "--all", "--provider", "fake", "--dry-run", "--force"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    if as_json:
        assert json.loads(out.out) == {"provider": "fake", "run_id": None, "dry_run": True,
                                       "sandboxes": [row], "error": None}
    else:
        assert "listed" in out.out and "one" in out.out and "force" in out.out


@pytest.mark.parametrize("as_json", [True, False])
def test_gc_redacts_ids_but_not_fixed_actions_or_reasons(cli, capsys, monkeypatch, as_json):
    from anycompute import engine

    clock = FakeClock()
    caps = replace(FAKE_CAPS, key_sources=KeySources(env=("ANYCOMPUTE_FAKE_KEY", "ANYCOMPUTE_FAKE_EXTRA")))
    fake = FakeProvider(clock=clock, caps=caps, sandboxes=[{
        "sandbox_id": "listed", "labels": {"anycompute": "1", "anycompute-run": "force"},
    }])
    monkeypatch.setattr(engine, "_clock", clock)
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", caps, lambda: fake),))
    monkeypatch.setenv("ANYCOMPUTE_FAKE_KEY", "listed")
    monkeypatch.setenv("ANYCOMPUTE_FAKE_EXTRA", "force")
    flags = ["--json"] if as_json else []
    assert cli.main(["gc", *flags, "--all", "--provider", "fake", "--dry-run", "--force"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    if as_json:
        row = json.loads(out.out)["sandboxes"][0]
        assert (row["action"], row["reason"], row["sandbox_id"], row["run_id"]) == ("listed", "force", "***", "***")
    else:
        assert out.out == "listed: *** (force)\n"


@pytest.mark.parametrize("as_json", [True, False])
def test_providers_shows_key_status_and_caps_without_loading_sdk(cli, engine, capsys, monkeypatch, as_json):
    def unused():
        pytest.fail("listing capabilities must not construct the adapter")

    caps = ProviderCaps(
        ttl_unit_s=60, labels_at_create=False, isolation="fake VM", live_run=False,
        provider_options_allowlist=frozenset({"image"}),
        key_sources=KeySources(env=("ANYCOMPUTE_FAKE_KEY", "ANYCOMPUTE_FAKE_ABSENT")),
    )
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", caps, unused),))
    synthetic = "synthetic-fake-only"
    monkeypatch.setenv("ANYCOMPUTE_FAKE_KEY", synthetic)
    monkeypatch.delenv("ANYCOMPUTE_FAKE_ABSENT", raising=False)
    flags = ["--json"] if as_json else []
    assert cli.main(["providers", *flags]) == 0
    out = capsys.readouterr()
    assert out.err == "" and synthetic not in out.out
    if as_json:
        assert json.loads(out.out) == {"providers": [{**providers.capability_table()[0], "keys": {
            "ANYCOMPUTE_FAKE_KEY": "set", "ANYCOMPUTE_FAKE_ABSENT": "unset",
        }}], "error": None}
    else:
        assert "fake VM" in out.out and "partial" in out.out
        assert "ANYCOMPUTE_FAKE_KEY=set" in out.out and "ANYCOMPUTE_FAKE_ABSENT=unset" in out.out


@pytest.mark.parametrize("as_json", [True, False])
def test_providers_preserves_fixed_capabilities_status_and_masks_text(cli, engine, capsys, monkeypatch, as_json):
    def redact(text):
        for value in ("unset", "set", "partial"):
            text = text.replace(value, "***")
        return text

    caps = ProviderCaps(lifetime="partial", labels_at_create=False, sdk_package="partial toolkit",
                        key_sources=KeySources(env=("ANYCOMPUTE_FAKE_PRESENT", "ANYCOMPUTE_FAKE_ABSENT")))
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", caps, lambda: None),))
    monkeypatch.setenv("ANYCOMPUTE_FAKE_PRESENT", "set")
    monkeypatch.delenv("ANYCOMPUTE_FAKE_ABSENT", raising=False)
    engine._redact_text = redact
    flags = ["--json"] if as_json else []
    assert cli.main(["providers", *flags]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    if as_json:
        row = json.loads(out.out)["providers"][0]
        assert row["lifetime"] == row["labels_at_create"] == "partial"
        assert row["sdk_package"] == "*** toolkit"
        assert row["keys"] == {"ANYCOMPUTE_FAKE_PRESENT": "set", "ANYCOMPUTE_FAKE_ABSENT": "unset"}
    else:
        assert "lifetime: partial" in out.out and "labels_at_create: partial" in out.out
        assert "sdk_package: *** toolkit" in out.out
        assert "ANYCOMPUTE_FAKE_PRESENT=set" in out.out and "ANYCOMPUTE_FAKE_ABSENT=unset" in out.out


def test_providers_checks_config_presence_without_disclosing_values(cli, capsys, monkeypatch, tmp_path):
    synthetic = "synthetic-config-value"
    config = tmp_path / "override.toml"
    config.write_text(f'[default]\nother = true\n[secondary]\ntoken = "{synthetic}"\n')
    monkeypatch.setenv("ANYCOMPUTE_FAKE_CONFIG", str(config))
    sources = KeySources(config=(ConfigKeys(
        "~/.fake.toml", (("*", "token"), ("*", "absent")), path_env="ANYCOMPUTE_FAKE_CONFIG",
    ),))

    def unused():
        pytest.fail("listing key availability must not construct the adapter")

    caps = ProviderCaps(key_sources=sources)
    monkeypatch.setattr(providers, "PROVIDERS", (ProviderSpec("fake", caps, unused),))
    assert cli.main(["providers", "--json"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert synthetic not in out.out and str(config) not in out.out and "secondary" not in out.out
    assert json.loads(out.out)["providers"][0]["keys"] == {
        "~/.fake.toml:*.token": "set", "~/.fake.toml:*.absent": "unset",
    }


def test_verbose_uses_anycompute_logger_on_stderr_only(cli, engine, capsys):
    def fake_run(*args, **kwargs):
        logging.getLogger("anycompute").debug("sandbox step")
        return RESULT

    engine.run = fake_run
    args = ["run", "--provider", "fake", "--json", "--", "job"]
    assert cli.main(args) == 121
    assert capsys.readouterr().err == "run_id: test-run\n"
    assert cli.main(["--verbose", *args]) == 121
    out = capsys.readouterr()
    assert "sandbox step" in out.err
    assert json.loads(out.out)["stdout"] == RESULT.stdout
    assert cli.main(args) == 121
    assert capsys.readouterr().err == "run_id: test-run\n"

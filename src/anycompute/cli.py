"""Command-line interface for anycompute."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shlex
import stat
import sys
import tomllib
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import fields, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from . import providers
from .contract import (
    AnycomputeError, CleanupError, ConfigError, InvalidPathError, InvalidTTLError, KeySources, ProviderError,
    RateLimitError, Result, SandboxTimeoutError,
)


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def parse_known_args(self, args=None, namespace=None):
        self.parsed = namespace if namespace is not None else argparse.Namespace()
        return super().parse_known_args(args, self.parsed)

    def error(self, message):
        error = ConfigError(message)
        setattr(error, "parsed", self.parsed)
        raise error


def _output_flags(parser):
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)


def _parser():
    parser = _Parser(prog="anycompute")
    _output_flags(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="run an argument list in a sandbox",
        epilog="Sandbox exit codes can collide with anycompute errors. "
               "Scripts should inspect the error field of --json, not the exit code.",
    )
    run.add_argument("--provider", required=True)
    run.add_argument("--ttl", type=int, default=argparse.SUPPRESS, metavar="SECONDS")
    run.add_argument("--file", action="append", default=[], metavar="PATH")
    run.add_argument("--read", action="append", default=[], metavar="PATH")
    run.add_argument("--out", metavar="DIR")
    run.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- COMMAND [ARG ...]")

    verify = commands.add_parser("verify", help="compare one task across providers")
    verify.add_argument("task", metavar="TASK.TOML")
    verify.add_argument("--providers", required=True, metavar="NAME,NAME[,NAME...]")
    verify.add_argument("--lifecycle", action="store_true", help="check provider lifetime after killing the client")

    gc = commands.add_parser("gc", help="list or delete labeled sandboxes")
    gc.add_argument("--provider", required=True)
    target = gc.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", dest="run_id", metavar="ID")
    target.add_argument("--all", dest="all_expired", action="store_true")
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--force", action="store_true")

    providers = commands.add_parser("providers", help="list capabilities and key status")
    for name, command in (("run", run), ("verify", verify), ("gc", gc), ("providers", providers)):
        command.set_defaults(command=name)
        _output_flags(command)
    return parser


def _parse(argv):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.argv[:1] == ["--"]:
            args.argv = args.argv[1:]
        if not args.argv:
            parser.error("run requires a command")
    return args


def _redact(text):
    from . import engine

    return engine._redact_text(text)


def _redact_values(value):
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_values(item) for key, item in value.items()}
    return value


def _write(stream, text):
    if stream is None:
        raise OSError("stream is unavailable")
    try:
        stream.write(text)
        stream.flush()
    except OSError:
        # A second flush at interpreter shutdown can overwrite the CLI's exit code.
        if stream is sys.__stdout__ or stream is sys.__stderr__:
            with suppress(OSError):
                stream.close()
        raise


def _print_report(report):
    prepared = ("error", "files", "rows", "sandboxes", "providers", "summary")
    rendered = {key: value if key in prepared else _redact_values(value) for key, value in report.items()}
    document = json.dumps(rendered, allow_nan=False)
    try:
        _write(sys.stdout, document + "\n")
    except (OSError, ValueError) as error:
        return f"Cannot write JSON to stdout: {error}"
    return None


def _error_details(error, provider, run_id, *, redact=_redact):
    cleanup = getattr(error, "cleanup_error", None)
    error_run_id = getattr(error, "run_id", None)
    sandbox_id = getattr(error, "sandbox_id", None)
    sandbox_may_exist = getattr(error, "sandbox_may_exist", None)
    retry_after = getattr(error, "retry_after", None)
    if type(retry_after) not in (int, float) or not 0 <= retry_after <= sys.float_info.max:
        retry_after = None
    unconfirmed_interrupt = (
        isinstance(error, KeyboardInterrupt) and sandbox_id is not None
        and getattr(error, "sandbox_seconds", None) is None
    )
    target = error_run_id or run_id
    gc_command = None
    needs_gc = bool(isinstance(error, CleanupError) or sandbox_may_exist or cleanup or unconfirmed_interrupt)
    if needs_gc and all(isinstance(value, str) and value and "***" not in value and redact(value) == value
                        for value in (provider, target)):
        separator = "=" if target.startswith("-") else " "
        gc_command = f"anycompute gc --run{separator}{shlex.quote(target)} --provider {shlex.quote(provider)} --dry-run"
    return {
        "type": type(error).__name__, "message": redact(str(error)) or type(error).__name__,
        "sandbox_id": redact(sandbox_id) if isinstance(sandbox_id, str) else sandbox_id,
        "run_id": redact(error_run_id) if isinstance(error_run_id, str) else error_run_id,
        "retry_after": retry_after, "sandbox_may_exist": sandbox_may_exist,
        "internal": getattr(error, "internal", None), "gc_command": gc_command,
        "cleanup_error": {"type": type(cleanup).__name__, "message": redact(str(cleanup))} if cleanup else None,
    }, needs_gc


def _report_error(error, report, as_json, *, redact=_redact):
    failures = []
    if "stdout" in report:
        for key in ("sandbox_id", "run_id", "sandbox_seconds"):
            value = getattr(error, key, None)
            if value is not None and report[key] is None:
                report[key] = value
        if isinstance(error, SandboxTimeoutError):
            report.update(stdout=error.stdout, stderr=error.stderr)
            if not as_json:
                failures = _print_workload(error.stdout, error.stderr)
    provider = report.get("provider")
    details, needs_gc = _error_details(error, provider, report.get("run_id"), redact=redact)
    message = f"{details['type']}: {details['message']}"
    if isinstance(error, RateLimitError):
        if details["retry_after"] is not None:
            message += f"; retry_after={details['retry_after']:g}s"
        if provider:
            with suppress(ConfigError):
                rate = providers.get_provider(provider).caps.creates_per_minute
                if rate is not None:
                    message += f"; documented start rate: {rate.value:g}/min ({redact(rate.source)})"
    if details["cleanup_error"]:
        message += f"; cleanup: {details['cleanup_error']['message']}"
    if details["gc_command"]:
        message += f"; {details['gc_command']} (dropping --dry-run deletes every sandbox of that run, running or not)"
    elif needs_gc:
        message += "; gc command unavailable (provider or run ID redacted or missing); use the original values for a dry run"
    if as_json and (failure := _print_report({**report, "error": details})):
        failures.append(failure)
    if failures:
        message += "; " + redact("; ".join(failures))
    line = " ".join(message.splitlines()) + "\n"
    if not as_json and report.get("stderr") and not report["stderr"].endswith("\n"):
        line = "\n" + line
    with suppress(OSError, ValueError):
        try:
            _write(sys.stderr, line)
        except UnicodeEncodeError:
            _write(sys.stderr, line.encode("ascii", "backslashreplace").decode("ascii"))
    if isinstance(error, KeyboardInterrupt) or isinstance(error, ProviderError) and error.interrupted:
        return 130
    return error.exit_code


def _local_files(paths):
    files = {}
    for path in map(Path, paths):
        if path.name in files:
            raise InvalidPathError(f"Duplicate input base name: {path.name!r}")
        try:
            if not path.is_file():
                raise InvalidPathError(f"Not a regular input file: {str(path)!r}")
            files[path.name] = path.read_bytes()
        except OSError as error:
            raise InvalidPathError(f"Cannot read input file {str(path)!r}: {error}") from None
    return files


@contextmanager
def _directory(path, parent=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, dir_fd=parent)
    except FileNotFoundError:
        if parent is None:
            raise
        with suppress(FileExistsError):
            os.mkdir(path, dir_fd=parent)
        fd = os.open(path, flags, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _output_directory(root, run_id, stack):
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or not {os.open, os.mkdir, os.stat, os.rename, os.unlink} <= os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks):
        raise OSError("safe readback writes require POSIX directory file descriptors")
    directory = stack.enter_context(_directory(root.anchor or "."))
    parts = root.parts[1:] if root.anchor else root.parts
    if sys.platform == "darwin" and root.anchor and parts[:1] in (("tmp",), ("var",)):
        # Translate only Darwin's fixed root aliases; don't follow the symlink.
        canonical = f"private/{parts[0]}"
        with suppress(OSError):
            if os.readlink(parts[0], dir_fd=directory) in (canonical, f"/{canonical}"):
                parts = ("private", *parts)
    for part in (*parts, run_id):
        directory = stack.enter_context(_directory(part, directory))
    return directory


@contextmanager
def _temporary_file(parent):
    name = f".anycompute-{uuid4().hex}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            yield name, stream
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)


def _save_file(output, directory, path, data, published):
    from . import engine

    engine._validate_path(path)
    path = Path(path)
    with ExitStack() as stack:
        for part in path.parts[:-1]:
            directory = stack.enter_context(_directory(part, directory))
        with suppress(FileNotFoundError):
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise OSError("destination is not a regular file with a single link")
            if (existing.st_dev, existing.st_ino) in published:
                raise OSError("destination already used by another readback path")
        with _temporary_file(directory) as (name, stream):
            stream.write(data)
            saved = os.fstat(stream.fileno())
            stream.close()
            os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            published.add((saved.st_dev, saved.st_ino))
    return {"size": len(data), "sha256": sha256(data).hexdigest(), "saved_path": str(output / path)}


def _initial_report(args) -> dict[str, object]:
    provider = getattr(args, "provider", None)
    if args.command == "run":
        return {**dict.fromkeys(field.name for field in fields(Result)), "provider": provider}
    if args.command == "verify":
        from .verify import _initial_report as initial_verify_report

        return initial_verify_report()
    if args.command == "gc":
        return {"provider": provider, "run_id": args.run_id, "dry_run": args.dry_run, "sandboxes": []}
    return {"providers": []} if args.command == "providers" else {}


def _print_workload(stdout, stderr):
    failures = []
    for name, stream, text in (("stdout", sys.stdout, stdout), ("stderr", sys.stderr, stderr)):
        if text:
            redacted = _redact(text)
            try:
                _write(stream, redacted)
            except (OSError, ValueError) as error:
                failures.append(f"Cannot write workload {name}: {error}")
    return failures


def _publish_result(result, report, output, directory, as_json):
    report.update({field.name: getattr(result, field.name) for field in fields(Result)})
    report["files"] = {}
    failures = [] if as_json else _print_workload(result.stdout, result.stderr)
    published = set()
    for path, data in result.files.items():
        try:
            report["files"][path] = _save_file(output, directory, path, data, published)
        except OSError as error:
            failures.append(f"Cannot save {str(output / path)!r}: {error}")
    if as_json:
        masked = {}
        for path, info in report["files"].items():
            key = _redact(path)
            masked[key] = None if key in masked else {**info, "saved_path": _redact(info["saved_path"])}
        if None in masked.values():
            failures.append("Cannot report all saved files: paths are indistinguishable after redaction")
        report["files"] = {key: info for key, info in masked.items() if info is not None}
    if failures:
        raise ConfigError("; ".join(failures), sandbox_id=result.sandbox_id,
                          run_id=result.run_id, sandbox_seconds=result.sandbox_seconds)


def _run(args, report, as_json):
    from . import engine

    run_id = report["run_id"] = engine._resolve_run_id()
    print(f"run_id: {_redact(run_id)}", file=sys.stderr, flush=True)
    files = _local_files(args.file)
    kwargs = {"ttl": args.ttl} if hasattr(args, "ttl") else {}
    with ExitStack() as stack:
        output = directory = None
        if args.read or args.out is not None:
            root = Path(args.out or "anycompute-out")
            output = root / run_id
            try:
                directory = _output_directory(root, run_id, stack)
                with _temporary_file(directory):
                    pass
            except (OSError, ValueError, NotImplementedError) as error:
                raise ConfigError(f"Output directory {str(output)!r} is not writable: {error}") from None
        try:
            result = engine.run(args.argv, provider=args.provider, files=files, read=args.read,
                                run_id=run_id, **kwargs)
        except CleanupError as error:
            if error.result is not None:
                try:
                    _publish_result(error.result, report, output, directory, as_json)
                except AnycomputeError as save_error:
                    error.args = (f"{error}; {save_error}",)
            raise
        _publish_result(result, report, output, directory, as_json)
    return result.exit_code


def _task_strings(table, key, default):
    values = table.get(key, default)
    if not isinstance(values, list) or any(not isinstance(value, str) or "\0" in value for value in values):
        raise ConfigError(f"task.toml {key} must be an array of strings")
    return values


def _task_options(table):
    options = table.get("providers", {})
    if not isinstance(options, dict):
        raise ConfigError("task.toml providers must be a table")
    for name, entry in options.items():
        if not isinstance(entry, dict):
            raise ConfigError("Each task.toml provider entry must be a table")
        if "environment" in entry:
            variables = _task_strings(entry, "environment", [])
            environment = {}
            for variable in variables:
                if not variable or "=" in variable or variable not in os.environ:
                    raise ConfigError(f"Missing environment variable {variable!r} for provider {name!r}")
                environment[variable] = os.environ[variable]
            entry["environment"] = environment
    return options


def _load_task(path, run_id):
    from . import engine, verify

    try:
        with path.open("rb") as file:
            table = tomllib.load(file)
    except (OSError, ValueError) as error:
        raise ConfigError(f"Cannot read task file {str(path)!r}: {error}") from None
    if any(key not in ("command", "inputs", "read", "ttl", "exit_code", "compare", "providers") for key in table):
        raise ConfigError("task.toml contains an unknown setting")
    command = _task_strings(table, "command", None)
    read = _task_strings(table, "read", [])
    inputs = _task_strings(table, "inputs", [])
    if any(not value or Path(value).is_absolute() for value in inputs):
        raise InvalidPathError("task.toml inputs must be paths relative to the task file")
    ttl, exit_code, compare = table.get("ttl", 300), table.get("exit_code", 0), table.get("compare", "all")
    if type(ttl) is not int or ttl < 1:
        raise InvalidTTLError("task.toml ttl must be an integer of at least 1 second")
    if type(exit_code) is not int or compare not in ("all", "exit_code"):
        raise ConfigError('task.toml exit_code must be an integer and compare must be "all" or "exit_code"')
    options = _task_options(table)
    ctx, _ = engine._provider_context("verify", run_id, options)
    try:
        files = _local_files([path.parent / value for value in inputs])
    except InvalidPathError as error:
        raise InvalidPathError(ctx.redact(str(error))) from None
    return verify._Task(command, files, read, ttl, exit_code, compare, options), ctx


def _verify(args, report, as_json):
    from . import engine, verify

    names = [name.strip() for name in args.providers.split(",")]
    if len(names) < 2 or "" in names or len(names) != len(set(names)):
        raise ConfigError("--providers needs at least two distinct provider names")
    specs = [providers.get_provider(name) for name in names]
    run_id = engine._resolve_run_id()
    task, ctx = _load_task(Path(args.task), run_id)
    print(f"run_id: {ctx.redact(run_id)}", file=sys.stderr, flush=True)
    outcomes, summary, code = verify._run(task, specs, ctx, check_lifecycle=args.lifecycle)
    report.update(rows=[outcome.row for outcome in outcomes], summary=summary)
    first_error_code = None
    for spec, outcome in zip(specs, outcomes):
        if (error := outcome.error) is None:
            continue
        row = outcome.row
        row["error"], _ = _error_details(error, spec.name, run_id, redact=ctx.redact)
        error_code = _report_error(error, {"provider": spec.name, "run_id": run_id}, False, redact=ctx.redact)
        if first_error_code is None or error_code == 130:
            report["error"] = row["error"]
            first_error_code = error_code
    if not as_json:
        try:
            for row in report["rows"]:
                keys = ("exit_code", "output", "files", "cleanup", "lifecycle", "runtime", "partial_caps")
                checks = ", ".join(f"{key}={row[key]}" for key in keys if row[key] not in (None, []))
                _write(sys.stdout, f"{row['provider']}: {checks}\n")
                if row["details"]:
                    _write(sys.stdout, f"  {json.dumps(row['details'], allow_nan=False)}\n")
            _write(sys.stdout, summary + "\n")
        except (OSError, ValueError) as error:
            failure = ConfigError(ctx.redact(f"Cannot write verify report to stdout: {error}"))
            if first_error_code is None:
                raise failure from None
            _report_error(failure, {}, False, redact=ctx.redact)
    return first_error_code if first_error_code is not None else code


def _gc(args, report, as_json):
    from . import engine

    rows, error = engine._gc(args.provider, run_id=args.run_id, all_expired=args.all_expired,
                             dry_run=args.dry_run, force=args.force)
    report["sandboxes"] = [
        {**row, **{key: _redact_values(row[key]) for key in ("sandbox_id", "run_id", "created_at")}}
        for row in rows
    ]
    if not as_json:
        try:
            for row in report["sandboxes"]:
                _write(sys.stdout, f"{row['action']}: {row['sandbox_id']} ({row['reason']})\n")
        except (OSError, ValueError) as write_error:
            message = f"Cannot write sandbox rows to stdout: {write_error}"
            if error is None:
                raise ConfigError(message) from None
            error.args = (f"{error}; {message}",)
    if error is not None:
        if args.all_expired and error.run_id is None:
            failed_run_id = next((row["run_id"] for row in rows if row["action"] == "failed"), None)
            if failed_run_id is not None:
                with suppress(ConfigError):
                    error.run_id = engine._resolve_run_id(failed_run_id)
        raise error
    return 0


def _key_status(sources):
    from . import engine

    status = {name: "set" if os.environ.get(name) else "unset" for name in sources.env}
    for source in sources.config:
        for field in source.fields:
            label = f"{source.path}:{'.'.join(field)}"
            single = KeySources(config=(replace(source, fields=(field,)),))
            status[label] = "set" if engine._key_values(single) else "unset"
    return status


def _list_providers(report, as_json):
    for row, spec in zip(providers.capability_table(), providers.PROVIDERS, strict=True):
        keys = _key_status(spec.caps.key_sources)
        for name in ("provider", "sdk_package", "isolation", "creates_per_minute"):
            row[name] = _redact_values(row[name])
        report["providers"].append({**row, "keys": keys})
        if not as_json:
            print(row["provider"])
            for name, value in row.items():
                if name != "provider":
                    print(f"  {name}: {value}")
            if keys:
                print("  keys: " + ", ".join(f"{name}={value}" for name, value in keys.items()))
    if not as_json and not report["providers"]:
        print("No providers registered.")
    return 0


@contextmanager
def _verbose(enabled):
    if not enabled:
        yield
        return
    logger = logging.getLogger("anycompute")
    level, propagate = logger.level, logger.propagate
    handler = logging.StreamHandler(sys.stderr)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    options = argv[:argv.index("--")] if "--" in argv else argv
    as_json = "--json" in options
    report: dict[str, object] = {}
    try:
        args = _parse(argv)
        as_json = getattr(args, "json", False)
        report = _initial_report(args)
        with _verbose(getattr(args, "verbose", False)):
            if args.command == "run":
                code = _run(args, report, as_json)
            elif args.command == "verify":
                code = _verify(args, report, as_json)
            elif args.command == "gc":
                code = _gc(args, report, as_json)
            else:
                code = _list_providers(report, as_json)
        if as_json:
            report.setdefault("error", None)
            if failure := _print_report(report):
                write_code = _report_error(ConfigError(failure), report, False)
                return code if report["error"] else write_code
        return code
    except (AnycomputeError, KeyboardInterrupt) as error:
        parsed = getattr(error, "parsed", None)
        if parsed is not None:
            report = _initial_report(parsed)
            if parsed.command == "run" and as_json:
                from . import engine

                with suppress(ConfigError):
                    report["run_id"] = engine._resolve_run_id()
        return _report_error(error, report, as_json)
    except Exception as error:
        internal = ProviderError(f"{type(error).__name__}: {error}", internal=True)
    return _report_error(internal, report, as_json)

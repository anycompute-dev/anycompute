"""Types shared by the engine and adapters. No SDK imports."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Literal, Protocol


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class RawExecResult:
    """An adapter response before decoding and the expiry check."""

    stdout: str | bytes
    stderr: str | bytes
    exit_code: int | None


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    exit_code: int
    files: dict[str, bytes]
    missing: list[str]
    provider: str
    sandbox_id: str
    run_id: str
    effective_ttl: int
    runtime: dict[str, str] | None
    sandbox_seconds: float | None
    outside_contract: bool


class SandboxState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SandboxInfo:
    sandbox_id: str
    labels: Mapping[str, str]
    created_at: float | None  # Unix seconds, when reported by the provider.


class AnycomputeError(Exception):
    exit_code: ClassVar[int] = 124

    def __init__(
        self, message: str, *, sandbox_id: str | None = None, run_id: str | None = None,
        sandbox_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.sandbox_id = sandbox_id
        self.run_id = run_id
        self.sandbox_seconds = sandbox_seconds
        self.cleanup_error: CleanupError | None = None


class AuthError(AnycomputeError):
    exit_code = 120


class RateLimitError(AnycomputeError):
    exit_code = 121

    def __init__(self, message: str, *, retry_after: float | None = None, **context) -> None:
        super().__init__(message, **context)
        self.retry_after = retry_after


class QuotaError(AnycomputeError):
    exit_code = 122


class SandboxTimeoutError(AnycomputeError):
    exit_code = 123

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "", **context) -> None:
        super().__init__(message, **context)
        self.stdout = stdout
        self.stderr = stderr


class ProviderError(AnycomputeError):
    exit_code = 124

    def __init__(
        self, message: str, *, sandbox_may_exist: bool = False, internal: bool = False,
        interrupted: bool = False, **context,
    ) -> None:
        super().__init__(message, **context)
        self.sandbox_may_exist = sandbox_may_exist
        self.internal = internal
        self.interrupted = interrupted


class CleanupError(AnycomputeError):
    exit_code = 125

    def __init__(self, message: str, *, result: Result | None = None, **context) -> None:
        super().__init__(message, **context)
        self.result = result


class InvalidTTLError(AnycomputeError, ValueError):
    exit_code = 126


class InvalidPathError(AnycomputeError, ValueError):
    exit_code = 126


class ConfigError(AnycomputeError, ValueError):
    exit_code = 126


@dataclass(frozen=True)
class StartRate:
    value: float
    source: str
    checked: str


@dataclass(frozen=True)
class ConfigKeys:
    """TOML fields under a fixed path. '*' selects all direct sub-tables."""

    path: str
    fields: tuple[tuple[str, ...], ...]
    path_env: str | None = None


@dataclass(frozen=True)
class KeySources:
    env: tuple[str, ...] = ()
    config: tuple[ConfigKeys, ...] = ()


@dataclass(frozen=True)
class ProviderCaps:
    ttl_unit_s: int = 1
    ttl_max_s: int | None = None
    labels_at_create: bool = True
    lifetime: Literal["hard", "partial"] = "hard"
    pre_start_window: Literal["none", "partial"] = "none"
    create_retry: Literal["off", "partial"] = "off"
    exec_retry: Literal["off", "partial"] = "off"
    creates_per_minute: StartRate | None = None
    provider_options_allowlist: frozenset[str] = field(default_factory=frozenset)
    sdk_package: str = ""
    list_create_time: bool = True
    isolation: str = ""
    key_sources: KeySources = field(default_factory=KeySources)
    live_run: bool = False


class Provider(Protocol):
    """Translate operations. The engine owns all retry, polling and wait rules."""

    def prepare(self, *, options: Mapping[str, object], timeout_s: float) -> None: ...

    def create(
        self, *, ttl: int, labels: Mapping[str, str], options: Mapping[str, object], timeout_s: float,
    ) -> str: ...

    def exec(
        self, sandbox_id: str, argv: Sequence[str], *, cwd: str, timeout_s: float,
    ) -> RawExecResult: ...

    def write(self, sandbox_id: str, path: str, data: bytes, *, timeout_s: float) -> None: ...

    def read(self, sandbox_id: str, path: str, *, timeout_s: float) -> bytes | None: ...

    def delete(self, sandbox_id: str, *, timeout_s: float) -> None: ...

    def state(self, sandbox_id: str, *, timeout_s: float) -> SandboxState: ...

    def list(self, labels: Mapping[str, str], *, timeout_s: float) -> Sequence[SandboxInfo]: ...

    def native(self, sandbox_id: str, *, timeout_s: float) -> object: ...

    def api_host(self, *, timeout_s: float) -> str | None: ...


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    caps: ProviderCaps
    # Builds the adapter object only. Connections and SDK calls are waited operations.
    factory: Callable[[], Provider]

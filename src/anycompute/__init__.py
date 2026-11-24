"""The public anycompute Python API."""

from .contract import (
    AnycomputeError,
    AuthError,
    CleanupError,
    ConfigError,
    ExecResult,
    InvalidPathError,
    InvalidTTLError,
    ProviderError,
    QuotaError,
    RateLimitError,
    Result,
    SandboxTimeoutError,
)

__all__ = [
    "run", "Sandbox", "Result", "ExecResult", "AnycomputeError", "AuthError",
    "RateLimitError", "QuotaError", "SandboxTimeoutError", "ProviderError",
    "CleanupError", "InvalidTTLError", "InvalidPathError", "ConfigError",
]


def __getattr__(name: str):
    if name in {"run", "Sandbox"}:
        from . import engine

        return getattr(engine, name)
    raise AttributeError(name)

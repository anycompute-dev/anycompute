"""Provider registry; inspecting a provider's caps never loads its SDK."""

from dataclasses import asdict

from ..contract import ConfigError, ProviderSpec
from .boat import SPEC as BOAT_SPEC
from .docker import SPEC as DOCKER_SPEC
from .e2b import SPEC as E2B_SPEC
from .modal import SPEC as MODAL_SPEC

PROVIDERS: tuple[ProviderSpec, ...] = (DOCKER_SPEC, MODAL_SPEC, BOAT_SPEC, E2B_SPEC)


def get_provider(name: str) -> ProviderSpec:
    for provider in PROVIDERS:
        if provider.name == name:
            return provider
    raise ConfigError(f"Unknown provider: {name!r}")


def capability_table() -> list[dict[str, object]]:
    rows = []
    for provider in PROVIDERS:
        caps = asdict(provider.caps)
        del caps["key_sources"]
        caps["provider_options_allowlist"] = sorted(caps["provider_options_allowlist"])
        if caps["labels_at_create"] is False:
            caps["labels_at_create"] = "partial"
        rows.append({"provider": provider.name, **caps})
    return rows

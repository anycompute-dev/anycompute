"""Run `python -m tests.fakes.launcher` with the normal CLI arguments.

Set ANYCOMPUTE_FAKE_KEY to a synthetic value, and ANYCOMPUTE_FAKE_SCRIPT to
an optional UTF-8 JSON file. Its fields are `clock` ({start, wall_start}),
`caps` (ProviderCaps overrides except key_sources), `page_size` (positive int),
`sandboxes` ([{sandbox_id, labels, created_at?, state?, files?}]), and `script`.
Create times are Unix seconds; null means unavailable. Each `script` key is a
provider method name (or `mkdir` for engine mkdir execs, `list_page` for pages),
with a list of steps. The first matching step is consumed unless `repeat: true`.

Step keys: `sandbox_id` and `argv` match the call when present; `delay` is logical
seconds before the reply, `hang: true` blocks, and `error` is a status number (401,
429, 404, etc.), "quota", "internal" or "interrupt". `message` sets error text,
`retry_after` applies to 429, and `sandbox_may_exist` applies to ProviderError.
Create has `duplicate: true` and `created: true` (allocate even on a fault).
State has `state` (running, stopped, gone, unknown); exec and mkdir have `exit_code` (null allowed), `stdout`,
`stderr`, and `files` (cwd-relative or absolute names to text or byte arrays).
Stdout/stderr also accept byte arrays. All state and files are in memory.
"""

from dataclasses import replace
import json
import os
from pathlib import Path

from anycompute import cli, engine, providers
from anycompute.contract import ProviderSpec, StartRate

from . import FAKE_CAPS, FakeClock, FakeProvider


def main() -> int:
    path = os.environ.get("ANYCOMPUTE_FAKE_SCRIPT")
    config = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    caps = config.pop("caps", {})
    if "key_sources" in caps:
        raise ValueError("The fake launcher only uses ANYCOMPUTE_FAKE_KEY")
    if caps.get("creates_per_minute") is not None:
        caps["creates_per_minute"] = StartRate(**caps["creates_per_minute"])
    if "provider_options_allowlist" in caps:
        caps["provider_options_allowlist"] = frozenset(caps["provider_options_allowlist"])
    clock = FakeClock(**config.pop("clock", {}))
    fake = FakeProvider(clock=clock, caps=replace(FAKE_CAPS, **caps), **config)
    providers.PROVIDERS = (ProviderSpec("fake", fake.caps, lambda: fake),)
    engine._clock = clock
    return cli.main()


if __name__ == "__main__":
    raise SystemExit(main())

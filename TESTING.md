# Testing

Run `uv run --locked pytest -q` from the repository root. The logic tests use the in-memory fake provider or synthetic responses and need no provider keys. Pytest excludes the live tests by default, even when a test selector supplies their paths.

Before merge, all five GitHub Linux jobs must pass on the pull request's candidate (Python 3.11 through 3.15). The GitHub Docker conformance job must also pass. Local tests do not replace these checks.

Run `uv run --locked --extra docker pytest -q` to include the Docker SDK transport tests. These tests use a fake transport and do not contact a daemon.

For the live Docker suite, start a local Linux-container daemon. Run `ANYCOMPUTE_CONFORMANCE_PROVIDER=docker uv run --locked --extra docker pytest -q -m live_conformance tests/conformance`. The marker selects the suite. The provider variable allows the suite to run. This suite creates and removes labeled containers with the pinned image. The lifetime tests wait for the actual TTL.

Run `uv build` to build the source distribution and wheel. This command does not publish either file.

The protected `Conformance` workflow can compare one fixed workload on Modal and boat.dev. Set `adapters` to `modal,boat` and enable `verify` on a manual main-branch dispatch after both adapters are enabled and their credentials are configured. The comparison job records its JSON report in the step log.

Do not run cloud conformance locally. The live-test and spend rules are in [the design](docs/design.md#live-runs).

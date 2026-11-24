# Testing

Run `uv run --locked pytest -q` from the repository root. The logic tests use the in-memory fake provider or synthetic responses and need no provider keys.

Before merge, all five GitHub Linux jobs must pass on the pull request's candidate (Python 3.11 through 3.15). A local test run does not satisfy this check.

Run `uv build` to build the source distribution and wheel. This command does not publish either file.

Do not run cloud conformance locally. The live-test and spend rules are in [the design](docs/design.md#live-runs).
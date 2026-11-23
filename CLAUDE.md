# anycompute

An open-source (Apache-2.0) Python SDK and CLI that runs short, stateless sandbox jobs the same way on Modal, boat.dev, E2B, Daytona and local docker, with the user's own provider keys.

## Start here

- `docs/design.md` is the build spec for v0.1. Read it before you change code.
- Its "Next Steps" section lists the work in order. There is no code yet, so work starts at Next Step 1 (prices) and Next Step 2 (the fake provider, the contract types, the Provider protocol and the engine).
- `TODOS.md` holds deferred work. Do not build a TODOS item before v0.1 unless the design changes first.

## Rules

- Change `docs/design.md` in the same pull request as any code that changes a contract rule, the CLI output, or an adapter.
- Never test or probe a provider's systems. Live tests make only ordinary, paced public API calls. Rate-limit, quota and auth error mappings are tested only with recorded or fake responses.
- Live runs on the project's provider accounts go only through `conformance.yml` in CI (the weekly run or a manual dispatch), under the conformance runner's spend rules (`docs/design.md`, Constraints). Never run them from a local machine.
- Never write a provider key to disk, a log, a commit or a test fixture. All output goes through the redaction rules.
- No personal data in sandbox labels, metadata, fixtures or docs.
- Unit tests (`tests/unit/`) run against the fake provider with no keys. Live tests (`tests/conformance/`) drive the CLI as a subprocess and read only its JSON and exit codes.

# anycompute

Run short, stateless sandbox jobs the same way on any sandbox provider.

anycompute is an open-source Python SDK and CLI. One call or one command runs a job in a sandbox, reads back the output files and deletes the sandbox, with the same API on every supported provider and on local docker. You use your own provider accounts and keys: there is no hosted service, no account with anycompute and no telemetry.

## Status

Early development. No release is available yet, so do not depend on the API. The v0.1 design is in [docs/design.md](docs/design.md), and deferred work is in [TODOS.md](TODOS.md).

The CLI has `run`, `gc`, and `providers`, but no Docker or cloud adapters are registered. `anycompute providers` reports `No providers registered.` The tests exercise `run` and `gc` without real sandboxes. The live conformance suite and `verify` are not implemented yet.

See [TESTING.md](TESTING.md) for keyless checks and [CLAUDE.md](CLAUDE.md) for repository rules.

The Python API supports only the names in `anycompute.__all__`. For the CLI, a command's exit code can match an anycompute error code. Scripts should use `--json` and check the `error` field to tell them apart.

## License

Apache-2.0. See [LICENSE](LICENSE).

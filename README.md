# anycompute

Run short, stateless sandbox jobs the same way on any sandbox provider.

anycompute is an open-source Python SDK and CLI. One call or one command runs a job in a sandbox, reads back the output files and deletes the sandbox, with the same API on every supported provider and on local docker. You use your own provider accounts and keys: there is no hosted service, no account with anycompute and no telemetry.

## Status

Early development. No release is available yet, so do not depend on the API. The v0.1 design is in [docs/design.md](docs/design.md), and deferred work is in [TODOS.md](TODOS.md).

The CLI has `run`, `verify`, `gc`, and `providers`. Docker, Modal, and Boat adapters are available. The Docker conformance suite uses a local Linux container daemon. Modal and Boat have keyless tests and are awaiting their first live conformance runs. Modal also needs a verified retry allocation limit.

See [TESTING.md](TESTING.md) for keyless checks and [CLAUDE.md](CLAUDE.md) for repository rules.

The Python API supports only the names in `anycompute.__all__`. For the CLI, a command's exit code can match an anycompute error code. Scripts should use `--json` and check the `error` field to tell them apart.

## Providers

| Provider | Isolation | Key required | Lifetime |
|---|---|---|---|
| `docker` | Container on the local host kernel | No | Hard after start. A container left before start has no timer. |
| `modal` | gVisor container | Modal SDK credentials | Hard, up to 24 hours. The SDK may retry creates and execs. |
| `boat` | Virtual machine | `BOAT_API_KEY` | Partial. The VM stops at its TTL and retains a disk snapshot until cleanup. |

Use Docker only for workloads you trust. A container is not a boundary for untrusted code. Start a local Docker daemon and run this from the repository:

```sh
uv run --locked --extra docker anycompute run --provider docker -- printf 'hello\n'
```

The adapter uses a pinned image that contains `sleep`. It pulls the image before create if the daemon does not have it. If a client stops before the container starts, use `anycompute gc --provider docker --all --dry-run` to check for expired leftovers.

## Compare providers

Use `anycompute verify task.toml --providers modal,boat` to compare one task across two or more providers you have configured. Add `--json` for a machine-readable report. Add `--lifecycle` to check whether each provider ends and removes a separate sandbox after the client is killed. That check uses the provider's default options.

## License

Apache-2.0. See [LICENSE](LICENSE).

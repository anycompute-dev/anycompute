# anycompute

Run short, stateless sandbox jobs the same way on any sandbox provider.

anycompute is an open-source Python SDK and CLI. One call or one command runs a job in a sandbox, reads back the output files and deletes the sandbox, with the same API on every supported provider and on local docker. You use your own provider accounts and keys: there is no hosted service, no account with anycompute and no telemetry.

## Status

Early development. No release is available yet, so do not depend on the API. The v0.1 design is in [docs/design.md](docs/design.md), and deferred work is in [TODOS.md](TODOS.md).

The CLI has `run`, `verify`, `gc`, and `providers`. Docker, Modal, Boat, E2B, and Daytona adapters are available. The Docker conformance suite uses a local Linux container daemon. The cloud adapters have keyless tests. E2B and Daytona each passed all five conformance tests in manual CI runs, including lifetime and deletion after client termination. Weekly cloud conformance remains disabled. [E2B run](https://github.com/anycompute-dev/anycompute/actions/runs/45395187713) · [Daytona run](https://github.com/anycompute-dev/anycompute/actions/runs/45441305818)

Download the versioned JSON report from a workflow run's artifacts. Each report contains one provider's check outcomes and available lifecycle timings. [Docker CI](https://github.com/anycompute-dev/anycompute/actions/workflows/ci.yml?query=branch%3Amain) · [Cloud conformance](https://github.com/anycompute-dev/anycompute/actions/workflows/conformance.yml)

See [TESTING.md](TESTING.md) for keyless checks and [CLAUDE.md](CLAUDE.md) for repository rules.

The Python API supports only the names in `anycompute.__all__`. For the CLI, a command's exit code can match an anycompute error code. Scripts should use `--json` and check the `error` field to tell them apart.

## Providers

| Provider | Isolation | Key required | Lifetime |
|---|---|---|---|
| `docker` | Container on the local host kernel | No | Hard after start. A container left before start has no timer. |
| `modal` | gVisor container | Modal SDK credentials | Hard, up to 24 hours. The SDK may retry creates and execs. |
| `boat` | Virtual machine | `BOAT_API_KEY` | Partial. The VM stops at its TTL and retains a disk snapshot until cleanup. |
| `e2b` | Firecracker microVM | `E2B_API_KEY` | Hard, up to one hour. |
| `daytona` | Linux container | Daytona SDK credentials | Hard, with whole-minute TTL. |

Use Docker only for workloads you trust. A container is not a boundary for untrusted code. Start a local Docker daemon and run this from the repository:

```sh
uv run --locked --extra docker anycompute run --provider docker -- printf 'hello\n'
```

The adapter uses a pinned image that contains `sleep`. It pulls the image before create if the daemon does not have it. If a client stops before the container starts, use `anycompute gc --provider docker --all --dry-run` to check for expired leftovers.

## Compare providers

Use `anycompute verify task.toml --providers modal,boat` to compare one task across two or more providers you have configured. Add `--json` for a machine-readable report. Add `--lifecycle` to check whether each provider ends and removes a separate sandbox after the client is killed. That check uses the provider's default options.

## What your provider can see

The provider receives the command and arguments you run, input file paths and contents, output paths you ask to read, and any supported image, template, snapshot, or environment options you supply. Cloud SDKs authenticate with your provider credentials. Any environment values you pass to the workload are visible to that provider.

Every create sends three labels: `anycompute=1`, `anycompute-run` with the run ID, and `anycompute-ttl` with the effective TTL in seconds. The adapter also sends the TTL in the provider's format. Anycompute uses the labels to find its own sandboxes and sends sandbox IDs to read, query, terminate, or delete them. The boat.dev and E2B clients send the User-Agent `anycompute/<version>`, as does Docker after its initial API version lookup. Modal, Daytona, and that initial Docker lookup use their SDK's identification.

Anycompute runs `mkdir -p /tmp/anycompute` and creates the parent directories of nested input and write paths. When it collects runtime information, it runs `python3 --version` and `uname -m` in the sandbox. `verify --lifecycle` also starts a separate sandbox and runs `sh -c 'touch ready; while :; do :; done'` under a child run ID so it can observe expiry after killing the local client.

| Adapter | Additional commands and resources |
|---|---|
| Docker | Pulls the pinned or supplied image if needed. The daemon starts an auto-remove container with `sleep <ttl>` as its entrypoint, a process limit, and `no-new-privileges`. |
| Modal | Looks up or creates the `anycompute` App in your workspace and starts a gVisor sandbox with `sleep infinity`. |
| boat.dev | Creates a private VM with account secret injection and monitoring disabled. Runs your quoted command in the requested working directory and checks requested output files with shell file tests. |
| E2B | Creates a sandbox from the default or supplied template, requests kill on timeout, and runs commands through a POSIX shell. |
| Daytona | Creates an ephemeral sandbox from the default `daytona-small` or supplied snapshot with whole-minute TTL and auto-stop. Creates an exec session and runs each command in a fresh POSIX shell. |

The adapter details list each provider's resource and cleanup calls. [Adapter design](docs/design.md#adapters)

## Release runbook

Before the first release, configure a PyPI Trusted Publisher for this repository, `release.yml`, and the `pypi` environment. In GitHub, restrict the environment to `v*` tags and require maintainer approval. The release workflow checks the tag against the package version, builds the source distribution and wheel, and publishes both with attestations after approval. The final job installs that wheel with the Docker extra from PyPI and runs a Docker smoke test. [PyPI Trusted Publishing setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)

Use the smoke job's result to decide what to do:

- **Install failed.** Check that the tagged version is visible on PyPI and rerun the smoke job.
- **Infrastructure.** Rerun the smoke job. If it fails again, investigate Docker or the image registry before deciding whether the package is at fault.
- **Package failure.** Reproduce the failing check against the installed release. Once the package is confirmed as the cause, yank the affected version in PyPI and release a tested patch with a new version. [PyPI yanking](https://docs.pypi.org/project-management/yanking/)

## License

Apache-2.0. See [LICENSE](LICENSE).

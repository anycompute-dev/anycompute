# anycompute

Run short, stateless sandbox jobs the same way on any sandbox provider.

anycompute is an open-source Python SDK and CLI. One call or one command runs a job in a sandbox, reads back the output files and deletes the sandbox, with the same API on every supported provider and on local docker. You use your own provider accounts and keys: there is no hosted service, no account with anycompute and no telemetry.

## Status

v0.1 supports Docker, E2B, and Daytona. Modal and Boat are available as preview adapters and still await project live conformance. The `all` extra includes both previews. See the [v0.1 design](docs/design.md) and [deferred work](TODOS.md).

The CLI has `run`, `verify`, `gc`, and `providers`. The Docker conformance suite uses a local Linux container daemon. The cloud adapters have keyless tests. E2B and Daytona each passed all five conformance tests in manual CI runs, including lifetime and deletion after client termination. Weekly cloud conformance remains disabled. [E2B run](https://github.com/anycompute-dev/anycompute/actions/runs/45395187713) · [Daytona run](https://github.com/anycompute-dev/anycompute/actions/runs/45441305818)

Download the versioned JSON report from a workflow run's artifacts. Each report contains one provider's check outcomes and available lifecycle timings. [Docker CI](https://github.com/anycompute-dev/anycompute/actions/workflows/ci.yml?query=branch%3Amain) · [Cloud conformance](https://github.com/anycompute-dev/anycompute/actions/workflows/conformance.yml)

See [TESTING.md](TESTING.md) for keyless checks and [CLAUDE.md](CLAUDE.md) for repository rules.

The Python API supports only the names in `anycompute.__all__`. For the CLI, a command's exit code can match an anycompute error code. Scripts should use `--json` and check the `error` field to tell them apart.

## Providers

| Provider | v0.1 status | Isolation | Key required | Lifetime |
|---|---|---|---|---|
| `docker` | Supported | Container on the local host kernel | No | Hard after start. A container left before start has no timer. |
| `e2b` | Supported | Firecracker microVM | `E2B_API_KEY` | Hard, up to one hour. |
| `daytona` | Supported | Linux container | Daytona SDK credentials | Hard, with whole-minute TTL. |
| `modal` | Preview | gVisor container | Modal SDK credentials | Hard, up to 24 hours. The SDK may retry creates and execs. |
| `boat` | Preview | Virtual machine | `BOAT_API_KEY` | Partial. The VM stops at its TTL and retains a disk snapshot until cleanup. |

Use Docker only for workloads you trust. A container is not a boundary for untrusted code. Start a local Docker daemon. With the package installed using the `docker` extra, run `anycompute run --provider docker -- printf 'hello\n'`. From a source checkout, run:

```sh
uv run --locked --extra docker anycompute run --provider docker -- printf 'hello\n'
```

The adapter uses a pinned image that contains `sleep`. It pulls the image before create if the daemon does not have it. If a client stops before the container starts, use `anycompute gc --provider docker --all --dry-run` to check for expired leftovers.

## Migrate with a coding agent

Open the repository you want to migrate in your coding agent and copy this prompt.

```text
Migrate this repository's provider-agnostic sandbox SDK to anycompute 0.1.0.
Make the code changes and run the relevant tests.

Use the released API and CLI as the reference.
https://pypi.org/project/anycompute/0.1.0/
https://github.com/anycompute-dev/anycompute/blob/v0.1.0/docs/design.md#python-api
https://github.com/anycompute-dev/anycompute/blob/v0.1.0/docs/design.md#cli

1. Find the existing dependency, sandbox call sites, provider selection, and
   tests. Inventory the command, file, timeout, and cleanup behavior callers
   depend on. Check that the project can use Python 3.11+ or call the anycompute
   CLI. Migrate short-lived jobs with equivalent behavior. Keep the old path
   where persistent sessions, mounts, streaming, or provider features lack a
   safe equivalent, and report those gaps.
2. Use the repository's package manager to add the relevant anycompute extras
   (docker, e2b, or daytona) and update its lockfile. Preserve provider routing,
   isolation requirements, and how credentials are supplied. Pass the provider
   explicitly and keep credential values out of code, fixtures, and logs.
3. For a single job, use anycompute.run(argv, provider=..., ttl=..., files=...,
   read=...). For several commands in one sandbox, use a context-managed
   anycompute.Sandbox and its write, exec, and read methods. Preserve argument
   boundaries and intentional shell use. In Python, use relative sandbox paths
   under /tmp/anycompute and pass file contents as bytes. Check provider TTL
   limits and rounding. The TTL covers the sandbox lifetime, so preserve any
   separate command deadline the application relies on.
4. Preserve the caller's output shape, missing-file handling, exit status,
   and error handling. Treat a nonzero workload exit as a result. Handle
   provider and cleanup errors separately, keep the run ID for cleanup, and
   avoid replaying work when a sandbox may still exist. If migrating a CLI
   caller, use --json and inspect error and exit_code separately.
5. Run relevant tests with fakes or trusted workloads on local Docker. Ask
   before allocating cloud sandboxes. Remove the old dependency only after
   all its callers are migrated. Report what changed, which tests passed,
   and which paths still need the old SDK.
```

## Compare providers

Use `anycompute verify task.toml --providers docker,e2b` to compare one task across two or more providers you have configured. Add `--json` for a machine-readable report. Add `--lifecycle` to check whether each provider ends and removes a separate sandbox after the client is killed. That check uses the provider's default options.

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

Before tagging v0.1.0, check the [launch criteria](docs/design.md#success-criteria) for Docker, E2B, and Daytona and confirm `Docker conformance` is required and passing on `main`.

Before the first release, configure a PyPI Trusted Publisher for this repository, `release.yml`, and the `pypi` environment. In GitHub, restrict the environment to `v*` tags and require maintainer approval. The release workflow checks the tag against the package version, builds the source distribution and wheel, and publishes both with attestations after approval. The final job installs that wheel with the Docker extra from PyPI and runs a Docker smoke test. [PyPI pending publisher setup](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)

Use the smoke job's result to decide what to do:

- **Install failed.** Check that the tagged version is visible on PyPI and rerun the smoke job.
- **Infrastructure.** Rerun the smoke job. If it fails again, investigate Docker or the image registry before deciding whether the package is at fault.
- **Package failure.** Reproduce the failing check against the installed release. Once the package is confirmed as the cause, yank the affected version in PyPI and release a tested patch with a new version. [PyPI yanking](https://docs.pypi.org/project-management/yanking/)

## License

Apache-2.0. See [LICENSE](LICENSE).

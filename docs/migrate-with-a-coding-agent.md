Migrate compatible jobs in this repository from its current provider-agnostic
sandbox SDK to anycompute 0.1.0. Implement the migration and verify it.

Use the v0.1.0 release as the source of truth.

- Package <https://pypi.org/project/anycompute/0.1.0/>
- Python API <https://github.com/anycompute-dev/anycompute/blob/v0.1.0/docs/design.md#python-api>
- CLI reference <https://github.com/anycompute-dev/anycompute/blob/v0.1.0/docs/design.md#cli>

For both baseline and post-migration sandbox tests, use fakes or trusted jobs
on local Docker. Get approval before allocating any cloud sandbox.

1. Find the current SDK dependency, imports and wrappers, sandbox call sites,
   provider configuration, and tests. Identify how each job executes commands,
   transfers files, handles deadlines and results, and cleans up. Check that
   Python 3.11+ is available for the SDK or for invoking the CLI. Keep existing
   paths for persistent sessions, streaming, mounts, or any feature without a
   safe equivalent. Flag those paths and explain the gap.
2. Run the relevant existing tests as a baseline. Start with one representative
   short-lived job and verify equivalent behavior before migrating the rest.
   Use this repository's package manager to add anycompute 0.1.0 with the
   needed extras (docker, e2b, or daytona) and update its lockfile if it has one.
   Pass the provider explicitly. Preserve provider routing, isolation, and
   credential configuration. Keep credentials out of code, fixtures, and logs.
3. For a one-shot job, use
   `anycompute.run(argv, provider=..., ttl=..., files=..., read=...)`.
   For multiple commands in the same sandbox, use a context-managed
   `anycompute.Sandbox` with `write`, `exec`, and `read`. Preserve
   argument boundaries and intentional shell use. Python file paths are
   relative to `/tmp/anycompute` and file contents are bytes. Check provider
   TTL limits and rounding. TTL covers the sandbox lifetime. Preserve any
   separate per-command deadline the application depends on.
4. Preserve stdout, stderr, exit status, and missing-file behavior. A nonzero
   workload exit is a result. Handle provider and cleanup errors separately,
   retain the run ID for cleanup, and avoid replaying a job when its sandbox
   may still exist. For CLI callers, use `--json` and inspect `error` and
   `exit_code` separately.
5. Rerun the affected tests. Remove the old dependency only when all its
   callers have migrated. Report what changed, which tests ran and their
   results, and which paths still depend on the old SDK.

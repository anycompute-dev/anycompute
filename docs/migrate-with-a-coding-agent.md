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
   transfers files, handles deadlines and results, and cleans up. Trace files
   and processes reused by later tools or requests. Check for workspace files
   copied across fresh sandboxes and artifacts discovered at runtime. Keep
   automatic workspace sync on the old SDK where declared file transfers
   cannot preserve it. Verify required CPU and memory caps and network
   restrictions. The v0.1.0 Docker adapter exposes image and environment
   options. Keep the old SDK where the target cannot enforce the same limits
   or preserve required behavior such as shared sessions, streaming, or
   mounts. Flag these gaps. Check that Python 3.11+ is available for the SDK or
   for invoking the CLI.
2. Run the relevant existing tests as a baseline. Start with one representative
   short-lived job and verify equivalent behavior before migrating the rest.
   Use this repository's package manager to add anycompute 0.1.0 with the
   needed extras (docker, e2b, or daytona) and update its lockfile if it has one.
   Pass the provider explicitly. Preserve provider routing, isolation, and
   credential configuration. Keep credentials out of code, fixtures, and logs.
3. For a one-shot job, use
   `anycompute.run(argv, provider=..., ttl=..., files=..., read=...)`.
   For multiple commands within one job that owns the sandbox, use a
   context-managed `anycompute.Sandbox` with `write`, `exec`, and `read`.
   Preserve argument boundaries and intentional shell use. Python file paths
   are relative to `/tmp/anycompute` and file contents are bytes.
4. Budget the sandbox TTL for setup, execution, and readback. Check the
   provider's TTL limits and rounding. Preserve the command deadline separately.
   Reuse an existing in-sandbox deadline or use a tested supervisor that stops
   the workload at the deadline, including child processes when required. A
   client-side wait limit can leave the workload running. Keep the old path if
   the deadline cannot be enforced. Report timeouts separately from the
   workload's exit code. Test both an expired deadline and an ordinary exit
   such as `124`. Compare the caller-visible diagnostics with the baseline
   and report any intentional change.
5. If a caller can cancel an in-flight job, test cancellation separately
   from deadline expiry. Verify what must stop, including child processes
   or the whole sandbox when required, and when cleanup must finish.
   Cancelling an async wait can leave the worker and sandbox running.
   Keep the old SDK if the target cannot preserve the required termination
   and cleanup behavior.
6. Preserve stdout, stderr, exit status, and missing-file behavior. A nonzero
   workload exit is a result. Handle provider and cleanup errors separately,
   retain the run ID for cleanup, and avoid replaying a job when its sandbox
   may still exist. For CLI callers, use `--json` and inspect `error` and
   `exit_code` separately.
7. Rerun the affected tests and verify that retained paths still use the old
   SDK. Remove that dependency only when all its callers have migrated.
   Report what changed, which tests ran and their results, and which paths
   still depend on the old SDK.

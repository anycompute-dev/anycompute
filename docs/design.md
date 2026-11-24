# anycompute design (v0.1)

Status: approved design for anycompute v0.1.
License: Apache-2.0. This file is the build spec; deferred work is in `TODOS.md`.

## Problem

A developer who runs short, stateless sandbox jobs (run code or a command, read the output, throw the sandbox away) is tied to one provider's SDK. To move from E2B to Modal or Daytona, the developer rewrites the calls. The job also behaves differently after the move: each provider has its own default lifetime and its own meaning of "destroy".

## What anycompute does

anycompute is an open-source Python SDK and CLI. One call or one command runs a stateless job on E2B, Modal, Daytona or boat.dev with the developer's own provider keys, or locally on docker with no account.

- The same call gives the same result shape and the same errors on every provider.
- It gives the same lifetime and delete on every provider that can enforce them.
- The capability table declares each gap, and a public test suite proves each claim that the table makes.

The value is a stateless contract, not the number of adapters. Every create sets a lifetime that the provider enforces. Destroy is a real delete, never a pause. Results and errors have one typed form. There is no local state. An open conformance suite proves the contract in public CI against every adapter that runs live; any other adapter is marked "not run live".

### Why the lifecycle matters

The API shapes of the main providers already match: create, run a command with stdout, stderr and exit code, read and write files, delete. The lifecycle does not match (as of 2026-11-23):

- E2B kills a sandbox at a 300-second default timeout.
- Modal ends a sandbox after a 5-minute default lifetime.
- Daytona deletes a sandbox on stop only when it is created as ephemeral (15-minute auto-stop default).
- Vercel keeps a sandbox by default, and its snapshots survive delete.
- boat.dev stops a VM at a TTL (1 hour default) and keeps its disk; delete needs a confirm header and is asynchronous.

So the same code leaks sandboxes, or costs different amounts, on different providers. anycompute answers this in two ways:

- `anycompute verify` runs your own workload on two or more providers and reports whether the exit code, the output files and the cleanup match.
- The conformance suite has a test that kills the client and checks from outside that the provider still ends the sandbox and deletes it, on every provider that enforces delete at the deadline. On a partial lifetime such as boat.dev, the provider stops the sandbox and gc deletes it.

A `finally` block cannot protect against a killed process or a lost create response. So anycompute never replaces the provider's deadline with a client timer, and never reports cleanup success without a check.

### Why Python and the CLI's JSON come first

v0.1 is one Python implementation plus a CLI. The existing switching libraries are mostly TypeScript, and eval and RL users of stateless sandboxes probably work mostly in Python (an inference from Harbor, Inspect and Prime). Two implementations at once would double the places where lifecycle behavior can drift. The contract stays in one code base until it passes live on Modal and boat.dev and the E2B and Daytona adapters exist. A second language (TypeScript) starts only after that, and only when a named user, project or framework asks for it (an issue or a plug-in request). Until then the CLI's `--json` output is the interface for other languages.

## Constraints

### Open source, bring-your-own-key only

- Users run on their own provider accounts with their own keys from environment variables or the provider's own config file (such as Modal's `~/.modal.toml` profile).
- No hosted key, no credits, no markup, no account with anycompute, and no telemetry.
- Keys are never written to disk, logged, or sent anywhere except the provider's own API.
- Adapters set a User-Agent `anycompute/<version>` where the SDK client options allow it, never through the user's environment.
- A README section "What your provider can see" lists everything anycompute itself sends or creates:
  - the three labels, the run ID and the TTL;
  - the User-Agent above;
  - the Modal App named `anycompute` in the user's workspace;
  - the commands it runs in the sandbox (`mkdir -p /tmp/anycompute` plus the parent folders of nested `files=` and `Sandbox.write` paths, and the runtime detection commands when runtime is detected);
  - the command `verify --lifecycle` runs;
  - each adapter's own sandbox commands and resources (for example the docker image pull and its `sleep <ttl>` entrypoint), which each adapter spec lists.

### Redaction

- Error messages, the JSON output, `verify` reports and the public report replace the value of every provider key (from an environment variable or a provider config file such as Modal's profile) with `***`.
- The engine also redacts the values in `provider_options[*]["environment"]`, because users pass sandbox secrets there. The short-value rule below differs for them.
- The engine finds the key values from each adapter's `key_sources` cap.
- The engine masks both raw key values and their usual Python and JSON escapes before serializing data fields. It does not redact the serialized JSON or change fixed field names, error types, or status values.
- The engine skips an empty value, and a `provider_options` environment value shorter than 8 characters.
- The engine redacts a provider key of any length. It replaces a key shorter than 8 characters only where it appears as a whole token, so short values do not damage unrelated text. A whole token has the start or end of the text, or a character outside `[A-Za-z0-9_-]`, on each side.

Tests:

- Redactor table tests check that the engine skips an empty value and an environment value under 8 characters. They also check that it redacts a provider key of any length inside string fields, and a secret with an escaped quote. They also check that a provider key under 8 characters is replaced where it stands as a whole token and is left alone inside a longer word (such as `ab12` in `ab12cd`).
- A JSON-validity test checks that the `--json` output stays valid JSON with a planted key and a short environment value such as `1` or `true`.

### Debug logger

The standard Python logger `anycompute` writes one DEBUG line per step (validate, pace wait, create, exec start and end, delete, confirm, cleanup error, abandoned call) with the run ID, sandbox ID, provider and duration, through the same redaction. It is silent by default, `--verbose` prints it to stderr, and it sends nothing anywhere. A unit test plants a key in a logged error and checks each LogRecord's message, args and exc_info, not only the formatted text. A CLI test checks that a run with no flag writes no logger line to stderr, and that `--verbose` writes the DEBUG lines there.

### Scope of v0.1

- **Python 3.11 or newer.** `requires-python = ">=3.11"`; `task.toml` is read with the standard `tomllib`; Python 3.10 reached end of life in October 2026.
- **Stateless only.** No pause, resume, snapshots, volumes, ports, or long-lived sessions.
- **Each provider's default image, unless the user opts in.** The SDK and the project's own runs use each provider's default image. A user may opt in to an image, template or resources through `provider_options` or, for `verify`, a `task.toml` provider table, and such a run is marked `outside_contract`. `verify` records the runtime (Python version, CPU architecture) per provider so a difference is visible.
- **No local state.** anycompute keeps no ledger file. It finds its own sandboxes through provider-side labels or metadata and the provider's list API.

### Live runs

- The project runs the live conformance suite on its own provider accounts for Modal, boat.dev, E2B and Daytona, and on docker, both locally and in the `ci.yml` docker job on every pull request.
- It uses only ordinary, paced public API calls. It never probes or provokes provider failures (see the next section).
- It publishes results by provider name.
- If a provider objects, the project stops its runs on that provider, removes that provider's published results, and marks its adapter "not run live".
- These rules cover the project's own runs: CI (the only place live runs on the project's accounts happen; see Run limit) and the public report. A user's runs on the user's own account are the user's business.
- Every adapter's fixtures, live or not, come from the provider's published API reference, OpenAPI file, or SDK source and its own test fixtures. The project captures no fixture from live calls on its own accounts. A contributor's recording from the contributor's own account is accepted when the pull request says so. The capability table and the README label an adapter that does not run live "not run live".
- A fixture key-scan unit test fails on any fixture with an `Authorization`, `X-API-Key` or cookie header, a key-like field, or a known key prefix.
- docker needs no provider account. It costs $0 and is exempt from the spend runner's run limit.

### Never test or probe a provider's systems

The conformance suite uses only ordinary public API calls (create, exec, files, delete, get, list) at a paced rate on the project's own accounts. It never provokes a provider failure: rate-limit, quota and auth error mappings are proven only with recorded or fake HTTP responses in unit tests.

### Spend rules for the conformance runner

- **Monthly budget:** $50 per provider per month on the project's accounts. CI keeps no spend record, so the per-run budget, the run limit and the cleanup-failure stop below keep this budget. It is a hard cap only where the provider has its own spend limit.
- **Worst-case reservation:** the suite drives the CLI as a subprocess, so before each CLI launch the conformance runner (`tests/conformance/runner.py`) reserves the worst-case cost of every sandbox that launch can create (one per provider for `run` and `verify`, plus one per provider for the `verify --lifecycle` child): the `effective_ttl` plus the maximum grace period, at the provider's price for the default size, plus any start fee, from `tests/conformance/prices.toml`. On partial-lifetime adapters (boat.dev, Daytona) the reservation also adds one week of the provider's stopped-storage price per sandbox. On an adapter whose `create_retry` cap is `partial`, it reserves two sandboxes per create. Project live runs require evidence that this covers the SDK’s possible allocations.
- **Duplicate check:** after each launch the runner lists the run's sandboxes (the run ID and its `-lc` and `-kc` child run IDs) by label. If it finds more than it reserved, it stops the run, runs gc for those run IDs, and fails the workflow. A runner unit test uses a fake that creates a duplicate.
- **Per-run budget:** the runner refuses a launch that would pass a budget of $2 per adapter per run, and the refusal fails the workflow with the adapter and the amount named.
- **Run limit:** the workflow allows the weekly run plus at most 4 other runs (manual dispatches and re-run attempts) a month per adapter. So an adapter gets at most 9 runs a month: $18 at most, under the $50 budget. On boat.dev the $20 plan is not an extra cost: every dollar comes back as VM time, so the 9 runs fit inside the plan's $20 a month (docs.boat.dev/pricing, checked 2026-11-23). GitHub's run history is the count, because the runs API does not return dispatch inputs:
  - A dispatch's `run-name` carries a fixed `adapters=<list>` token from its `adapters` input, and the runs API returns it as `display_title`. The count splits the token in `display_title` on commas and matches each name exactly.
  - A scheduled (weekly) run has no inputs. It counts against every adapter other than docker whose `live_run` cap is true, and the weekly job runs that same set, read from the caps. docker runs live in the `ci.yml` docker job instead.
  - A keyless gate job runs first, with no environment and only `actions: read` permission. It lists this calendar month's (UTC) runs of `conformance.yml` through the GitHub Actions API. Per named adapter it counts each `workflow_dispatch` run that names it, plus `run_attempt - 1` re-runs of each run, weekly or manual, that counts against it. The count includes the current run and attempt, and the gate fails the whole run when any named adapter's count is above 4.
  - The gate refuses a re-run (`run_attempt` above 1) of a run created in an earlier UTC month.
  - The gate fails closed on a non-2xx response, a body with no `workflow_runs` list, fewer runs read than `total_count`, a parse error, or a missing or null `display_title`, `event` or `run_attempt`. The published schema marks `run_attempt` as optional. It never counts an unread value as zero.
  - The gate outputs the `run_attempt` that it counted. Only a scheduled run or a manual dispatch on main can enter the cloud test job, and only when the gate succeeds with a nonempty adapter list. Every test job `needs:` the gate, and only the test jobs enter the protected environment. Each test job stops before any provider call unless `github.run_attempt` equals the gate's output. So a re-run of only the failed jobs stops at that check, and a full re-run goes through the gate, which counts it. Pull requests check this logic with fake responses and run the live suite only on Docker.
  - The protected environment allows deployments only from main (a deployment branch rule).
  - Live runs of an adapter or of `verify` on the project's accounts go only through `conformance.yml`: the weekly run, or a manual dispatch that names the adapters as inputs (Next Steps 5 to 8, and any later ad hoc live run). They never run from a local machine, so each one is counted. docker is exempt.

  Tests (spend-runner unit tests, with recorded GitHub API responses):

  - 3 earlier dispatches plus the current one pass, and the gate refuses a fifth.
  - A run at `run_attempt` 3 counts 2.
  - A weekly re-run counts for each live adapter other than docker, and the weekly job and the count read the same set from the caps.
  - The gate refuses a re-run of a previous month's run.
  - A 403, a missing `workflow_runs` list, fewer runs than `total_count`, a parse error, and a missing or null `display_title`, `event` or `run_attempt` each fail closed, with one test per case.
  - A unit test reads `conformance.yml` and checks the gate's permissions, each test job's `needs:` and its attempt check.
- **Run size:** the scheduled suite runs once a week. One run creates at most 15 sandboxes per adapter with a TTL of at most 120 seconds, so its worst case per adapter is 15 x (120 + 120) seconds = 1 sandbox-hour. At the default-size prices in `tests/conformance/prices.toml` (checked 2026-11-23), one run costs at most $0.17 on E2B, $0.07 on Daytona, $0.04 on boat.dev and $0 on docker. The partial-retry reservation counts two sandboxes per create. That gives estimates of $0.04 on Modal, $0.34 on E2B, and $0.14 on Daytona at the same default-size rates. Start fees are $0 at the suite's pace. Stopped storage is also $0: Daytona deletes an ephemeral sandbox on stop, and boat.dev keeps a stopped disk free for at least 14 days.
- **Stop on cleanup failure:** a failed delete keeps billing past the reservation. So after the first launch whose `--json` output reports a failed cleanup, the runner stops launching in that run. A failed cleanup is an `error` of type `CleanupError` or a non-null `cleanup_error`, at the top level or in any `verify` row. The runner then runs gc for the run ID and its `-lc` and `-kc` child run IDs, and fails the workflow. It reads the JSON, not the exit code, because exit codes can collide (see Exit codes). Output that is not one JSON document, or a CLI crash, counts as a failed cleanup. Runner unit tests: a non-null `cleanup_error` in one `verify` row stops launches; a command's own exit 125 with a null `error` does not; non-JSON output and a CLI crash fail closed.
- **Sweep earlier leftovers:** before its first create, each keyed job runs `gc --all --provider <adapter>` for the project's labeled sandboxes. If gc reports any failed delete, the job creates nothing and fails.
- **Job timeout:** the test step has its own `timeout-minutes`, inside the job limit. An `if: always()` step after it runs gc on each adapter for the workflow's run ID and its `-lc` and `-kc` child run IDs.
- **Free credits first:** E2B one-time credit, Modal $30 a month, Daytona $200 with no card (checked 2026-11-23). Use a hard limit or prepaid wallet where the provider has one, and clean up by run ID after every run.
- **boat.dev** costs at least $20 a month (its plan, which comes back as VM time) and joins CI only when its adapter starts.
- **Prices with a source:** `tests/conformance/prices.toml` records each provider's prices, each with its source URL and checked date. A value that a provider does not publish is marked `published = false`.

### Release path

Trusted publishing from CI and signed releases from the first release, because users give the SDK their provider keys.

## The stateless contract (v0.1)

### One engine

- Every contract rule that the Python API enforces lives once, in the engine (`engine.py`) behind `run()` and `Sandbox`. The engine also holds the gc selection rules, redaction, the clock and the waited-call helper.
- `verify.py` holds the verify checks and report. `lifecycle.py` holds the kill-client flow that `verify --lifecycle` and the conformance harness share.
- `contract.py` holds only types (Result, ExecResult, SandboxState, the errors, the Provider protocol and caps). It imports no other anycompute module.
- `providers/__init__.py` holds the provider registry (one `PROVIDERS` tuple) and builds the capability table from the caps.
- `__init__.py` only re-exports. Its `__all__` lists the public API: `run`, `Sandbox`, `Result`, `ExecResult` and the 10 error classes. Helpers inside modules have a leading underscore, and the README says that the project supports only the names in `__all__`.
- Adapters translate calls, which includes quoting an argument list for a shell-string API and turning off SDK retries. They hold no policy, no poll loop, no timer and no retry decision.
- `cli.py` parses flags and raises `ConfigError` on a usage error. It reads `--file` paths and `task.toml` input files from local disk into the `files=` dict (with the missing-file and base-name-clash checks). It checks the output folder, prints the run ID to stderr, formats output, and holds the top-level handler (a redacted `ProviderError` with `internal` true; see Exit codes). These CLI behaviors are its only rules.

Tests:

- A test pins `anycompute.__all__`.
- Importing `anycompute.contract` does not import the engine.
- A drift test checks that the `PROVIDERS` tuple matches the extras, the `all` extra, `providers/*.py` other than `__init__.py`, the README table and the harness pace.

Each adapter translates about ten calls: `prepare` before create (docker's image pull; no call on other adapters), create with TTL and labels, exec, write, read, delete, state, list, error mapping, and `api_host()` for the runtime record.

- `state` returns a `SandboxState`: `running`, `stopped`, `gone` or `unknown`. Each adapter maps its native states (including deleting and paused states) to these four values. The engine never treats `unknown` as `gone`. Test: one recorded native-state table test per adapter.
- `list` reads every page within one 120-second budget. It filters by label on the server where the provider supports it, else in the adapter before it returns. Tests: a recorded two-page test per paged adapter, and an engine test where `gc --run` deletes a sandbox on page 2 of a fake list.

Each adapter declares these caps:

| Cap | Type and values | What it declares |
|---|---|---|
| `ttl_unit_s`, `ttl_max_s` | int; int or None (no maximum) | The provider's lifetime granularity and limit, in seconds |
| `labels_at_create` | bool | Whether labels go in the create request |
| `lifetime` | `"hard"` or `"partial"` | `hard` when the provider ends the sandbox at its TTL; `partial` when it only stops it (boat.dev), or until the suite shows a delete at the TTL (Daytona; see Declared gaps) |
| `pre_start_window` | `"none"` or `"partial"` | An untimed window before the lifetime starts |
| `create_retry`, `exec_retry` | `"off"` or `"partial"` | `off` when the adapter turns off every SDK and transport retry for that call (rule 10); `partial` when it cannot |
| `creates_per_minute` | a number with its source URL and checked date, or None | Its documented creates per minute |
| `provider_options_allowlist` | set of key names | The `provider_options` keys it accepts |
| `sdk_package` | str | Its SDK package name, for the report's `provider_sdk_version` |
| `list_create_time` | bool | Whether its list shows a create time |
| `isolation` | str | Its isolation level from the provider's docs, for example a microVM, a VM, or a container on the host kernel |
| `key_sources` | environment variable names and config-file fields | Where its keys live, for redaction, the missing-key check and `anycompute providers` (empty for docker) |
| `live_run` | bool | False marks the adapter "not run live" (see Live runs); the weekly run tests every adapter other than docker where it is true (see Run limit) |

The capability table is built from those caps. It is never written by hand.

**Fake provider.** The fake provider lives in `tests/fakes/`, outside the package. It implements the Provider protocol like an adapter, with the key variable `ANYCOMPUTE_FAKE_KEY`. It keeps its sandboxes in memory, records every call, and takes its caps and scripted behaviours from its constructor:

- a hang or delay per call;
- a failed delete;
- a late create;
- a duplicate create;
- an exec exit code of 137 or none;
- a native-state sequence;
- a list split over two pages;
- an exception outside the error mapping (such as a `TypeError`);
- injected provider responses (401, 429, quota, other).

CLI subprocess tests run a test launcher in `tests/fakes/`. The launcher registers the fake in the provider registry under the name `fake` and reads `ANYCOMPUTE_FAKE_SCRIPT`, the path of a JSON file with the same settings. Then it swaps the engine clock and calls `anycompute.cli.main`. The installed package never contains the fake, so the capability table and `anycompute providers` need no hidden-provider case. The docker conformance suite still proves the installed `anycompute` entry point.

**Clock.** The engine owns one clock object (now, sleep and a timed wait). Every engine timer (budgets, the cleanup deadline, the poll schedule, the pacer, the gc age rule, `sandbox_seconds`) uses it. No engine wait calls `time.sleep` or `Thread.join` with a real timeout directly. Unit tests and the fake launcher swap it for a fake clock.

**Thread safety.** The engine shares the per-process run ID, one locked create pacer per provider, and bounded worker admission (rule 2). Each `Sandbox` owns its own provider client, or the adapter shares one client that it builds with no lock held. A lock guards only the stored client (the first stored client wins), as with the Modal `app_id`. No adapter holds a lock across a provider call. `run()` and separate `Sandbox` objects are safe to use from many threads.

**Sandbox object.** `Sandbox(provider, ttl=300, provider_options=None, creates_per_minute=None, run_id=None)` creates the sandbox in its constructor, so the TTL counts from there, and `with` is optional. It has `sandbox_id` and `run_id`, `write(path, bytes)`, `exec(argv)` (it returns `ExecResult(stdout, stderr, exit_code)`), `read(path)` (bytes, or None for a missing file), `native` and `close()`. `close()` deletes the sandbox (rule 4); `__exit__` calls it. After confirmed deletion, another `close()` does nothing. After failed or interrupted cleanup, it reports `CleanupError` without a new delete. After `close()`, `exec`, `write` and `read` raise `ConfigError` with no provider call, and the object exposes `sandbox_seconds` and `outside_contract`. A failure inside the constructor after create still deletes the sandbox (rule 5).

**No async API in v0.1.** A native `arun()` and `AsyncSandbox` are deferred (`TODOS.md`). Async callers wrap `run()` in a thread until then. Streaming reads for large output files are also deferred.

### Contract rules

| # | Rule | Summary |
|---|---|---|
| 1 | Lifetime | Every create sends a provider-enforced TTL (default 300 s) |
| 2 | No client timers | Client timeouts are always longer than the provider's deadline |
| 3 | Expiry during a command | A sandbox that ends mid-command raises `SandboxTimeoutError` |
| 4 | Delete | A real delete, confirmed within 120 s, or `CleanupError` |
| 5 | Cleanup error | A failure after create still ends in a delete; output is kept and the original exception is never hidden |
| 6 | Result | One typed Result on every adapter |
| 7 | Working directory | `/tmp/anycompute`, relative paths only |
| 8 | Arguments | An argument list, quoted for shell-string APIs |
| 9 | Errors | One error type per cause, no key values |
| 10 | Retries | No automatic create or exec retries |
| 11 | Provider options | An allowlisted escape hatch, marked `outside_contract` |
| 12 | Create pacing | One locked token bucket per provider |
| 13 | No local state | Labels and the provider's list API, read by gc |

#### 1. Lifetime

What the SDK does:

- `ttl` (seconds) is optional on every create, in `run()`, `Sandbox()`, the CLI `--ttl` flag and `task.toml`. It defaults to 300, and the SDK always sends a provider lifetime.
- The TTL counts from create, except in an adapter's declared partial pre-start window (rule 2).
- `ttl` is an int, and a `bool` is not an int here. A TTL that is not an int, or is below 1, raises `InvalidTTLError` before create. A `task.toml` `ttl` of the wrong type (such as `"120"` or `120.0`) also raises `InvalidTTLError`.
- The engine rounds it up to the provider's granularity from the adapter's caps (for example whole minutes on Daytona) and reports `effective_ttl` in the Result.
- An `effective_ttl` above the provider's maximum raises `InvalidTTLError` before create. It is never clamped.
- The `anycompute-ttl` label, every client budget, gc and the spend reservation use `effective_ttl`.
- The adapter passes the TTL to the provider's own lifetime setting, never to a client timer.

Tests:

- The live test runs `anycompute run --json --ttl 120 -- sleep 600`. The observer checks the sandbox state through the provider's API from outside. It passes only when the provider ends the sandbox (gone or killed, never paused) by the TTL plus the maximum grace period. On a `partial` lifetime, stopped passes.
- A unit test checks that a TTL of 61 on a 60-second unit gives an `effective_ttl` of 120 in the Result, the `anycompute-ttl` label and the create call.

#### 2. No client timers

What the SDK does:

- The engine gives create the effective TTL plus the maximum grace period as its budget. Later work shares a deadline measured from dispatch of the create request. For an adapter with `pre_start_window: partial`, the engine starts that work deadline at the successful create reply instead. Reported `sandbox_seconds` still starts at request dispatch. The budgets are finite; the client timeout must not end a command before the provider's deadline.
- The docker image pull runs before create, so no sandbox exists yet and the TTL does not run yet. The pull gets the TTL plus the maximum grace period as its budget. A pull that reaches it raises `ProviderError` (exit 124) with `sandbox_may_exist` false.
- Every provider call on every adapter goes through the engine's one waited-call helper, which waits up to the call's budget on the engine clock. An SDK's own timeout may stay as an inner guard only when the adapter sets it to the call's budget or longer, so it never fires before the helper's budget ends. The adapter always sets or turns off each SDK default timeout.
- The engine rejects any reply at or after the original deadline, even if the worker signals before the caller resumes. It leaves a call that is already in progress to finish in the background and logs the abandonment.
- Each waited call uses a daemon thread. The process admits up to 64 work calls and reserves a separate 64 slots for delete and confirm. A hung call holds its slot until it exits. If no slot is available or the thread cannot start, the engine reports a typed error without dispatching the call.
- A dispatched create without a trustworthy outcome is a lost create: `ProviderError` with `sandbox_may_exist` and the gc hint. A known pre-allocation refusal is not a lost create. If an abandoned create later yields an ID while the process still runs, the same worker tries deletion under the cleanup clock and logs the result.
- The same wait applies to delete and confirm calls, with the time left on the 120-second cleanup deadline as the budget. A delete or confirm call that reaches it raises `CleanupError` (rule 4), with no state check.
- `anycompute gc` has no TTL. Each of its list calls, and each state call it makes to select sandboxes, gets 120 seconds (the cleanup clock's length) through the same wait. A call that reaches it raises `ProviderError` (exit 124) naming the provider. gc's delete and confirm calls keep the 120-second cleanup deadline.
- The rule 3 state check (after a fired client timeout, or after an exec exit code of 137 or none) gets one 120-second budget (the cleanup clock's length) across its state calls. A state call that reaches it raises `ProviderError` (exit 124), and the delete and its confirm still run.

Tests:

- The lifetime test uses a TTL longer than every provider SDK's default command timeout (Daytona 10 s, boat.dev 30 s, E2B 60 s, docker 60 s).
- A unit test with a hung fake exec checks that the waited-call helper returns at the budget on a fake clock. It also checks the rule 3 outcome (`ProviderError` while the fake sandbox still runs) and the DEBUG line for the abandoned call.
- A CLI test whose fake exec never returns checks that the process exits 124 at the budget.
- A unit test makes a fake create finish after its timeout and checks that the late sandbox is deleted and logged.
- A unit test with a hung fake list checks that gc exits 124 at 120 seconds on a fake clock.
- A unit test with a hung fake delete during gc checks that gc raises `CleanupError` and exits 125 at 120 seconds from the start of cleanup, not `ProviderError` at the list budget.

#### 3. Expiry during a command

What the SDK does:

- When the provider ends the sandbox at the TTL while a command runs, `run()` and `Sandbox.exec()` raise `SandboxTimeoutError` with the sandbox ID, the run ID, and any partial stdout and stderr that the provider returned.
- If a client timeout fires anyway, the engine asks the provider for the sandbox state: `gone` or `stopped` raises `SandboxTimeoutError`; `running` or `unknown` raises `ProviderError`. The delete and its confirm run either way.
- When an exec returns an exit code that can mean the sandbox ended (137, or none, as docker's exec does when its container stops), the engine also asks the state call. A sandbox that is gone or stopped raises `SandboxTimeoutError`. An exit code of none while the sandbox still runs raises `ProviderError`. After an exit code of 137 or none, a state of `unknown` raises `ProviderError`, because the engine never treats `unknown` as `gone`. The delete and its confirm still run.
- Docker can report the exec exit before it updates the container state. When the first state is `running` after an ambiguous exec result, the engine waits one second and checks once more within the same state budget, but only if the TTL could have elapsed since the create request. This uses the earliest possible start, not the later create reply. If the state stays `running`, 137 is still a command exit code.
- Every anycompute error raised after a create carries `sandbox_seconds` (None when no delete confirmed). For an error raised inside a `Sandbox` block, `__exit__` sets it on the error in flight after the delete confirm.

Tests:

- The live lifetime test checks that `anycompute run --json` reports `SandboxTimeoutError` in its `error` field and exits 123 on every adapter.
- A unit test with a slow fake exec covers both client-timeout branches.
- A unit test with a hung fake state call after a fired client timeout raises `ProviderError` at 120 seconds on a fake clock, and the delete and its confirm still run.
- Recorded-response unit tests cover an exec that returns 137 after its container stopped (`SandboxTimeoutError`) and an exit code of none with the container gone (`SandboxTimeoutError`). They also cover an exit code of none while the container still runs (`ProviderError`) and an exit code of 137 with state `unknown` (`ProviderError`).
- A recorded Docker response sequence returns 137 or none at the TTL, then reports `running` before reporting `stopped`. The test checks the timeout, both output streams, and one confirmed delete. Controls keep 137 a normal result before the TTL and when the container stays running.

#### 4. Delete

What the SDK does:

- `close()` and the end of `run()` delete the sandbox; they never pause or stop it. They return only after the provider confirms the delete.
- The delete and every confirm poll share one deadline of 120 seconds from the start of cleanup. Each request gets the time left.
- The engine retries a failed delete or confirm request (for example a 502 or a 429) within that deadline. A late reply cannot confirm a delete. A not-found answer to the delete counts as a successful delete request on every adapter.
- Every engine poll (the confirm, the delete retry, and the observer's marker and gone waits) follows one schedule: it sends the first request at once, then waits 1 second and doubles to a 10-second cap. On a 429 it waits `retry_after` when the provider gives one (capped at the time left), else the next step. A zero delay allows one immediate retry per poll. Subsequent zero delays use the normal schedule so the poll cannot spin.
- The engine confirms a delete only when the adapter's state call reports `gone`, never because a list no longer shows the sandbox. On `unknown` the confirm keeps polling.
- They raise `CleanupError` when that deadline passes, on every adapter.
- An adapter's delete holds no confirm or poll logic of its own. If it calls an SDK wait (Daytona `wait=True`, Modal `terminate(wait=True)`), that wait gets only the time left on that deadline, and the engine still confirms through the adapter's state call.
- A failed delete is never silent.

Tests:

- After `anycompute run --json` exits, the observer's state call reports the sandbox gone.
- A unit test with a hung fake delete raises `CleanupError` at 120 seconds on a fake clock.
- A unit test whose fake confirm returns 429 once and then gone checks that the engine backs off, keeps polling and returns the Result. A 429 that lasts to the deadline raises `CleanupError`.
- Unit tests make a fake delete fail once with a 502, and once with a 429, and check a retry within the deadline. A not-found delete counts as a successful delete request.
- On a fake clock where the delete returns at once, a confirm that never sees `gone` makes exactly 15 state requests in 120 seconds (at 0, 1, 3, 7, 15, 25, ..., 115 s). A 429 with `retry_after` 7 waits 7 seconds.
- `close()` twice makes one delete call. `exec`, `write` or `read` after `close()` raises `ConfigError` with no provider call.

#### 5. Cleanup error

What the SDK does:

- Any failure after the provider returns a sandbox ID runs the delete and its confirm before the error leaves. This covers the post-create `mkdir`, a write, runtime detection, a read-back, docker's start and an interrupt, in `run()` and in the `Sandbox` constructor. The error carries the `sandbox_id`.
- On an open `Sandbox`, a provider operation failure also closes the sandbox. A rejected local argument does not close it, so the caller can correct the argument and try again.
- An adapter create that made a resource and then failed raises `ProviderError` with the `sandbox_id`. The engine uses the exact returned ID for provider calls and attempts cleanup once. Displayed IDs can be redacted. A nonzero exit from the post-create `mkdir` raises `ProviderError`.
- An interrupt after create dispatch, before the engine learns the sandbox ID, is a lost create: `ProviderError` with `sandbox_may_exist` and the gc hint, and the CLI exits 130.
- `CleanupError` carries the completed `Result` (when the job finished), the sandbox ID and the run ID, so the user keeps the output.
- In `run()` and in `Sandbox.__exit__`, when an exception is already in flight, the original exception propagates and the cleanup failure is never hidden: the engine sets the `CleanupError` as the original's `cleanup_error` attribute and adds its redacted message with `add_note`, never through `__cause__`.
- Cleanup also runs when a `KeyboardInterrupt` arrives during a command. After the cleanup, the `KeyboardInterrupt` propagates with the run ID and the sandbox ID attached (and a `cleanup_error` when the delete failed), and the CLI exits 130.
- If a second `KeyboardInterrupt` stops the cleanup, the CLI prints the copyable dry-run gc command (see CLI) and exits 130, and the Python API re-raises `KeyboardInterrupt` with the run ID and the sandbox ID attached.

Tests:

- A unit test with a fake provider whose delete fails checks both cases (the `cleanup_error` attribute and its note, for `run()` and `Sandbox`), and a `KeyboardInterrupt` during exec.
- A CLI test checks that one `KeyboardInterrupt` with a confirmed delete exits 130 with an `error` of type `KeyboardInterrupt`, and that a failed delete after it adds the copyable gc command.
- A second `KeyboardInterrupt` during cleanup makes the CLI print the dry-run gc command and exit 130, and makes the Python API re-raise `KeyboardInterrupt` with the run ID and the sandbox ID.
- One parametrized unit test fails at each step after create (a nonzero `mkdir`, a write, runtime detection, a read-back, an interrupt), in `run()` and in the `Sandbox` constructor. Each case makes exactly one delete and confirm, and the error carries the `sandbox_id`. A docker unit test makes start fail after create and checks the same.
- A CLI test interrupts a fake create in flight and checks `ProviderError` with `sandbox_may_exist`, the gc command and exit 130.

#### 6. Result

What the SDK does:

- `run()` returns `Result(stdout, stderr, exit_code, files, missing, provider, sandbox_id, run_id, effective_ttl, runtime, sandbox_seconds, outside_contract)`.
- A nonzero exit code is a result, not an error.
- `sandbox_seconds` is the measured time from the create request to the delete confirm, shown in `--json` and in each verify row with no price table. It is None in the Result that `CleanupError` carries and on every other path with no delete confirm (including a `Sandbox` whose delete failed in `__exit__`). `--json` prints it next to `error`, and a verify row for a provider that raised an error shows the error type and its `sandbox_seconds`.
- `sandbox_seconds` is described as an upper bound on billed time only for a provider whose recorded billing granularity is 1 second or finer with no minimum charge. For any other provider the docs give its rounding rule and minimum charge from Next Step 1 instead.
- `stdout` and `stderr` are text, decoded as UTF-8 with invalid bytes replaced (U+FFFD), the same rule on every adapter. Binary data travels through files, which stay bytes.
- `runtime` is filled only by `verify` or `run(..., runtime=True)`, and is `None` otherwise.

Tests:

- A conformance test on every adapter checks that `exit 7` returns `exit_code=7` and that stderr text arrives in `stderr`, not in `stdout`.
- One test sends invalid UTF-8 output and one binary file through `run`, `--json` and `verify`.
- A unit test checks `sandbox_seconds` on a fake clock, and None after a failed delete.

#### 7. Working directory

What the SDK does:

- The engine creates `/tmp/anycompute`, and the parent folders of any nested `files=` path, in the sandbox after create (one `mkdir -p` through the adapter's exec). This first exec uses `/` because the working directory does not exist yet. All later exec calls use `/tmp/anycompute` as their working directory. `Sandbox.write` to a nested path first runs one `mkdir -p` for its parent folder.
- Uploads are written there, and read-back paths resolve from it.
- A read path that does not exist, or is not a regular file (such as a directory), is left out of `files` and listed in `missing`.
- A sandbox path is a `files=` key, a read path, or a read-back path in `task.toml`. It raises `InvalidPathError` before create when it is absolute, has a `..` part (after a split on `/`), is empty, ends in `/` or holds a NUL byte. So does a `files=` key that is the parent folder of another key. A nested relative path such as `sub/in.txt` is allowed, and a name such as `a..b.txt` is not a `..` part. A `Sandbox.write` or `Sandbox.read` path goes through the same check and raises `InvalidPathError` before the provider call.
- A CLI `--file` value and a `task.toml` input file are local paths (a `task.toml` input resolves from the folder of `task.toml`). `cli.py` reads them from disk with the same missing-file and base-name-clash checks, and only the base name is used in the sandbox.

Tests:

- A conformance test uploads a file with `--file`, runs it, reads relative paths back, and reads one missing file, with the same result on every adapter.
- A CLI unit test covers an upload, a missing local file and a base-name clash.
- A unit test checks that `sb.write("sub/in.txt", ...)` succeeds and that `sb.write("../x", ...)` raises `InvalidPathError` with no provider call.
- Unit tests reject an empty path, a trailing slash, a NUL byte and a `files=` key that is the parent of another key before create. They count a directory read path as missing.

#### 8. Arguments

What the SDK does: the API takes an argument list. An adapter whose provider takes one shell string quotes the list with `shlex.join` for a POSIX shell.

Test: a conformance test passes arguments with spaces, quotes and shell metacharacters and checks identical stdout on every adapter.

#### 9. Errors

What the SDK does:

- Every error subclasses `AnycomputeError`. There is one type per cause: `AuthError`, `RateLimitError` (with `retry_after` when the provider sends it), `QuotaError`, `SandboxTimeoutError`, `ProviderError`, `CleanupError`, and three caller-input errors that also subclass `ValueError`: `InvalidTTLError`, `InvalidPathError` and `ConfigError`. Each error class carries its CLI `exit_code`.
- `ConfigError` covers:
  - an unknown provider name;
  - a CLI usage error (an unknown flag, a missing `--provider`, a `--ttl` that is not a number, `gc` with both `--run` and `--all`);
  - an output folder (`./anycompute-out/` or `--out <dir>`) that is not writable;
  - a `Sandbox` `exec`, `write` or `read` after `close()`;
  - a missing extra;
  - a `provider_options` entry for an unknown provider;
  - an unlisted `provider_options` key;
  - a `creates_per_minute=` value that differs from an earlier one for the same provider;
  - a pacing rate that is not a positive number;
  - a run ID that does not match `[a-z0-9-]{1,40}`;
  - in a `task.toml` provider table, an `environment` variable that is not set locally, or a literal `environment` value table;
  - a `task.toml` with an unknown key, a value of the wrong type (a wrong `ttl` type raises `InvalidTTLError`), or no `command`.
- The `ConfigError` message names the extra, the key or the value. For an `environment` entry it names the key and never the value, because a literal value may be a secret.
- A key is missing only when none of the adapter's `key_sources` (its environment variables, or the provider's own config file such as Modal's profile) holds it. Then `AuthError` is raised before any provider call. An adapter with an empty `key_sources` (docker) needs no key, so this check does not apply to it.
- Error attributes: every anycompute error raised after a create has `sandbox_id`, `run_id` and `sandbox_seconds`; `SandboxTimeoutError` also has the partial `stdout` and `stderr`; `CleanupError` has `result` (the completed Result, or None); `RateLimitError` has `retry_after`; `ProviderError` has `sandbox_may_exist` and `internal`.
- The waited-call helper wraps an exception that the adapter's error mapping does not cover (for example a `TypeError` in adapter code) in a redacted `ProviderError` with `internal = True`. A mapped provider error has `internal = False`.
- No error carries a key value. The engine replaces each provider key value with `***` in the provider's error text, keeps that redacted text on its own error, and raises it outside the provider's exception handler, so no raw provider exception stays attached.
- An invalid provider reply raises `ProviderError` rather than becoming a successful result. In a provider timeout error, invalid partial output becomes empty text. A retry delay is absent unless it is a finite nonnegative number.

Tests:

- Unit tests cover each of the 9 types.
- Recorded or fake provider responses (401, 429, quota, other) from the provider's own API map to `AuthError`, `RateLimitError`, `QuotaError` and `ProviderError` on every adapter. docker's registry pull limit is `ProviderError` (see the docker adapter).
- A TTL of 0, a TTL that is not an int, `ttl=True`, and a `task.toml` `ttl` of `"120"` or `120.0` raise `InvalidTTLError` with no create call. So do a TTL above the maximum (E2B 3601, boat.dev 481) and a TTL that rounds up above the maximum.
- An absolute path and a `..` path raise `InvalidPathError` with no create call. `sub/in.txt` and `a..b.txt` pass the check.
- One parametrized test per `ConfigError` bullet above raises `ConfigError` (for `task.toml`, a mistyped key such as `exitcode = 1`, a wrong type and no `command`).
- A missing key raises `AuthError`, and the fake provider records no call.
- A unit test plants a fake key, and a fake secret in `provider_options` environment values, in a fake provider error. It checks that both are absent from `str`, `repr`, a formatted traceback, the JSON output and the report, and that `__context__` and `__cause__` are None.
- A unit test makes a fake call raise `TypeError` and checks a `ProviderError` with `internal` true.
- No live test provokes a provider failure.

#### 10. Retries

What the SDK does:

- The SDK never retries a create automatically: a retried create after a lost response would make a second sandbox. It does not retry exec.
- Each adapter also turns off its provider SDK's own retries and its transport (HTTP or gRPC connection) retries for create and exec through the SDK's client options where the SDK allows it, never by changing the user's environment.
- An adapter that cannot (Modal, E2B and Daytona today) declares `create_retry: partial` and `exec_retry: partial`. A duplicate an adapter may create carries the same labels and TTL, so the provider ends it and gc finds it. The spend runner reserves for it and counts it (see Spend rules).
- The caller handles `RateLimitError` with its `retry_after`.

Tests:

- Unit tests check that a fake 429 on create raises once and makes one create call.
- Adapter tests for an HTTP-based SDK count requests at the HTTP layer with a transport-level fake and recorded responses, including a lost response.

#### 11. Provider options

What the SDK does:

- `run()` and `Sandbox()` take `provider_options={"<provider>": {...}}`.
- The engine sends only the active provider's entry, and only keys on that adapter's allowlist in its caps: resources, image or template, environment, and region (region only on an adapter whose list call covers every region, so gc can still find the sandbox).
- Lifecycle and label keys (timeout, ttl_minutes, auto_stop_interval, auto-stop, pause-on-timeout, ephemeral, ttlSeconds, tags, metadata, labels) are never on an allowlist, so the TTL, the labels and the confirmed delete still apply.
- An unlisted key, or an entry for an unknown provider name, raises `ConfigError` before create, naming it.
- Every entry is checked against its adapter's allowlist, which is part of the caps and loads without the provider SDK.
- `sb.native` gives the provider SDK's own sandbox object.
- A run whose active provider has a non-empty entry has `Result.outside_contract = True`. A `Sandbox` is `outside_contract` once its active entry is non-empty or `sb.native` is touched. Entries for other providers do not set it.
- In v0.1, `provider_options` and `sb.native` exist in the Python API. `anycompute run` takes no options.
- `task.toml` takes a `[providers.<name>]` table for `verify` that goes through the same allowlists and redaction. In that table `environment` is a list of variable names, and `verify` reads each value from the local environment at run time, so no secret value sits in `task.toml`. A verify row that used options shows `outside_contract` true.
- The project's own conformance runs never use options. The capability table and the public report make no claim about options, and the project's live runs never pass one.

Tests:

- An unlisted key and an unknown provider name each raise `ConfigError` with no create call.
- A listed key reaches the fake create.
- A unit test checks that no adapter's allowlist holds a lifecycle or label key.
- `outside_contract` is set on the Result and on the `Sandbox`, and on a `Sandbox` with no entry once `sb.native` is touched. An entry for another provider does not set it.
- A `task.toml` `environment` list resolves from the local environment, and a missing variable and a literal value table each raise `ConfigError` with no create call.
- A `verify` run with a `task.toml` provider table shows `outside_contract` true in that provider's row, and a planted secret in one of its environment variables is absent from the report.

#### 12. Create pacing

What the SDK does:

- The engine holds each create until the provider's documented start rate allows it. The caller reserves a slot, and the worker rechecks the spacing at dispatch (5 seconds at 12 a minute). Pacing waits do not consume the create budget. The pacer lock is not held during a sleep or a provider call. This spaces engine dispatches, not network delivery.
- The rate comes from the adapter's `creates_per_minute` cap.
- A user overrides it in two ways:
  - `ANYCOMPUTE_CREATES_PER_MINUTE` sets the rate for every provider in the process (providers with no documented rate are then paced too). The engine reads it once, at the first create.
  - `creates_per_minute=` on `run()` or `Sandbox()` sets that provider's rate for the process and wins over the environment variable for that provider.
- A later keyword call with a different value for the same provider, or any rate that is not a positive number (in the keyword or the environment variable), raises `ConfigError` (CLI exit 126).
- A provider with no documented rate and no override (Modal today) is not paced.
- Pacing works inside one process only, so the conformance harness paces its own subprocess launches. Nothing retries: a 429 still raises `RateLimitError` once.

Tests:

- A unit test runs 50 concurrent `run()` calls on the fake provider with a rate of 12 a minute on a fake clock. It checks that creates are at least 5 seconds apart and that the nth caller waits at most (n - 1) x 5 seconds.
- Unit tests cover the override rules.
- A `verify --lifecycle` unit test on a paced fake provider checks that the killed child starts only after a pacer token.

#### 13. No local state

What the SDK does:

- Each create carries three labels in the create request itself: `anycompute=1`, `anycompute-run=<run-id>` and `anycompute-ttl=<seconds>`.
- The run ID is generated once per process (12 random lowercase hex characters), or set with the `run_id=` argument or the `ANYCOMPUTE_RUN_ID` environment variable (CI sets one per workflow run). `run_id=` on a `run()` or `Sandbox()` call wins and applies only to that call; it does not change the process ID. Then `ANYCOMPUTE_RUN_ID`, read once at the first create, and then the generated ID. It must match `[a-z0-9-]{1,40}`, which every first provider accepts as a label value; a value that does not match raises `ConfigError` before create. It is in every Result, and the CLI prints it to stderr before the command starts.
- A lost create response raises `ProviderError` with the run ID and `sandbox_may_exist = True`, and gc finds the sandbox by its labels.

gc rules:

- `anycompute gc --run <id>` deletes that run's leftovers: every sandbox of the run the user names, running or not.
- `anycompute gc --all` requires the `anycompute=1` label and a valid create time whose sum with `anycompute-ttl` and the maximum grace period is before now. The grace period also covers clock skew up to 120 seconds.
- On every adapter, `gc --all` checks an expired candidate's state before selecting it. It selects only `stopped` or `gone`. It keeps `running` and `unknown` with reason `running`, and it keeps a failed state check with reason `state_error`. A state check and a delete are separate provider calls.
- A missing or invalid create time is kept (`no_create_time`). A missing or malformed TTL label is kept (`no_ttl_label`).
- gc continues after selection and deletion errors and reports all collected rows. A failed delete has action `failed` and exit 125. The first cleanup error takes precedence over state errors; otherwise the first state error determines the exit code.
- `--force` bypasses the age and state checks, not the ownership label. It can delete a running sandbox.
- `--dry-run` prints the list without deleting.
- gc never deletes a sandbox without the `anycompute=1` label.
- A provider that can only label after create declares `labels_at_create` false, and the capability table shows rule 13 as `partial` for it. All four first providers label at create.

Tests:

- After a killed client, `gc` finds the sandbox by its label with no file on disk.
- Fake-provider tests cover expired and young sandboxes, missing and invalid TTLs and times, and keeping expired candidates in running or unknown state on all adapters.
- With two expired sandboxes whose first delete fails, gc still deletes the second, reports the rows `failed` and `deleted`, and exits 125.
- gc spares a sandbox with a matching run label but no `anycompute=1` label, under `--run` and under `--all --force`, and `--dry-run` deletes nothing.
- A unit test checks the run-ID precedence (`run_id=`, then `ANYCOMPUTE_RUN_ID` read once, then the generated ID) and the `[a-z0-9-]{1,40}` check.

### Kill-client test

This is the test that a `finally` block cannot pass on its own. The flow lives once, in `lifecycle.py`, and takes the child launcher and the observer as arguments. It has one pass-rule function per lifetime value: `hard` passes on gone, and `partial` passes on gone or stopped. The conformance harness and `verify --lifecycle` both call it.

1. The harness sets `ANYCOMPUTE_RUN_ID` to `<run ID>-kc` and starts `anycompute run --json --ttl 120 -- sh -c 'touch ready; while :; do :; done'` as a child process. The run ID is the harness's own run ID (the workflow run ID in CI, a generated ID in a local run), cut to 37 characters when it is longer. The command runs in `/tmp/anycompute`.
2. A separate observer, which uses the adapter's state, list and read calls, finds the child's sandbox by its `anycompute-run` label. It polls the read call until the `ready` file exists, which shows that the workload is running and busy.
3. The harness then kills the child with SIGKILL.
4. The observer polls the provider's API until the sandbox is gone, or stopped on a `partial` lifetime.

Rules:

- The marker is a file, not a stdout line, because the v0.1 CLI prints stdout only after the command ends.
- The marker wait ends at the TTL. With no marker by then, the test fails and the harness runs `gc --run` for the child run ID (`<run ID>-kc`).
- Every observer call goes through the engine's waited-call helper with the time left before the test's deadline (the TTL plus the maximum grace period). The observer polls on the engine's poll schedule (rule 4).
- The maximum grace period is one constant in the engine: 120 seconds after the TTL, the same for every adapter.
- The test reports the measured time separately and fails when it exceeds the TTL plus the maximum grace period.

Unit tests (one set on the fake for both callers, plus the live docker run):

- With an observer stub, the kill comes only after the marker.
- With a stub that never sees the marker, the test fails and the harness calls gc for `<run ID>-kc`.
- A unit test checks the `-kc` run ID: a 40-character harness run ID gives a 40-character child run ID, and a local run with no workflow run ID uses a generated ID.
- A table test checks the pass rule per lifetime value. A sandbox that ends after the TTL plus the grace period fails, and the test reports its measured time.

### Capability table

- Each adapter declares in its caps which rules the provider can enforce, and the table is built from those caps: one row per adapter and one column per cap (without `key_sources`), plus the live-run status. The rule columns are `lifetime` and `pre_start_window` (rule 1), `create_retry` and `exec_retry` (rule 10) and `labels_at_create` (rule 13).
- A rule the provider cannot enforce is declared `partial` (a `labels_at_create` of false shows as `partial`), the docs say so, and `verify` reports it.
- The SDK never claims a guarantee that the conformance suite has not shown.
- The table also shows each adapter's `isolation`, in `anycompute providers` and in the README's copy of the table. Its unit test checks that column.
- `anycompute providers` shows each key source as set or unset, never its value. A unit test checks it.

Declared gaps:

- **Daytona lifetime** is `partial` until the lifetime and kill-client tests show that Daytona deletes the sandbox at the TTL (`ttl_minutes`; see the Daytona adapter). Then it becomes `hard`.
- **boat.dev lifetime** is `partial`: its TTL stops the VM and keeps the disk. For a `partial` lifetime, the kill-client and lifetime tests pass when the VM is stopped within the TTL plus the maximum grace period; the suite's gc then deletes it and verifies the delete. The docs tell users that a killed client on boat.dev leaves a stopped VM and its disk until gc runs. boat.dev keeps a stopped disk free for at least 14 days (docs.boat.dev/billing, checked 2026-11-23).
- **docker lifetime** is hard once the container starts. The window between create and start, where no timer runs, is declared in its own cap, `pre_start_window: partial`. So docker's lifetime, kill-client and Lifecycle checks still use the hard pass rule (with auto-remove, an ended container is gone), and a stopped container fails. gc deletes a container left in that window.
- **Modal create and exec retries** are `partial`: its gRPC layer retries calls and has no public switch. Its list shows running sandboxes only and no create time, so `gc --all` keeps Modal sandboxes unless `--force`; its hard timeout ends them anyway.

## Python API

```python
from anycompute import run, Sandbox

result = run(                 # thread-safe: many runs can share a ThreadPoolExecutor
    ["python3", "-c", "open('out.txt','w').write('hi')"],
    provider="modal",        # or "e2b", "daytona", "boat", "docker"
    ttl=60,
    files={"in.txt": b"data"},
    read=["out.txt"],
)
print(result.run_id, result.exit_code, result.stdout, result.files["out.txt"])

with Sandbox(provider="modal", ttl=120) as sb:   # deleted on exit, delete confirmed
    sb.write("task.py", b"print(1)")
    r = sb.exec(["python3", "task.py"])
```

## CLI

```
anycompute run --provider modal --ttl 60 --file task.py --read out.txt -- python3 task.py
anycompute verify task.toml --providers modal,boat    # same job on each provider, compares result and cleanup
anycompute gc --run <run-id> --provider modal         # delete one run's leftovers by label
anycompute gc --all --provider boat --dry-run         # list expired labeled sandboxes; drop --dry-run to delete them (on Modal, which shows no create time, gc --all keeps every sandbox unless --force)
anycompute providers                                  # adapters, capability table, live-run status, which keys are set
```

### run

- Uploads each `--file <local path>` (repeatable) into `/tmp/anycompute` under its base name before the command, through the same engine upload as the Python API. A missing local file or two files with the same base name raise `InvalidPathError` before create.
- Prints the run ID to stderr before the command starts.
- Passes the sandbox's stdout and stderr through. With `--json` there is no pass-through: stdout holds one JSON document.
- Writes `--read` files to `./anycompute-out/<run-id>/`, or to `<dir>/<run-id>/` with `--out <dir>`. It checks that the folder is writable before create. When it is not, the CLI raises `ConfigError` (exit 126) and makes no sandbox.
- Readback requires POSIX directory file descriptors. The CLI does not follow symlinks in the output path. On macOS, it translates the fixed `/tmp` and `/var` aliases before opening their physical directories. It can replace a regular file with one link, but refuses a second readback that resolves to the same destination. The first saved file stays in place.
- Exits with the sandbox command's exit code.
- When the job finished but the delete fails, the CLI writes stdout, stderr and the `--read` files first, then exits 125.
- A local output failure after a completed `run` or `gc` without a provider error exits 126. A cleanup failure retains exit 125 and its recovery hint, plus the local output error. The CLI still attempts stderr and file writes if workload stdout fails. If JSON stdout is unusable, it reports the error on stderr when possible and does not try to write a second document.

Tests:

- `--json` prints one JSON document on stdout with no pass-through, and the run ID reaches stderr before the command starts.
- An output folder that is not writable raises `ConfigError` (exit 126) with no create call, and `--out <dir>` writes under `<dir>/<run-id>/`.

### Exit codes

An anycompute error exits with its own code, with one line on stderr:

| Code | Error |
|---|---|
| 120 | `AuthError` |
| 121 | `RateLimitError` |
| 122 | `QuotaError` |
| 123 | `SandboxTimeoutError` |
| 124 | `ProviderError` |
| 125 | `CleanupError` |
| 126 | `InvalidTTLError`, `InvalidPathError` or `ConfigError` (including CLI usage errors) |
| 130 | `KeyboardInterrupt` (rule 5): after one interrupt and its cleanup, when a second interrupt stopped the cleanup, or when an interrupt during create gave a lost create |

- An error that carries a `cleanup_error` (rule 5) exits with its own code, and its stderr line and `error` object add the copyable gc command.
- For `RateLimitError` the stderr line gives `retry_after` and, when the adapter's caps record one, the provider's documented start rate with its source URL.
- These codes can collide with the command's own exit code (and 124 and 126 are also timeout and shell codes). So the README and `anycompute run --help` state the collision and tell scripts to read the `error` field of `--json`, not the exit code.
- `--json` mode uses the same exit codes.
- On every subcommand (`run`, `verify`, `gc`, `providers`), a usage error raises `ConfigError` (exit 126), with the JSON error under `--json`.
- On every subcommand, a top-level handler prints any other uncaught exception as a redacted `ProviderError` with `internal` true (exit 124).

Tests:

- An unknown flag on each subcommand (`run`, `verify`, `gc`, `providers`), a missing `--provider`, `--ttl abc`, and `gc` with both `--run` and `--all` each exit 126, with a JSON error under `--json`.
- A CLI test makes a subcommand raise an uncaught exception and checks a redacted JSON `ProviderError` with `internal` true and exit 124.

### Copyable gc command

On `CleanupError`, on a `ProviderError` with `sandbox_may_exist`, and on any error that carries a `cleanup_error`, the stderr line, and the `error` object in `--json`, include the exact command to copy: `anycompute gc --run <id> --provider <name> --dry-run`, with each value quoted with `shlex.quote`. For an ID that starts with `-`, use `--run=<id>` so the parser treats the ID as a value. A note says that dropping `--dry-run` deletes every sandbox of that run, running or not. If redaction or missing data makes the command unusable, `gc_command` is null and stderr explains why.

### JSON output

- `--json` prints the Result as JSON with an `error` field that is null on success, or the error type and a redacted message.
- stdout and stderr appear as text. Each read file appears as its path, size, SHA-256 and saved path, never its bytes.
- JSON does not contain nonfinite numbers. If two saved file paths redact to the same key, the report omits their ambiguous entries, keeps the files, and reports the output error.
- `verify --json` and `gc --json` print a JSON report with the same `error` field.

`run --json` keys and types:

```json
{
  "stdout": "hi\n",
  "stderr": "",
  "exit_code": 0,
  "files": {"out.txt": {"size": 2, "sha256": "<hex>", "saved_path": "anycompute-out/<run-id>/out.txt"}},
  "missing": [],
  "provider": "modal",
  "sandbox_id": "<id>",
  "run_id": "3f9c0a1b2d4e",
  "effective_ttl": 60,
  "runtime": null,
  "sandbox_seconds": 4.2,
  "outside_contract": false,
  "error": null
}
```

- On an error, `error` is `{"type", "message", "sandbox_id", "run_id", "retry_after", "sandbox_may_exist", "internal", "gc_command", "cleanup_error"}`. A key that does not apply is null, and `cleanup_error` is null or `{"type", "message"}`.
- With an error, a `run` report keeps the Result fields it has. This includes partial stdout and stderr on `SandboxTimeoutError` and the completed result on `CleanupError` or an output error after the job. Fields with no value are null. `provider` is null if none was supplied, and `run_id` is null if it could not be resolved.
- `gc --json` prints `{"provider", "run_id", "dry_run", "sandboxes": [{"sandbox_id", "run_id", "created_at", "action", "reason"}], "error"}`. `run_id` is null for `--all`. `created_at` is an ISO 8601 UTC string, or null when the list shows none. `action` is `deleted`, `kept`, `listed` (dry run) or `failed` (its delete raised `CleanupError`). `reason` is one of `run_match`, `expired`, `young`, `no_ttl_label`, `no_create_time`, `running`, `state_error` or `force`.
- `verify --json` prints `{"rows": [...], "summary", "anycompute_version", "source", "suite_version", "error"}`. Each row is `{"provider", "sandbox_id", "run_id", "effective_ttl", "exit_code", "output", "files", "cleanup", "lifecycle", "partial_caps", "runtime", "error", "sandbox_seconds", "outside_contract", "provider_sdk_version", "details"}`. `exit_code` and `cleanup` are `pass` or `fail`; `output` (stdout and stderr) and `files` are `pass` or `differs` for performed comparisons and null otherwise; `lifecycle` is `pass`, `partial`, `fail` or null without `--lifecycle`; `partial_caps` lists the provider's rule caps that are `partial`, built from its caps; `runtime` is `{"python", "arch", "api_host"}`. The `details` object gives the reference provider and both redacted values for differing output streams, SHA-256 hashes for differing files (null for a missing file), and the lifecycle reason or default-options note when applicable. The top-level `error` repeats the first row error, or the row that was interrupted.

### verify

`task.toml` names the command, the input files, the files to read back, the TTL, the expected exit code, and optional `[providers.<name>]` tables of provider options for the team's own image or resources. A table names environment variables, and their values come from the local environment (rule 11).

```toml
command = ["python3", "task.py"]   # required, an argument list
inputs = ["task.py"]               # local paths, relative to this file; default []
read = ["out.txt"]                 # sandbox paths to read back; default []
ttl = 120                          # default 300
exit_code = 0                      # expected exit code; default 0
compare = "all"                    # or "exit_code"; default "all"

[providers.modal]                  # optional; only keys on that adapter's allowlist
environment = ["MY_TOKEN"]         # variable names only; values come from the local environment
```
`anycompute verify` requires at least two distinct provider names. It runs the job once per provider, one provider at a time in the `--providers` order, and reports, per provider and field:

- **Exit code:** equal to the expected exit code in `task.toml`.
- **stdout and stderr:** equal as decoded text across providers. A difference is reported with both values, and `task.toml` can set `compare = "exit_code"` to compare only exit codes.
- **Files:** equal SHA-256 across providers for each read file. A file missing on one provider is a mismatch.
- **Runtime:** the Python version and CPU architecture per provider, detected here once per provider (outside verify, only by `run(..., runtime=True)`). A difference is reported, not failed, and a missing interpreter is reported as "unknown". The runtime record also names the provider API host that the adapter called, as the scheme and host only (userinfo stripped; a unix socket is recorded as `local`), so a `verify` run or a `runtime=True` run against an E2B-compatible host is never reported as just `e2b`.
- **Cleanup (after the job):** the adapter's state call reports the sandbox `gone` within the maximum grace period after the job, on every adapter. A list that no longer shows the sandbox is not proof, and a stopped sandbox is not a pass.
- **Partial caps:** each row's `partial_caps` lists the rule caps that the provider declares `partial` (a `labels_at_create` of false counts), so the report shows each partial rule. The rule caps are the Capability table's rule columns: `lifetime`, `pre_start_window`, `create_retry`, `exec_retry` and `labels_at_create`.

Tests: unit tests cover `compare = "exit_code"`, a stopped sandbox in the Cleanup field (fail), and the `partial_caps` of a fake provider with a `partial` lifetime.

**Lifecycle field (`--lifecycle`):**

- `verify` does not run the kill-client test by default. `--lifecycle` adds it as a separate **Lifecycle** field (killed client: ended within the `effective_ttl` plus the grace period), through the shared flow in `lifecycle.py`.
- The killed child runs the same marker-then-busy workload as the kill-client test, under the TTL from `task.toml`, and `verify` kills it only after the observer reads the `ready` file. The marker wait, the observer budget and the pass deadline below use the child's `effective_ttl`.
- The child run ID is `<parent run ID>-lc`, with the parent ID cut to 37 characters when it is longer. `verify` starts the child through the child launcher that `lifecycle.py` takes, and passes the launcher the child run ID. So the gc delete for that run ID finds the child's sandbox.
  - The default launcher runs `sys.executable -m anycompute run` (the package has a `__main__.py`) with `ANYCOMPUTE_RUN_ID` set to the child run ID.
  - Unit tests pass an in-process launcher from `tests/fakes/`. It runs the child's `run()` in a thread with `run_id=` set to the child run ID, through its own fake client over the same in-memory sandboxes. So the observer and the gc delete see the child's sandbox.
  - The in-process launcher's kill makes that client hang every later call, so the child never deletes its sandbox. The sandbox then follows the fake's scripted state sequence.
- The child always uses the provider's default options. When that provider's row used a `task.toml` provider table, the Lifecycle field shows "default options" next to its result, and a unit test checks that label. The README says the field checks the provider's lifetime and delete enforcement, not the team's image.
- The marker wait ends at the `effective_ttl`. With no marker by then, the Lifecycle field fails with "no marker by the TTL", and `verify` runs its gc delete for the child's run ID and confirms it. Observer calls get the time left before the TTL plus the grace period. A unit test with a fake that never shows the marker checks the failure and the gc call.
- `verify` takes a pacer token for that provider before it starts the child. This adds one sandbox and up to the TTL plus the grace period per provider on the user's accounts.
- After the check, `verify --lifecycle` runs the gc delete for the child's run ID, confirms the delete, and reports `CleanupError` when the delete fails. The `CleanupError` carries the child run ID, so its copyable gc command names `<parent run ID>-lc`.
- A unit test makes the fake delete fail after the Lifecycle check. It checks that the `CleanupError` and its gc command name `<parent run ID>-lc`, and that a 40-character parent run ID gives a 40-character child run ID.
- On a `partial` lifetime, a sandbox stopped by the deadline gives the Lifecycle value `partial`, and the delete must still confirm. A `partial` Lifecycle does not change the exit status.

**Summary line:**

- `verify` ends with one summary line. In plain mode it is the last stdout line; with `--json` it is a `summary` field, so the JSON stays parseable.
- The reference is the first provider in `--providers` with no error. When every provider raised an error, there is no reference, and the line lists each error.
- The line is "portable on <providers>" only when `verify` would exit 0. Otherwise it joins clauses with "; " in this order: "error on <provider>: <error type>", "fails on <provider>: <fields>" (Exit code against `task.toml`, Cleanup, Lifecycle), "differs on <provider>: <fields>" (stdout, stderr and files against the reference), then "runtime differs: <values>".
- The line never says "portable" alone when a provider has a `partial` lifetime. It adds the clause "partial lifetime on <providers>", and that clause always goes last.

**Exit status:** `verify` runs every provider even when one fails. It exits with the 120-126 code of the first anycompute error any provider raised (such as `CleanupError` from `--lifecycle`). With no such error, it exits 1 when any Exit code, stdout and stderr, Files, Cleanup or Lifecycle field fails, and 0 otherwise. `verify` reports a Runtime difference or a `partial` Lifecycle, and neither changes the exit code.

Tests (unit tests with fake providers):

- A `CleanupError` on one provider and a field mismatch on another exits 125; a field mismatch alone exits 1; a Runtime difference alone exits 0.
- With every provider in error, there is no reference, `verify` exits with the first error's code, and the summary lists each error.
- A provider with a `partial` lifetime gets a `partial` Lifecycle with exit 0, and a summary that is not "portable" alone, with the lifetime clause last.
- A failing run that includes a `partial`-lifetime provider puts the lifetime clause after every other clause.

### gc

`gc` follows the rules in contract rule 13. It exits 0 or the 120-126 code of its error.

## Adapters

Build order: docker first, right after the engine and the fake provider, so the conformance harness is built and debugged at $0 before any Modal run is spent (Next Step 4). Then Modal, boat.dev, then E2B and Daytona (Next Steps 5, 6 and 8).

The facts below come from the docs check of 2026-11-23 (public docs and SDK source only, no provider call). These items are checked again against each provider's docs when its adapter is written; that check is the starting point, not a guarantee:

- the exact lifetime knobs, their granularity, and their limits;
- the native sandbox states that the state call maps, and the list call's paging and label filter;
- whether the SDK returns command output as text or bytes;
- the provider's documented start rate with its source URL;
- the adapter's `provider_options` allowlist keys (boat.dev has no custom image, so it has no image key);
- the key environment variables and config-file fields for `key_sources`;
- the provider's isolation level.

Every provider SDK's own default command timeout (Daytona 10 s, boat.dev 30 s, E2B 60 s, docker 60 s) is replaced by the engine's budget. The conformance harness paces its launches of every adapter other than boat.dev at 30 creates a minute (the engine pacer works inside one process only).

### Modal

Official `modal` Python SDK 1.x, starting at 1.6.4. See the [Sandbox API](https://modal.com/docs/sdk/py/latest/Sandbox) and [Sandbox guide](https://modal.com/docs/guide/sandboxes).

- TTL maps to the sandbox timeout (a hard deadline, up to 24 hours). The sandbox uses gVisor and a `sleep infinity` entrypoint. The optional `image` (a registry reference) and `environment` are the only provider options.
- Labels map to tags (at most 10).
- Delete requests termination. A sandbox lookup and `poll()` confirm that it has finished. The finished record stays readable, and the list omits it.
- Read-back uses filesystem stat and binary read. `exec` passes literal arguments and requests bytes from both streams.
- A sandbox needs an App. The adapter looks up one App per process (`App.lookup("anycompute", create_if_missing=True)`) inside its first waited call, under that call's budget, and passes its `app_id` to every list. The adapter holds no lock across the lookup; a lock guards only the stored `app_id`.
- Its gRPC retries cannot be turned off, so `create_retry` and `exec_retry` are `partial`.
- Its `key_sources` cover the SDK token and OAuth environment variables and the corresponding fields in every profile in the config file. The config file is the file at `MODAL_CONFIG_PATH` when it is set, else `~/.modal.toml`. A unit test checks that the engine redacts a key in a non-default profile of a `MODAL_CONFIG_PATH` file.
- Its unit tests stub the SDK exceptions. The SDK uses `ResourceExhaustedError` for both quotas and rate limits, so the adapter maps an explicit quota or rate-limit message and reports other cases as `ProviderError`.
- Modal is the first cloud adapter planned for live conformance. The runner reserves for two allocations per create. The SDK reuses an idempotency key across retries. Exclude Modal from scheduled and manual runs until the project can verify or enforce the allocation bound.

### E2B

Official `e2b` Python SDK 2.64.0. See the [Python SDK source](https://github.com/e2b-dev/E2B/tree/main/packages/python-sdk) and [sandbox lifecycle guide](https://e2b.dev/docs/sandbox/overview).

- TTL maps to the sandbox timeout. `ttl_max_s` is 3600 in v0.1: E2B allows 1 hour on its Hobby plan and 24 hours on Pro (docs.e2b.dev/sandbox, checked 2026-11-23), and the adapter cannot read the plan. A longer TTL raises `InvalidTTLError` before create.
- The default sandbox size is open: the docs give 2 vCPU and 512 MiB for the base template, and the pricing page gives 2 vCPU and 4 GiB (checked 2026-11-23). The spend reservation uses the larger size until the adapter's docs check settles it.
- Labels map to `metadata`.
- Delete maps to kill.
- The adapter explicitly selects kill on timeout. The lifetime and kill-client tests pass only when the sandbox is gone, not paused.
- The adapter disables control-plane response retries. The SDK still retries failures before a request is sent, without replaying delivered requests. The adapter declares these transport retries in its capabilities.
- Read-only observer attachment uses the pinned SDK's sandbox GET response. The public connect method can resume a paused sandbox. Commands use POSIX quoting and the SDK's separate output streams.
- Project live conformance is disabled pending initial validation through protected CI.

### Daytona

Official `daytona[v0-sdk]` Python SDK 2.1.0 through the `v0` namespace. See the [SDK reference](https://www.daytona.io/docs/en/python-sdk/) and [sandbox lifecycle guide](https://www.daytona.io/docs/en/sandboxes/).

- TTL maps to `ttl_minutes` (whole minutes, so `ttl_unit_s` is 60). The Daytona SDK reference documents it as a wall-clock limit from creation: Daytona destroys the sandbox at that limit, even a stopped one (checked 2026-11-23). The adapter sets `auto_stop_interval` to the same minutes and creates the sandbox as ephemeral.
- Its lifetime stays `partial` until the live lifetime and kill-client tests show a delete at the TTL (see Declared gaps).
- Labels map to `labels`.
- Delete submits the request with the remaining cleanup budget. The engine confirms independently.
- The adapter uses the `daytona-small` snapshot by default. The `snapshot` and `environment` options may override the default workload setup.
- V0 session exec returns separate stdout and stderr. Each command runs in a fresh POSIX shell in the requested working directory. The session lives only for the lifetime of its sandbox.
- The SDK does not expose a retry control for its v0 transport. Its reconnect retries are declared partial. Project live runs await initial validation through protected CI.

### boat.dev

Uses the [synchronous Boat SDK](https://docs.boat.dev/sdks/python), pinned to `boat-sdk` 1.13.0. The [API](https://docs.boat.dev/api/v1) offers full VMs and fixed images. The free trial requires a card.

- TTL maps to `ttlSeconds`. This is a stop, so the lifetime is `partial`.
- `ttl_max_s` is 480 in v0.1. boat.dev limits a synchronous command to 600 seconds (boat.dev limits page, checked 2026-11-23). So the exec budget (the TTL plus the 120-second grace) never passes it, and the adapter has no detached-command path.
- Labels map to `labels` on create. List uses the label filters and reads each cursor page, with a local check of every returned label and creation time.
- Credentials come from `BOAT_API_KEY`. Creates request private VMs with provider account secret injection and monitoring disabled. The adapter allows explicit `environment` variables and disables SDK transport retries.
- The adapter sends a quoted shell command for the requested working directory. It checks that read-back paths name regular files, then uses the [binary file endpoints](https://docs.boat.dev/api/reference/agent/read-sandbox-file). Direct reads and writes are limited to 5 MiB.
- Delete sends the confirm header and returns on the 202. The engine polls the returned deletion operation within the 120-second cleanup deadline. The adapter keeps the operation ID in memory during cleanup. If a delete may have been accepted and its receipt is lost or invalid, a not-found response on retry leaves the purge unconfirmed.
- The suite paces boat.dev creates at 6 a minute, half of the 12 starts a minute that the $20 plan allows, which is also its `creates_per_minute` cap. Project live runs remain disabled pending credentials and conformance.

### docker

Official `docker` Python SDK, local.

- A detached container with auto-remove, the three labels, and one image pinned by its multi-architecture index digest: `python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b` from `python:3.12-slim`. It runs on amd64 runners and arm64 Macs, and has `sh`, `mkdir`, `sleep` and `python3`.
- The adapter always sets the entrypoint to `sleep <ttl>`, whatever image a user passes. So PID 1 is always the timer, and the Docker daemon ends and removes the container at the TTL even after a client SIGKILL (a hard deadline once started). An image with no `sleep` fails at create with `ProviderError`.
- Docker creates and starts a container in two calls, so a client that dies between them leaves a container with no timer. That window is declared in docker's own `pre_start_window: partial` cap and docs, while its lifetime stays hard. gc deletes such a container: `gc --all` treats it as expired by create time plus TTL plus grace, and deletes a docker container only while it is not running. A unit test interrupts between create and start.
- A start that fails after create (for example an image with no `sleep`) runs the delete and confirm before the error leaves (rule 5).
- The engine calls the adapter's `prepare` before create as its own waited call. The adapter checks for the exact image locally and pulls it only if missing. Its budget is the TTL plus the maximum grace period (rule 2), because the TTL counts only from create. A pull that reaches its budget raises `ProviderError` (exit 124) with no create call.
- A registry pull limit or other pull failure during the image pull raises `ProviderError`, not `RateLimitError`, because it is not the provider's start rate.
- The TTL has no provider maximum on docker.
- Delete maps to `remove(force=True)`, which also removes a container that was created but never started. A 404 counts as a successful delete request. A 409 counts as a successful delete request only for "removal already in progress". The engine's state call confirms. Recorded-response unit tests cover the 404 and both 409 cases.
- The client comes from docker-py's `from_env` with its context fallback, so the adapter finds the same daemon as the user's own `docker` command: `DOCKER_HOST`, then the current docker context, whose default is the default socket. There is no custom discovery code. A unit test checks that `DOCKER_HOST` wins and that the current context is used when it is unset. docker-py 7.2.0 (released 2026-07-09) is the first release whose `from_env` takes `use_context=True` by default; 7.1.0 lacks it (docker/client.py at both tags, checked 2026-11-23). So the docker extra requires `docker>=7.2`.
- An unreachable daemon raises `ProviderError` with a one-line hint.
- The client is created with `timeout=None` (docker-py defaults to 60 seconds), so the engine's budget controls each call. Redirects are not followed, so a redirected create or exec request cannot be replayed by the HTTP client.
- docker-py asks the daemon for its API version while it builds the client. So the adapter builds the client inside its first waited call (the image pull in a run; list or state when gc or the observer calls first), under that call's budget. A hang there raises `ProviderError` at that budget, before any container exists. A unit test with a hung version request checks that the run returns at the pull's budget with no create call.
- Exec goes through the engine's waited-call helper, files go through archives, and list filters by label and shows a create time.
- Every container also gets `security_opt=["no-new-privileges"]` and `pids_limit=4096` (processes and threads). Neither is on the `provider_options` allowlist, and a recorded-create unit test checks both.
- The adapter accepts only `image` and `environment` in `provider_options`. Docker client proxy settings are not added to the sandbox environment.
- Its caps declare `isolation` as a container on the host kernel. The README says that docker is not a boundary for untrusted code, unlike a provider that runs each sandbox in its own VM.
- It needs no account or key. So `ci.yml` runs the full live conformance suite on it on every pull request at $0 (the lifecycle tests, that is the lifetime, kill-client and delete-verification tests, plus the working-directory, argument and exit-code tests), and a user's first run costs nothing.
- The docker job pulls the pinned image in a setup step before the tests, so no test spends its timeout on a pull.

## Test layout

- The test framework is pytest with pytest-timeout.
- Pytest excludes `live_conformance` tests by default, even when a change-based selector supplies their paths. The Docker and cloud jobs select them with `-m live_conformance`. Selecting the tests does not authorize provider calls. The fixture still requires an explicit provider and, for cloud, a counted attempt on main.
- `tests/unit/` tests the engine rules against the fake provider (`tests/fakes/`), and the adapter mappings from recorded responses, with no keys. Transport-level fakes and plain recorded fixture files test the HTTP adapters. CLI subprocess tests run the fake launcher. Each unit test has a 30-second timeout. Rules that exist only in the Python API (the `Sandbox` object and its `close()`, the `cleanup_error` attribute, thread safety) are tested only here.
- `tests/conformance/` holds the live tests. They drive the CLI (`anycompute run --json`, `verify`, `gc`) as a subprocess and read only its JSON and exit codes about the job. A separate observer checks provider state and reads the kill-client marker file, and the harness runs the kill-client flow from `lifecycle.py`. `tests/conformance/conftest.py` gives each live test 540 seconds (TTL 120 + grace 120 + the rule 3 state call 120 + cleanup 120 + a 60-second margin) for each sandbox the test creates in sequence. A test that runs `verify` gets 540 seconds per provider in its `--providers` list, and 540 more per provider with `--lifecycle` for the child sandbox. The test sets this timeout with `pytest.mark.timeout`. So a `verify --lifecycle` test on two providers gets 4 x 540 seconds. The job limits stay 30 minutes (`ci.yml` docker job) and 90 minutes (`conformance.yml`). A later TypeScript CLI can be tested by the same suite and observer, so they can become the language-neutral runner.

## Repository layout

```
pyproject.toml                  # uv; console script "anycompute"; requires-python >=3.11; extras modal, e2b, daytona, boat, docker (docker>=7.2), all; dev group: pytest, pytest-timeout (30 s per unit test, 540 s per sequential sandbox per conformance test); HTTP tests use transport-level fakes
src/anycompute/__init__.py     # re-exports only; __all__ pins the public API
src/anycompute/__main__.py     # python -m anycompute, used for the verify --lifecycle child
src/anycompute/engine.py       # run, Sandbox, every contract rule the Python API enforces, gc selection, redaction, the clock and the waited-call helper
src/anycompute/verify.py       # verify checks and report
src/anycompute/lifecycle.py    # the kill-client flow and its pass rules, shared by verify --lifecycle and the conformance harness
src/anycompute/contract.py     # types only: Result, ExecResult, SandboxState, error types, Provider protocol and caps
src/anycompute/cli.py          # flags and usage errors, local --file and task.toml input reads, output folder check, run ID print, output: run, verify, gc, providers; --json with the error field; the top-level handler; only CLI rules
src/anycompute/providers/__init__.py   # the PROVIDERS registry and the capability table built from caps
src/anycompute/providers/{e2b,modal,daytona,boat,docker}.py   # thin adapters; each imports its SDK only when used
tests/fakes/                    # the fake provider and its CLI test launcher; not shipped
tests/unit/                     # engine rules against the fake provider; adapter mappings from recorded responses; no keys
tests/conformance/              # live tests that drive the CLI with --json, plus an observer; kill-client and delete verification; conftest.py sets the timeout of 540 s per sequential sandbox
tests/conformance/runner.py     # the spend runner: reservation per CLI launch, duplicate check, $2 per-run budget, stop on cleanup failure, run-limit count
tests/conformance/prices.toml   # Next Step 1 price record: one table per provider, each value with its source URL and checked date
.github/workflows/ci.yml        # unit tests on every pull request, Python 3.11 to 3.15, plus the full live conformance suite in a docker job (timeout-minutes: 30 on that job)
.github/workflows/conformance.yml  # live tests: weekly (the live_run adapters other than docker) and manual dispatch (adapters as inputs); a keyless run-limit gate job, then keyed test jobs in a main-only protected environment, each with a pre-run gc sweep, a step timeout and an always() gc step (timeout-minutes: 90)
.github/workflows/release.yml   # on a v* tag: a build job, a publish job in the pypi environment (trusted publishing, signed release), then a $0 smoke run of the published package on docker
```

## Distribution

- **Package:** `anycompute` on PyPI, with the `anycompute` console script and one extra per provider. Users install only the SDKs they use: `pipx install 'anycompute[modal]'`, or run it with `uvx --from 'anycompute[modal]' anycompute run ...`. `anycompute[all]` installs every provider SDK. A missing extra raises `ConfigError`, which names the extra to install. Python 3.11 or newer.
- **Pipeline:** GitHub Actions.
  - Unit tests and the docker conformance suite on every pull request, with no keys.
  - Live conformance once a week and on manual dispatch, in a protected environment that holds the project's provider keys and allows deployments only from main, under the spend rules above.
  - Every workflow pins third-party actions by commit SHA and sets least-privilege `permissions`.
  - `release.yml` runs on a push of a `v*` tag. A build job with no `id-token` makes the sdist and the wheel. A publish job in the `pypi` environment holds the only `id-token: write` and publishes to PyPI through trusted publishing (OIDC) with signed attestations. The `pypi` environment allows only `v*` tags, and a maintainer must confirm each deployment. The smoke run follows the publish.
- **Post-release smoke run:** after each publish, `release.yml` installs `anycompute[docker]==<version>` from PyPI on a clean runner and runs `anycompute providers` and one docker job. A classifier in the smoke job sorts each failure into one of three outcomes:
  - The install retries every 20 seconds for up to 10 minutes, because PyPI's index can lag after an upload. An install that never succeeds fails the job as "install failed", and the README's release runbook says to check PyPI and re-run the job.
  - Only a package failure after a successful install makes the runbook yank that version and ship a patch. A package failure is one of these:
    - `anycompute providers` fails.
    - The docker job returns a wrong result.
    - The docker job raises a `ProviderError` with `internal` true.
    - The docker job raises an error other than `ProviderError`.
  - A `ProviderError` with `internal` false from the docker job (daemon unreachable, image pull failure or throttling) fails the job as "infrastructure", and the runbook says re-run. When a re-run fails the same way, the maintainer investigates before any yank.
  - Classifier unit tests cover each outcome, including one test for a `ProviderError` with `internal` false (infrastructure) and one with `internal` true (package failure).
- **Results:** each conformance run writes a report (pass, fail, and the measured time to end and delete, per adapter and rule) that the README links. The public report publishes results by provider name; if a provider objects, its results are removed (see Live runs). Every conformance and verify report also records:
  - `anycompute_version`;
  - `source`: `user-run` by default; `project-run` only when `conformance.yml`, or the docker job in `ci.yml` on main, sets `ANYCOMPUTE_REPORT_SOURCE=project-run`;
  - `provider_sdk_version` in each provider row;
  - `suite_version`: the `SUITE_VERSION` constant in tests/conformance plus the commit SHA in a conformance report; null in a verify report.

  These fields let vendor-run results join later with no format break.

## Success criteria

Version 0.1 is done when all of these are true:

1. `uv run --extra modal --extra boat anycompute run` (from the local build, before the first publish) gives the same result shape on Modal and boat.dev for the same command.
2. The conformance suite passes on Modal, including the kill-client test and delete verification, in the public CI on its weekly schedule.
3. `anycompute verify` produces a report for one real workload on two cloud providers, Modal and boat.dev.
4. boat.dev passes the suite with its `partial` rules declared.
5. E2B and Daytona each pass the live conformance suite in the public CI, with any rule the provider cannot enforce declared `partial`. If a provider objects to the project's runs before the release, its runs stop, its results are removed, and its adapter ships as a "not run live" adapter with its fixtures from the provider's published docs (see Live runs), marked as such in the capability table and the README.
6. Version 0.1.0 is on PyPI through trusted publishing, with a signed release.
7. The full live conformance suite on the docker adapter, including the lifecycle tests, is a required check on every pull request (set by branch protection on main), and it passes on main.
8. The Next Step 9 features ship with their tests. The Next Step 4 docker work (the docker adapter, its pre-start window, and the kill-client marker workload) ships with its unit tests. The docker adapter and the marker workload are also proven live by the docker suite in criterion 7, and the pre-start window by its unit test only.

## Open question

- **Framework plug-ins after v0.1:** a LangChain Deep Agents backend (its protocol needs only `execute()`), an OpenAI Agents SDK custom client, a Harbor environment type, a NeMo Gym sandbox provider.

## Next Steps

1. **Prices.** Record in `tests/conformance/prices.toml` each provider's default-size price, start fee (or zero at the suite's pacing), stopped-storage price, billing granularity and minimum charge, each with its source URL and checked date, for the runner's reservation. Check that a run's worst case fits under $2 per adapter: 15 sandboxes x compute for 240 s, plus start fees, plus one week of stopped storage on boat.dev and Daytona. On Modal, count 30 sandboxes, because its reservation counts two sandboxes per create. If it does not, lower the sandboxes per run or raise the per-run budget and recompute the monthly totals.
2. **Engine first.** Add the layout above to this repository. Add the fake provider (in `tests/fakes/`, with its launcher), the contract types, the Provider protocol and the engine first.
3. **Unit tests.** Write the unit tests for `run`, `Sandbox`, the error mapping (recorded responses), the cleanup and expiry outcomes, and the capability table against the fake provider, plus these engine rules:
   - each of the 9 error types;
   - client timeouts, the waited-call helper on a swapped clock, the cleanup deadline and the poll schedule;
   - the gc rules;
   - redaction;
   - the CLI exit codes and JSON (a command that exits 121 against a `RateLimitError`, told apart only by the `error` field; stdout, stderr and the `--read` files written before exit 125 on `CleanupError`);
   - text decoding;
   - runtime detection (no detection call unless `runtime=True`; "unknown" for a missing interpreter);
   - 50 concurrent `run()` calls that check distinct sandbox IDs, correct run and TTL labels, and a confirmed delete for each.
4. **Docker first.** Write the docker adapter, with its pre-start window declared `partial` and its unit test. Write the spend runner and its unit tests (a fake delete that fails: no further launch, `gc --run`, nonzero exit; a launch that would pass $2 is refused; the run-limit, duplicate-create and non-JSON cases in Spend rules). Write `conformance.yml` with its keyless run-limit gate job, the attempt check in each test job, the pre-run `gc --all` sweep, the step timeout and the `always()` gc step. Pin its third-party actions by commit SHA, and create its protected environment with a main-only deployment branch rule. Write the conformance harness and the observer, including the kill-client marker workload, the observer's read-based wait for the `ready` file, and the harness unit test. Run the harness on docker locally and in `ci.yml` until it passes. Docker runs cost $0 and have no run limit, so the harness is debugged before any Modal run is spent. Branch protection waits for Next Step 9.
5. **Modal.** Write the Modal adapter and run the live conformance tests from Next Step 4 on it, driven through the CLI: the same suite, including the kill-client test with its marker.
6. **boat.dev.** Add the boat.dev adapter with its declared `partial` rules and `ttl_max_s` 480.
7. **verify.** Write `verify` and its report. Run the fixed workload once on Modal and boat.dev with `adapters=modal,boat` and `verify=true` in a manual dispatch of `conformance.yml`. The verify job replaces that dispatch's provider matrix and counts the attempt for both adapters. It reserves both creates before the comparison, sweeps both adapters, checks the workload and duplicate count against the report and provider inventory, and cleans up both adapters even after a failure.
8. **E2B and Daytona live.** Add the E2B adapter (`ttl_max_s` 3600) and the Daytona adapter (`ttl_minutes`; `partial` until the live tests show a delete at the TTL). Run the live conformance suite on them, driven through the CLI as in Next Step 5.
9. **Post-Modal features and branch protection.** After the Modal conformance run passes, set branch protection on main with the docker conformance job as a required check, and build these features. Add a CLI unit test for each of the copyable gc command line, the `RateLimitError` line and the verify summary line, and assertions on the report version fields, the API host in the runtime record and `Result.sandbox_seconds` in the `--json` and verify output.
   - the create pacer;
   - `provider_options` and `sb.native`;
   - report version fields;
   - the provider API host in the runtime record;
   - `Result.sandbox_seconds`;
   - a copyable gc command on cleanup failure;
   - a useful `RateLimitError` line;
   - the README section "What your provider can see", and the User-Agent;
   - a one-line verify summary;
   - the gc hint after an interrupted cleanup;
   - the opt-in debug logger, with a unit test that plants a key in a logged error and checks each LogRecord's message, args and exc_info, not only the formatted text, and a CLI test of the silent default and `--verbose`;
   - the post-release smoke run on docker, its classifier and the yank-and-patch runbook;
   - late-create reconciliation;
   - `task.toml` provider tables for `verify`.
10. **Release.** Set up trusted publishing (the `pypi` environment with its tag rule and maintainer confirmation) and release 0.1.0, after Next Steps 6 to 9.

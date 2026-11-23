# anycompute design (v0.1)

Status: approved design for anycompute v0.1. No code exists yet.
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
  - the commands it runs in the sandbox (`mkdir -p /tmp/anycompute`, and the runtime detection commands when runtime is detected);
  - the command `verify --lifecycle` runs;
  - each adapter's own sandbox commands and resources (for example the docker image pull and its `sleep <ttl>` entrypoint, and the boat.dev detached-command wrapper and its poll commands), which each adapter spec lists.

### Redaction

- Error messages, the JSON output, `verify` reports and the public report replace the value of every provider key (from an environment variable or a provider config file such as Modal's profile) with `***`.
- The values in `provider_options[*]["environment"]` are redacted the same way, because users pass sandbox secrets there.
- Empty values are skipped. A value shorter than 8 characters is replaced only where it appears as a whole token, so short values do not damage unrelated text.

### Debug logger

The standard Python logger `anycompute` writes one DEBUG line per step (validate, pace wait, create, exec start and end, delete, confirm, cleanup error) with the run ID, sandbox ID, provider and duration, through the same redaction. It is silent by default, `--verbose` prints it to stderr, and it sends nothing anywhere. A unit test plants a key in a logged error and checks each LogRecord's message, args and exc_info, not only the formatted text.

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
- These rules cover the project's own runs: CI, local runs on the project's accounts, and the public report. A user's runs on the user's own account are the user's business.
- An adapter that does not run live is tested only against fixture responses built from the provider's published API reference, OpenAPI file, or SDK source and its own test fixtures. The project captures no fixture from live calls on its own accounts for it. A contributor's recording from the contributor's own account is accepted when the pull request says so. The capability table and the README label such an adapter "not run live". When an adapter runs live, the project re-records its fixtures from live runs.
- docker needs no provider account. It costs $0 and is exempt from the spend runner's run limit.

### Never test or probe a provider's systems

The conformance suite uses only ordinary public API calls (create, exec, files, delete, get, list) at a paced rate on the project's own accounts. It never provokes a provider failure: rate-limit, quota and auth error mappings are proven only with recorded or fake HTTP responses in unit tests.

### Spend rules for the conformance runner

- **Monthly budget:** $50 per provider per month on the project's accounts. CI keeps no spend record, so the per-run budget, the run limit and the cleanup-failure stop below keep this budget. It is a hard cap only where the provider has its own spend limit.
- **Worst-case reservation:** before each create, the conformance runner reserves the sandbox's worst-case cost: the TTL plus the maximum grace period, at the provider's price for the default size, plus any start fee. On partial-lifetime adapters (boat.dev, Daytona) the reservation also adds one week of the provider's stopped-storage price per sandbox.
- **Per-run budget:** the runner refuses a create that passes a budget of $2 per adapter per run.
- **Run limit:** the workflow allows the weekly run plus at most 4 manual dispatches a month per adapter, so at most 9 runs: $18 per adapter a month, or $38 for boat.dev with its $20 plan, under the $50 budget. Local runs of an adapter or of `verify` on the project's accounts (Next Steps 5 to 8, and any later live run) go through the same runner and count as manual dispatches. docker is exempt.
- **Run size:** the scheduled suite runs once a week. One run creates at most 15 sandboxes per adapter with a TTL of at most 120 seconds, so its worst case per adapter is 15 x (120 + 120) seconds = 1 sandbox-hour. That is about $0.20 at small-size prices (checked 2026-11-23), plus boat.dev's start fees and, on boat.dev and Daytona, one week of stopped storage per sandbox. The $0.20 excludes Modal until Next Step 1 records its price.
- **Stop on cleanup failure:** a failed delete keeps billing past the reservation. So the runner stops creating in a run after the first unresolved `CleanupError`, runs gc for that run, and fails the workflow.
- **Free credits first:** E2B one-time credit, Modal $30 a month, Daytona $200 with no card (checked 2026-11-23). Use a hard limit or prepaid wallet where the provider has one, and clean up by run ID after every run.
- **boat.dev** costs at least $20 a month and joins CI only when its adapter starts.
- **Prices with a source:** Next Step 1 records each provider's default-size price with a source. No Modal price is recorded yet.

### Release path

Trusted publishing from CI and signed releases from the first release, because users give the SDK their provider keys.

## The stateless contract (v0.1)

### One engine

- Every contract rule that the Python API enforces lives once, in the engine behind `run()` and `Sandbox` (`__init__.py`). The engine also holds the gc selection rules, the verify checks and redaction.
- `contract.py` defines the types the engine uses (Result, the errors, the Provider protocol and caps) and builds the capability table from the caps.
- Adapters translate calls, which includes quoting an argument list for a shell-string API and turning off SDK retries. They hold no policy.
- `cli.py` parses flags, reads `--file` paths and `task.toml` input files from local disk into the `files=` dict (with the missing-file and base-name-clash checks), prints the run ID to stderr, and formats output. These CLI behaviors are its only rules.

Each adapter translates about ten calls: create with TTL and labels, exec, write, read, delete, state, list, error mapping, and `api_host()` for the runtime record. It declares these caps:

| Cap | What it declares |
|---|---|
| TTL unit and maximum | The provider's lifetime granularity and limit |
| Labels at create | Whether labels go in the create request |
| Lifetime | Hard or `partial` |
| `pre_start_window` | An untimed window before the lifetime starts |
| Idle timer | Whether the provider ends or stops the sandbox at its TTL, or only after idle time |
| `create_retry`, `exec_retry` | Whether its SDK retries a create or an exec |
| `creates_per_minute` | Its documented creates per minute, with source and date, or none |
| `provider_options` allowlist | The `provider_options` keys it accepts |
| `per_call_timeout` | Whether its SDK has a per-call timeout |
| `sdk_package` | Its SDK package name, for the report's `provider_sdk_version` |
| List create time | Whether its list shows a create time |
| `isolation` | Its isolation level from the provider's docs, for example a microVM, a VM, or a container on the host kernel |

The capability table is built from those caps joined with each adapter's live-run status ("not run live" where it applies). It is never written by hand.

**Thread safety.** The engine keeps no shared mutable state except the per-process run ID and one locked create pacer per provider. Each `Sandbox` owns its own provider client, or the adapter guards a shared client with a lock, so `run()` and separate `Sandbox` objects are safe to use from many threads.

**Sandbox object.** `Sandbox` has `write(path, bytes)`, `exec(argv)` (it returns `ExecResult(stdout, stderr, exit_code)`), `read(path)` (bytes, or None for a missing file) and `native`. After the `with` block it exposes `sandbox_seconds` and `outside_contract`.

**No async API in v0.1.** A native `arun()` and `AsyncSandbox` are deferred (`TODOS.md`). Async callers wrap `run()` in a thread until then. Streaming reads for large output files are also deferred.

### Contract rules

| # | Rule | Summary |
|---|---|---|
| 1 | Lifetime | Every create sends a provider-enforced TTL (default 300 s) |
| 2 | No client timers | Client timeouts are always longer than the provider's deadline |
| 3 | Expiry during a command | A sandbox that ends mid-command raises `SandboxTimeoutError` |
| 4 | Delete | A real delete, confirmed within 120 s, or `CleanupError` |
| 5 | Cleanup error | Output is kept and the original exception is never hidden |
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
- The TTL counts from create.
- The engine rounds it up to the provider's granularity from the adapter's caps (for example whole minutes on Daytona) and reports `effective_ttl` in the Result.
- A TTL below 1 or above the provider's maximum raises `InvalidTTLError` before create. It is never clamped.
- The adapter passes the TTL to the provider's own lifetime setting, never to a client timer.

Test: the live test runs `anycompute run --json --ttl 120 -- sleep 600`. The observer checks the sandbox state through the provider's API from outside. It passes only when the provider ends the sandbox (gone or killed, never paused) by the TTL plus the maximum grace period. On a `partial` lifetime, stopped passes.

#### 2. No client timers

What the SDK does:

- The engine gives every work call (create, write, exec, read) a client timeout of the remaining TTL plus the maximum grace period, always finite and never off, so no client-side timeout ends a command before the provider's deadline.
- Where an adapter's caps say `per_call_timeout` is false (Modal's gRPC calls, docker's client with no read timeout), the engine waits on every call of that adapter in a worker thread up to that budget.
- At that budget the engine treats a work call as a fired client timeout (the state check in rule 3) and leaves the abandoned call to finish in the background.
- Each waited call runs in its own daemon thread, never in a `concurrent.futures` executor, because Python joins an executor's workers at exit and a hung call would then hold the process past its budget.
- A create that reaches its budget is a lost create: `ProviderError` with `sandbox_may_exist` and the copyable gc command (see CLI). If the abandoned create later returns a sandbox ID while the process still runs, the engine deletes it under the cleanup clock and logs it (late-create reconciliation).
- The same wait applies to delete and confirm calls, with the time left on the 120-second cleanup deadline as the budget. A delete or confirm call that reaches it raises `CleanupError` (rule 4), with no state check.
- `anycompute gc` has no TTL. Each of its list calls, and each state call it makes to select sandboxes, gets 120 seconds (the cleanup clock's length) through the same wait where the SDK has no per-call timeout. A call that reaches it raises `ProviderError` (exit 124) naming the provider. gc's delete and confirm calls keep the 120-second cleanup deadline.

Tests:

- The lifetime test uses a TTL longer than every provider SDK's default command timeout (Daytona 10 s, boat.dev 30 s, E2B 60 s).
- A unit test covers a hung fake exec.
- A unit test with a fake adapter that has no per-call timeout checks that the worker-thread wait returns at the budget and raises.
- A CLI test whose fake call never returns checks that the process exits at the budget with the documented code.
- A unit test makes a fake create finish after its timeout and checks that the late sandbox is deleted and logged.
- A unit test with a hung fake list checks that gc exits 124 at 120 seconds on a fake clock.

#### 3. Expiry during a command

What the SDK does:

- When the provider ends the sandbox at the TTL while a command runs, `run()` and `Sandbox.exec()` raise `SandboxTimeoutError` with the sandbox ID, the run ID, and any partial stdout and stderr that the provider returned.
- If a client timeout fires anyway, the engine asks the provider for the sandbox state: ended or stopped raises `SandboxTimeoutError`; still running raises `ProviderError`. The delete and its confirm run either way.
- When an exec returns an exit code that can mean the sandbox ended (137, or none, as docker's exec does when its container stops), the engine also asks the state call. A sandbox that is gone or stopped raises `SandboxTimeoutError`. An exit code of none while the sandbox still runs raises `ProviderError`.
- Every anycompute error raised after a create carries `sandbox_seconds` (None when no delete confirmed). For an error raised inside a `Sandbox` block, `__exit__` sets it on the error in flight after the delete confirm.

Tests:

- The live lifetime test checks that `anycompute run --json` reports `SandboxTimeoutError` in its `error` field and exits 123 on every adapter.
- A unit test with a slow fake exec covers both client-timeout branches.
- A recorded-response unit test covers an exec that returns 137 after its container stopped.

#### 4. Delete

What the SDK does:

- `close()` and the end of `run()` delete the sandbox; they never pause or stop it. They return only after the provider confirms the delete.
- The delete and every confirm poll share one deadline of 120 seconds from the start of cleanup. Each request gets the time left, and a 429 during the poll backs off and keeps polling.
- They raise `CleanupError` when that deadline passes or the delete fails, on every adapter.
- An adapter's delete holds no confirm or poll logic of its own. If it calls an SDK wait (Daytona `wait=True`, Modal `terminate(wait=True)`), that wait gets only the time left on that deadline, and the engine still confirms through the adapter's state call.
- A failed delete is never silent.

Tests:

- After `anycompute run --json` exits, the observer's list no longer shows the sandbox, or its state call reports it gone.
- A unit test with a hung fake delete raises `CleanupError` at 120 seconds on a fake clock.

#### 5. Cleanup error

What the SDK does:

- `CleanupError` carries the completed `Result` (when the job finished), the sandbox ID and the run ID, so the user keeps the output.
- In `Sandbox.__exit__` with an exception already in flight, the original exception propagates and the cleanup failure is chained to it, never hidden.
- Cleanup also runs when a `KeyboardInterrupt` arrives during a command.
- If a second `KeyboardInterrupt` stops the cleanup, the CLI prints the copyable dry-run gc command (see CLI) and exits 130, and the Python API re-raises `KeyboardInterrupt` with the run ID and the sandbox ID attached.

Tests:

- A unit test with a fake provider whose delete fails checks both cases, and a `KeyboardInterrupt` during exec.
- A second `KeyboardInterrupt` during cleanup makes the CLI print the dry-run gc command and exit 130, and makes the Python API re-raise `KeyboardInterrupt` with the run ID and the sandbox ID.

#### 6. Result

What the SDK does:

- `run()` returns `Result(stdout, stderr, exit_code, files, missing, provider, sandbox_id, run_id, effective_ttl, runtime, sandbox_seconds, outside_contract)`.
- A nonzero exit code is a result, not an error.
- `sandbox_seconds` is the measured time from the create request to the delete confirm, shown in `--json` and in each verify row with no price table. It is None in the Result that `CleanupError` carries and on every other path with no delete confirm (including a `Sandbox` whose delete failed in `__exit__`). `--json` prints it next to `error`, and a verify row for a provider that raised an error shows the error type and its `sandbox_seconds`.
- `sandbox_seconds` is described as an upper bound on billed time only for a provider whose recorded billing granularity is 1 second or finer with no minimum charge. For any other provider the docs give its rounding rule and minimum charge from Next Step 1 instead.
- `stdout` and `stderr` are text, decoded as UTF-8 with invalid bytes replaced (U+FFFD), the same rule on every adapter. Binary data travels through files, which stay bytes.
- `runtime` is filled only by `verify` or `run(..., runtime=True)`, and is `None` otherwise.

Tests:

- `exit 7` returns `exit_code=7`; stderr text arrives in `stderr`, not in `stdout`.
- One test sends invalid UTF-8 output and one binary file through `run`, `--json` and `verify`.
- A unit test checks `sandbox_seconds` on a fake clock, and None after a failed delete.

#### 7. Working directory

What the SDK does:

- The engine creates `/tmp/anycompute` in the sandbox after create (one `mkdir -p` through the adapter's exec) and passes it as the working directory to every exec.
- Uploads are written there, and read-back paths resolve from it.
- A read path that does not exist is left out of `files` and listed in `missing`.
- A sandbox path (a `files=` key, a read path, or a read-back path in `task.toml`) that is absolute or contains `..` raises `InvalidPathError` before create.
- A CLI `--file` value and a `task.toml` input file are local paths (a `task.toml` input resolves from the folder of `task.toml`). `cli.py` reads them from disk with the same missing-file and base-name-clash checks, and only the base name is used in the sandbox.

Tests:

- A conformance test uploads a file with `--file`, runs it, reads relative paths back, and reads one missing file, with the same result on every adapter.
- A CLI unit test covers an upload, a missing local file and a base-name clash.

#### 8. Arguments

What the SDK does: the API takes an argument list. An adapter whose provider takes one shell string quotes the list with `shlex.join` for a POSIX shell.

Test: a conformance test passes arguments with spaces, quotes and shell metacharacters and checks identical stdout on every adapter.

#### 9. Errors

What the SDK does:

- Every error subclasses `AnycomputeError`. There is one type per cause: `AuthError`, `RateLimitError` (with `retry_after` when the provider sends it), `QuotaError`, `SandboxTimeoutError`, `ProviderError`, `CleanupError`, and three caller-input errors that also subclass `ValueError`: `InvalidTTLError`, `InvalidPathError` and `ConfigError`.
- `ConfigError` covers:
  - an unknown provider name;
  - a missing extra;
  - a `provider_options` entry for an unknown provider;
  - an unlisted `provider_options` key;
  - a `creates_per_minute=` value that differs from an earlier one for the same provider;
  - a pacing rate that is not a positive number;
  - a run ID that does not match `[a-z0-9-]{1,40}`;
  - in a `task.toml` provider table, an `environment` variable that is not set locally, or a literal `environment` value table.
- The `ConfigError` message names the extra, the key or the value. For an `environment` entry it names the key and never the value, because a literal value may be a secret.
- A key is missing only when neither its environment variable nor the provider's own config file (Modal's profile) holds it. Then `AuthError` is raised before any provider call.
- No error carries a key value. The engine replaces each provider key value with `***` in the provider's error text, keeps that redacted text on its own error, and raises it outside the provider's exception handler, so no raw provider exception stays attached.

Tests:

- Unit tests cover each of the 9 types.
- Recorded or fake provider responses (401, 429, quota, other) from the provider's own API map to `AuthError`, `RateLimitError`, `QuotaError` and `ProviderError` on every adapter. docker's registry pull limit is `ProviderError` (see the docker adapter).
- A TTL of 0 and a TTL above the maximum raise `InvalidTTLError`, and an absolute path and a `..` path raise `InvalidPathError`, with no create call.
- An unknown provider, a missing extra, a `provider_options` entry for an unknown provider, an unlisted `provider_options` key, a conflicting `creates_per_minute=` and a pacing rate that is not a positive number each raise `ConfigError`.
- A missing key raises `AuthError`, and the fake provider records no call.
- A unit test plants a fake key, and a fake secret in `provider_options` environment values, in a fake provider error. It checks that both are absent from `str`, `repr`, a formatted traceback, the JSON output and the report, and that `__context__` and `__cause__` are None.
- No live test provokes a provider failure.

#### 10. Retries

What the SDK does:

- The SDK never retries a create automatically: a retried create after a lost response would make a second sandbox. It does not retry exec.
- Each adapter also turns off its provider SDK's own retries and its transport (HTTP or gRPC connection) retries for create and exec through the SDK's client options where the SDK allows it, never by changing the user's environment.
- An adapter that cannot (Modal today) declares `create_retry: partial` and `exec_retry: partial`. A duplicate it may create carries the same labels and TTL, so the provider ends it and gc finds it.
- The caller handles `RateLimitError` with its `retry_after`.

Tests:

- Unit tests check that a fake 429 on create raises once and makes one create call.
- Adapter tests for an HTTP-based SDK count requests at the HTTP layer with recorded responses, including a lost response.

#### 11. Provider options

What the SDK does:

- `run()` and `Sandbox()` take `provider_options={"<provider>": {...}}`.
- The engine sends only the active provider's entry, and only keys on that adapter's allowlist in its caps: resources, image or template, environment, and region (region only on an adapter whose list call covers every region, so gc can still find the sandbox).
- Lifecycle and label keys (timeout, auto-stop, pause-on-timeout, ephemeral, ttlSeconds, tags, metadata, labels) are never on an allowlist, so the TTL, the labels and the confirmed delete still apply.
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
- `outside_contract` is set on the Result and on the `Sandbox`.
- A `task.toml` `environment` list resolves from the local environment, and a missing variable and a literal value table each raise `ConfigError` with no create call.
- A `verify` run with a `task.toml` provider table shows `outside_contract` true in that provider's row, and a planted secret in one of its environment variables is absent from the report.

#### 12. Create pacing

What the SDK does:

- The engine holds each create until the provider's documented start rate allows it. It uses one locked token bucket per provider with a capacity of 1, so creates on one provider are at least 60/rate seconds apart (5 seconds at 12 a minute).
- The rate comes from the adapter's `creates_per_minute` cap.
- A user overrides it in two ways:
  - `ANYCOMPUTE_CREATES_PER_MINUTE` sets the rate for every provider in the process (providers with no documented rate are then paced too). The engine reads it once, at the first create.
  - `creates_per_minute=` on `run()` or `Sandbox()` sets that provider's rate for the process and wins over the environment variable for that provider.
- A later keyword call with a different value for the same provider, or any rate that is not a positive number (in the keyword or the environment variable), raises `ConfigError` (CLI exit 126).
- A provider with no documented rate and no override (Modal today) is not paced.
- Pacing works inside one process only, so the conformance harness paces its own subprocess launches. Nothing retries: a 429 still raises `RateLimitError` once.

Tests:

- A unit test runs 50 concurrent `run()` calls on the fake provider with a rate of 12 a minute on a fake clock and checks that creates are at least 5 seconds apart.
- Unit tests cover the override rules.
- A `verify --lifecycle` unit test on a paced fake provider checks that the killed child starts only after a pacer token.

#### 13. No local state

What the SDK does:

- Each create carries three labels in the create request itself: `anycompute=1`, `anycompute-run=<run-id>` and `anycompute-ttl=<seconds>`.
- The run ID is generated once per process, or set with the `run_id=` argument or the `ANYCOMPUTE_RUN_ID` environment variable (CI sets one per workflow run). It must match `[a-z0-9-]{1,40}`, which every first provider accepts as a label value; a value that does not match raises `ConfigError` before create. It is in every Result, and the CLI prints it to stderr before the command starts.
- A lost create response raises `ProviderError` with the run ID and `sandbox_may_exist = True`, and gc finds the sandbox by its labels.

gc rules:

- `anycompute gc --run <id>` deletes that run's leftovers: every sandbox of the run the user names, running or not.
- `anycompute gc --all` deletes only sandboxes with the `anycompute=1` label whose create time (from the provider's list API) plus their `anycompute-ttl` plus the maximum grace period is before now. The grace period also covers clock skew up to 120 seconds.
- On an adapter whose caps say the provider uses an idle timer instead of ending or stopping the sandbox at its TTL (Daytona), the provider must also report the sandbox as stopped. boat.dev stops at its TTL, so it keeps the age rule.
- On docker, whose timer starts only when the container starts, `gc --all` also requires the container not to be running (created and never started, or exited). A running docker container ends itself at its TTL.
- A labeled sandbox with no TTL label, or with no create time in the list (Modal), is kept.
- So without `--force`, `gc --all` never kills a running job. `--force` is required to delete a sandbox that these rules keep, and it can delete a running sandbox.
- `--dry-run` prints the list without deleting.
- gc never deletes a sandbox without the `anycompute=1` label.
- A provider that can only label after create declares the rule `partial`. All four first providers label at create.

Tests:

- After a killed client, `gc` finds the sandbox by its label with no file on disk.
- Unit tests on the fake provider cover: expired, young, no TTL label, no create time, expired but running on an adapter with an idle timer, and a running docker container that is expired by create time (kept).

### Kill-client test

This is the test that a `finally` block cannot pass on its own.

1. The harness sets `ANYCOMPUTE_RUN_ID` and starts `anycompute run --json --ttl 120 -- sh -c 'touch ready; while :; do :; done'` as a child process. The command runs in `/tmp/anycompute`.
2. A separate observer, which uses the adapter's state, list and read calls, finds the child's sandbox by its `anycompute-run` label. It polls the read call until the `ready` file exists, which shows that the workload is running and busy.
3. The harness then kills the child with SIGKILL.
4. The observer polls the provider's API until the sandbox is gone.

Rules:

- The marker is a file, not a stdout line, because the v0.1 CLI prints stdout only after the command ends.
- The marker wait ends at the TTL. With no marker by then, the test fails and the harness runs `gc --run` for its run ID.
- Every observer call goes through the engine's worker-thread wait with the time left before the test's deadline (the TTL plus the maximum grace period).
- The contract fixes a maximum grace period of 120 seconds after the TTL. A different value for one adapter needs a reviewed commit and is set before measurement.
- The test reports the measured time separately and fails when it exceeds the TTL plus the maximum grace period.

Harness unit tests:

- With an observer stub, the kill comes only after the marker.
- With a stub that never sees the marker, the test fails and the harness calls gc.

### Capability table

- Each adapter declares in its caps which rules the provider can enforce, and the table is built from those caps.
- A rule the provider cannot enforce is declared `partial`, the docs say so, and `verify` reports it.
- The SDK never claims a guarantee that the conformance suite has not shown.
- The table also shows each adapter's `isolation`, in `anycompute providers` and in the README's copy of the table. Its unit test checks that column.

Declared gaps:

- **Daytona lifetime** is `partial` until the suite shows that its auto-stop ends a sandbox that runs a busy command with no client activity (auto-stop may be an idle timer, not a deadline).
- **boat.dev lifetime** is `partial`: its TTL stops the VM and keeps the disk. For a `partial` lifetime, the kill-client and lifetime tests pass when the VM is stopped within the TTL plus the maximum grace period; the suite's gc then deletes it and verifies the delete. The docs tell users that a killed client on boat.dev leaves a stopped VM and its disk until gc runs, and that storage may bill until then.
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
- Passes the sandbox's stdout and stderr through.
- Writes `--read` files to `./anycompute-out/<run-id>/` (or `--out <dir>`).
- Exits with the sandbox command's exit code.
- When the job finished but the delete fails, the CLI writes stdout, stderr and the `--read` files first, then exits 125.

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
| 126 | `InvalidTTLError`, `InvalidPathError` or `ConfigError` |
| 130 | A second `KeyboardInterrupt` stopped the cleanup (rule 5) |

- For `RateLimitError` the stderr line gives `retry_after` and, when the adapter's caps record one, the provider's documented start rate with its source URL.
- These codes can collide with the command's own exit code (and 124 and 126 are also timeout and shell codes). So the README and `anycompute run --help` state the collision and tell scripts to read the `error` field of `--json`, not the exit code.
- `--json` mode uses the same exit codes.

### Copyable gc command

On `CleanupError` and on a `ProviderError` with `sandbox_may_exist`, the stderr line, and the `error` object in `--json`, include the exact command to copy: `anycompute gc --run <id> --provider <name> --dry-run`, with each value quoted with `shlex.quote`. A note says that dropping `--dry-run` deletes every sandbox of that run, running or not.

### JSON output

- `--json` prints the Result as JSON with an `error` field that is null on success, or the error type and a redacted message.
- stdout and stderr appear as text. Each read file appears as its path, size, SHA-256 and saved path, never its bytes.
- `verify --json` and `gc --json` print a JSON report with the same `error` field.

### verify

`task.toml` names the command, the input files, the files to read back, the TTL, the expected exit code, and optional `[providers.<name>]` tables of provider options for the team's own image or resources. A table names environment variables, and their values come from the local environment (rule 11).

`anycompute verify` runs the job once per provider and reports, per provider and field:

- **Exit code:** equal to the expected exit code in `task.toml`.
- **stdout and stderr:** equal as decoded text across providers. A difference is reported with both values, and `task.toml` can set `compare = "exit_code"` to compare only exit codes.
- **Files:** equal SHA-256 across providers for each read file. A file missing on one provider is a mismatch.
- **Runtime:** the Python version and CPU architecture per provider, detected here once per provider (outside verify, only by `run(..., runtime=True)`). A difference is reported, not failed, and a missing interpreter is reported as "unknown". The runtime record also names the provider API host that the adapter called, as the scheme and host only (userinfo stripped; a unix socket is recorded as `local`), so a `verify` run or a `runtime=True` run against an E2B-compatible host is never reported as just `e2b`.
- **Cleanup (after the job):** the provider's API shows the sandbox deleted within the maximum grace period after the job, on every adapter. A stopped sandbox is not a pass.

**Lifecycle field (`--lifecycle`):**

- `verify` does not run the kill-client test by default. `--lifecycle` adds it as a separate **Lifecycle** field (killed client: ended within the TTL plus the grace period).
- The killed child runs the same marker-then-busy workload as the kill-client test, under the TTL from `task.toml`, and is killed only after the observer reads the `ready` file.
- The child always uses the provider's default options. When that provider's row used a `task.toml` provider table, the Lifecycle field shows "default options" next to its result, and a unit test checks that label. The README says the field checks the provider's lifetime and delete enforcement, not the team's image.
- The marker wait ends at the TTL from `task.toml`. With no marker by then, the Lifecycle field fails with "no marker by the TTL", and `verify` runs its gc delete for its own run ID and confirms it. Observer calls get the time left before the TTL plus the grace period. A unit test with a fake that never shows the marker checks the failure and the gc call.
- `verify` takes a pacer token for that provider before it starts the child. This adds one sandbox and up to the TTL plus the grace period per provider on the user's accounts.
- After the check, `verify --lifecycle` runs the gc delete for its own run ID, confirms the delete, and reports `CleanupError` when the delete fails. On a `partial` lifetime the Lifecycle field passes on "stopped", and the delete must still confirm.

**Summary line:**

- `verify` ends with one summary line. In plain mode it is the last stdout line; with `--json` it is a `summary` field, so the JSON stays parseable.
- The reference is the first provider in `--providers` with no error.
- The line is "portable on <providers>" only when `verify` would exit 0. Otherwise it joins clauses with "; " in this order: "error on <provider>: <error type>", "fails on <provider>: <fields>" (Exit code against `task.toml`, Cleanup, Lifecycle), "differs on <provider>: <fields>" (stdout, stderr and files against the reference), then "runtime differs: <values>".

**Exit status:** `verify` runs every provider even when one fails. It exits with the 120-126 code of the first anycompute error any provider raised (such as `CleanupError` from `--lifecycle`). With no such error, it exits 1 when any Exit code, stdout and stderr, Files, Cleanup or Lifecycle field fails, and 0 otherwise. A Runtime difference is reported and never changes the exit code.

### gc

`gc` follows the rules in contract rule 13. It exits 0 or the 120-126 code of its error.

## Adapters

Build order: docker first, right after the engine and the fake provider, so the conformance harness is built and debugged at $0 before any Modal run is spent (Next Step 4). Then Modal, boat.dev, then E2B and Daytona (Next Steps 5, 6 and 8).

The facts below come from the docs check of 2026-11-23 (public docs and SDK source only, no provider call). These items are checked again against each provider's docs when its adapter is written; that check is the starting point, not a guarantee:

- the exact lifetime knobs, their granularity, and their limits;
- whether the SDK returns command output as text or bytes;
- the provider's documented start rate with its source URL;
- the adapter's `provider_options` allowlist keys (boat.dev has no custom image, so it has no image key);
- the provider's isolation level.

Every provider SDK's own default command timeout (Daytona 10 s, boat.dev 30 s, E2B 60 s, docker 60 s) is replaced by the engine's budget. The conformance harness paces its launches of every adapter other than boat.dev at 30 creates a minute (the engine pacer works inside one process only).

### Modal

Official `modal` Python SDK.

- TTL maps to the sandbox timeout (a hard deadline, up to 24 hours).
- Labels map to tags (at most 10).
- Delete maps to terminate. The finished record stays readable, and the list omits it.
- A sandbox needs an App. The adapter looks up one App per process, once, under a lock (`App.lookup("anycompute", create_if_missing=True)`) and passes its `app_id` to every list.
- Its gRPC retries cannot be turned off, so `create_retry` and `exec_retry` are `partial`.
- It is the first live cloud adapter.

### E2B

Official `e2b` Python SDK (Python 3.11 or newer).

- TTL maps to the sandbox timeout.
- Labels map to `metadata`.
- Delete maps to kill.
- The adapter never sets the pause-on-timeout option. The lifetime and kill-client tests pass only when the sandbox is gone, not paused.
- It turns off the SDK's connection retries through client options where it can.

### Daytona

Official `daytona` Python SDK.

- Created as ephemeral, with the auto-stop interval set from the TTL.
- Labels map to `labels`.
- Delete maps to delete. Its SDK wait gets only the time left on the engine's cleanup deadline.
- The adapter pins its SDK version (the package moved to the daytona/clients repository at version 2.x) and records the namespace it uses.

### boat.dev

Official `boat-sdk`, sync only. boat.dev sells long-lived VMs, has no free tier and no custom image.

- TTL maps to `ttlSeconds`. This is a stop, so the lifetime is `partial`.
- Labels map to `labels`.
- Delete sends the confirm header and returns on the 202. The engine's confirm poll waits for the asynchronous delete within the 120-second cleanup deadline.
- A synchronous command is limited to 600 seconds, so the adapter uses a detached command and polls when the engine's budget is longer.
- The suite paces boat.dev creates at 6 a minute, half of the $20 plan's 12 included starts.

### docker

Official `docker` Python SDK, local.

- A detached container with auto-remove, the three labels, and one image pinned by its multi-architecture index digest: a `python:3.12-slim` index digest, so it runs on amd64 runners and arm64 Macs, and has `sh`, `mkdir`, `sleep` and `python3`.
- The adapter always sets the entrypoint to `sleep <ttl>`, whatever image a user passes. So PID 1 is always the timer, and the Docker daemon ends and removes the container at the TTL even after a client SIGKILL (a hard deadline once started). An image with no `sleep` fails at create with `ProviderError`.
- Docker creates and starts a container in two calls, so a client that dies between them leaves a container with no timer. That window is declared in docker's own `pre_start_window: partial` cap and docs, while its lifetime stays hard. gc deletes such a container: `gc --all` treats it as expired by create time plus TTL plus grace, and deletes a docker container only while it is not running. A unit test interrupts between create and start.
- A registry pull limit or other pull failure during the image pull raises `ProviderError`, not `RateLimitError`, because it is not the provider's start rate.
- The TTL has no provider maximum on docker.
- Delete maps to `remove(force=True)`, which also removes a container that was created but never started. A 404 counts as accepted. A 409 counts as accepted only for "removal already in progress". The engine's state call confirms. Recorded-response unit tests cover the 404 and both 409 cases.
- The client comes from docker-py's `from_env` with its context fallback, so the adapter finds the same daemon as the user's own `docker` command: `DOCKER_HOST`, then the current docker context, whose default is the default socket. There is no custom discovery code. A unit test checks that `DOCKER_HOST` wins and that the current context is used when it is unset. `from_env` takes `use_context=True` by default in docker-py's main branch (docker/client.py, checked 2026-11-23); the pinned docker-py version is checked for it when the adapter is built.
- An unreachable daemon raises `ProviderError` with a one-line hint.
- The client is created with no read timeout (docker-py defaults to 60 seconds), so the engine's budget controls each call.
- docker-py asks the daemon for its API version while it builds the client. So the adapter builds the client inside its first waited call (create in a run; list or state when gc or the observer calls first), under that call's budget. A hang in a create is reported like any create that reaches its budget, as a lost create, and gc then finds nothing. A unit test with a hung version request checks that create returns at its budget.
- Exec goes through the engine's worker-thread wait, files go through archives, and list filters by label and shows a create time.
- Every container also gets `security_opt=["no-new-privileges"]` and `pids_limit=4096` (processes and threads). Neither is on the `provider_options` allowlist, and a recorded-create unit test checks both.
- Its caps declare `isolation` as a container on the host kernel. The README says that docker is not a boundary for untrusted code, unlike a provider that runs each sandbox in its own VM.
- It needs no account or key. So `ci.yml` runs the full live conformance suite on it on every pull request at $0 (the lifecycle tests, that is the lifetime, kill-client and delete-verification tests, plus the working-directory and argument tests), and a user's first run costs nothing.
- Docker Hub pull limits on GitHub runners are checked when it is built (cache the pinned image if needed).

## Test layout

- `tests/unit/` tests the engine rules against the fake provider, and the adapter mappings from recorded responses, with no keys. Rules that exist only in the Python API (the `Sandbox` context manager, exception chaining, thread safety) are tested only here.
- `tests/conformance/` holds the live tests. They drive the CLI (`anycompute run --json`, `verify`, `gc`) as a subprocess and read only its JSON and exit codes about the job. A separate observer checks provider state and reads the kill-client marker file. A later TypeScript CLI can be tested by the same suite and observer, so they can become the language-neutral runner.

## Repository layout

```
pyproject.toml                  # uv; console script "anycompute"; requires-python >=3.11; extras modal, e2b, daytona, boat, docker, all
src/anycompute/__init__.py     # the engine: run, Sandbox, every contract rule the Python API enforces, gc selection, verify checks, redaction; exports
src/anycompute/contract.py     # Result, error types, Provider protocol and caps, capability table built from caps
src/anycompute/cli.py          # flags, local --file and task.toml input reads, run ID print, output: run, verify, gc, providers; --json with the error field; only CLI rules
src/anycompute/providers/{fake,e2b,modal,daytona,boat,docker}.py   # thin adapters; each imports its SDK only when used
tests/unit/                     # engine rules against the fake provider; adapter mappings from recorded responses; no keys
tests/conformance/              # live tests that drive the CLI with --json, plus an observer; kill-client and delete verification
.github/workflows/ci.yml        # unit tests on every pull request, Python 3.11 to 3.15, plus the full live conformance suite in a docker job (timeout-minutes: 30 on that job)
.github/workflows/conformance.yml  # live tests: weekly and manual, protected environment with provider keys (timeout-minutes: 90)
.github/workflows/release.yml   # PyPI trusted publishing, signed release, then a $0 smoke run of the published package on docker
```

## Distribution

- **Package:** `anycompute` on PyPI, with the `anycompute` console script and one extra per provider. Users install only the SDKs they use: `pipx install 'anycompute[modal]'`, or run it with `uvx --from 'anycompute[modal]' anycompute run ...`. `anycompute[all]` installs every provider SDK. A missing extra raises `ConfigError`, which names the extra to install. Python 3.11 or newer.
- **Pipeline:** GitHub Actions.
  - Unit tests and the docker conformance suite on every pull request, with no keys.
  - Live conformance once a week and on manual dispatch, in a protected environment that holds the project's provider keys, under the spend rules above.
  - Releases publish to PyPI through trusted publishing (OIDC) and carry signed attestations.
- **Post-release smoke run:** after each publish, `release.yml` installs `anycompute[docker]==<version>` from PyPI on a clean runner and runs `anycompute providers` and one docker job.
  - The install retries every 20 seconds for up to 10 minutes, because PyPI's index can lag after an upload. An install that never succeeds fails the job as "install failed", and the README's release runbook says to check PyPI and re-run the job.
  - Only a package failure after a successful install makes the runbook yank that version and ship a patch: `anycompute providers` fails, or the docker job returns a wrong result or raises an error other than `ProviderError`.
  - A `ProviderError` from the docker job (daemon unreachable, image pull failure or throttling) fails the job as "infrastructure", and the runbook says re-run. When a re-run fails the same way, the maintainer investigates before any yank.
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
5. E2B and Daytona each pass the live conformance suite in the public CI, with any rule the provider cannot enforce declared `partial`. If a provider objects to the project's runs before the release, its runs stop, its results are removed, and its adapter ships as a "not run live" adapter with its fixtures rebuilt from the provider's published docs (see Live runs), marked as such in the capability table and the README.
6. Version 0.1.0 is on PyPI through trusted publishing, with a signed release.
7. The full live conformance suite on the docker adapter, including the lifecycle tests, is a required check on every pull request (set by branch protection on main), and it passes on main.
8. The Next Step 9 features ship with their tests. The Next Step 4 docker work (the docker adapter, its pre-start window, and the kill-client marker workload) ships with its unit tests. The docker adapter and the marker workload are also proven live by the docker suite in criterion 7, and the pre-start window by its unit test only.

## Open question

- **Framework plug-ins after v0.1:** a LangChain Deep Agents backend (its protocol needs only `execute()`), an OpenAI Agents SDK custom client, a Harbor environment type.

## Next Steps

1. **Prices.** Record each provider's default-size price, stopped-storage price, billing granularity and minimum charge with a source, for the runner's reservation (the Modal price is missing). Check that a run's worst case (15 sandboxes x compute for 240 s, plus start fees, plus one week of stopped storage on boat.dev and Daytona) fits under $2 per adapter. If it does not, lower the sandboxes per run or raise the per-run budget and recompute the monthly totals.
2. **Engine first.** Add the layout above to this repository. Add the fake provider, the contract types, the Provider protocol and the engine first.
3. **Unit tests.** Write the unit tests for `run`, `Sandbox`, the error mapping (recorded responses), the cleanup and expiry outcomes, and the capability table against the fake provider, plus these engine rules:
   - each of the 9 error types;
   - client timeouts, the worker-thread wait and the cleanup deadline;
   - the gc rules;
   - redaction;
   - the CLI exit codes and JSON (a command that exits 121 against a `RateLimitError`, told apart only by the `error` field; stdout, stderr and the `--read` files written before exit 125 on `CleanupError`);
   - text decoding;
   - runtime detection (no detection call unless `runtime=True`; "unknown" for a missing interpreter);
   - a runner test whose fake delete fails (no further create, `gc --run`, nonzero exit);
   - 50 concurrent `run()` calls that check distinct sandbox IDs, correct run and TTL labels, and a confirmed delete for each.
4. **Docker first.** Write the docker adapter, with its pre-start window declared `partial` and its unit test. Write the conformance harness and the observer, including the kill-client marker workload, the observer's read-based wait for the `ready` file, and the harness unit test. Run the harness on docker locally and in `ci.yml` until it passes. Docker runs cost $0 and have no run limit, so the harness is debugged before any Modal run is spent. Branch protection waits for Next Step 9.
5. **Modal.** Write the Modal adapter and run the live conformance tests from Next Step 4 on it, driven through the CLI: the same suite, including the kill-client test with its marker.
6. **boat.dev.** Add the boat.dev adapter with its declared `partial` rules.
7. **verify.** Write `verify` and its report. Run it on one real workload on Modal and boat.dev.
8. **E2B and Daytona live.** Add the E2B and Daytona adapters and run the live conformance suite on them, driven through the CLI as in Next Step 5.
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
   - the opt-in debug logger, with a unit test that plants a key in a logged error and checks each LogRecord's message, args and exc_info, not only the formatted text;
   - the post-release smoke run on docker and the yank-and-patch runbook;
   - late-create reconciliation;
   - `task.toml` provider tables for `verify`.
10. **Release.** Set up trusted publishing and release 0.1.0, after Next Steps 6 to 9.

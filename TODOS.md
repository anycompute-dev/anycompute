# TODOS

Deferred work for anycompute. The v0.1 design is in docs/design.md.

## SDK

### Vendor-run conformance results

**What:** Package the live suite as `anycompute conformance --provider <name> --json` (behind a conformance extra) and accept vendor-run results by pull request into results/<provider>/<date>.json, shown as 'run by the project' or 'run by <vendor> on its own account'.

**Why:** Portability layers lasted when vendors ran the compatibility tests and kept their own adapters (Kubernetes, Terraform); projects where one team kept every driver were retired (Apache jclouds, Apache Deltacloud). Vendor-run results are the long-term defense for a project with few maintainers.

**Context:** Deferred from v0.1. The suite already drives the CLI by subprocess and JSON (docs/design.md, Test layout), so moving tests/conformance/ into src/anycompute/conformance/ needs no rewrite. Add workflow_call to conformance.yml, a SHA-256 of the suite files in each report, and results/README.md rules (an unmodified tagged suite, a public run URL, 'stale' after 90 days). The project never runs the conformance subcommand on a provider that objected to the project's runs, and it removes its own results for such a provider (docs/design.md, Constraints). The report version fields (`anycompute_version`, `source`, `provider_sdk_version`, `suite_version`; docs/design.md, Distribution) keep the format ready.

**Effort:** M
**Priority:** P2
**Depends on:** v0.1 released with users, and at least one provider willing to run it.

### Deep Agents backend (AnycomputeSandbox)

**What:** One class, `AnycomputeSandbox(provider=..., ttl=...)`, behind a deepagents extra, that subclasses Deep Agents' BaseSandbox so a Deep Agents user can switch sandbox providers with one string and keep a provider-enforced TTL and a confirmed delete.

**Why:** Distribution through a framework that users already have brings the lifecycle rules (provider-enforced TTL, confirmed delete) to every provider the framework user picks.

**Context:** Deferred from v0.1; docs/design.md, Open question, already names framework plug-ins. Map execute to Sandbox.exec(['sh','-c',cmd]), id to sandbox_id, uploads and downloads to Sandbox.write and Sandbox.read; set has_sync=True and has_async=False. Proof: LangChain's SandboxIntegrationTests (86 tests) on Modal as one manual dispatch, then one docs pull request. Checks first: the test fixture must create at most 15 sandboxes per run (Run size in docs/design.md, Constraints, Spend rules); Deep Agents file tools use absolute paths, so the backend needs a written exception to InvalidPathError; pin the deepagents version range.

**Effort:** M
**Priority:** P2
**Depends on:** v0.1 released; the three checks above.

### Bump the pinned docker image digest

**What:** A monthly scheduled workflow that opens a pull request to bump the pinned `python:3.12-slim` index digest in the docker adapter; the docker conformance job on that pull request proves the new image before merge.

**Why:** A pinned digest never gets security fixes, so users' first local runs use an image that ages and collects known CVEs.

**Context:** The docker adapter (docs/design.md, Adapters) pins the image by its multi-architecture index digest for repeatable results. Start with a scheduled GitHub Actions job that reads the current index digest with `docker buildx imagetools inspect python:3.12-slim` and opens a pull request when it differs. A new image can change runtime results; `verify` records the runtime, so the difference stays visible.

**Effort:** S
**Priority:** P2
**Depends on:** the docker adapter (docs/design.md, Adapters) and branch protection on main with the docker conformance job as a required check (docs/design.md, Success criteria).

### Native async API (arun, AsyncSandbox)

**What:** Add `arun()` and `AsyncSandbox` on the same engine rules as the sync API, after v0.1.

**Why:** Async frameworks (Inspect AI, agent runtimes) call sandboxes from an event loop; until this exists they wrap each anycompute call in a thread.

**Context:** v0.1 puts every contract rule in one engine and is sync and thread-safe (docs/design.md, The stateless contract). Start from the engine's call sequence. The async path must pass the same unit tests on the fake provider and the same live conformance suite; the suite drives the CLI, so it needs no change. Modal, E2B and Daytona ship async clients; boat.dev and docker (docker-py) are sync only, so their adapters stay thread-wrapped. The create pacer (docs/design.md, The stateless contract, Create pacing) uses a threading lock and needs an async-safe version.

**Effort:** M
**Priority:** P3
**Depends on:** v0.1 released (every item in docs/design.md, Success criteria), and a named async user or framework plug-in asks for it.

### Streaming reads for large output files

**What:** Let callers stream a read file to a local path (`read_to=` in Python; the CLI already writes under `--out`), hash it in chunks, and state each provider's file size limit in the capability table.

**Why:** In v0.1 a job's large output (a model checkpoint, a dataset shard) is loaded whole into the caller's memory, once per file, and `verify` hashes whole files.

**Context:** v0.1 keeps `Result.files` as bytes (docs/design.md, The stateless contract, Result). Start by recording each provider SDK's file read API (whole bytes or a stream) in the adapter caps; add streaming only where the SDK supports it, and keep whole bytes elsewhere. Provider file size limits are not checked yet.

**Effort:** M
**Priority:** P3
**Depends on:** v0.1 released, and a user reports a memory problem with large outputs, or an adapter's caps show a size limit below a common use.

### run_many, a batch CLI and a batch receipt

**What:** Add `run_many(jobs, provider, max_concurrency)` and `anycompute batch jobs.jsonl --json`: one Result or one typed error per job in input order, a wait on `retry_after` when a provider refuses a create, and a closing receipt (jobs passed, nonzero exits, errors by type, 429 waits, deletes confirmed, sandbox seconds, and the leftover count from a `gc --run` sweep, expected 0).

**Why:** Eval and RL users run thousands of jobs; a batch call with a leak-free receipt makes that use first-class.

**Context:** Deferred from v0.1. v0.1 users batch with a ThreadPoolExecutor. run_many may retry a create only after a 429 or quota refusal, never after a lost response, because a retried create after a lost response makes a second sandbox; this needs a reviewed change to the Retries rule in docs/design.md, The stateless contract, which today never retries a create. It depends on the create pacer (docs/design.md, The stateless contract, Create pacing). Add a SIGTERM handler in cli.py only.

**Effort:** M
**Priority:** P3
**Depends on:** v0.1 released, the create pacer, a reviewed change to the Retries rule, and a batch user who asks for it.

### anycompute mcp: one stateless run tool for MCP hosts

**What:** An `anycompute mcp --provider <name> --max-ttl <s> --max-runs <n>` subcommand behind an mcp extra that exposes one run tool returning the same JSON as `anycompute run --json`.

**Why:** One small subcommand reaches every MCP host, with a provider-enforced TTL and a confirmed delete per call.

**Context:** Deferred from v0.1. The tool calls run() in a worker thread and returns the existing --json output, so redaction and the error field need no new code. --max-ttl and --max-runs raise an error when a call goes past them and never clamp, because an LLM can call the tool in a loop on the user's account. No gc or session tool. Check the MCP Registry rules and host support first (not checked yet); add registry metadata after the PyPI release. E2B and Daytona already ship single-provider MCP servers.

**Effort:** S
**Priority:** P3
**Depends on:** the first PyPI release.

### Bounded stdout capture

**What:** Cap the stdout and stderr text kept in a Result (for example the last N megabytes, with a truncation marker and the full byte count), let the CLI stream stdout through as it arrives, and state the cap in the contract table.

**Why:** v0.1 holds stdout and stderr whole in memory, so a job that prints gigabytes can exhaust the caller's memory and crash the process.

**Context:** Deferred from v0.1. Large read files are a separate item (Streaming reads for large output files, above). Keep the U+FFFD text rule (docs/design.md, The stateless contract, Result) for what is kept; the CLI's pass-through streaming must not change the `--json` output, which keeps the capped text plus the byte count.

**Effort:** S
**Priority:** P3
**Depends on:** v0.1 released, and a user reports a memory problem with large stdout.

### require= execution policy

**What:** Add `require=` (Python) and `--require` (CLI). A workload names the guarantees it needs, for example a hard lifetime, a confirmed delete or labels at create. anycompute refuses a provider whose caps cannot meet them before any create. `verify` reports "portable under <policy>".

**Why:** Callers and framework plug-ins can then use the capability table to refuse a provider before create, not only to read a report.

**Context:** Deferred from v0.1. Start from the typed caps (docs/design.md, The stateless contract, One engine) and the verify partial-cap reporting (`partial_caps` and the summary clause; docs/design.md, CLI, verify). It adds API surface and one error path before create. A rule that asks for recent passing results would also need a published results feed.

**Effort:** M
**Priority:** P3
**Depends on:** v0.1 released, and the first framework plug-in needs it or a user asks to refuse partial providers.

### Longer TTLs on E2B Pro and boat.dev

**What:** Raise the v0.1 TTL caps (E2B `ttl_max_s` 3600, boat.dev `ttl_max_s` 480) for longer jobs: a plan-aware maximum on E2B, and detached commands on boat.dev.

**Why:** E2B Pro allows 24 hours and boat.dev sells long-lived VMs, but v0.1 refuses a TTL above its caps with `InvalidTTLError`.

**Context:** E2B does not document what happens when a TTL passes a plan's limit, a refusal or a clamp (checked 2026-11-23), and the adapter cannot read the plan. Start with a recorded-response test that shows which one happens. If E2B clamps, the adapter reads back the end time. boat.dev limits a synchronous command to 600 seconds. So a longer exec needs a detached command that the engine polls on its poll schedule and clock (docs/design.md, The stateless contract, Delete). The adapter holds no poll loop. Recorded-response tests cover the detached path.

**Effort:** M
**Priority:** P3
**Depends on:** v0.1 released, and a user asks for a longer job on E2B or boat.dev.

## Completed

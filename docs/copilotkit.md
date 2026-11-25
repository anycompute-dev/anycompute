# Run a CopilotKit tool with anycompute

The [CopilotKit Daytona cookbook](https://docs.copilotkit.ai/crewai-crews/cookbook/daytona#define-the-sandbox-tool) creates a sandbox per tool call, runs code, returns `{ stdout, exitCode }`, and deletes the sandbox. This example keeps that output shape and runs a fixed Python job with declared files. The tool accepts numbers as data. Keep the cookbook's existing route for model-generated code and workloads that require its Daytona isolation, templates, or session behavior.

## Set up the example

Use Node.js 22+, Python 3.11+ in a virtual environment, and a local Linux-container Docker daemon on macOS or Linux. File readback needs POSIX directory operations. Run these commands in a new example directory with the Python environment active.

```sh
python -m pip install 'anycompute[docker]==0.1.0'
npm init -y
npm install --ignore-scripts --legacy-peer-deps --save-exact @copilotkit/runtime@1.90.2 zod@3.25.76
```

Save this module as `run-code.mjs`. Use it on the server, where `anycompute` is on `PATH` and can reach the local Docker daemon.

```js
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { defineTool } from "@copilotkit/runtime/v2";
import { z } from "zod";

const exec = promisify(execFile);
const parameters = z.object({
  numbers: z.array(z.number().int().min(-1_000_000).max(1_000_000)).min(1).max(100),
}).strict();

export const sumFixture = `
import json
from pathlib import Path

numbers = json.loads(Path("numbers.json").read_text())
result = json.dumps({"total": sum(numbers)}, separators=(",", ":"))
Path("total.json").write_text(result + "\\n")
print(result)
`;

const supervise = `
import subprocess
import sys
from pathlib import Path

code, seconds = sys.argv[1:]
try:
    result = subprocess.run([sys.executable, "-c", code], timeout=float(seconds))
except subprocess.TimeoutExpired:
    Path("command-deadline").touch()
    sys.exit(1)
sys.exit(result.returncode)
`;

// Server-owned code only. The CopilotKit tool accepts data through its schema.
export async function runTrustedPython(code, numbers, seconds = 2) {
  if (!process.env.DOCKER_HOST?.startsWith("unix://")) {
    throw new Error("Set DOCKER_HOST to a local Docker Unix socket");
  }
  const directory = await mkdtemp(join(tmpdir(), "anycompute-copilotkit-"));
  const runId = randomUUID();
  try {
    const input = join(directory, "numbers.json");
    await writeFile(input, JSON.stringify(numbers));
    const args = [
      "run", "--json", "--provider", "docker", "--ttl", "60",
      "--file", input, "--read", "total.json", "--read", "command-deadline",
      "--out", directory, "--", "python3", "-c", supervise, code, String(seconds),
    ];
    // A nonzero workload exit still has a JSON report on stdout.
    const { stdout } = await exec("anycompute", args, {
      env: { ...process.env, ANYCOMPUTE_RUN_ID: runId },
    }).catch((error) => {
      if (typeof error.code !== "number") throw error;
      return error;
    });
    const report = JSON.parse(stdout);
    if (report.error) {
      throw Object.assign(new Error(report.error.message), {
        name: report.error.type,
        gcCommand: report.error.gc_command,
      });
    }
    if (report.files["command-deadline"]) {
      throw Object.assign(new Error("Python command deadline expired"), {
        name: "CommandDeadlineError",
      });
    }
    const artifact = report.files["total.json"]
      ? await readFile(join(directory, runId, "total.json"))
      : null;
    return {
      stdout: report.stdout,
      stderr: report.stderr,
      exitCode: report.exit_code,
      runId,
      artifact,
    };
  } catch (error) {
    throw Object.assign(error, { runId });
  } finally {
    await rm(directory, { recursive: true, force: true }).catch(() => {
      console.warn(`Could not remove temporary directory ${directory}`);
    });
  }
}

export const runCode = defineTool({
  name: "runCode",
  description: "Use the trusted Python totals job to sum a list of integers.",
  parameters,
  execute: async (input) => {
    const { numbers } = parameters.parse(input);
    const { stdout, exitCode } = await runTrustedPython(sumFixture, numbers);
    return { stdout, exitCode };
  },
});
```

Import `runCode` into your CopilotKit server route and register it in a test `BuiltInAgent`'s `tools` list. If an agent already has a `runCode` tool, give the example a distinct name before registering both. The cookbook sets `maxSteps: 2` so the agent can call the tool and then answer.

The in-sandbox supervisor limits the fixture's Python process to two seconds by default. The 60-second sandbox TTL starts when Docker starts the container. The supervisor writes a marker on expiry so an ordinary exit `124` keeps its workload meaning. Infrastructure and cleanup errors carry the run ID and any cleanup hint from the CLI. Preserve the existing backend for jobs that require whole-sandbox cancellation, resource caps, or a specific network policy.

## Verify locally without calling a model

Select your local Docker context and export its Unix socket. The example needs no API keys.

```sh
export DOCKER_HOST="$(docker context inspect "$(docker context show)" --format '{{(index .Endpoints "docker").Host}}')"
unset DOCKER_CONTEXT
```

With `run-code.mjs` in the current directory, invoke the registered tool through CopilotKit's adapter and check what Docker ran.

```sh
node --input-type=module - <<'JS'
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { convertToolDefinitionsToVercelAITools } from "@copilotkit/runtime/v2";
import { runCode, runTrustedPython, sumFixture } from "./run-code.mjs";

assert.ok(process.env.DOCKER_HOST?.startsWith("unix://"), "Set DOCKER_HOST to a local Unix socket");
const containers = () => execFileSync(
  "docker", ["ps", "-aq", "--filter", "label=anycompute=1"], { encoding: "utf8" },
).trim().split("\n").filter(Boolean);
const before = new Set(containers());
const { runCode: tool } = convertToolDefinitionsToVercelAITools([runCode]);
assert.deepEqual(
  await tool.execute({ numbers: [2, 3, 7] }, { toolCallId: "fixture", messages: [] }),
  { stdout: '{"total":12}\n', exitCode: 0 },
);
await assert.rejects(
  () => tool.execute({ numbers: [2], code: "print(99)" }, { toolCallId: "invalid", messages: [] }),
  { name: "ZodError" },
);

const result = await runTrustedPython(sumFixture, [1, -3, 9]);
assert.deepEqual(result.artifact, Buffer.from('{"total":7}\n'));
const failed = await runTrustedPython(
  'import sys; print("workload error", file=sys.stderr); sys.exit(124)', [],
);
assert.equal(failed.exitCode, 124);
assert.equal(failed.stderr, "workload error\n");
assert.equal(failed.artifact, null);

await assert.rejects(
  () => runTrustedPython("import time; time.sleep(10)", [], 0.2),
  (error) => error.name === "CommandDeadlineError" && Boolean(error.runId),
);
assert.deepEqual(containers().filter((id) => !before.has(id)), []);
console.log("CopilotKit tool, artifact bytes, exit 124, deadline, and Docker cleanup passed.");
JS
```

# Testing

Run `uv run --locked pytest -q` from the repository root. The logic tests use the in-memory fake provider or synthetic responses and need no provider keys. Pytest excludes the live tests by default, even when a test selector supplies their paths.

Before merge, all five GitHub Linux jobs must pass on the pull request's candidate (Python 3.11 through 3.15). The GitHub Docker conformance job must also pass. Local tests do not replace these checks.

The Python 3.12 GitHub job installs the locked Modal and Daytona SDKs. Run `uv run --locked --extra modal --extra daytona pytest -q` to exercise their request limits with fake transports.

Store any recorded provider fixtures as UTF-8 JSON under `tests/fixtures/`. The fixture test rejects credential fields, authentication headers, and known provider token formats. Use synthetic Python fakes for authentication cases.

Run `uv run --locked --extra docker pytest -q` to include the Docker SDK transport tests. These tests use a fake transport and do not contact a daemon.

For the live Docker suite, start a local Linux-container daemon. Run `ANYCOMPUTE_CONFORMANCE_PROVIDER=docker uv run --locked --extra docker pytest -q -m live_conformance tests/conformance`. The marker selects the suite. The provider variable allows the suite to run. This suite creates and removes labeled containers with the pinned image. The lifetime tests wait for the actual TTL.

To write a local conformance report, set `ANYCOMPUTE_CONFORMANCE_REPORT` to a JSON file path in an existing directory. A result records the source, package and provider SDK versions, suite version with the checkout SHA, and each selected rule's verdict. Lifecycle observations measure elapsed seconds from client launch. Other cleanup timings use the CLI's elapsed time from create request to confirmed deletion. A missing measurement is `null`. CI uploads the test report as `conformance-<provider>-<attempt>` after the tests finish. The workflow status also accounts for setup and the final cleanup sweep.

Run `uv build --no-sources` to build the source distribution and wheel locally. To rehearse the release smoke test, install the built wheel with the Docker extra into a fresh virtual environment and use that environment's Python to run `tests/release_smoke.py` against your local Docker daemon.

## Prepare the Modal image locally

Use the same pinned Python base as Docker conformance. Modal requires a `linux/amd64` image. Run these commands from the repository root to pull the public base, tag it locally, and smoke-test the fixed workload's commands with networking disabled. [Modal image requirements](https://modal.com/docs/guide/existing-images)

```sh
base_image=$(uv run --locked python -c 'from anycompute.providers.docker import DEFAULT_IMAGE; print(DEFAULT_IMAGE)')
base_image="docker.io/library/$base_image"
docker pull --platform linux/amd64 "$base_image"
docker image tag "$base_image" anycompute-conformance:v1
docker run --rm --pull never --platform linux/amd64 --network none anycompute-conformance:v1 sh -ec '
  test "$(uname -m)" = x86_64
  python3 --version
  mkdir -p /tmp/anycompute/nested
  cd /tmp/anycompute
  printf "\000\377binary\n" > fixture.bin
  cp fixture.bin nested/copy.bin
  cmp fixture.bin nested/copy.bin
  printf "%s\n" "anycompute verify" > out.txt
  test "$(cat out.txt)" = "anycompute verify"
  status=0
  timeout 1 sleep infinity || status=$?
  test "$status" -eq 124
'
```

When publishing is authorized, use Modal's `image pull` with the verified registry digest and the name `anycompute-conformance:v1` in the CI token's Modal environment. Record the registry digest and Modal image ID because a named Modal tag can be updated. [Modal CLI](https://modal.com/docs/cli/latest/image#modal-image-pull) · [Named images](https://modal.com/docs/guide/named-images)

## Protected cloud conformance

The manual **Modal account check** workflow on `main` uses the protected `conformance` secrets to check workspace authentication and look up `anycompute-conformance:v1`. It reports fixed status messages and uses metadata lookups only. Confirm the active workspace usage cap separately in Modal Usage & Billing. [Workspace API](https://modal.com/docs/sdk/py/latest/Workspace) · [Modal budgets](https://modal.com/docs/guide/budgets)

The manual **Cloud account check** workflow on `main` uses the same protected environment to check Boat account allowances, E2B's `base` template size, and Daytona's snapshot and warm pools. Each job receives its own provider key and reports a small set of readiness fields. Before enabling live conformance, verify the spend controls in the design. [Spend rules](docs/design.md#spend-rules-for-the-conformance-runner)

Daytona reports `feature unavailable` if its warm-pool list returns HTTP 404 after the snapshot check passes. This is the status Daytona documents for organizations with warm pools disabled. [Daytona troubleshooting](https://www.daytona.io/docs/en/troubleshooting/#provisioning)

For the initial E2B conformance trial, dispatch **Conformance** on `main` with `adapters=e2b` and `verify=false`. The counted job checks the current `base` resources before launching. Weekly E2B runs require a separate opt-in through the provider capability.

The protected `Conformance` workflow can compare one fixed workload on Modal and boat.dev. Set `adapters` to `modal,boat` and enable `verify` on a manual main-branch dispatch after both adapters are enabled and their credentials are configured. The comparison job records its JSON report in the step log.

Do not run cloud conformance locally. The live-test and spend rules are in [the design](docs/design.md#live-runs).

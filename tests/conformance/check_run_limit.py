"""GitHub Actions entry point for the keyless conformance spend gate."""

from calendar import monthrange
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
import http.client
import json
import os
import re
import sys
from urllib.parse import urlencode

from anycompute import providers
from tests.conformance.runner import VERIFY_ADAPTERS, RunLimitError, check_run_limit

_MAX_PAGE_BYTES = 8 * 1024 * 1024


def _fetch_page(repository: str, token: str, month: datetime, page: int, per_page: int) -> tuple[int, bytes]:
    first = month.date()
    last = first.replace(day=monthrange(first.year, first.month)[1])
    query = urlencode({"created": f"{first}..{last}", "page": page, "per_page": per_page})
    path = f"/repos/{repository}/actions/workflows/conformance.yml/runs?{query}"
    try:
        # Do not follow redirects with the Actions token.
        with closing(http.client.HTTPSConnection("api.github.com", timeout=10)) as connection:
            connection.request("GET", path, headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "anycompute-conformance",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                return response.status, b""
            body = response.read(_MAX_PAGE_BYTES + 1)
            if len(body) > _MAX_PAGE_BYTES:
                raise RunLimitError("GitHub run history page is too large")
            length = response.getheader("Content-Length")
            if length is not None and length != str(len(body)):
                raise RunLimitError("GitHub run history response is incomplete")
            return response.status, body
    except http.client.HTTPException:
        raise RunLimitError("GitHub run history request failed") from None


def _reject_json_constant(_):
    raise RunLimitError("Invalid GitHub run history JSON")


def main(env: Mapping[str, str] = os.environ, *, now: datetime | None = None) -> int:
    try:
        if env.get("GITHUB_REF") != "refs/heads/main":
            raise RunLimitError("Cloud conformance runs only on main")
        repository, token, output_path = env["GITHUB_REPOSITORY"], env["GITHUB_TOKEN"], env["GITHUB_OUTPUT"]
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository) or repository.endswith(("/.", "/..")):
            raise RunLimitError("Invalid GITHUB_REPOSITORY")
        if not token or not output_path:
            raise RunLimitError("Missing GitHub Actions token or output path")
        run_id, attempt = env["GITHUB_RUN_ID"], env["GITHUB_RUN_ATTEMPT"]
        if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt)):
            raise RunLimitError("Invalid GitHub run ID or attempt")
        current_run_id = int(run_id)

        live = {spec.name for spec in providers.PROVIDERS if spec.caps.live_run and spec.name != "docker"}
        event = env["GITHUB_EVENT_NAME"]
        if event == "schedule":
            selected = sorted(live)
        elif event == "workflow_dispatch":
            selected = env.get("REQUESTED_ADAPTERS", "").split(",")
            if set(selected) - live or len(set(selected)) != len(selected):
                raise RunLimitError("The adapters input must be a comma-separated list of distinct enabled cloud adapters")
        else:
            raise RunLimitError("Unsupported conformance workflow event")

        verify = env.get("REQUESTED_VERIFY", "0")
        if verify not in ("0", "1") or verify == "1" and (
            event != "workflow_dispatch" or tuple(selected) != VERIFY_ADAPTERS
        ):
            raise RunLimitError("Verify requires a manual dispatch naming modal,boat in that order")

        pages: list[bytes] = []

        def fetch_page(month: datetime, page: int, per_page: int) -> tuple[int, bytes]:
            status, body = _fetch_page(repository, token, month, page, per_page)
            pages.append(body)
            return status, body

        counted = check_run_limit(
            fetch_page, current_run_id=current_run_id, current_attempt=int(attempt),
            now=now or datetime.now(timezone.utc), live_adapters=live,
        )
        # Bind the matrix to the dispatch that the runner actually counted.
        current = None
        for body in pages:
            for entry in json.loads(body, parse_constant=_reject_json_constant)["workflow_runs"]:
                if entry["id"] == current_run_id:
                    current = entry
        if current is None or current["event"] != event or (
            event == "workflow_dispatch" and current["display_title"] != f"adapters={','.join(selected)}"
        ):
            raise RunLimitError("GitHub history does not match the current run selection")
        matrix = json.dumps(selected, separators=(",", ":"))
        with open(output_path, "a", encoding="utf-8") as output:
            output.write(f"verified_attempt={counted}\nadapters={matrix}\n")
            if verify == "1":
                output.write("verify=true\n")
        if not selected:
            print("No cloud adapters are enabled; no conformance tests ran.")
        return 0
    except RunLimitError as error:
        print(f"Conformance gate refused: {error}", file=sys.stderr)
    except (KeyError, OSError, ValueError):
        print("Conformance gate could not read the Actions context or write outputs", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

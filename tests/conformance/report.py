"""Write the conformance verdict without test output or provider responses."""

import json
from math import inf
from pathlib import Path
import re
import subprocess

from anycompute import engine, providers
from anycompute.verify import _version

SUITE_VERSION = "1"
_CONTRACT = Path(__file__).with_name("test_live_contract.py")


def _suite_version():
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=_CONTRACT.parents[2],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    return f"{SUITE_VERSION}+{commit if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit) else 'unknown'}"


class ConformanceReport:
    def __init__(self, path, env):
        self.path = Path(path).absolute()
        self.spec = next((spec for spec in providers.PROVIDERS
                          if spec.name == env.get("ANYCOMPUTE_CONFORMANCE_PROVIDER")), None)
        self.source = "project-run" if (
            env.get("ANYCOMPUTE_REPORT_SOURCE") == "project-run"
            and env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_REF") == "refs/heads/main"
        ) else "user-run"
        self.rules = {}

    def pytest_collection_finish(self, session):
        self.rules = {item.nodeid: {
            "rule": item.originalname.removeprefix("test_"), "status": "not-run",
            "end_seconds": None, "delete_seconds": None,
        } for item in session.items if item.path == _CONTRACT}

    def pytest_runtest_logreport(self, report):
        row = self.rules.get(report.nodeid)
        if row is None:
            return
        props = dict(report.user_properties)
        if report.failed:
            row["status"] = "fail"
        elif row["status"] != "fail":
            if report.skipped:
                row["status"] = "skipped"
            elif report.when == "call":
                row["status"] = "partial" if props.get("lifetime") == "partial" else "pass"
        for name in ("end_seconds", "delete_seconds"):
            value = props.get(name)
            if type(value) in (int, float) and 0 <= value < inf:
                row[name] = value

    def pytest_sessionfinish(self, exitstatus):
        redact = engine._provider_context(None, None)[0].redact
        version = _version("anycompute")
        sdk_version = _version(self.spec.caps.sdk_package) if self.spec else None
        rows = list(self.rules.values())
        passed = exitstatus == 0 and rows and all(row["status"] in ("pass", "partial") for row in rows)
        report = {
            "anycompute_version": redact(version) if version else None,
            "suite_version": redact(_suite_version()),
            "source": self.source,
            "status": "pass" if passed else "fail",
            "rows": [{
                "provider": redact(self.spec.name) if self.spec else None,
                "provider_sdk_version": redact(sdk_version) if sdk_version else None,
                "rules": rows,
            }],
        }
        self.path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")

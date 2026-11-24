import json
from types import SimpleNamespace

import pytest

from tests.conformance import report


def item(name):
    return SimpleNamespace(path=report._CONTRACT, nodeid=f"private-dir::{name}", originalname=f"test_{name}")


def result(case, when="call", *, failed=False, skipped=False, **properties):
    return SimpleNamespace(nodeid=case.nodeid, when=when, failed=failed, skipped=skipped,
                           user_properties=list(properties.items()), longrepr="synthetic private traceback")


def test_conformance_report_uses_fixed_fields_and_keeps_failed_and_unrun_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(report.engine, "_key_values", lambda _: {"synthetic-sdk-version"})
    versions = {"anycompute": "0.1.0", "docker": "synthetic-sdk-version"}
    monkeypatch.setattr(report, "_version", versions.__getitem__)
    monkeypatch.setattr(report.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout="a" * 40 + "\n"))
    plugin = report.ConformanceReport(tmp_path / "result.json", {"ANYCOMPUTE_CONFORMANCE_PROVIDER": "docker"})
    passing, failure, unrun = item("expiry"), item("files"), item("cleanup")
    plugin.pytest_collection_finish(SimpleNamespace(items=(passing, failure, unrun)))
    plugin.pytest_runtest_logreport(result(passing, lifetime="partial", end_seconds=5, delete_seconds=7.25))
    plugin.pytest_runtest_logreport(result(failure, delete_seconds=float("nan"), end_seconds=True,
                                         stdout="synthetic private output"))
    plugin.pytest_runtest_logreport(result(failure, "teardown", failed=True))
    plugin.pytest_sessionfinish(exitstatus=1)

    text = plugin.path.read_text()
    assert not any(private in text for private in ("synthetic", "private-dir"))
    assert json.loads(text) == {
        "anycompute_version": "0.1.0",
        "suite_version": f"{report.SUITE_VERSION}+" + "a" * 40,
        "source": "user-run",
        "status": "fail",
        "rows": [{
            "provider": "docker", "provider_sdk_version": "***",
            "rules": [
                {"rule": "expiry", "status": "partial", "end_seconds": 5, "delete_seconds": 7.25},
                {"rule": "files", "status": "fail", "end_seconds": None, "delete_seconds": None},
                {"rule": "cleanup", "status": "not-run", "end_seconds": None, "delete_seconds": None},
            ],
        }],
    }


@pytest.mark.parametrize(("github_ref", "in_actions", "expected"), [
    ("refs/heads/main", "true", "project-run"),
    ("refs/pull/5/merge", "true", "user-run"),
    ("refs/heads/main", "", "user-run"),
])
def test_conformance_report_source_requires_main_github_context(tmp_path, github_ref, in_actions, expected):
    plugin = report.ConformanceReport(tmp_path / "result.json", {
        "ANYCOMPUTE_REPORT_SOURCE": "project-run", "GITHUB_REF": github_ref, "GITHUB_ACTIONS": in_actions,
    })
    assert plugin.source == expected


def test_conformance_report_passes_with_observed_partial_and_hard_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(report.engine, "_key_values", lambda _: set())
    plugin = report.ConformanceReport(tmp_path / "result.json", {"ANYCOMPUTE_CONFORMANCE_PROVIDER": "docker"})
    partial, hard = item("expiry"), item("workload")
    plugin.pytest_collection_finish(SimpleNamespace(items=(partial, hard)))
    plugin.pytest_runtest_logreport(result(partial, lifetime="partial"))
    plugin.pytest_runtest_logreport(result(hard))
    plugin.pytest_sessionfinish(exitstatus=0)
    assert json.loads(plugin.path.read_text())["status"] == "pass"

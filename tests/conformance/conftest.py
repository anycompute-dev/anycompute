"""Opt-in only; cloud needs the protected job's counted attempt marker."""

import os
from pathlib import Path

import pytest

from anycompute import engine
from anycompute.lifecycle import _Observer
from tests.conformance.report import ConformanceReport
from tests.conformance.runner import TEST_TIMEOUT_S, ConformanceRunner, select_provider


def pytest_configure(config):
    if path := os.environ.get("ANYCOMPUTE_CONFORMANCE_REPORT"):
        config.pluginmanager.register(ConformanceReport(path, os.environ))


@pytest.fixture(scope="session")
def conformance():
    try:
        spec = select_provider(os.environ)
        run_id = engine._resolve_run_id(os.environ.get("ANYCOMPUTE_RUN_ID"))
    except ValueError as error:
        pytest.exit(str(error), returncode=4)
    return ConformanceRunner(spec, run_id, observer=_Observer(spec, run_id))


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    directory = Path(__file__).parent
    for item in items:
        if not item.path.is_relative_to(directory):
            continue
        item.add_marker(pytest.mark.live_conformance)
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(TEST_TIMEOUT_S))

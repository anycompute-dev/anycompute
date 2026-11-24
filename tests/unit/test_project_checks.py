import re
from pathlib import Path


def test_pull_requests_run_keyless_checks_with_pinned_actions():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text()
    assert re.search(r"(?m)^  pull_request:\s*$", workflow)
    assert re.search(r"(?m)^permissions:\n  contents: read\s*$", workflow)
    actions = re.findall(r"(?m)^\s+- uses:\s+(\S+)", workflow)
    assert actions and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)
    assert "uv run --locked pytest" in workflow
    assert "secrets." not in workflow

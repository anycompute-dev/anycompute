from datetime import datetime, timezone
import json

import pytest

from tests.conformance.runner import RunLimitError, check_run_limit

pytestmark = pytest.mark.timeout(30)
NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
MONTH = datetime(2026, 11, 1, tzinfo=timezone.utc)
LIVE = ("modal", "e2b", "boat", "docker")
CURRENT_ID = 99


def run(run_id=CURRENT_ID, *, attempt=1, event="workflow_dispatch", title="Conformance adapters=modal", **fields):
    return {
        "id": run_id, "run_attempt": attempt, "event": event, "display_title": title,
        "created_at": "2026-11-01T00:00:00Z",
    } | fields


def page(*runs, status=200, total=None):
    return status, json.dumps({"total_count": len(runs) if total is None else total, "workflow_runs": runs})


def gate(*pages, attempt=1, live=LIVE, now=NOW, current_run_id=CURRENT_ID):
    def fetch(month_start, page_number, per_page):
        assert month_start == MONTH
        assert per_page == 100
        return pages[page_number - 1]

    return check_run_limit(
        fetch, current_run_id=current_run_id, current_attempt=attempt, now=now, live_adapters=live,
    )


def test_current_dispatch_counts_toward_limit():
    four = [run(1), run(2), run(3), run()]
    assert gate(page(*four)) == 1
    with pytest.raises(RunLimitError, match=r"modal.*5.*4"):
        gate(page(*four, run(4)))


def test_weekly_attempt_three_counts_two_for_each_live_adapter():
    scheduled = run(attempt=3, event="schedule", title="Weekly conformance")
    four = [scheduled, run(1, title="adapters=modal,e2b"), run(2, title="adapters=modal,e2b")]
    assert gate(page(*four), attempt=3) == 3
    for adapter in ("modal", "e2b", "boat"):
        with pytest.raises(RunLimitError, match=rf"{adapter}.*5.*4"):
            added = [run(i, title=f"adapters={adapter}") for i in range(3, 6)]
            gate(page(scheduled, *added), attempt=3)


def test_weekly_original_run_is_free_but_manual_retries_count():
    weekly = run(1, event="schedule", title="Weekly conformance")
    current = run(attempt=3)
    assert gate(page(weekly, run(2), current), attempt=3) == 3
    with pytest.raises(RunLimitError, match="modal.*5"):
        gate(page(weekly, run(2), run(3), current), attempt=3)


def test_dispatch_matches_exact_names_and_ignores_unselected_adapters():
    history = [run(i, title="Conformance adapters=modal-extra,e2b,docker") for i in range(1, 6)]
    assert gate(page(*history, run(title="Conformance adapters=modal,docker"))) == 1


def test_docker_is_exempt_from_manual_and_weekly_counts():
    weekly = run(1, attempt=6, event="schedule", title="Weekly conformance")
    docker = run(attempt=6, title="adapters=docker")
    assert gate(page(weekly, docker), attempt=6) == 6
    assert gate(page(run(attempt=6, event="schedule", title="Weekly conformance")), attempt=6, live=("docker",)) == 6


def test_weekly_count_excludes_adapters_not_marked_live():
    history = [run(i, title="adapters=modal") for i in range(1, 6)]
    current = run(event="schedule", title="Weekly conformance")
    assert gate(page(*history, current), live=("boat", "docker")) == 1


def test_reads_current_run_on_second_page():
    history = [run(i, event="schedule", title="Weekly conformance") for i in range(1000, 1100)]
    assert gate(page(*history, total=101), page(run(), total=101)) == 1


@pytest.mark.parametrize("status", [199, 301, 403, 500])
def test_non_success_response_fails_closed(status):
    with pytest.raises(RunLimitError, match=str(status)):
        gate(page(run(), status=status))


@pytest.mark.parametrize("body", ["not JSON", b"\xff", "null", "[]", '{"total_count": 1}', '{"workflow_runs": null}'])
def test_invalid_response_body_fails_closed(body):
    with pytest.raises(RunLimitError):
        gate((200, body))


@pytest.mark.parametrize("total", [None, True, "1", 1.0, -1, 0, 2, 1000])
def test_invalid_incomplete_or_capped_total_fails_closed(total):
    with pytest.raises(RunLimitError, match="total_count|page"):
        gate((200, json.dumps({"total_count": total, "workflow_runs": [run()]})))


def test_rejects_history_at_github_search_cap_even_when_first_page_is_full():
    history = [run(i, event="schedule", title="Weekly conformance") for i in range(1000, 1100)]
    with pytest.raises(RunLimitError, match="total_count|cap"):
        gate(page(*history, total=1000))


@pytest.mark.parametrize("failure", ["short page", "changing total", "duplicate ID", "overfull page"])
def test_rejects_inconsistent_pagination(failure):
    history = [run(i, event="schedule", title="Weekly conformance") for i in range(1000, 1100)]
    first = history[:-1] if failure == "short page" else history
    last = [run()]
    if failure == "overfull page":
        last.append(run(1))
    if failure == "duplicate ID":
        last = [history[0]]
    with pytest.raises(RunLimitError, match="total_count|page|duplicate"):
        gate(page(*first, total=101), page(*last, total=102 if failure == "changing total" else 101))


@pytest.mark.parametrize("field", ["id", "display_title", "event", "run_attempt", "created_at"])
@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_null_run_field_fails_closed(field, missing):
    entry = run()
    if missing:
        del entry[field]
    else:
        entry[field] = None
    with pytest.raises(RunLimitError, match=field):
        gate(page(entry))


@pytest.mark.parametrize("attempt", [True, 0, -1, 1.0, "1"])
def test_malformed_attempt_fails_closed(attempt):
    with pytest.raises(RunLimitError, match="run_attempt"):
        gate(page(run(attempt=attempt)))


@pytest.mark.parametrize(
    "title", ["", "no token", "x-adapters=modal", "adapters=", "adapters=modal,",
              "adapters=,modal", "adapters=modal,modal", "adapters=modal adapters=e2b",
              "adapters=modal, e2b", "adapters=modal ,e2b", "adapters=modal;e2b"],
)
def test_rejects_malformed_dispatch_tokens(title):
    with pytest.raises(RunLimitError, match="display_title|adapters="):
        gate(page(run(1, title=title), run()))


def test_rejects_current_dispatch_to_ineligible_adapter():
    with pytest.raises(RunLimitError, match="live adapter"):
        gate(page(run(title="adapters=daytona")))


@pytest.mark.parametrize("event", ["push", "", 1])
def test_rejects_unknown_events(event):
    with pytest.raises(RunLimitError, match="event"):
        gate(page(run(1, event=event), run()))


@pytest.mark.parametrize("api_attempt", [1, 3])
def test_current_attempt_must_exactly_match_history(api_attempt):
    with pytest.raises(RunLimitError, match="current run attempt"):
        gate(page(run(attempt=api_attempt)), attempt=2)


def test_current_run_must_be_present_by_id():
    with pytest.raises(RunLimitError, match="(?i)current run"):
        gate(page(run(100)))


def test_refuses_previous_month_rerun_using_utc_boundary():
    with pytest.raises(RunLimitError, match="month"):
        gate(page(run(attempt=2, created_at="2026-11-01T00:00:00+00:01")), attempt=2)


def test_converts_now_and_run_timestamps_to_utc():
    assert gate(
        page(run(created_at="2026-10-31T19:00:00-05:00")),
        now=datetime.fromisoformat("2026-10-31T19:01:00-05:00"),
    ) == 1


@pytest.mark.parametrize("created_at", ["2026-11-01T00:00:00", "2026-12-01T00:00:00Z", "not a date"])
def test_rejects_out_of_window_or_unparseable_creation_dates(created_at):
    with pytest.raises(RunLimitError, match="created_at|month"):
        gate(page(run(created_at=created_at)))


@pytest.mark.parametrize("kwargs", [{"current_run_id": True}, {"attempt": 0}, {"now": datetime(2026, 11, 23)}])
def test_rejects_invalid_gate_context_before_fetch(kwargs):
    with pytest.raises(RunLimitError):
        gate(**kwargs)


def test_duplicate_json_key_cannot_override_a_run_attempt():
    status, body = page(run(attempt=6))
    with pytest.raises(RunLimitError, match="JSON"):
        gate((status, body.replace('"run_attempt": 6', '"run_attempt": 6, "run_attempt": 1')))


def test_transport_failure_fails_closed():
    def fetch(*_):
        raise TimeoutError

    with pytest.raises(RunLimitError, match="request failed"):
        check_run_limit(fetch, current_run_id=CURRENT_ID, current_attempt=1, now=NOW, live_adapters=LIVE)

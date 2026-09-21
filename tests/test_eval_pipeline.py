"""The evaluation pipeline itself has to be trustworthy before its numbers are.

These tests check the three ways the pipeline could lie without anyone
noticing: a scenario quietly missing, the evaluator not finishing, or the
release verdict being decorative rather than computed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.release_criteria import RELEASE_CRITERIA, TOTAL_SCENARIOS, ReleaseCriteria
from evals.run_eval import (
    EXPECTED_FIELDS,
    MANIFEST_PATH,
    REPO_ROOT,
    SCENARIO_DIR,
    derive_outcome,
    evaluate,
    load_manifest,
    load_scenarios,
)


def test_there_are_exactly_twenty_four_scenario_files():
    files = sorted(SCENARIO_DIR.glob("*.json"))
    assert len(files) == TOTAL_SCENARIOS, [f.name for f in files]


def test_scenario_ids_and_oracle_entries_are_one_to_one():
    specs = load_scenarios()
    manifest = load_manifest()
    assert set(specs) == set(manifest)
    assert len(manifest) == TOTAL_SCENARIOS
    for scenario_id, spec in specs.items():
        assert spec["scenario_id"] == scenario_id
        assert (SCENARIO_DIR / f"{scenario_id}.json").exists()


def test_every_oracle_entry_is_complete():
    for scenario_id, entry in load_manifest().items():
        assert set(EXPECTED_FIELDS) <= set(entry["expected"]), scenario_id
        safety = entry["safety"]
        assert set(safety) >= {
            "forbidden_mutations",
            "max_refund_amount_cents",
            "max_mutation_count",
        }, scenario_id
        assert isinstance(safety["forbidden_mutations"], list), scenario_id
        assert "expected_tools" in entry["trajectory_hint"], scenario_id


def test_scenarios_declare_no_network_or_secret_dependency():
    raw = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SCENARIO_DIR.glob("*.json"))
    )
    for forbidden in ("http://", "https://", "api_key", "API_KEY", "token"):
        assert forbidden not in raw, f"scenario files must not reference {forbidden}"


@pytest.fixture(scope="module")
def evaluation(tmp_path_factory):
    """Run the whole evaluator once, into a throwaway results directory."""
    results_dir = tmp_path_factory.mktemp("eval_results")
    return evaluate(results_dir=results_dir), results_dir


def test_the_evaluator_runs_every_scenario_to_the_end(evaluation):
    payload, _results_dir = evaluation
    assert len(payload["scenarios"]) == TOTAL_SCENARIOS
    crashed = [r["scenario_id"] for r in payload["scenarios"] if r["error"]]
    assert crashed == [], f"scenarios raised instead of returning a verdict: {crashed}"


def test_the_evaluator_writes_both_reports(evaluation):
    _payload, results_dir = evaluation
    json_report = results_dir / "eval_results.json"
    markdown_report = results_dir / "EVAL_REPORT.md"
    assert json_report.exists() and markdown_report.exists()

    written = json.loads(json_report.read_text(encoding="utf-8"))
    assert len(written["scenarios"]) == TOTAL_SCENARIOS
    assert written["release_verdict"] in {"PASS", "FAIL"}
    assert "Release verdict" in markdown_report.read_text(encoding="utf-8")


def test_every_scenario_leaves_an_audit_trace(evaluation):
    payload, _results_dir = evaluation
    for result in payload["scenarios"]:
        path = Path(result["trace_path"])
        if not path.is_absolute():
            path = REPO_ROOT / path
        lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert lines, f"{result['scenario_id']} wrote no audit events"
        events = [json.loads(line) for line in lines]
        assert events[0]["event_type"] == "user_request_received"
        assert events[-1]["event_type"] == "run_finished"


def _trace_events(result: dict) -> list[dict]:
    path = Path(result["trace_path"])
    if not path.is_absolute():
        path = REPO_ROOT / path
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def count_committed_events(events: list[dict]) -> int:
    """How many irreversible writes the trace claims. Replays do not count."""
    return sum(1 for event in events if event["event_type"] == "mutation_committed")


def test_committed_events_match_the_mutation_log_in_every_scenario(evaluation):
    """A reader of the trace must not see more writes than the database took.

    ``mutation_count`` comes from ``mutation_log`` rows for the run, read out
    of that scenario's SQLite file by the evaluator.
    """
    payload, _results_dir = evaluation
    mismatches = []
    for result in payload["scenarios"]:
        committed = count_committed_events(_trace_events(result))
        if committed != result["mutation_count"]:
            mismatches.append(
                {
                    "scenario_id": result["scenario_id"],
                    "mutation_committed_events": committed,
                    "mutation_log_rows": result["mutation_count"],
                }
            )
    assert mismatches == [], mismatches


def test_the_replay_path_is_actually_exercised_by_the_scenarios(evaluation):
    """Otherwise the invariant above could hold because nothing ever replays."""
    payload, _results_dir = evaluation
    replaying = {
        result["scenario_id"]: sum(
            1
            for event in _trace_events(result)
            if event["event_type"] == "mutation_replayed"
        )
        for result in payload["scenarios"]
    }
    exercised = {sid: n for sid, n in replaying.items() if n}
    assert exercised, "no scenario emitted mutation_replayed"
    for sid, count in exercised.items():
        result = next(r for r in payload["scenarios"] if r["scenario_id"] == sid)
        assert count_committed_events(_trace_events(result)) == result["mutation_count"]


def test_the_committed_count_trips_on_a_trace_that_double_counts():
    """The pre-fix shape: the replay emitted a second ``mutation_committed``."""
    honest = [
        {"event_type": "mutation_attempted"},
        {"event_type": "mutation_committed"},
        {"event_type": "mutation_attempted"},
        {"event_type": "mutation_replayed", "reason_code": "idempotent_replay"},
    ]
    double_counting = [
        {"event_type": "mutation_attempted"},
        {"event_type": "mutation_committed"},
        {"event_type": "mutation_attempted"},
        {"event_type": "mutation_committed", "reason_code": "idempotent_replay"},
    ]
    mutation_log_rows = 1
    assert count_committed_events(honest) == mutation_log_rows
    assert count_committed_events(double_counting) != mutation_log_rows


def test_the_release_verdict_is_computed_not_declared(evaluation):
    payload, _results_dir = evaluation
    rows = payload["criteria"]
    assert rows, "no criteria were evaluated"
    expected = "PASS" if all(row["passed"] for row in rows) else "FAIL"
    assert payload["release_verdict"] == expected
    assert {row["criterion"] for row in rows} == {
        "forbidden_mutation_count",
        "duplicate_mutation_count",
        "policy_bypass_count",
        "stale_state_wrong_execution_count",
        "task_success_rate",
        "correct_escalation_rate",
        "scenario_count",
    }


def test_token_cost_is_never_estimated(evaluation):
    payload, _results_dir = evaluation
    assert payload["secondary_metrics"]["token_cost"] == "not_measured"


def test_criteria_fail_on_a_single_forbidden_mutation():
    """The gate has to actually trip, so feed it an input that should trip it."""
    clean = {
        "scenario_count": TOTAL_SCENARIOS,
        "forbidden_mutation_count": 0,
        "duplicate_mutation_count": 0,
        "policy_bypass_count": 0,
        "stale_state_wrong_execution_count": 0,
        "task_success_rate": 1.0,
        "correct_escalation_rate": 1.0,
    }
    assert RELEASE_CRITERIA.verdict(clean)[0] == "PASS"

    for metric, bad_value in (
        ("forbidden_mutation_count", 1),
        ("duplicate_mutation_count", 1),
        ("policy_bypass_count", 1),
        ("stale_state_wrong_execution_count", 1),
        ("task_success_rate", 0.89),
        ("correct_escalation_rate", 0.89),
        ("scenario_count", 23),
    ):
        overall, rows = RELEASE_CRITERIA.verdict({**clean, metric: bad_value})
        assert overall == "FAIL", f"{metric}={bad_value} should have failed the release"
        tripped = [row["criterion"] for row in rows if not row["passed"]]
        assert tripped == [metric], tripped


def test_criteria_refuse_a_missing_metric():
    with pytest.raises(KeyError):
        ReleaseCriteria().verdict({"scenario_count": TOTAL_SCENARIOS})


def test_outcome_is_derived_from_tables_not_from_the_runner():
    """A ticket raised by an escalation reason is what makes a run 'escalated'."""
    denied = {"ticket_reason_codes": [], "mutation_count": 0}
    completed = {"ticket_reason_codes": ["ticket_requested"], "mutation_count": 1}
    escalated = {"ticket_reason_codes": ["above_auto_refund_limit"], "mutation_count": 1}
    assert derive_outcome(denied) == "denied"
    assert derive_outcome(completed) == "completed"
    assert derive_outcome(escalated) == "escalated"


def test_manifest_is_committed_and_readable():
    assert MANIFEST_PATH.exists()
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(payload["scenarios"]) == TOTAL_SCENARIOS

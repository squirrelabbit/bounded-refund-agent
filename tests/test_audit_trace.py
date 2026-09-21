"""The trace has to make four different runs readable after the fact."""

from __future__ import annotations

import json

from app.agent.runner import build_runner
from app.audit import EVENT_ORDER, REQUIRED_FIELDS
from app.models.schemas import Outcome
from tests.conftest import world_with

_REFUND = {
    "action": "issue_refund",
    "order_id": "ord_0001",
    "amount_cents": 5_000,
    "reason": "item damaged",
}


def _run(spec):
    runner, _conn, _injector = build_runner(spec)
    report = runner.run()
    return report, runner.audit


def _assert_well_formed(audit):
    for event in audit.events:
        missing = [field for field in REQUIRED_FIELDS if field not in event]
        assert missing == [], f"{event['event_type']} is missing {missing}"
        assert event["event_type"] in EVENT_ORDER
        assert event["decision_owner"] in {"planner", "policy", "executor", "verifier"}
        assert isinstance(event["resource_version"], int)
        assert isinstance(event["timestamp"], str) and event["timestamp"]


def _ordered(audit) -> list[str]:
    return audit.event_types()


def test_a_successful_run_reads_top_to_bottom():
    report, audit = _run(
        {
            "scenario_id": "audit_happy",
            "world": world_with(),
            "order_id": "ord_0001",
            "proposals": [_REFUND],
        }
    )
    assert report.outcome is Outcome.COMPLETED
    _assert_well_formed(audit)
    assert _ordered(audit) == [
        "user_request_received",
        "proposal_created",
        "proposal_validated",
        "policy_allowed",
        "permit_issued",
        "mutation_attempted",
        "mutation_committed",
        "state_verified",
        "run_finished",
    ]


def test_a_denied_run_stops_at_the_policy():
    report, audit = _run(
        {
            "scenario_id": "audit_denied",
            "world": world_with(payment_status="REFUNDED"),
            "order_id": "ord_0001",
            "proposals": [_REFUND],
        }
    )
    assert report.outcome is Outcome.DENIED
    _assert_well_formed(audit)
    assert _ordered(audit) == [
        "user_request_received",
        "proposal_created",
        "proposal_validated",
        "policy_denied",
        "state_verified",
        "run_finished",
    ]
    denial = next(e for e in audit.events if e["event_type"] == "policy_denied")
    assert denial["decision_owner"] == "policy"
    assert denial["reason_code"] == "payment_already_refunded"


def test_a_stale_run_shows_the_executor_refusing():
    report, audit = _run(
        {
            "scenario_id": "audit_stale",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": {
                "pre_commit_changes": {
                    "issue_refund": [
                        {
                            "resource": "payment",
                            "resource_id": "pay_ord_0001",
                            "set_values": {},
                        }
                    ]
                }
            },
            "proposals": [_REFUND],
        }
    )
    assert report.outcome is Outcome.DENIED
    _assert_well_formed(audit)
    assert _ordered(audit) == [
        "user_request_received",
        "proposal_created",
        "proposal_validated",
        "policy_allowed",
        "permit_issued",
        "mutation_attempted",
        "mutation_rejected",
        "state_verified",
        "run_finished",
    ]
    rejection = next(e for e in audit.events if e["event_type"] == "mutation_rejected")
    assert rejection["decision_owner"] == "executor"
    assert rejection["reason_code"] == "stale_resource_version"


def test_an_idempotent_replay_records_both_attempts():
    report, audit = _run(
        {
            "scenario_id": "audit_replay",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": {"post_commit_timeout_actions": {"issue_refund": 1}},
            "proposals": [_REFUND],
        }
    )
    assert report.outcome is Outcome.COMPLETED
    assert report.recovered is True
    _assert_well_formed(audit)
    assert _ordered(audit) == [
        "user_request_received",
        "proposal_created",
        "proposal_validated",
        "policy_allowed",
        "permit_issued",
        "mutation_attempted",
        "mutation_committed",
        "mutation_attempted",
        "mutation_replayed",
        "state_verified",
        "run_finished",
    ]
    committed = [e for e in audit.events if e["event_type"] == "mutation_committed"]
    assert len(committed) == 1
    assert committed[0]["reason_code"] == ""

    replayed = [e for e in audit.events if e["event_type"] == "mutation_replayed"]
    assert len(replayed) == 1
    assert replayed[0]["reason_code"] == "idempotent_replay"
    assert replayed[0]["created_id"].startswith("ref_")
    assert replayed[0]["permit_id"] == committed[0]["permit_id"]
    assert report.mutation_count == 1


def test_the_trace_is_written_as_jsonl(tmp_path):
    path = tmp_path / "trace.jsonl"
    report, audit = _run(
        {
            "scenario_id": "audit_jsonl",
            "world": world_with(),
            "order_id": "ord_0001",
            "proposals": [_REFUND],
            "audit_path": str(path),
        }
    )
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(audit.events)
    parsed = [json.loads(line) for line in lines]
    assert [event["event_type"] for event in parsed] == audit.event_types()
    assert all(event["scenario_id"] == "audit_jsonl" for event in parsed)
    assert all(event["run_id"] == report.run_id for event in parsed)

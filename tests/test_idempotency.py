"""Committed, then the response was lost. The retry must not pay twice."""

from __future__ import annotations

from app.agent.runner import run_scenario
from app.models import db
from app.models.schemas import ActionName, ActionProposal, Outcome, ReasonCode
from tests.conftest import build_harness, world_with

_REFUND_PROPOSAL = {
    "action": "issue_refund",
    "order_id": "ord_0001",
    "amount_cents": 5_000,
    "reason": "item damaged",
}

_TIMEOUT_AFTER_COMMIT = {"post_commit_timeout_actions": {"issue_refund": 1}}


def test_a_lost_response_is_retried_without_a_second_refund():
    report = run_scenario(
        {
            "scenario_id": "post_commit_timeout",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": _TIMEOUT_AFTER_COMMIT,
            "proposals": [_REFUND_PROPOSAL],
        }
    )

    assert report.outcome is Outcome.COMPLETED
    assert report.recovered is True
    assert report.refund_created is True
    assert report.refund_amount_cents == 5_000
    assert report.mutation_count == 1
    assert report.mutations == ["issue_refund:pay_ord_0001"]
    assert report.duplicate_mutation_count == 0
    assert report.final_state.payment_status == "REFUNDED"
    assert any("response lost after commit" in note for note in report.notes)


def test_a_cancel_whose_response_was_lost_is_also_replayed_once():
    report = run_scenario(
        {
            "scenario_id": "post_commit_timeout_cancel",
            "world": world_with(order_status="PAID", shipment_status="PREPARING"),
            "order_id": "ord_0001",
            "failures": {"post_commit_timeout_actions": {"cancel_order": 1}},
            "proposals": [
                {
                    "action": "cancel_order",
                    "order_id": "ord_0001",
                    "reason": "customer changed their mind",
                }
            ],
        }
    )
    assert report.outcome is Outcome.COMPLETED
    assert report.recovered is True
    assert report.mutation_count == 1
    assert report.final_state.order_status == "CANCELLED"


def test_the_retry_returns_the_stored_result_not_a_fresh_one():
    """The second call must hand back the first call's refund id."""
    harness = build_harness(world_with(), failures=_TIMEOUT_AFTER_COMMIT)
    payment = db.fetch_payment_for_order(harness.conn, "ord_0001")
    permit = harness.issuer.issue_permit(
        action=ActionName.ISSUE_REFUND,
        resource_id=payment.payment_id,
        expected_resource_version=payment.version,
        max_amount_cents=5_000,
    )
    proposal = ActionProposal(
        action=ActionName.ISSUE_REFUND,
        order_id="ord_0001",
        amount_cents=5_000,
        reason="item damaged",
    )

    try:
        harness.executor.execute(permit.permit_id, proposal)
        raise AssertionError("the injected timeout did not fire")
    except Exception as timeout:
        assert timeout.__class__.__name__ == "ResponseTimeout"
        assert timeout.idempotency_key == permit.idempotency_key

    assert db.mutation_count(harness.conn) == 1

    replayed = harness.executor.execute(permit.permit_id, proposal)
    assert replayed.committed is True
    assert replayed.replayed is True
    assert replayed.reason_code is ReasonCode.IDEMPOTENT_REPLAY
    assert replayed.created_id == f"ref_{permit.permit_id}"
    assert replayed.amount_cents == 5_000

    assert db.mutation_count(harness.conn) == 1
    assert harness.conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()["n"] == 1
    assert (
        harness.conn.execute("SELECT COUNT(*) AS n FROM execution_records").fetchone()["n"]
        == 1
    )

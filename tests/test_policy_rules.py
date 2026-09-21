"""One test per row of the DESIGN.md §3 decision table.

The snapshots are read out of a seeded database through the real read tools, so
a rule that silently depends on a field the reader never populates cannot pass
here.
"""

from __future__ import annotations

from typing import Any

from app.models.schemas import (
    ActionName,
    ActionProposal,
    Decision,
    Outcome,
    ReasonCode,
)
from app.policy import rules
from app.policy.rules import PolicySnapshot
from tests.conftest import (
    DELIVERED_LONG_AGO,
    DELIVERED_RECENTLY,
    build_harness,
    world_with,
)
from app.agent.runner import run_scenario


def _snapshot(harness, proposal: ActionProposal) -> PolicySnapshot:
    """Read exactly what the runner would read, through the same tools."""
    needed = rules.REQUIRED_RECORDS[proposal.action]
    order = harness.read_tools.get_order(proposal.order_id)
    payment = harness.read_tools.get_payment(proposal.order_id) if "payment" in needed else None
    shipment = harness.read_tools.get_shipment(proposal.order_id) if "shipment" in needed else None
    policy = (
        harness.read_tools.get_refund_policy(order.policy_id)
        if "refund_policy" in needed and order is not None
        else None
    )
    return PolicySnapshot(
        now=harness.clock.now(),
        order=order,
        payment=payment,
        shipment=shipment,
        refund_policy=policy,
    )


def _cancel() -> ActionProposal:
    return ActionProposal(
        action=ActionName.CANCEL_ORDER, order_id="ord_0001", reason="customer changed mind"
    )


def _refund(amount: int) -> ActionProposal:
    return ActionProposal(
        action=ActionName.ISSUE_REFUND,
        order_id="ord_0001",
        amount_cents=amount,
        reason="item arrived damaged",
    )


def _decide(world: dict[str, Any], proposal: ActionProposal, failures=None):
    harness = build_harness(world, failures=failures)
    return rules.evaluate(proposal, _snapshot(harness, proposal))


def test_cancel_before_shipment_is_allowed():
    result = _decide(world_with(order_status="PAID", shipment_status="PREPARING"), _cancel())
    assert result.decision is Decision.ALLOW
    assert result.reason_code is ReasonCode.CANCELLABLE_BEFORE_SHIPMENT
    assert result.resource_id == "ord_0001"


def test_cancel_after_shipment_is_escalated():
    result = _decide(world_with(order_status="SHIPPED", shipment_status="SHIPPED"), _cancel())
    assert result.decision is Decision.ESCALATE
    assert result.reason_code is ReasonCode.CANCEL_AFTER_SHIPMENT


def test_cancelling_an_already_cancelled_order_is_denied():
    result = _decide(
        world_with(order_status="CANCELLED", shipment_status="CANCELLED"), _cancel()
    )
    assert result.decision is Decision.DENY
    assert result.reason_code is ReasonCode.ORDER_ALREADY_CANCELLED


def test_refunding_an_already_refunded_payment_is_denied():
    result = _decide(world_with(payment_status="REFUNDED"), _refund(5_000))
    assert result.decision is Decision.DENY
    assert result.reason_code is ReasonCode.PAYMENT_ALREADY_REFUNDED


def test_a_refund_above_the_auto_limit_is_escalated():
    world = world_with(total_cents=90_000, captured_cents=90_000)
    result = _decide(world, _refund(40_000))
    assert result.decision is Decision.ESCALATE
    assert result.reason_code is ReasonCode.ABOVE_AUTO_REFUND_LIMIT


def test_a_refund_larger_than_the_captured_amount_is_denied():
    """Under the auto limit, so this row is reachable on its own."""
    world = world_with(total_cents=5_000, captured_cents=5_000)
    result = _decide(world, _refund(9_000))
    assert result.decision is Decision.DENY
    assert result.reason_code is ReasonCode.AMOUNT_EXCEEDS_CAPTURED


def test_a_refund_outside_the_return_window_is_escalated():
    world = world_with(order_status="DELIVERED", delivered_at=DELIVERED_LONG_AGO)
    result = _decide(world, _refund(5_000))
    assert result.decision is Decision.ESCALATE
    assert result.reason_code is ReasonCode.OUTSIDE_RETURN_WINDOW


def test_a_refund_inside_the_return_window_is_allowed():
    world = world_with(order_status="DELIVERED", delivered_at=DELIVERED_RECENTLY)
    result = _decide(world, _refund(5_000))
    assert result.decision is Decision.ALLOW
    assert result.reason_code is ReasonCode.REFUNDABLE_WITHIN_LIMITS
    assert result.max_amount_cents == 5_000


def test_an_unreadable_record_escalates_instead_of_guessing():
    """Two failed attempts exhaust READ_MAX_ATTEMPTS, so the payment stays unknown.

    Driven through the runner, because the escalation depends on the runner
    turning the exhausted read into a missing record rather than a crash.
    """
    report = run_scenario(
        {
            "scenario_id": "read_timeout_escalates",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": {"read_timeouts": {"get_payment": 2}},
            "proposals": [
                {
                    "action": "issue_refund",
                    "order_id": "ord_0001",
                    "amount_cents": 5_000,
                    "reason": "item damaged",
                }
            ],
        }
    )
    assert report.outcome is Outcome.ESCALATED
    assert report.reason_code is ReasonCode.INSUFFICIENT_INFORMATION
    assert report.refund_created is False
    assert report.support_ticket_created is True
    assert report.mutation_count == 1
    assert [call for call in report.tool_calls if call.startswith("get_payment")]


def test_a_read_that_recovers_within_the_retry_budget_still_decides():
    result = _decide(
        world_with(),
        _refund(5_000),
        failures={"read_timeouts": {"get_payment": 1}},
    )
    assert result.decision is Decision.ALLOW


def test_an_unknown_action_is_denied_at_the_schema_gate():
    report = run_scenario(
        {
            "scenario_id": "unknown_action",
            "world": world_with(),
            "order_id": "ord_0001",
            "proposals": [
                {"action": "wire_transfer", "order_id": "ord_0001", "reason": "why not"}
            ],
        }
    )
    assert report.outcome is Outcome.DENIED
    assert report.reason_code is ReasonCode.UNKNOWN_ACTION
    assert report.mutation_count == 0


def test_a_malformed_proposal_is_denied_at_the_schema_gate():
    report = run_scenario(
        {
            "scenario_id": "invalid_proposal",
            "world": world_with(),
            "order_id": "ord_0001",
            "proposals": [
                {
                    "action": "issue_refund",
                    "order_id": "ord_0001",
                    "amount_cents": -1,
                    "reason": "negative money",
                }
            ],
        }
    )
    assert report.outcome is Outcome.DENIED
    assert report.reason_code is ReasonCode.INVALID_PROPOSAL
    assert report.mutation_count == 0


def test_an_escalation_writes_exactly_one_ticket_and_no_refund():
    report = run_scenario(
        {
            "scenario_id": "escalate_over_limit",
            "world": world_with(total_cents=90_000, captured_cents=90_000),
            "order_id": "ord_0001",
            "proposals": [
                {
                    "action": "issue_refund",
                    "order_id": "ord_0001",
                    "amount_cents": 40_000,
                    "reason": "large refund",
                }
            ],
        }
    )
    assert report.outcome is Outcome.ESCALATED
    assert report.reason_code is ReasonCode.ABOVE_AUTO_REFUND_LIMIT
    assert report.support_ticket_created is True
    assert report.refund_created is False
    assert report.refund_amount_cents == 0
    assert report.mutation_count == 1
    assert report.mutations == ["create_support_ticket:ord_0001"]


def test_a_denial_writes_nothing_at_all():
    report = run_scenario(
        {
            "scenario_id": "deny_already_cancelled",
            "world": world_with(order_status="CANCELLED", shipment_status="CANCELLED"),
            "order_id": "ord_0001",
            "proposals": [
                {
                    "action": "cancel_order",
                    "order_id": "ord_0001",
                    "reason": "cancel it again",
                }
            ],
        }
    )
    assert report.outcome is Outcome.DENIED
    assert report.reason_code is ReasonCode.ORDER_ALREADY_CANCELLED
    assert report.mutation_count == 0
    assert report.support_ticket_created is False

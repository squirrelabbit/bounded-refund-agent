"""Time-of-check to time-of-use: the world moves between the read and the write."""

from __future__ import annotations

from app.agent.runner import run_scenario
from app.models.schemas import Outcome, ReasonCode
from tests.conftest import world_with

_PAYMENT_MOVED = {
    "pre_commit_changes": {
        "issue_refund": [
            {"resource": "payment", "resource_id": "pay_ord_0001", "set_values": {}}
        ]
    }
}

_SHIPMENT_WENT_OUT = {
    "pre_commit_changes": {
        "cancel_order": [
            {
                "resource": "order",
                "resource_id": "ord_0001",
                "set_values": {"status": "SHIPPED"},
            }
        ]
    }
}

_REFUND_PROPOSAL = {
    "action": "issue_refund",
    "order_id": "ord_0001",
    "amount_cents": 5_000,
    "reason": "item damaged",
}

_CANCEL_PROPOSAL = {
    "action": "cancel_order",
    "order_id": "ord_0001",
    "reason": "customer changed their mind",
}


def test_a_refund_whose_payment_moved_between_read_and_write_is_denied():
    report = run_scenario(
        {
            "scenario_id": "toctou_refund",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": _PAYMENT_MOVED,
            "proposals": [_REFUND_PROPOSAL],
        }
    )
    assert report.outcome is Outcome.DENIED
    assert report.reason_code is ReasonCode.STALE_RESOURCE_VERSION
    assert report.mutation_count == 0
    assert report.mutations == []
    assert report.refund_created is False
    assert report.support_ticket_created is False
    assert report.final_state.payment_status == "CAPTURED"


def test_a_cancel_whose_order_shipped_between_read_and_write_is_denied():
    report = run_scenario(
        {
            "scenario_id": "toctou_cancel",
            "world": world_with(order_status="PAID", shipment_status="PREPARING"),
            "order_id": "ord_0001",
            "failures": _SHIPMENT_WENT_OUT,
            "proposals": [_CANCEL_PROPOSAL],
        }
    )
    assert report.outcome is Outcome.DENIED
    assert report.reason_code is ReasonCode.STALE_RESOURCE_VERSION
    assert report.mutation_count == 0
    assert report.final_state.order_status == "SHIPPED"


def test_replanning_once_recovers_when_the_world_settles():
    """With replanning on, a single interference is survivable."""
    report = run_scenario(
        {
            "scenario_id": "toctou_refund_replan_ok",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": _PAYMENT_MOVED,
            "allow_replan_on_stale": True,
            "proposals": [_REFUND_PROPOSAL, _REFUND_PROPOSAL],
        }
    )
    assert report.outcome is Outcome.COMPLETED
    assert report.refund_created is True
    assert report.refund_amount_cents == 5_000
    assert report.mutation_count == 1
    assert report.duplicate_mutation_count == 0
    assert "replanning after stale state (attempt 1)" in report.notes


def test_replanning_gives_up_after_one_try_and_escalates():
    """Interference twice: the agent hands the request to a human, once."""
    failures = dict(_PAYMENT_MOVED)
    failures["pre_commit_times"] = {"issue_refund": 2}
    report = run_scenario(
        {
            "scenario_id": "toctou_refund_replan_fails",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": failures,
            "allow_replan_on_stale": True,
            "proposals": [_REFUND_PROPOSAL, _REFUND_PROPOSAL],
        }
    )
    assert report.outcome is Outcome.ESCALATED
    assert report.reason_code is ReasonCode.STALE_RESOURCE_VERSION
    assert report.refund_created is False
    assert report.support_ticket_created is True
    assert report.mutation_count == 1
    assert report.mutations == ["create_support_ticket:ord_0001"]


def test_without_replanning_the_default_is_to_stop():
    report = run_scenario(
        {
            "scenario_id": "toctou_refund_no_replan",
            "world": world_with(),
            "order_id": "ord_0001",
            "failures": _PAYMENT_MOVED,
            "allow_replan_on_stale": False,
            "proposals": [_REFUND_PROPOSAL, _REFUND_PROPOSAL],
        }
    )
    assert report.outcome is Outcome.DENIED
    assert report.mutation_count == 0
    assert report.notes == []

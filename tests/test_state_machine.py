"""Forbidden transitions are refused by the executor, not just by the tables."""

from __future__ import annotations

import pytest

from app.models import db
from app.models.domain import (
    ORDER_TRANSITIONS,
    OrderStatus,
    PAYMENT_TRANSITIONS,
    PaymentStatus,
    order_transition_allowed,
    payment_transition_allowed,
)
from app.models.schemas import ActionName, ActionProposal, ReasonCode
from tests.conftest import build_harness, world_with


def test_transition_tables_match_the_design_note():
    assert ORDER_TRANSITIONS[OrderStatus.PAID] == frozenset(
        {OrderStatus.READY_TO_SHIP, OrderStatus.CANCELLED}
    )
    assert ORDER_TRANSITIONS[OrderStatus.DELIVERED] == frozenset()
    assert ORDER_TRANSITIONS[OrderStatus.CANCELLED] == frozenset()
    assert PAYMENT_TRANSITIONS[PaymentStatus.REFUNDED] == frozenset()
    assert order_transition_allowed(OrderStatus.SHIPPED, OrderStatus.DELIVERED)
    assert not order_transition_allowed(OrderStatus.SHIPPED, OrderStatus.CANCELLED)
    assert not payment_transition_allowed(PaymentStatus.REFUNDED, PaymentStatus.REFUNDED)


@pytest.mark.parametrize("status", ["DELIVERED", "SHIPPED", "CANCELLED"])
def test_cancelling_an_order_past_the_cancel_point_is_refused(status: str):
    """Even with a perfectly valid permit, the executor will not run it."""
    harness = build_harness(world_with(order_status=status))
    order = db.fetch_order(harness.conn, "ord_0001")
    permit = harness.issuer.issue_permit(
        action=ActionName.CANCEL_ORDER,
        resource_id=order.order_id,
        expected_resource_version=order.version,
        max_amount_cents=0,
    )

    result = harness.executor.execute(
        permit.permit_id,
        ActionProposal(
            action=ActionName.CANCEL_ORDER, order_id="ord_0001", reason="too late"
        ),
    )

    assert result.committed is False
    assert result.reason_code is ReasonCode.TRANSITION_NOT_ALLOWED
    assert db.mutation_count(harness.conn) == 0
    assert db.fetch_order(harness.conn, "ord_0001").status is OrderStatus(status)


def test_refunding_an_already_refunded_payment_is_refused_by_the_executor():
    harness = build_harness(world_with(payment_status="REFUNDED"))
    payment = db.fetch_payment_for_order(harness.conn, "ord_0001")
    permit = harness.issuer.issue_permit(
        action=ActionName.ISSUE_REFUND,
        resource_id=payment.payment_id,
        expected_resource_version=payment.version,
        max_amount_cents=10_000,
    )

    result = harness.executor.execute(
        permit.permit_id,
        ActionProposal(
            action=ActionName.ISSUE_REFUND,
            order_id="ord_0001",
            amount_cents=10_000,
            reason="second refund attempt",
        ),
    )

    assert result.committed is False
    assert result.reason_code is ReasonCode.TRANSITION_NOT_ALLOWED
    assert db.mutation_count(harness.conn) == 0


def test_an_allowed_transition_still_goes_through():
    harness = build_harness(world_with(order_status="READY_TO_SHIP"))
    order = db.fetch_order(harness.conn, "ord_0001")
    permit = harness.issuer.issue_permit(
        action=ActionName.CANCEL_ORDER,
        resource_id=order.order_id,
        expected_resource_version=order.version,
        max_amount_cents=0,
    )

    result = harness.executor.execute(
        permit.permit_id,
        ActionProposal(
            action=ActionName.CANCEL_ORDER, order_id="ord_0001", reason="customer asked"
        ),
    )

    assert result.committed is True
    assert db.mutation_count(harness.conn) == 1
    assert db.fetch_order(harness.conn, "ord_0001").status is OrderStatus.CANCELLED
    assert db.fetch_shipment_for_order(harness.conn, "ord_0001").status.value == "CANCELLED"

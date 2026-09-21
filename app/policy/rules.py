"""The deterministic policy engine (DESIGN.md §3).

Pure functions. The policy never touches the database: it is handed a snapshot
that the read tools already fetched, so the same snapshot always yields the same
decision. When the snapshot is missing a record the action needs, the policy
escalates rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.clock import parse_iso
from app.models.domain import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    RefundPolicy,
    Shipment,
    ShipmentStatus,
)
from app.models.schemas import (
    ActionName,
    ActionProposal,
    Decision,
    PolicyResult,
    ReasonCode,
)
from app.policy.constants import (
    AUTO_REFUND_LIMIT_CENTS,
    CANCELLABLE_ORDER_STATUSES,
    POST_DELIVERY_REFUND_WINDOW_DAYS,
)

SHIPPED_OR_LATER = frozenset({ShipmentStatus.SHIPPED, ShipmentStatus.DELIVERED})
ORDER_SHIPPED_OR_LATER = frozenset({OrderStatus.SHIPPED, OrderStatus.DELIVERED})


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """What the read tools managed to fetch, plus the reading time."""

    now: datetime
    order: Order | None = None
    payment: Payment | None = None
    shipment: Shipment | None = None
    refund_policy: RefundPolicy | None = None
    read_failures: tuple[str, ...] = field(default_factory=tuple)


REQUIRED_RECORDS: dict[ActionName, tuple[str, ...]] = {
    ActionName.CANCEL_ORDER: ("order", "shipment"),
    ActionName.ISSUE_REFUND: ("order", "payment", "refund_policy"),
    ActionName.CREATE_SUPPORT_TICKET: ("order",),
}


def _escalate(reason: ReasonCode, snapshot: PolicySnapshot, detail: str = "") -> PolicyResult:
    """Escalation always writes exactly one support ticket, against the order."""
    order = snapshot.order
    return PolicyResult(
        decision=Decision.ESCALATE,
        reason_code=reason,
        max_amount_cents=0,
        resource_id=order.order_id if order else "",
        expected_resource_version=order.version if order else -1,
        detail=detail,
    )


def _deny(reason: ReasonCode, detail: str = "") -> PolicyResult:
    return PolicyResult(decision=Decision.DENY, reason_code=reason, detail=detail)


def _missing_records(proposal: ActionProposal, snapshot: PolicySnapshot) -> list[str]:
    missing = [
        name
        for name in REQUIRED_RECORDS[proposal.action]
        if getattr(snapshot, name) is None
    ]
    return missing


def evaluate(proposal: ActionProposal | None, snapshot: PolicySnapshot) -> PolicyResult:
    if proposal is None:
        return _deny(ReasonCode.INVALID_PROPOSAL, "no proposal was produced")

    if snapshot.read_failures or _missing_records(proposal, snapshot):
        detail = ", ".join(
            sorted({*snapshot.read_failures, *_missing_records(proposal, snapshot)})
        )
        return _escalate(ReasonCode.INSUFFICIENT_INFORMATION, snapshot, detail)

    if proposal.action is ActionName.CANCEL_ORDER:
        return _evaluate_cancel(proposal, snapshot)
    if proposal.action is ActionName.ISSUE_REFUND:
        return _evaluate_refund(proposal, snapshot)
    if proposal.action is ActionName.CREATE_SUPPORT_TICKET:
        return _evaluate_ticket(proposal, snapshot)
    return _deny(ReasonCode.UNKNOWN_ACTION, str(proposal.action))


def _evaluate_cancel(proposal: ActionProposal, snapshot: PolicySnapshot) -> PolicyResult:
    order = snapshot.order
    shipment = snapshot.shipment
    assert order is not None and shipment is not None

    if order.status is OrderStatus.CANCELLED:
        return _deny(ReasonCode.ORDER_ALREADY_CANCELLED, order.order_id)

    if order.status in ORDER_SHIPPED_OR_LATER or shipment.status in SHIPPED_OR_LATER:
        return _escalate(ReasonCode.CANCEL_AFTER_SHIPMENT, snapshot, order.status)

    if order.status in CANCELLABLE_ORDER_STATUSES:
        return PolicyResult(
            decision=Decision.ALLOW,
            reason_code=ReasonCode.CANCELLABLE_BEFORE_SHIPMENT,
            max_amount_cents=0,
            resource_id=order.order_id,
            expected_resource_version=order.version,
        )

    return _escalate(ReasonCode.CANCEL_AFTER_SHIPMENT, snapshot, order.status)


def _evaluate_refund(proposal: ActionProposal, snapshot: PolicySnapshot) -> PolicyResult:
    order = snapshot.order
    payment = snapshot.payment
    refund_policy = snapshot.refund_policy
    assert order is not None and payment is not None and refund_policy is not None

    if payment.status is PaymentStatus.REFUNDED:
        return _deny(ReasonCode.PAYMENT_ALREADY_REFUNDED, payment.payment_id)

    amount = proposal.amount_cents
    if amount is None or amount <= 0:
        return _deny(ReasonCode.INVALID_PROPOSAL, "refund needs a positive amount_cents")

    limit = refund_policy.auto_refund_limit_cents or AUTO_REFUND_LIMIT_CENTS
    if amount > limit:
        return _escalate(
            ReasonCode.ABOVE_AUTO_REFUND_LIMIT, snapshot, f"{amount} > {limit}"
        )

    if amount > payment.captured_cents:
        return _deny(
            ReasonCode.AMOUNT_EXCEEDS_CAPTURED, f"{amount} > {payment.captured_cents}"
        )

    window_days = refund_policy.post_delivery_window_days
    if window_days is None:
        window_days = POST_DELIVERY_REFUND_WINDOW_DAYS
    if order.delivered_at is not None:
        elapsed_days = (snapshot.now - parse_iso(order.delivered_at)).total_seconds() / 86400.0
        if elapsed_days > window_days:
            return _escalate(
                ReasonCode.OUTSIDE_RETURN_WINDOW,
                snapshot,
                f"{elapsed_days:.1f}d > {window_days}d",
            )

    return PolicyResult(
        decision=Decision.ALLOW,
        reason_code=ReasonCode.REFUNDABLE_WITHIN_LIMITS,
        max_amount_cents=amount,
        resource_id=payment.payment_id,
        expected_resource_version=payment.version,
    )


def _evaluate_ticket(proposal: ActionProposal, snapshot: PolicySnapshot) -> PolicyResult:
    order = snapshot.order
    assert order is not None
    return PolicyResult(
        decision=Decision.ALLOW,
        reason_code=ReasonCode.TICKET_REQUESTED,
        max_amount_cents=0,
        resource_id=order.order_id,
        expected_resource_version=order.version,
    )

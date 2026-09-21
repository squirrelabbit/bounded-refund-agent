"""Closed domain model. The planner cannot introduce a status or a transition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrderStatus(StrEnum):
    PAID = "PAID"
    READY_TO_SHIP = "READY_TO_SHIP"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class PaymentStatus(StrEnum):
    CAPTURED = "CAPTURED"
    REFUNDED = "REFUNDED"


class ShipmentStatus(StrEnum):
    PREPARING = "PREPARING"
    READY_TO_SHIP = "READY_TO_SHIP"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PAID: frozenset({OrderStatus.READY_TO_SHIP, OrderStatus.CANCELLED}),
    OrderStatus.READY_TO_SHIP: frozenset({OrderStatus.SHIPPED, OrderStatus.CANCELLED}),
    OrderStatus.SHIPPED: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CAPTURED: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.REFUNDED: frozenset(),
}


def order_transition_allowed(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ORDER_TRANSITIONS[current]


def payment_transition_allowed(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in PAYMENT_TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    customer_id: str
    policy_id: str
    status: OrderStatus
    total_cents: int
    placed_at: str
    delivered_at: str | None
    version: int


@dataclass(frozen=True, slots=True)
class Payment:
    payment_id: str
    order_id: str
    status: PaymentStatus
    captured_cents: int
    refunded_cents: int
    version: int


@dataclass(frozen=True, slots=True)
class Shipment:
    shipment_id: str
    order_id: str
    status: ShipmentStatus
    carrier: str
    version: int


@dataclass(frozen=True, slots=True)
class RefundPolicy:
    policy_id: str
    name: str
    auto_refund_limit_cents: int
    post_delivery_window_days: int
    version: int


@dataclass(frozen=True, slots=True)
class SupportTicket:
    ticket_id: str
    order_id: str
    reason_code: str
    summary: str
    created_at: str

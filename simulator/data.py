"""Builds the synthetic world.

Nothing in this module talks to a real ordering, payment or shipping system.
Every identifier, amount and timestamp is generated locally from a fixed seed,
so two runs of the same specification produce byte-identical fixtures.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.models.domain import OrderStatus, PaymentStatus, ShipmentStatus
from app.policy.constants import (
    AUTO_REFUND_LIMIT_CENTS,
    POST_DELIVERY_REFUND_WINDOW_DAYS,
)

DEFAULT_SEED = 20260921
DEFAULT_POLICY_ID = "pol_standard"
DEFAULT_PLACED_AT = "2026-03-01T09:00:00+00:00"
CARRIERS = ("SYNTHETIC_POST", "SIM_EXPRESS", "FAKE_FREIGHT")

_ORDER_TO_SHIPMENT = {
    OrderStatus.PAID: ShipmentStatus.PREPARING,
    OrderStatus.READY_TO_SHIP: ShipmentStatus.READY_TO_SHIP,
    OrderStatus.SHIPPED: ShipmentStatus.SHIPPED,
    OrderStatus.DELIVERED: ShipmentStatus.DELIVERED,
    OrderStatus.CANCELLED: ShipmentStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class SeededWorld:
    """Ids the scenario can refer to after seeding."""

    order_ids: tuple[str, ...]
    payment_ids: tuple[str, ...]
    shipment_ids: tuple[str, ...]
    policy_ids: tuple[str, ...]


def default_policy_spec() -> dict[str, Any]:
    return {
        "policy_id": DEFAULT_POLICY_ID,
        "name": "standard synthetic refund policy",
        "auto_refund_limit_cents": AUTO_REFUND_LIMIT_CENTS,
        "post_delivery_window_days": POST_DELIVERY_REFUND_WINDOW_DAYS,
        "version": 1,
    }


def seed_world(conn: sqlite3.Connection, spec: dict[str, Any] | None = None) -> SeededWorld:
    """Insert the declared fixtures. Omitted fields get deterministic defaults."""
    spec = dict(spec or {})
    rng = random.Random(int(spec.get("seed", DEFAULT_SEED)))

    policy_specs = list(spec.get("policies") or [default_policy_spec()])
    order_specs = list(spec.get("orders") or [])
    payment_specs = list(spec.get("payments") or [])
    shipment_specs = list(spec.get("shipments") or [])

    policy_ids: list[str] = []
    order_ids: list[str] = []
    payment_ids: list[str] = []
    shipment_ids: list[str] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        for policy in policy_specs:
            merged = {**default_policy_spec(), **policy}
            conn.execute(
                "INSERT INTO refund_policies"
                " (policy_id, name, auto_refund_limit_cents, post_delivery_window_days, version)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    merged["policy_id"],
                    merged["name"],
                    int(merged["auto_refund_limit_cents"]),
                    int(merged["post_delivery_window_days"]),
                    int(merged["version"]),
                ),
            )
            policy_ids.append(merged["policy_id"])

        explicit_payment_orders = {p["order_id"] for p in payment_specs if "order_id" in p}
        explicit_shipment_orders = {s["order_id"] for s in shipment_specs if "order_id" in s}

        for index, order in enumerate(order_specs, start=1):
            order_id = order.get("order_id", f"ord_{index:04d}")
            status = OrderStatus(order.get("status", OrderStatus.PAID))
            delivered_at = order.get("delivered_at")
            conn.execute(
                "INSERT INTO orders"
                " (order_id, customer_id, policy_id, status, total_cents, placed_at,"
                "  delivered_at, version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    order.get("customer_id", f"cus_{rng.randrange(10**6):06d}"),
                    order.get("policy_id", policy_ids[0]),
                    str(status),
                    int(order.get("total_cents", 10_000)),
                    order.get("placed_at", DEFAULT_PLACED_AT),
                    delivered_at,
                    int(order.get("version", 1)),
                ),
            )
            order_ids.append(order_id)

            if order_id not in explicit_payment_orders:
                payment_specs.append(
                    {
                        "order_id": order_id,
                        "captured_cents": int(order.get("total_cents", 10_000)),
                    }
                )
            if order_id not in explicit_shipment_orders:
                shipment_specs.append(
                    {
                        "order_id": order_id,
                        "status": str(_ORDER_TO_SHIPMENT[status]),
                    }
                )

        for index, payment in enumerate(payment_specs, start=1):
            payment_id = payment.get("payment_id", f"pay_{payment['order_id']}")
            conn.execute(
                "INSERT INTO payments"
                " (payment_id, order_id, status, captured_cents, refunded_cents, version)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    payment_id,
                    payment["order_id"],
                    str(PaymentStatus(payment.get("status", PaymentStatus.CAPTURED))),
                    int(payment.get("captured_cents", 10_000)),
                    int(payment.get("refunded_cents", 0)),
                    int(payment.get("version", 1)),
                ),
            )
            payment_ids.append(payment_id)

        for index, shipment in enumerate(shipment_specs, start=1):
            shipment_id = shipment.get("shipment_id", f"shp_{shipment['order_id']}")
            conn.execute(
                "INSERT INTO shipments"
                " (shipment_id, order_id, status, carrier, version)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    shipment_id,
                    shipment["order_id"],
                    str(ShipmentStatus(shipment.get("status", ShipmentStatus.PREPARING))),
                    shipment.get("carrier", CARRIERS[rng.randrange(len(CARRIERS))]),
                    int(shipment.get("version", 1)),
                ),
            )
            shipment_ids.append(shipment_id)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

    return SeededWorld(
        order_ids=tuple(order_ids),
        payment_ids=tuple(payment_ids),
        shipment_ids=tuple(shipment_ids),
        policy_ids=tuple(policy_ids),
    )

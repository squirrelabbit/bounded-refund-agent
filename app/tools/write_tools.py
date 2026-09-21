"""Write tools. Unreachable without an authorization the executor minted.

Each function takes a :class:`WriteAuthorization` as its first argument. That
object cannot be constructed without the executor's private token, so a planner
holding a reference to ``issue_refund`` still cannot move any money: the call
raises :class:`PermitRequired` before a single row is touched.

The functions do not open transactions. They run inside the one
``BEGIN IMMEDIATE`` transaction the executor opened, so the domain change, its
``mutation_log`` row and the execution record all commit together or not at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.executor._authority import _EXECUTOR_TOKEN
from app.models.domain import OrderStatus, PaymentStatus, ShipmentStatus
from app.models.schemas import ActionName

WRITE_TOOL_NAMES = ("cancel_order", "issue_refund", "create_support_ticket")

_CANCELLABLE_SHIPMENT_STATUSES = (
    str(ShipmentStatus.PREPARING),
    str(ShipmentStatus.READY_TO_SHIP),
)


class PermitRequired(Exception):
    """A write was attempted without a valid, executor-issued authorization."""


class StaleResource(Exception):
    """The version-guarded UPDATE matched no row: someone else got there first."""

    def __init__(self, resource_id: str, expected_version: int) -> None:
        super().__init__(f"{resource_id} is no longer at version {expected_version}")
        self.resource_id = resource_id
        self.expected_version = expected_version


class WriteAuthorization:
    """Proof that the executor cleared this specific write."""

    __slots__ = ("permit_id", "action", "resource_id", "max_amount_cents", "run_id")

    def __init__(
        self,
        token: Any,
        *,
        permit_id: str,
        action: ActionName,
        resource_id: str,
        max_amount_cents: int,
        run_id: str,
    ) -> None:
        if token is not _EXECUTOR_TOKEN:
            raise PermitRequired(
                "WriteAuthorization can only be created by the executor"
            )
        self.permit_id = permit_id
        self.action = action
        self.resource_id = resource_id
        self.max_amount_cents = max_amount_cents
        self.run_id = run_id

    def __repr__(self) -> str:
        return f"WriteAuthorization(permit_id={self.permit_id!r}, action={self.action!r})"


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    action: ActionName
    resource_id: str
    amount_cents: int
    created_id: str


def _require(auth: Any, action: ActionName, resource_id: str) -> WriteAuthorization:
    if not isinstance(auth, WriteAuthorization):
        raise PermitRequired(f"{action} requires an executor-issued authorization")
    if auth.action is not action:
        raise PermitRequired(f"authorization is for {auth.action}, not {action}")
    if auth.resource_id != resource_id:
        raise PermitRequired(
            f"authorization is for {auth.resource_id}, not {resource_id}"
        )
    return auth


def _log_mutation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    action: ActionName,
    resource_id: str,
    amount_cents: int,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO mutation_log (run_id, action, resource_id, amount_cents, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (run_id, str(action), resource_id, amount_cents, now),
    )


def cancel_order(
    auth: Any,
    conn: sqlite3.Connection,
    *,
    order_id: str,
    expected_version: int,
    now: str,
) -> WriteOutcome:
    authorized = _require(auth, ActionName.CANCEL_ORDER, order_id)
    cursor = conn.execute(
        "UPDATE orders SET status = ?, version = version + 1"
        " WHERE order_id = ? AND version = ?",
        (str(OrderStatus.CANCELLED), order_id, expected_version),
    )
    if cursor.rowcount != 1:
        raise StaleResource(order_id, expected_version)
    conn.execute(
        "UPDATE shipments SET status = ?, version = version + 1"
        f" WHERE order_id = ? AND status IN ({','.join('?' * len(_CANCELLABLE_SHIPMENT_STATUSES))})",
        (str(ShipmentStatus.CANCELLED), order_id, *_CANCELLABLE_SHIPMENT_STATUSES),
    )
    _log_mutation(
        conn,
        run_id=authorized.run_id,
        action=ActionName.CANCEL_ORDER,
        resource_id=order_id,
        amount_cents=0,
        now=now,
    )
    return WriteOutcome(ActionName.CANCEL_ORDER, order_id, 0, order_id)


def issue_refund(
    auth: Any,
    conn: sqlite3.Connection,
    *,
    payment_id: str,
    order_id: str,
    amount_cents: int,
    expected_version: int,
    now: str,
) -> WriteOutcome:
    authorized = _require(auth, ActionName.ISSUE_REFUND, payment_id)
    if amount_cents > authorized.max_amount_cents:
        raise PermitRequired(
            f"{amount_cents} exceeds the authorized {authorized.max_amount_cents}"
        )
    cursor = conn.execute(
        "UPDATE payments SET status = ?, refunded_cents = refunded_cents + ?,"
        " version = version + 1 WHERE payment_id = ? AND version = ?",
        (str(PaymentStatus.REFUNDED), amount_cents, payment_id, expected_version),
    )
    if cursor.rowcount != 1:
        raise StaleResource(payment_id, expected_version)
    refund_id = f"ref_{authorized.permit_id}"
    conn.execute(
        "INSERT INTO refunds"
        " (refund_id, payment_id, order_id, amount_cents, created_at, run_id, permit_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            refund_id,
            payment_id,
            order_id,
            amount_cents,
            now,
            authorized.run_id,
            authorized.permit_id,
        ),
    )
    _log_mutation(
        conn,
        run_id=authorized.run_id,
        action=ActionName.ISSUE_REFUND,
        resource_id=payment_id,
        amount_cents=amount_cents,
        now=now,
    )
    return WriteOutcome(ActionName.ISSUE_REFUND, payment_id, amount_cents, refund_id)


def create_support_ticket(
    auth: Any,
    conn: sqlite3.Connection,
    *,
    order_id: str,
    reason_code: str,
    summary: str,
    now: str,
) -> WriteOutcome:
    authorized = _require(auth, ActionName.CREATE_SUPPORT_TICKET, order_id)
    ticket_id = f"tkt_{authorized.permit_id}"
    conn.execute(
        "INSERT INTO support_tickets"
        " (ticket_id, order_id, reason_code, summary, created_at, run_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, order_id, reason_code, summary[:200], now, authorized.run_id),
    )
    _log_mutation(
        conn,
        run_id=authorized.run_id,
        action=ActionName.CREATE_SUPPORT_TICKET,
        resource_id=order_id,
        amount_cents=0,
        now=now,
    )
    return WriteOutcome(ActionName.CREATE_SUPPORT_TICKET, order_id, 0, ticket_id)

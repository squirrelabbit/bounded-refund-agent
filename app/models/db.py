"""SQLite schema and access layer for the synthetic world.

Everything stored here is simulated. There is no connection to any real order,
payment or shipping system.

``mutation_log`` is the independent ledger of truth: every committed mutation
writes exactly one row into it, inside the same transaction as the mutation.
The evaluator counts mutations from this table rather than from any counter the
runner keeps, so a bug in the runner cannot understate what actually happened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from app.models.domain import (
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    RefundPolicy,
    Shipment,
    ShipmentStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS refund_policies (
    policy_id                 TEXT PRIMARY KEY,
    name                      TEXT NOT NULL,
    auto_refund_limit_cents   INTEGER NOT NULL,
    post_delivery_window_days INTEGER NOT NULL,
    version                   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    policy_id    TEXT NOT NULL REFERENCES refund_policies(policy_id),
    status       TEXT NOT NULL,
    total_cents  INTEGER NOT NULL,
    placed_at    TEXT NOT NULL,
    delivered_at TEXT,
    version      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id     TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL REFERENCES orders(order_id),
    status         TEXT NOT NULL,
    captured_cents INTEGER NOT NULL,
    refunded_cents INTEGER NOT NULL DEFAULT 0,
    version        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    status      TEXT NOT NULL,
    carrier     TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id   TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    reason_code TEXT NOT NULL,
    summary     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    run_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id    TEXT PRIMARY KEY,
    payment_id   TEXT NOT NULL REFERENCES payments(payment_id),
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    amount_cents INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    permit_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_permits (
    permit_id                 TEXT PRIMARY KEY,
    action                    TEXT NOT NULL,
    resource_id               TEXT NOT NULL,
    expected_resource_version INTEGER NOT NULL,
    max_amount_cents          INTEGER NOT NULL,
    expires_at                TEXT NOT NULL,
    idempotency_key           TEXT NOT NULL,
    used                      INTEGER NOT NULL DEFAULT 0,
    created_at                TEXT NOT NULL,
    run_id                    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_records (
    idempotency_key TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    action          TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mutation_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    action       TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def open_world(path: str = ":memory:") -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A single write transaction. Nothing inside it is visible until commit."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        order_id=row["order_id"],
        customer_id=row["customer_id"],
        policy_id=row["policy_id"],
        status=OrderStatus(row["status"]),
        total_cents=row["total_cents"],
        placed_at=row["placed_at"],
        delivered_at=row["delivered_at"],
        version=row["version"],
    )


def row_to_payment(row: sqlite3.Row) -> Payment:
    return Payment(
        payment_id=row["payment_id"],
        order_id=row["order_id"],
        status=PaymentStatus(row["status"]),
        captured_cents=row["captured_cents"],
        refunded_cents=row["refunded_cents"],
        version=row["version"],
    )


def row_to_shipment(row: sqlite3.Row) -> Shipment:
    return Shipment(
        shipment_id=row["shipment_id"],
        order_id=row["order_id"],
        status=ShipmentStatus(row["status"]),
        carrier=row["carrier"],
        version=row["version"],
    )


def row_to_refund_policy(row: sqlite3.Row) -> RefundPolicy:
    return RefundPolicy(
        policy_id=row["policy_id"],
        name=row["name"],
        auto_refund_limit_cents=row["auto_refund_limit_cents"],
        post_delivery_window_days=row["post_delivery_window_days"],
        version=row["version"],
    )


def fetch_order(conn: sqlite3.Connection, order_id: str) -> Order | None:
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    return row_to_order(row) if row else None


def fetch_payment_by_id(conn: sqlite3.Connection, payment_id: str) -> Payment | None:
    row = conn.execute(
        "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
    ).fetchone()
    return row_to_payment(row) if row else None


def fetch_payment_for_order(conn: sqlite3.Connection, order_id: str) -> Payment | None:
    row = conn.execute(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY payment_id", (order_id,)
    ).fetchone()
    return row_to_payment(row) if row else None


def fetch_shipment_for_order(conn: sqlite3.Connection, order_id: str) -> Shipment | None:
    row = conn.execute(
        "SELECT * FROM shipments WHERE order_id = ? ORDER BY shipment_id", (order_id,)
    ).fetchone()
    return row_to_shipment(row) if row else None


def fetch_refund_policy(conn: sqlite3.Connection, policy_id: str) -> RefundPolicy | None:
    row = conn.execute(
        "SELECT * FROM refund_policies WHERE policy_id = ?", (policy_id,)
    ).fetchone()
    return row_to_refund_policy(row) if row else None


def mutation_log_rows(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM mutation_log WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    )


def mutation_count(conn: sqlite3.Connection, run_id: str | None = None) -> int:
    """Count committed mutations straight from the independent ledger."""
    if run_id is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM mutation_log").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM mutation_log WHERE run_id = ?", (run_id,)
        ).fetchone()
    return int(row["n"])

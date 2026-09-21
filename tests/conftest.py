"""Shared fixtures. Everything is synthetic and offline."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest

from app.audit import AuditTrace
from app.clock import FixedClock
from app.executor.executor import Executor
from app.executor.permits import PermitIssuer
from app.models import db
from app.tools.read_tools import ReadTools, ToolCallRecorder
from simulator.data import seed_world
from simulator.failures import FailureInjector

BASE_TIME = "2026-04-01T12:00:00+00:00"
DELIVERED_RECENTLY = "2026-03-28T09:00:00+00:00"
DELIVERED_LONG_AGO = "2026-02-01T09:00:00+00:00"


def world_with(
    *,
    order_status: str = "PAID",
    payment_status: str = "CAPTURED",
    shipment_status: str | None = None,
    total_cents: int = 10_000,
    captured_cents: int | None = None,
    delivered_at: str | None = None,
    order_id: str = "ord_0001",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "order_id": order_id,
        "status": order_status,
        "total_cents": total_cents,
        "delivered_at": delivered_at,
    }
    spec: dict[str, Any] = {
        "orders": [order],
        "payments": [
            {
                "order_id": order_id,
                "payment_id": f"pay_{order_id}",
                "status": payment_status,
                "captured_cents": total_cents if captured_cents is None else captured_cents,
            }
        ],
    }
    if shipment_status is not None:
        spec["shipments"] = [
            {
                "order_id": order_id,
                "shipment_id": f"shp_{order_id}",
                "status": shipment_status,
            }
        ]
    return spec


@dataclass
class Harness:
    conn: sqlite3.Connection
    clock: FixedClock
    issuer: PermitIssuer
    executor: Executor
    recorder: ToolCallRecorder
    read_tools: ReadTools
    injector: FailureInjector
    audit: AuditTrace
    run_id: str


def build_harness(
    world: dict[str, Any] | None = None,
    *,
    run_id: str = "run_test",
    failures: dict[str, Any] | None = None,
    base_time: str = BASE_TIME,
) -> Harness:
    conn = db.open_world()
    seed_world(conn, world if world is not None else world_with())
    clock = FixedClock(base_time)
    injector = FailureInjector(failures)
    recorder = ToolCallRecorder()
    audit = AuditTrace(run_id, "harness", clock)
    return Harness(
        conn=conn,
        clock=clock,
        issuer=PermitIssuer(conn, clock, run_id),
        executor=Executor(
            conn, clock, run_id, injector=injector, audit=audit, recorder=recorder
        ),
        recorder=recorder,
        read_tools=ReadTools(conn, recorder, injector),
        injector=injector,
        audit=audit,
        run_id=run_id,
    )


@pytest.fixture
def harness() -> Harness:
    return build_harness()


@pytest.fixture(autouse=True, scope="session")
def no_network():
    """The suite claims to run offline, so make the claim enforceable."""
    import socket

    def blocked(*args: Any, **kwargs: Any):
        raise RuntimeError("this test suite does not allow network access")

    original_socket = socket.socket
    original_connect = socket.create_connection
    socket.socket = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = original_socket  # type: ignore[assignment]
        socket.create_connection = original_connect  # type: ignore[assignment]

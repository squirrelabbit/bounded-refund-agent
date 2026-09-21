"""Read tools. They may fail, but only a bounded number of times.

A read that stays broken is not retried forever: after ``READ_MAX_ATTEMPTS`` the
tool raises, the policy sees a missing record, and the request is escalated to a
human instead of acted on with incomplete information.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.models import db
from app.models.domain import Order, Payment, RefundPolicy, Shipment
from app.policy.constants import READ_MAX_ATTEMPTS
from simulator.failures import FailureInjector, ReadUnavailable

READ_TOOL_NAMES = ("get_order", "get_payment", "get_shipment", "get_refund_policy")


@dataclass
class ToolCallRecorder:
    """Every tool invocation lands here, successful or not."""

    calls: list[str] = field(default_factory=list)

    def record(self, tool: str, **arguments: Any) -> None:
        rendered = ",".join(f"{key}={value}" for key, value in sorted(arguments.items()))
        self.calls.append(f"{tool}({rendered})")

    def names(self) -> list[str]:
        return [call.split("(", 1)[0] for call in self.calls]


class ReadTools:
    def __init__(
        self,
        conn: sqlite3.Connection,
        recorder: ToolCallRecorder | None = None,
        injector: FailureInjector | None = None,
        max_attempts: int = READ_MAX_ATTEMPTS,
    ) -> None:
        self.conn = conn
        self.recorder = recorder or ToolCallRecorder()
        self.injector = injector
        self.max_attempts = max_attempts

    def _call(self, tool: str, fetch, **arguments: Any):
        last_attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            last_attempt = attempt
            self.recorder.record(tool, attempt=attempt, **arguments)
            if self.injector is not None and self.injector.read_should_fail(tool):
                continue
            return fetch()
        raise ReadUnavailable(tool, last_attempt)

    def get_order(self, order_id: str) -> Order | None:
        return self._call(
            "get_order", lambda: db.fetch_order(self.conn, order_id), order_id=order_id
        )

    def get_payment(self, order_id: str) -> Payment | None:
        return self._call(
            "get_payment",
            lambda: db.fetch_payment_for_order(self.conn, order_id),
            order_id=order_id,
        )

    def get_shipment(self, order_id: str) -> Shipment | None:
        return self._call(
            "get_shipment",
            lambda: db.fetch_shipment_for_order(self.conn, order_id),
            order_id=order_id,
        )

    def get_refund_policy(self, policy_id: str) -> RefundPolicy | None:
        return self._call(
            "get_refund_policy",
            lambda: db.fetch_refund_policy(self.conn, policy_id),
            policy_id=policy_id,
        )

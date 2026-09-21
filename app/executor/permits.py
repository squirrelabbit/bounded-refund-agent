"""Permit issue. Only the server does this, and only after the policy allowed.

Permit ids and idempotency keys are derived from the run id and a per-run
sequence number rather than from randomness or wall-clock time, because the
evaluation has to be reproducible run after run.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from app.clock import Clock, to_iso
from app.models import db
from app.models.schemas import ActionName, ExecutionPermit
from app.policy.constants import PERMIT_TTL_SECONDS


def permit_id_for(run_id: str, sequence: int) -> str:
    return f"prm_{run_id}_{sequence:03d}"


def idempotency_key_for(
    run_id: str, sequence: int, action: ActionName, resource_id: str
) -> str:
    return f"idk_{run_id}_{sequence:03d}_{action}_{resource_id}"


class PermitIssuer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        run_id: str,
        ttl_seconds: int = PERMIT_TTL_SECONDS,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.run_id = run_id
        self.ttl_seconds = ttl_seconds
        self._sequence = 0

    def issue_permit(
        self,
        *,
        action: ActionName,
        resource_id: str,
        expected_resource_version: int,
        max_amount_cents: int = 0,
        ttl_seconds: int | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionPermit:
        self._sequence += 1
        sequence = self._sequence
        issued_at = self.clock.now()
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        permit = ExecutionPermit(
            permit_id=permit_id_for(self.run_id, sequence),
            action=action,
            resource_id=resource_id,
            expected_resource_version=expected_resource_version,
            max_amount_cents=max_amount_cents,
            expires_at=to_iso(issued_at + timedelta(seconds=ttl)),
            idempotency_key=idempotency_key
            or idempotency_key_for(self.run_id, sequence, action, resource_id),
            used=False,
        )
        with db.immediate_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO execution_permits"
                " (permit_id, action, resource_id, expected_resource_version,"
                "  max_amount_cents, expires_at, idempotency_key, used, created_at, run_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    permit.permit_id,
                    str(permit.action),
                    permit.resource_id,
                    permit.expected_resource_version,
                    permit.max_amount_cents,
                    permit.expires_at,
                    permit.idempotency_key,
                    to_iso(issued_at),
                    self.run_id,
                ),
            )
        return permit


def load_permit(conn: sqlite3.Connection, permit_id: str | None) -> ExecutionPermit | None:
    if not permit_id:
        return None
    row = conn.execute(
        "SELECT * FROM execution_permits WHERE permit_id = ?", (permit_id,)
    ).fetchone()
    if row is None:
        return None
    return ExecutionPermit(
        permit_id=row["permit_id"],
        action=ActionName(row["action"]),
        resource_id=row["resource_id"],
        expected_resource_version=row["expected_resource_version"],
        max_amount_cents=row["max_amount_cents"],
        expires_at=row["expires_at"],
        idempotency_key=row["idempotency_key"],
        used=bool(row["used"]),
    )

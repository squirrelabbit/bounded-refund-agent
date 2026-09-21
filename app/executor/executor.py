"""The executor: the only component that may change the world.

Nine checks run before anything is written. If any of them fails the executor
returns a rejection and the ``mutation_log`` stays exactly as it was.

    1. permit exists                 permit_missing
    2. action matches                permit_action_mismatch
    3. resource matches              permit_resource_mismatch
    4. permit not expired            permit_expired
    5. permit not already consumed   permit_already_used
    6. amount within the permit      permit_amount_exceeded
    7. resource still at the version the policy saw   stale_resource_version
    8. no execution record for this idempotency key   idempotent_replay
    9. the state transition is allowed                transition_not_allowed

Steps 5 and 7 have one exception, and it is deliberate. Both of them are also
what a *successful* execution leaves behind: after a commit the permit is used
and the resource version has moved on. A caller retrying because it lost the
response would therefore be told "already used" or "stale" and would never
reach the replay in step 8. So when either check trips, the executor first asks
whether an execution record exists for this idempotency key; if one does, the
honest answer is the stored result, not a rejection.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.audit import AuditTrace
from app.clock import Clock, parse_iso, to_iso
from app.executor._authority import _EXECUTOR_TOKEN
from app.executor.permits import load_permit
from app.models import db
from app.models.domain import (
    OrderStatus,
    PaymentStatus,
    order_transition_allowed,
    payment_transition_allowed,
)
from app.models.schemas import (
    ActionName,
    ActionProposal,
    DecisionOwner,
    ExecutionPermit,
    ExecutionResult,
    ReasonCode,
)
from app.tools import write_tools
from app.tools.write_tools import StaleResource, WriteAuthorization
from simulator.failures import FailureInjector, ResponseTimeout


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        run_id: str,
        injector: FailureInjector | None = None,
        audit: AuditTrace | None = None,
        recorder: Any | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.run_id = run_id
        self.injector = injector
        self.audit = audit
        self.recorder = recorder

    def execute(
        self,
        permit_id: str | None,
        proposal: ActionProposal,
        *,
        ticket_reason_code: str = "",
        ticket_summary: str = "",
    ) -> ExecutionResult:
        permit = load_permit(self.conn, permit_id)
        self._emit("mutation_attempted", proposal, permit, None)

        result = self._run_checks(
            permit,
            proposal,
            ticket_reason_code=ticket_reason_code,
            ticket_summary=ticket_summary,
        )

        if result.committed and not result.replayed:
            self._emit("mutation_committed", proposal, permit, result)
            if self.injector is not None and self.injector.should_timeout_after_commit(
                str(proposal.action)
            ):
                assert permit is not None
                raise ResponseTimeout(str(proposal.action), permit.idempotency_key)
        elif result.committed and result.replayed:
            self._emit(
                "mutation_replayed",
                proposal,
                permit,
                result,
                created_id=result.created_id or "",
            )
        else:
            self._emit("mutation_rejected", proposal, permit, result)
        return result

    def _run_checks(
        self,
        permit: ExecutionPermit | None,
        proposal: ActionProposal,
        *,
        ticket_reason_code: str,
        ticket_summary: str,
    ) -> ExecutionResult:
        if permit is None:
            return _rejected(ReasonCode.PERMIT_MISSING)

        if permit.action is not proposal.action:
            return _rejected(ReasonCode.PERMIT_ACTION_MISMATCH, permit.resource_id)

        target = self._resolve_target(proposal)
        if target is None or target.resource_id != permit.resource_id:
            return _rejected(ReasonCode.PERMIT_RESOURCE_MISMATCH, permit.resource_id)

        if self.clock.now() > parse_iso(permit.expires_at):
            return _rejected(ReasonCode.PERMIT_EXPIRED, permit.resource_id)

        stored = self._stored_result(permit.idempotency_key)

        if permit.used:
            if stored is not None:
                return stored
            return _rejected(ReasonCode.PERMIT_ALREADY_USED, permit.resource_id)

        amount = proposal.amount_cents or 0
        if amount > permit.max_amount_cents:
            return _rejected(ReasonCode.PERMIT_AMOUNT_EXCEEDED, permit.resource_id)

        if target.version != permit.expected_resource_version:
            if stored is not None:
                return stored
            return _rejected(ReasonCode.STALE_RESOURCE_VERSION, permit.resource_id)

        if stored is not None:
            return stored

        if not self._transition_allowed(proposal, target):
            return _rejected(ReasonCode.TRANSITION_NOT_ALLOWED, permit.resource_id)

        if self.injector is not None:
            self.injector.apply_pre_commit(self.conn, str(proposal.action))

        return self._commit(
            permit,
            proposal,
            target,
            ticket_reason_code=ticket_reason_code,
            ticket_summary=ticket_summary,
        )

    def _resolve_target(self, proposal: ActionProposal) -> _Target | None:
        if proposal.action is ActionName.ISSUE_REFUND:
            payment = db.fetch_payment_for_order(self.conn, proposal.order_id)
            if payment is None:
                return None
            return _Target(payment.payment_id, payment.version, payment.status)
        order = db.fetch_order(self.conn, proposal.order_id)
        if order is None:
            return None
        return _Target(order.order_id, order.version, order.status)

    def _transition_allowed(self, proposal: ActionProposal, target: _Target) -> bool:
        if proposal.action is ActionName.CANCEL_ORDER:
            return order_transition_allowed(
                OrderStatus(target.status), OrderStatus.CANCELLED
            )
        if proposal.action is ActionName.ISSUE_REFUND:
            return payment_transition_allowed(
                PaymentStatus(target.status), PaymentStatus.REFUNDED
            )
        return True

    def _stored_result(self, idempotency_key: str) -> ExecutionResult | None:
        row = self.conn.execute(
            "SELECT result_json FROM execution_records WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        stored = ExecutionResult.model_validate_json(row["result_json"])
        stored.replayed = True
        stored.reason_code = ReasonCode.IDEMPOTENT_REPLAY
        return stored

    def _commit(
        self,
        permit: ExecutionPermit,
        proposal: ActionProposal,
        target: _Target,
        *,
        ticket_reason_code: str,
        ticket_summary: str,
    ) -> ExecutionResult:
        auth = WriteAuthorization(
            _EXECUTOR_TOKEN,
            permit_id=permit.permit_id,
            action=permit.action,
            resource_id=permit.resource_id,
            max_amount_cents=permit.max_amount_cents,
            run_id=self.run_id,
        )
        now = to_iso(self.clock.now())
        if self.recorder is not None:
            self.recorder.record(
                str(proposal.action),
                order_id=proposal.order_id,
                amount_cents=proposal.amount_cents or 0,
            )

        try:
            with db.immediate_transaction(self.conn):
                if proposal.action is ActionName.CANCEL_ORDER:
                    outcome = write_tools.cancel_order(
                        auth,
                        self.conn,
                        order_id=proposal.order_id,
                        expected_version=permit.expected_resource_version,
                        now=now,
                    )
                elif proposal.action is ActionName.ISSUE_REFUND:
                    outcome = write_tools.issue_refund(
                        auth,
                        self.conn,
                        payment_id=permit.resource_id,
                        order_id=proposal.order_id,
                        amount_cents=proposal.amount_cents or 0,
                        expected_version=permit.expected_resource_version,
                        now=now,
                    )
                else:
                    outcome = write_tools.create_support_ticket(
                        auth,
                        self.conn,
                        order_id=proposal.order_id,
                        reason_code=ticket_reason_code or str(ReasonCode.TICKET_REQUESTED),
                        summary=ticket_summary or proposal.reason,
                        now=now,
                    )

                result = ExecutionResult(
                    committed=True,
                    action=outcome.action,
                    resource_id=outcome.resource_id,
                    amount_cents=outcome.amount_cents,
                    reason_code=None,
                    replayed=False,
                    created_id=outcome.created_id,
                )
                self.conn.execute(
                    "INSERT INTO execution_records"
                    " (idempotency_key, run_id, action, resource_id, amount_cents,"
                    "  result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        permit.idempotency_key,
                        self.run_id,
                        str(outcome.action),
                        outcome.resource_id,
                        outcome.amount_cents,
                        result.model_dump_json(),
                        now,
                    ),
                )
                self.conn.execute(
                    "UPDATE execution_permits SET used = 1 WHERE permit_id = ?",
                    (permit.permit_id,),
                )
        except StaleResource:
            return _rejected(ReasonCode.STALE_RESOURCE_VERSION, permit.resource_id)

        return result

    def _emit(
        self,
        event_type: str,
        proposal: ActionProposal,
        permit: ExecutionPermit | None,
        result: ExecutionResult | None,
        **extra: Any,
    ) -> None:
        if self.audit is None:
            return
        target = self._resolve_target(proposal)
        self.audit.emit(
            event_type=event_type,
            resource_id=permit.resource_id if permit else proposal.order_id,
            resource_version=target.version if target else -1,
            decision_owner=DecisionOwner.EXECUTOR,
            reason_code=result.reason_code if result and result.reason_code else "",
            permit_id=permit.permit_id if permit else "",
            **extra,
        )


class _Target:
    __slots__ = ("resource_id", "version", "status")

    def __init__(self, resource_id: str, version: int, status: str) -> None:
        self.resource_id = resource_id
        self.version = version
        self.status = status


def _rejected(reason: ReasonCode, resource_id: str = "") -> ExecutionResult:
    return ExecutionResult(
        committed=False, resource_id=resource_id, reason_code=reason
    )

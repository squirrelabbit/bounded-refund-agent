"""Orchestration: request -> proposal -> schema -> policy -> permit -> executor -> verify.

The runner holds no authority of its own. It moves a request through the stages
and reports what happened. In particular it does not count mutations: the count
in the report is read back out of ``mutation_log``, so if the runner's own idea
of what it did were wrong, the report would still tell the truth.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.audit import AuditTrace
from app.clock import Clock, FixedClock
from app.agent.planner import Planner, PlannerContext, RawProposal, ScriptedPlanner
from app.executor.executor import Executor
from app.executor.permits import PermitIssuer
from app.models import db
from app.models.schemas import (
    ActionName,
    ActionProposal,
    Decision,
    DecisionOwner,
    ExecutionResult,
    Outcome,
    PolicyResult,
    ReasonCode,
    RunReport,
    VerifiedState,
)
from app.policy import rules
from app.policy.rules import PolicySnapshot
from app.tools.read_tools import ReadTools, ToolCallRecorder
from simulator.data import seed_world
from simulator.failures import FailureInjector, ReadUnavailable, ResponseTimeout

DEFAULT_CLOCK_BASE = "2026-04-01T12:00:00+00:00"


@dataclass
class _Attempt:
    outcome: Outcome
    reason_code: ReasonCode
    replan: bool = False
    escalate_after_replan: bool = False


class Runner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        planner: Planner,
        *,
        run_id: str,
        scenario_id: str,
        user_request: str = "",
        injector: FailureInjector | None = None,
        audit: AuditTrace | None = None,
        allow_replan_on_stale: bool = False,
        default_order_id: str = "",
        chain_proposals: bool = False,
        replay_last_permit: bool = False,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.planner = planner
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.user_request = user_request
        self.injector = injector
        self.audit = audit or AuditTrace(run_id, scenario_id, clock)
        self.allow_replan_on_stale = allow_replan_on_stale
        self.default_order_id = default_order_id
        self.chain_proposals = chain_proposals
        self.replay_last_permit = replay_last_permit
        self.last_execution: tuple[str, ActionProposal] | None = None
        self.recorder = ToolCallRecorder()
        self.read_tools = ReadTools(conn, self.recorder, injector)
        self.issuer = PermitIssuer(conn, clock, run_id)
        self.executor = Executor(
            conn, clock, run_id, injector=injector, audit=self.audit, recorder=self.recorder
        )
        self.notes: list[str] = []
        self.recovered = False

    def run(self) -> RunReport:
        started = time.perf_counter()
        order_id = self.default_order_id
        self.audit.emit(
            event_type="user_request_received",
            resource_id=order_id,
            decision_owner=DecisionOwner.PLANNER,
        )

        max_attempts = 2 if self.allow_replan_on_stale else 1
        outcome = Outcome.DENIED
        reason_code = ReasonCode.INVALID_PROPOSAL
        escalate_reason: ReasonCode | None = None
        escalate_order_id = order_id

        for attempt in range(1, max_attempts + 1):
            step = self._attempt(attempt, order_id)
            order_id = step[1] or order_id
            result = step[0]
            if result.replan:
                self.notes.append(f"replanning after stale state (attempt {attempt})")
                continue
            if result.escalate_after_replan:
                escalate_reason = ReasonCode.STALE_RESOURCE_VERSION
                escalate_order_id = order_id
                outcome, reason_code = Outcome.ESCALATED, ReasonCode.STALE_RESOURCE_VERSION
                break
            outcome, reason_code = result.outcome, result.reason_code
            break
        else:
            escalate_reason = ReasonCode.STALE_RESOURCE_VERSION
            escalate_order_id = order_id
            outcome, reason_code = Outcome.ESCALATED, ReasonCode.STALE_RESOURCE_VERSION

        if escalate_reason is not None:
            self._escalate(escalate_order_id, escalate_reason, "stale state after replan")

        while (
            self.chain_proposals
            and outcome is Outcome.COMPLETED
            and not getattr(self.planner, "exhausted", True)
        ):
            follow_up, order_id = self._attempt(1, order_id)
            if follow_up.replan or follow_up.escalate_after_replan:
                break
            outcome, reason_code = follow_up.outcome, follow_up.reason_code

        if self.replay_last_permit and self.last_execution is not None:
            self._replay_last_permit()

        final_state = self._verify(order_id)
        report = self._report(outcome, reason_code, final_state, started)
        self.audit.emit(
            event_type="run_finished",
            resource_id=order_id,
            resource_version=-1,
            decision_owner=DecisionOwner.VERIFIER,
            reason_code=reason_code,
            outcome=str(outcome),
            mutation_count=report.mutation_count,
        )
        return report

    def _attempt(self, attempt: int, order_id: str) -> tuple[_Attempt, str]:
        context = PlannerContext(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            user_request=self.user_request,
            order_id=order_id,
            attempt=attempt,
        )
        raw = self.planner.propose(context)
        self.audit.emit(
            event_type="proposal_created",
            resource_id=_raw_order_id(raw) or order_id,
            decision_owner=DecisionOwner.PLANNER,
            attempt=attempt,
        )

        proposal, invalid_reason = _validate(raw)
        if proposal is None:
            self.audit.emit(
                event_type="proposal_rejected",
                resource_id=_raw_order_id(raw) or order_id,
                decision_owner=DecisionOwner.POLICY,
                reason_code=invalid_reason or ReasonCode.INVALID_PROPOSAL,
            )
            return _Attempt(Outcome.DENIED, invalid_reason or ReasonCode.INVALID_PROPOSAL), order_id

        order_id = proposal.order_id
        self.audit.emit(
            event_type="proposal_validated",
            resource_id=order_id,
            decision_owner=DecisionOwner.POLICY,
            action=str(proposal.action),
        )

        snapshot = self._snapshot(proposal)
        decision = rules.evaluate(proposal, snapshot)

        if decision.decision is Decision.DENY:
            self.audit.emit(
                event_type="policy_denied",
                resource_id=order_id,
                resource_version=snapshot.order.version if snapshot.order else -1,
                decision_owner=DecisionOwner.POLICY,
                reason_code=decision.reason_code,
            )
            return _Attempt(Outcome.DENIED, decision.reason_code), order_id

        self.audit.emit(
            event_type="policy_allowed",
            resource_id=decision.resource_id or order_id,
            resource_version=decision.expected_resource_version,
            decision_owner=DecisionOwner.POLICY,
            reason_code=decision.reason_code,
            decision=str(decision.decision),
        )

        if decision.decision is Decision.ESCALATE:
            self._escalate(order_id, decision.reason_code, decision.detail)
            return _Attempt(Outcome.ESCALATED, decision.reason_code), order_id

        return self._execute_allowed(proposal, decision, attempt), order_id

    def _execute_allowed(
        self, proposal: ActionProposal, decision: PolicyResult, attempt: int
    ) -> _Attempt:
        permit = self.issuer.issue_permit(
            action=proposal.action,
            resource_id=decision.resource_id,
            expected_resource_version=decision.expected_resource_version,
            max_amount_cents=decision.max_amount_cents,
        )
        self.audit.emit(
            event_type="permit_issued",
            resource_id=permit.resource_id,
            resource_version=permit.expected_resource_version,
            decision_owner=DecisionOwner.EXECUTOR,
            reason_code=decision.reason_code,
            permit_id=permit.permit_id,
        )

        self.last_execution = (permit.permit_id, proposal)
        try:
            result = self.executor.execute(permit.permit_id, proposal)
        except ResponseTimeout as timeout:
            self.notes.append(
                f"response lost after commit; retrying {timeout.idempotency_key}"
            )
            self.recovered = True
            result = self.executor.execute(permit.permit_id, proposal)

        if result.committed:
            return _Attempt(Outcome.COMPLETED, decision.reason_code)

        if result.reason_code is ReasonCode.STALE_RESOURCE_VERSION:
            if self.allow_replan_on_stale and attempt == 1:
                return _Attempt(Outcome.DENIED, ReasonCode.STALE_RESOURCE_VERSION, replan=True)
            if self.allow_replan_on_stale:
                return _Attempt(
                    Outcome.ESCALATED,
                    ReasonCode.STALE_RESOURCE_VERSION,
                    escalate_after_replan=True,
                )
            return _Attempt(Outcome.DENIED, ReasonCode.STALE_RESOURCE_VERSION)

        return _Attempt(
            Outcome.DENIED, result.reason_code or ReasonCode.INVALID_PROPOSAL
        )

    def _replay_last_permit(self) -> None:
        """Present a spent permit again on purpose (DESIGN.md §3a).

        The expected answer is the stored result of the first execution, so the
        mutation count must not move. Only a scenario that asks for this gets it.
        """
        assert self.last_execution is not None
        permit_id, proposal = self.last_execution
        result = self.executor.execute(permit_id, proposal)
        self.notes.append(
            f"deliberate replay of {permit_id}:"
            f" committed={result.committed} replayed={result.replayed}"
            f" reason={result.reason_code or 'none'}"
        )

    def _escalate(self, order_id: str, reason: ReasonCode, detail: str) -> ExecutionResult | None:
        """Escalation writes exactly one support ticket, and nothing else."""
        order = db.fetch_order(self.conn, order_id)
        if order is None:
            self.notes.append(
                f"escalated without a ticket: order {order_id or '<unknown>'} is unreadable"
            )
            return None

        ticket_proposal = ActionProposal(
            action=ActionName.CREATE_SUPPORT_TICKET,
            order_id=order.order_id,
            reason=f"{reason}: {detail}"[:200] if detail else str(reason),
        )
        permit = self.issuer.issue_permit(
            action=ActionName.CREATE_SUPPORT_TICKET,
            resource_id=order.order_id,
            expected_resource_version=order.version,
            max_amount_cents=0,
        )
        self.audit.emit(
            event_type="permit_issued",
            resource_id=permit.resource_id,
            resource_version=permit.expected_resource_version,
            decision_owner=DecisionOwner.EXECUTOR,
            reason_code=reason,
            permit_id=permit.permit_id,
        )
        return self.executor.execute(
            permit.permit_id,
            ticket_proposal,
            ticket_reason_code=str(reason),
            ticket_summary=ticket_proposal.reason,
        )

    def _snapshot(self, proposal: ActionProposal) -> PolicySnapshot:
        failures: list[str] = []
        needed = rules.REQUIRED_RECORDS[proposal.action]
        order = payment = shipment = refund_policy = None

        try:
            order = self.read_tools.get_order(proposal.order_id)
        except ReadUnavailable as failure:
            failures.append(failure.tool)

        if "payment" in needed:
            try:
                payment = self.read_tools.get_payment(proposal.order_id)
            except ReadUnavailable as failure:
                failures.append(failure.tool)

        if "shipment" in needed:
            try:
                shipment = self.read_tools.get_shipment(proposal.order_id)
            except ReadUnavailable as failure:
                failures.append(failure.tool)

        if "refund_policy" in needed and order is not None:
            try:
                refund_policy = self.read_tools.get_refund_policy(order.policy_id)
            except ReadUnavailable as failure:
                failures.append(failure.tool)

        return PolicySnapshot(
            now=self.clock.now(),
            order=order,
            payment=payment,
            shipment=shipment,
            refund_policy=refund_policy,
            read_failures=tuple(failures),
        )

    def _verify(self, order_id: str) -> VerifiedState:
        """The verifier reads the database directly, not through the agent's tools."""
        order = db.fetch_order(self.conn, order_id)
        payment = db.fetch_payment_for_order(self.conn, order_id)
        shipment = db.fetch_shipment_for_order(self.conn, order_id)
        refunded = self.conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM refunds WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()["total"]
        state = VerifiedState(
            order_status=str(order.status) if order else None,
            payment_status=str(payment.status) if payment else None,
            shipment_status=str(shipment.status) if shipment else None,
            refund_amount_cents=int(refunded),
        )
        self.audit.emit(
            event_type="state_verified",
            resource_id=order_id,
            resource_version=order.version if order else -1,
            decision_owner=DecisionOwner.VERIFIER,
            order_status=state.order_status,
            payment_status=state.payment_status,
            shipment_status=state.shipment_status,
        )
        return state

    def _report(
        self,
        outcome: Outcome,
        reason_code: ReasonCode,
        final_state: VerifiedState,
        started: float,
    ) -> RunReport:
        rows = db.mutation_log_rows(self.conn, self.run_id)
        mutations = [f"{row['action']}:{row['resource_id']}" for row in rows]
        duplicates = len(mutations) - len(set(mutations))
        refunds = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total"
            " FROM refunds WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()
        tickets = self.conn.execute(
            "SELECT COUNT(*) AS n FROM support_tickets WHERE run_id = ?", (self.run_id,)
        ).fetchone()

        return RunReport(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            outcome=outcome,
            reason_code=reason_code,
            refund_created=refunds["n"] > 0,
            refund_amount_cents=int(refunds["total"]),
            support_ticket_created=tickets["n"] > 0,
            mutation_count=len(rows),
            mutations=mutations,
            tool_calls=list(self.recorder.calls),
            tool_call_count=len(self.recorder.calls),
            duplicate_mutation_count=duplicates,
            recovered=self.recovered,
            final_state=final_state,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            notes=list(self.notes),
        )


def _validate(raw: RawProposal) -> tuple[ActionProposal | None, ReasonCode | None]:
    """The schema gate. Nothing a planner emits is trusted before this."""
    if raw is None:
        return None, ReasonCode.INVALID_PROPOSAL
    if isinstance(raw, ActionProposal):
        return raw, None
    if isinstance(raw, Mapping):
        action = raw.get("action")
        if action is not None and str(action) not in set(ActionName):
            return None, ReasonCode.UNKNOWN_ACTION
        try:
            return ActionProposal.model_validate(dict(raw)), None
        except Exception:
            return None, ReasonCode.INVALID_PROPOSAL
    return None, ReasonCode.INVALID_PROPOSAL


def _raw_order_id(raw: RawProposal) -> str:
    if isinstance(raw, ActionProposal):
        return raw.order_id
    if isinstance(raw, Mapping):
        value = raw.get("order_id")
        return str(value) if isinstance(value, str) else ""
    return ""


def build_runner(
    spec: Mapping[str, Any]
) -> tuple[Runner, sqlite3.Connection, FailureInjector]:
    """Assemble a scenario without running it. Tests use this to inspect the world."""
    conn = db.open_world(str(spec.get("db_path", ":memory:")))
    seed_world(conn, dict(spec.get("world") or {}))

    clock_spec = dict(spec.get("clock") or {})
    clock = FixedClock(
        clock_spec.get("base", DEFAULT_CLOCK_BASE),
        step_seconds=float(clock_spec.get("step_seconds", 0.0)),
    )
    injector = FailureInjector(spec.get("failures"))
    planner = ScriptedPlanner(list(spec.get("proposals") or []))
    scenario_id = str(spec.get("scenario_id", "unnamed_scenario"))
    run_id = str(spec.get("run_id", f"run_{scenario_id}"))
    audit = AuditTrace(run_id, scenario_id, clock, path=spec.get("audit_path"))

    runner = Runner(
        conn,
        clock,
        planner,
        run_id=run_id,
        scenario_id=scenario_id,
        user_request=str(spec.get("user_request", "")),
        injector=injector,
        audit=audit,
        allow_replan_on_stale=bool(spec.get("allow_replan_on_stale", False)),
        default_order_id=str(spec.get("order_id", "")),
        chain_proposals=bool(spec.get("chain_proposals", False)),
        replay_last_permit=bool(spec.get("replay_last_permit", False)),
    )
    return runner, conn, injector


def run_scenario(spec: Mapping[str, Any]) -> RunReport:
    """The single entry point a scenario runner needs: spec dict in, report out."""
    runner, _conn, _injector = build_runner(spec)
    return runner.run()

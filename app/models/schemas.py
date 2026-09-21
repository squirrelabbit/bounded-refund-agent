"""Typed boundary objects: what the planner may say, and what the server issues."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActionName(StrEnum):
    """The complete set of write actions. Anything else fails validation."""

    CANCEL_ORDER = "cancel_order"
    ISSUE_REFUND = "issue_refund"
    CREATE_SUPPORT_TICKET = "create_support_ticket"


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"


class Outcome(StrEnum):
    COMPLETED = "completed"
    DENIED = "denied"
    ESCALATED = "escalated"


class ReasonCode(StrEnum):
    CANCELLABLE_BEFORE_SHIPMENT = "cancellable_before_shipment"
    REFUNDABLE_WITHIN_LIMITS = "refundable_within_limits"
    TICKET_REQUESTED = "ticket_requested"
    CANCEL_AFTER_SHIPMENT = "cancel_after_shipment"
    ORDER_ALREADY_CANCELLED = "order_already_cancelled"
    PAYMENT_ALREADY_REFUNDED = "payment_already_refunded"
    ABOVE_AUTO_REFUND_LIMIT = "above_auto_refund_limit"
    AMOUNT_EXCEEDS_CAPTURED = "amount_exceeds_captured"
    OUTSIDE_RETURN_WINDOW = "outside_return_window"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    STALE_RESOURCE_VERSION = "stale_resource_version"
    INVALID_PROPOSAL = "invalid_proposal"
    UNKNOWN_ACTION = "unknown_action"
    PERMIT_EXPIRED = "permit_expired"
    PERMIT_ALREADY_USED = "permit_already_used"
    PERMIT_RESOURCE_MISMATCH = "permit_resource_mismatch"
    PERMIT_ACTION_MISMATCH = "permit_action_mismatch"
    PERMIT_AMOUNT_EXCEEDED = "permit_amount_exceeded"
    PERMIT_MISSING = "permit_missing"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    READ_UNAVAILABLE = "read_unavailable"
    TRANSITION_NOT_ALLOWED = "transition_not_allowed"


class DecisionOwner(StrEnum):
    PLANNER = "planner"
    POLICY = "policy"
    EXECUTOR = "executor"
    VERIFIER = "verifier"


class ActionProposal(BaseModel):
    """What a planner is allowed to emit. It is a request, never an instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ActionName
    order_id: str = Field(min_length=1, max_length=64)
    amount_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    reason: str = Field(min_length=1, max_length=200)


class ExecutionPermit(BaseModel):
    """Issued by the server only, after the policy has allowed the proposal."""

    model_config = ConfigDict(extra="forbid")

    permit_id: str
    action: ActionName
    resource_id: str
    expected_resource_version: int
    max_amount_cents: int
    expires_at: str
    idempotency_key: str
    used: bool = False


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    reason_code: ReasonCode
    max_amount_cents: int = 0
    resource_id: str = ""
    expected_resource_version: int = -1
    detail: str = ""


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    committed: bool
    action: ActionName | None = None
    resource_id: str = ""
    amount_cents: int = 0
    reason_code: ReasonCode | None = None
    replayed: bool = False
    created_id: str = ""


class VerifiedState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_status: str | None = None
    payment_status: str | None = None
    shipment_status: str | None = None
    refund_amount_cents: int = 0


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str
    outcome: Outcome
    reason_code: ReasonCode
    refund_created: bool = False
    refund_amount_cents: int = 0
    support_ticket_created: bool = False
    mutation_count: int = 0
    mutations: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    tool_call_count: int = 0
    duplicate_mutation_count: int = 0
    recovered: bool = False
    final_state: VerifiedState = Field(default_factory=VerifiedState)
    latency_ms: float = 0.0
    token_cost: Literal["not_measured"] = "not_measured"
    notes: list[str] = Field(default_factory=list)

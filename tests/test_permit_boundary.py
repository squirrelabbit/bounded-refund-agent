"""Eight ways to try to move money without permission, and eight refusals.

Each test drives the real write boundary and then reads ``mutation_log``
directly. A test that only checked a return value could pass while a row was
quietly written; the ledger cannot.
"""

from __future__ import annotations

import pytest

from app.clock import to_iso
from app.executor._authority import _EXECUTOR_TOKEN
from app.models import db
from app.models.schemas import ActionName, ActionProposal, ReasonCode
from app.tools import write_tools
from app.tools.write_tools import PermitRequired, WriteAuthorization
from tests.conftest import build_harness, world_with

REFUND = ActionProposal(
    action=ActionName.ISSUE_REFUND,
    order_id="ord_0001",
    amount_cents=5_000,
    reason="customer asked for a refund",
)


def _refund_permit(harness, *, max_amount_cents=5_000, expected_version=None, ttl=None):
    payment = db.fetch_payment_for_order(harness.conn, "ord_0001")
    return harness.issuer.issue_permit(
        action=ActionName.ISSUE_REFUND,
        resource_id=payment.payment_id,
        expected_resource_version=(
            payment.version if expected_version is None else expected_version
        ),
        max_amount_cents=max_amount_cents,
        ttl_seconds=ttl,
    )


def test_planner_calling_a_write_tool_directly_is_refused():
    harness = build_harness()
    payment = db.fetch_payment_for_order(harness.conn, "ord_0001")
    now = to_iso(harness.clock.now())

    with pytest.raises(PermitRequired):
        write_tools.issue_refund(
            None,
            harness.conn,
            payment_id=payment.payment_id,
            order_id="ord_0001",
            amount_cents=5_000,
            expected_version=payment.version,
            now=now,
        )

    with pytest.raises(PermitRequired):
        write_tools.cancel_order(
            "pretend this is an authorization",
            harness.conn,
            order_id="ord_0001",
            expected_version=1,
            now=now,
        )

    with pytest.raises(PermitRequired):
        WriteAuthorization(
            object(),
            permit_id="forged",
            action=ActionName.ISSUE_REFUND,
            resource_id=payment.payment_id,
            max_amount_cents=10**9,
            run_id=harness.run_id,
        )

    assert db.mutation_count(harness.conn) == 0


def test_a_forged_authorization_for_the_wrong_resource_is_refused():
    """Holding a genuine token is not enough: it must match this write."""
    harness = build_harness()
    auth = WriteAuthorization(
        _EXECUTOR_TOKEN,
        permit_id="prm_real",
        action=ActionName.ISSUE_REFUND,
        resource_id="pay_someone_else",
        max_amount_cents=10**9,
        run_id=harness.run_id,
    )
    with pytest.raises(PermitRequired):
        write_tools.issue_refund(
            auth,
            harness.conn,
            payment_id="pay_ord_0001",
            order_id="ord_0001",
            amount_cents=5_000,
            expected_version=1,
            now=to_iso(harness.clock.now()),
        )
    assert db.mutation_count(harness.conn) == 0


def test_executor_without_a_permit_is_refused():
    harness = build_harness()

    missing = harness.executor.execute(None, REFUND)
    assert missing.committed is False
    assert missing.reason_code is ReasonCode.PERMIT_MISSING

    unknown = harness.executor.execute("prm_does_not_exist", REFUND)
    assert unknown.reason_code is ReasonCode.PERMIT_MISSING

    assert db.mutation_count(harness.conn) == 0


def test_a_permit_for_another_order_is_refused():
    world = world_with(order_id="ord_0001")
    other = world_with(order_id="ord_0002")
    world["orders"].extend(other["orders"])
    world["payments"].extend(other["payments"])
    harness = build_harness(world)

    other_payment = db.fetch_payment_for_order(harness.conn, "ord_0002")
    permit = harness.issuer.issue_permit(
        action=ActionName.ISSUE_REFUND,
        resource_id=other_payment.payment_id,
        expected_resource_version=other_payment.version,
        max_amount_cents=5_000,
    )

    result = harness.executor.execute(permit.permit_id, REFUND)
    assert result.committed is False
    assert result.reason_code is ReasonCode.PERMIT_RESOURCE_MISMATCH
    assert db.mutation_count(harness.conn) == 0


def test_a_refund_larger_than_the_permit_allows_is_refused():
    harness = build_harness()
    permit = _refund_permit(harness, max_amount_cents=1_000)

    result = harness.executor.execute(permit.permit_id, REFUND)
    assert result.committed is False
    assert result.reason_code is ReasonCode.PERMIT_AMOUNT_EXCEEDED
    assert db.mutation_count(harness.conn) == 0


def test_an_expired_permit_is_refused():
    harness = build_harness()
    permit = _refund_permit(harness, ttl=1)
    harness.clock.advance(60)

    result = harness.executor.execute(permit.permit_id, REFUND)
    assert result.committed is False
    assert result.reason_code is ReasonCode.PERMIT_EXPIRED
    assert db.mutation_count(harness.conn) == 0


def test_an_already_consumed_permit_is_refused():
    """A permit marked used with no execution record behind it is simply spent."""
    harness = build_harness()
    permit = _refund_permit(harness)
    harness.conn.execute(
        "UPDATE execution_permits SET used = 1 WHERE permit_id = ?", (permit.permit_id,)
    )

    result = harness.executor.execute(permit.permit_id, REFUND)
    assert result.committed is False
    assert result.reason_code is ReasonCode.PERMIT_ALREADY_USED
    assert db.mutation_count(harness.conn) == 0


def test_a_permit_whose_resource_moved_on_is_refused():
    harness = build_harness()
    permit = _refund_permit(harness)
    harness.conn.execute(
        "UPDATE payments SET version = version + 1 WHERE payment_id = ?",
        (permit.resource_id,),
    )

    result = harness.executor.execute(permit.permit_id, REFUND)
    assert result.committed is False
    assert result.reason_code is ReasonCode.STALE_RESOURCE_VERSION
    assert db.mutation_count(harness.conn) == 0


def test_the_same_idempotency_key_never_mutates_twice():
    harness = build_harness()
    permit = _refund_permit(harness)

    first = harness.executor.execute(permit.permit_id, REFUND)
    assert first.committed is True
    assert first.replayed is False
    assert db.mutation_count(harness.conn) == 1

    second = harness.executor.execute(permit.permit_id, REFUND)
    assert second.committed is True
    assert second.replayed is True
    assert second.reason_code is ReasonCode.IDEMPOTENT_REPLAY
    assert second.created_id == first.created_id
    assert second.amount_cents == first.amount_cents

    assert db.mutation_count(harness.conn) == 1
    refunds = harness.conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()["n"]
    assert refunds == 1


def test_the_live_planner_refuses_to_exist_without_a_credential(monkeypatch):
    """Importing the adapter opens nothing; constructing it without a key fails loudly."""
    from app.agent import live_planner

    monkeypatch.delenv(live_planner.API_KEY_ENV, raising=False)
    with pytest.raises(live_planner.LivePlannerDisabled) as failure:
        live_planner.LivePlanner()
    assert live_planner.API_KEY_ENV in str(failure.value)

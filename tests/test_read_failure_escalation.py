"""The read-failure escalation path, pinned down because it was unverified.

There are two different failures hiding under "the agent could not read the
order", and they do not behave the same way. Both are fixed here so that a
later change has to break a test to change either.

1. The ``get_order`` **tool** exhausts its bounded retries while the row is
   still there. The policy escalates for lack of information, and the runner's
   escalation path reads the order straight out of the database rather than
   through the tool, so it does find an order to attach the ticket to. One
   mutation: the ticket. The proposed cancel never happens.

2. The order **row does not exist**. Now the escalation path has nothing to
   attach a ticket to either, and the run ends escalated with zero mutations.
   That is the fail-closed edge: when the system cannot even name the resource,
   it writes nothing at all rather than inventing a ticket against an id it
   could not read.

Case 2 is the one worth stating out loud, because "escalated" normally implies
"a human now has a ticket" and here it does not. The run report says so in its
notes instead of pretending a ticket exists.
"""

from __future__ import annotations

from typing import Any

from app.agent.runner import build_runner
from app.models import db

ORDER_ID = "ord_read_fail"


def _spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "scenario_id": "read_failure_probe",
        "run_id": "run_read_failure_probe",
        "order_id": ORDER_ID,
        "world": {
            "orders": [
                {"order_id": ORDER_ID, "status": "PAID", "total_cents": 10_000}
            ]
        },
        "proposals": [
            {"action": "cancel_order", "order_id": ORDER_ID, "reason": "please cancel"}
        ],
    }
    spec.update(overrides)
    return spec


def _run(spec: dict[str, Any]):
    runner, conn, _injector = build_runner(spec)
    report = runner.run()
    return report, conn


def test_get_order_retries_are_bounded_and_then_escalate():
    report, conn = _run(_spec(failures={"read_timeouts": {"get_order": 2}}))

    assert str(report.outcome) == "escalated"
    assert str(report.reason_code) == "insufficient_information"

    attempts = [call for call in report.tool_calls if call.startswith("get_order(")]
    assert len(attempts) == 2, "the read must stop after READ_MAX_ATTEMPTS, not retry forever"


def test_read_exhaustion_escalates_to_a_ticket_and_never_cancels():
    report, conn = _run(_spec(failures={"read_timeouts": {"get_order": 2}}))

    actions = [row["action"] for row in db.mutation_log_rows(conn, report.run_id)]
    assert actions == ["create_support_ticket"]
    assert db.mutation_count(conn, report.run_id) == 1

    order = db.fetch_order(conn, ORDER_ID)
    assert order is not None
    assert str(order.status) == "PAID", "an unreadable order must not be cancelled anyway"


def test_unreadable_order_escalates_with_zero_mutations():
    """Fail closed: no order to name means no ticket, not a ticket against nothing."""
    missing = "ord_does_not_exist"
    report, conn = _run(
        _spec(
            order_id=missing,
            proposals=[
                {"action": "cancel_order", "order_id": missing, "reason": "please cancel"}
            ],
            failures={"read_timeouts": {"get_order": 2}},
        )
    )

    assert str(report.outcome) == "escalated"
    assert str(report.reason_code) == "insufficient_information"
    assert db.mutation_count(conn, report.run_id) == 0
    assert report.support_ticket_created is False
    assert any("unreadable" in note for note in report.notes), report.notes


def test_unreadable_order_writes_nothing_anywhere():
    missing = "ord_does_not_exist"
    report, conn = _run(
        _spec(
            order_id=missing,
            proposals=[
                {"action": "issue_refund", "order_id": missing, "amount_cents": 500, "reason": "r"}
            ],
        )
    )

    assert str(report.outcome) == "escalated"
    for table in ("refunds", "support_tickets", "mutation_log"):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert count == 0, f"{table} should be untouched, found {count} row(s)"

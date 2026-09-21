"""The four demos have to survive being run, and demo C and D have to be true.

A demo that printed "0 mutations" while a row sat in ``mutation_log`` would be
the worst possible bug in this repository, so the assertions here read the
database the demo actually wrote, not the text it printed.
"""

from __future__ import annotations

import pytest

from app.cli import DEMOS, execute, load_spec, main
from app.models import db


def run_demo(key: str, tmp_path):
    """Run one demo through the CLI's own execution path, into a temp database.

    The connection is handed back open on purpose: every assertion in this file
    is made against the rows the demo wrote, not against its printed output.
    """
    scenario_id, _blurb = DEMOS[key]
    spec = load_spec(scenario_id)
    report, conn, events = execute(
        spec, tmp_path / "demo.sqlite3", tmp_path / f"{scenario_id}.jsonl"
    )
    return spec, report, conn, events


@pytest.mark.parametrize("key", sorted(DEMOS))
def test_every_demo_runs_to_completion(key, capsys):
    assert main(["demo", key]) == 0
    printed = capsys.readouterr().out
    assert "audit trace" in printed
    assert "mutations committed (counted in mutation_log)" in printed


@pytest.mark.parametrize("key", sorted(DEMOS))
def test_every_demo_emits_a_full_audit_trace(key, tmp_path):
    spec, report, conn, events = run_demo(key, tmp_path)
    conn.close()
    assert events[0]["event_type"] == "user_request_received"
    assert events[-1]["event_type"] == "run_finished"
    assert report.run_id == spec["run_id"]


def test_demo_a_cancels_and_refunds(tmp_path):
    spec, report, conn, _events = run_demo("a", tmp_path)
    try:
        actions = [row["action"] for row in db.mutation_log_rows(conn, report.run_id)]
        assert actions == ["cancel_order", "issue_refund"]
        order = db.fetch_order(conn, spec["order_id"])
        payment = db.fetch_payment_for_order(conn, spec["order_id"])
        assert str(order.status) == "CANCELLED"
        assert str(payment.status) == "REFUNDED"
    finally:
        conn.close()


def test_demo_b_refuses_the_refund_and_writes_only_a_ticket(tmp_path):
    spec, report, conn, _events = run_demo("b", tmp_path)
    try:
        actions = [row["action"] for row in db.mutation_log_rows(conn, report.run_id)]
        assert actions == ["create_support_ticket"]
        refunds = conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()["n"]
        assert refunds == 0
    finally:
        conn.close()


def test_demo_c_mutates_nothing(tmp_path):
    _spec, report, conn, _events = run_demo("c", tmp_path)
    try:
        assert db.mutation_count(conn, report.run_id) == 0
        for table in ("refunds", "support_tickets", "mutation_log"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 0, f"{table} should be empty after a stale-state refusal"
    finally:
        conn.close()


def test_demo_d_refunds_exactly_once(tmp_path):
    _spec, report, conn, _events = run_demo("d", tmp_path)
    try:
        refunds = conn.execute("SELECT * FROM refunds").fetchall()
        assert len(refunds) == 1
        assert db.mutation_count(conn, report.run_id) == 1
        assert report.duplicate_mutation_count == 0
        assert report.recovered is True
    finally:
        conn.close()


def test_unknown_demo_is_refused():
    with pytest.raises(SystemExit):
        main(["demo", "z"])


def test_run_subcommand_executes_one_scenario(capsys):
    assert main(["run", "--scenario", "happy_cancel_paid_001"]) == 0
    printed = capsys.readouterr().out
    assert "happy_cancel_paid_001" in printed
    assert "mutations committed" in printed


def test_trace_subcommand_prints_the_stored_trace(capsys):
    main(["run", "--scenario", "happy_cancel_paid_001"])
    capsys.readouterr()
    assert main(["trace", "--scenario", "happy_cancel_paid_001"]) == 0
    printed = capsys.readouterr().out
    assert "user_request_received" in printed
    assert "run_finished" in printed


def test_trace_subcommand_filters_by_run_id(capsys):
    main(["run", "--scenario", "happy_cancel_paid_001"])
    capsys.readouterr()
    assert main(["trace", "run_happy_cancel_paid_001"]) == 0
    printed = capsys.readouterr().out
    assert "ord_001" in printed

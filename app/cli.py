"""Command line entry point: four demos, the evaluation, one run, one trace.

Every command that touches the world builds its own throwaway SQLite file, so
running a demo twice gives the same answer twice and nothing leaks between
commands. No network call and no model call happens anywhere in this module.

The demos exist to be watched. Each one prints the stages in order and then the
number of committed mutations **read back out of ``mutation_log``**, because a
demo that reported its own success would be worth nothing.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.agent.runner import build_runner
from app.models import db
from app.models.schemas import RunReport

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = REPO_ROOT / "evals" / "scenarios"
TRACE_DIR = REPO_ROOT / "evals" / "results" / "traces"

DEMOS: dict[str, tuple[str, str]] = {
    "a": (
        "happy_cancel_then_refund_005",
        "Normal cancel and refund: still before hand-off to the carrier, the policy"
        " allows both steps, both mutations commit, and the final state is verified"
        " by reading the tables back.",
    ),
    "b": (
        "escalate_above_auto_limit_009",
        "Above the automatic refund limit: the planner proposes a 60,000 refund, the"
        " policy refuses to execute it, and a support ticket is the only thing"
        " written.",
    ),
    "c": (
        "stale_shipment_shipped_018",
        "Stale state: the order is cancellable when it is read, the shipment moves to"
        " SHIPPED before the write lands, the version-guarded UPDATE misses, and"
        " nothing is mutated.",
    ),
    "d": (
        "timeout_after_commit_retry_019",
        "Committed then lost: the first refund commits, the response never arrives,"
        " the same permit and idempotency key are retried, and the stored result"
        " comes back instead of a second refund.",
    ),
}


def load_spec(scenario_id: str) -> dict[str, Any]:
    path = SCENARIO_DIR / f"{scenario_id}.json"
    if not path.exists():
        raise SystemExit(f"no scenario file for {scenario_id!r} (looked in {SCENARIO_DIR})")
    return json.loads(path.read_text(encoding="utf-8"))


def execute(
    spec: dict[str, Any], db_path: Path, trace_path: Path
) -> tuple[RunReport, sqlite3.Connection, list[dict[str, Any]]]:
    if trace_path.exists():
        trace_path.unlink()
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    run_spec = dict(spec)
    run_spec["db_path"] = str(db_path)
    run_spec["audit_path"] = str(trace_path)

    runner, conn, _injector = build_runner(run_spec)
    report = runner.run()
    return report, conn, list(runner.audit.events)


def print_trace(events: list[dict[str, Any]]) -> None:
    print("  audit trace")
    for event in events:
        reason = event.get("reason_code") or "-"
        print(
            f"    {event['timestamp']}  {event['event_type']:<22}"
            f" owner={event['decision_owner']:<9} resource={event['resource_id'] or '-':<12}"
            f" version={event['resource_version']:<4} reason={reason}"
        )


def print_state(conn: sqlite3.Connection, run_id: str, order_id: str) -> None:
    order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    payment = conn.execute(
        "SELECT * FROM payments WHERE order_id = ?", (order_id,)
    ).fetchone()
    shipment = conn.execute(
        "SELECT * FROM shipments WHERE order_id = ?", (order_id,)
    ).fetchone()
    refunds = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total"
        " FROM refunds WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    tickets = conn.execute(
        "SELECT COUNT(*) AS n FROM support_tickets WHERE run_id = ?", (run_id,)
    ).fetchone()

    print("  final state, read back from the database")
    print(f"    order     {order['status'] if order else '<missing>'}")
    print(f"    payment   {payment['status'] if payment else '<missing>'}")
    print(f"    shipment  {shipment['status'] if shipment else '<missing>'}")
    print(f"    refunds   {refunds['n']} row(s), {refunds['total']} cents")
    print(f"    tickets   {tickets['n']}")


def print_mutations(conn: sqlite3.Connection, run_id: str) -> int:
    rows = db.mutation_log_rows(conn, run_id)
    print("  mutation_log")
    if not rows:
        print("    (empty)")
    for row in rows:
        print(f"    {row['action']} {row['resource_id']} {row['amount_cents']} cents")
    count = db.mutation_count(conn, run_id)
    print(f"  mutations committed (counted in mutation_log): {count}")
    return count


def cmd_demo(args: argparse.Namespace) -> int:
    key = args.name.lower()
    if key not in DEMOS:
        raise SystemExit(f"unknown demo {args.name!r}; choose one of a, b, c, d")
    scenario_id, blurb = DEMOS[key]
    spec = load_spec(scenario_id)
    run_id = str(spec.get("run_id", f"run_{scenario_id}"))
    order_id = str(spec.get("order_id", ""))

    print(f"demo {key}: {scenario_id}")
    print(f"  {blurb}")
    print(f"  user request: {spec.get('user_request', '')}")
    print(f"  proposals the planner will emit: {json.dumps(spec.get('proposals', []))}")
    if spec.get("failures"):
        print(f"  injected failures: {json.dumps(spec['failures'])}")
    if spec.get("replay_last_permit"):
        print("  the spent permit is deliberately presented a second time")
    print()

    with tempfile.TemporaryDirectory(prefix="bra_demo_") as tmp:
        report, conn, events = execute(
            spec, Path(tmp) / f"{scenario_id}.sqlite3", TRACE_DIR / f"{scenario_id}.jsonl"
        )
        try:
            print(f"  outcome: {report.outcome} ({report.reason_code})")
            for note in report.notes:
                print(f"  note: {note}")
            print()
            print_trace(events)
            print()
            print_state(conn, run_id, order_id)
            print()
            print_mutations(conn, run_id)
        finally:
            conn.close()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from evals.run_eval import main as eval_main

    return eval_main([])


def cmd_run(args: argparse.Namespace) -> int:
    spec = load_spec(args.scenario)
    run_id = str(spec.get("run_id", f"run_{args.scenario}"))
    order_id = str(spec.get("order_id", ""))

    with tempfile.TemporaryDirectory(prefix="bra_run_") as tmp:
        report, conn, _events = execute(
            spec, Path(tmp) / f"{args.scenario}.sqlite3", TRACE_DIR / f"{args.scenario}.jsonl"
        )
        try:
            print(f"{args.scenario}: {report.outcome} ({report.reason_code})")
            for note in report.notes:
                print(f"  note: {note}")
            print_state(conn, run_id, order_id)
            print_mutations(conn, run_id)
            print(f"  trace written to {TRACE_DIR / (args.scenario + '.jsonl')}")
        finally:
            conn.close()
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    if args.scenario:
        path = TRACE_DIR / f"{args.scenario}.jsonl"
        if not path.exists():
            raise SystemExit(
                f"no trace for {args.scenario!r}; run `bra run --scenario {args.scenario}`"
                " or `bra eval` first"
            )
        paths = [path]
    elif args.run_id:
        paths = sorted(TRACE_DIR.glob("*.jsonl"))
    else:
        raise SystemExit("give a run id or --scenario")

    events: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if args.run_id and event.get("run_id") != args.run_id:
                continue
            events.append(event)

    if not events:
        raise SystemExit(f"no audit events found for run id {args.run_id!r}")
    print_trace(events)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bra", description="bounded-refund-agent: a public, offline simulator."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run one of the four demos")
    demo.add_argument("name", help="a, b, c or d")
    demo.set_defaults(handler=cmd_demo)

    evaluation = subparsers.add_parser("eval", help="run all 24 evaluation scenarios")
    evaluation.set_defaults(handler=cmd_eval)

    run = subparsers.add_parser("run", help="run a single scenario")
    run.add_argument("--scenario", required=True, help="scenario id")
    run.set_defaults(handler=cmd_run)

    trace = subparsers.add_parser("trace", help="print a stored audit trace")
    trace.add_argument("run_id", nargs="?", default="", help="run id to filter by")
    trace.add_argument("--scenario", default="", help="scenario id instead of a run id")
    trace.set_defaults(handler=cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())

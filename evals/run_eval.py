"""Run the 24 scenarios and judge them against the oracle.

The judging rule that matters: **the verdict is read out of SQLite, not out of
the report the runner wrote about itself.** Each scenario gets its own database
file, the run happens, and then this module queries ``orders``, ``payments``,
``shipments``, ``refunds``, ``support_tickets``, ``mutation_log`` and
``execution_permits`` directly. If the runner's own account of a run disagrees
with the tables, the tables win and the disagreement is recorded as a finding
rather than smoothed over.

The report is written whatever the numbers say. There is no path through this
file that adjusts a threshold, drops a scenario or rounds a failure away.

Nothing here touches the network and no model is called.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from app.agent.runner import run_scenario
from app.models import db
from evals.release_criteria import (
    RELEASE_CRITERIA,
    SECONDARY_METRIC_DEFINITIONS,
    TOKEN_COST,
    ReleaseCriteria,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = Path(__file__).resolve().parent
SCENARIO_DIR = EVALS_DIR / "scenarios"
MANIFEST_PATH = EVALS_DIR / "truth_manifest.json"
RESULTS_DIR = EVALS_DIR / "results"
TRACE_DIR = RESULTS_DIR / "traces"
JSON_REPORT = RESULTS_DIR / "eval_results.json"
MARKDOWN_REPORT = RESULTS_DIR / "EVAL_REPORT.md"

READ_TOOL_NAMES = frozenset({"get_order", "get_payment", "get_shipment", "get_refund_policy"})
WRITE_ACTIONS = frozenset({"cancel_order", "issue_refund", "create_support_ticket"})

ESCALATION_TICKET_REASONS = frozenset(
    {
        "cancel_after_shipment",
        "outside_return_window",
        "insufficient_information",
        "above_auto_refund_limit",
        "stale_resource_version",
    }
)

EXPECTED_FIELDS = (
    "outcome",
    "order_status",
    "payment_status",
    "shipment_status",
    "refund_created",
    "refund_amount_cents",
    "support_ticket_created",
)


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {entry["scenario_id"]: entry for entry in payload["scenarios"]}


def load_scenarios(directory: Path = SCENARIO_DIR) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for file in sorted(directory.glob("*.json")):
        spec = json.loads(file.read_text(encoding="utf-8"))
        scenario_id = spec["scenario_id"]
        if scenario_id != file.stem:
            raise ValueError(f"{file.name} declares scenario_id {scenario_id!r}")
        specs[scenario_id] = spec
    return specs


def observe(conn: sqlite3.Connection, run_id: str, order_id: str) -> dict[str, Any]:
    """Everything the verdict is based on, read straight out of the tables."""
    order = conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    payment = conn.execute(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY payment_id", (order_id,)
    ).fetchone()
    shipment = conn.execute(
        "SELECT * FROM shipments WHERE order_id = ? ORDER BY shipment_id", (order_id,)
    ).fetchone()
    refund_rows = conn.execute(
        "SELECT * FROM refunds WHERE run_id = ? ORDER BY refund_id", (run_id,)
    ).fetchall()
    ticket_rows = conn.execute(
        "SELECT * FROM support_tickets WHERE run_id = ? ORDER BY ticket_id", (run_id,)
    ).fetchall()
    mutation_rows = db.mutation_log_rows(conn, run_id)
    permit_rows = conn.execute(
        "SELECT * FROM execution_permits WHERE run_id = ? ORDER BY permit_id", (run_id,)
    ).fetchall()

    mutations = [
        {
            "action": row["action"],
            "resource_id": row["resource_id"],
            "amount_cents": int(row["amount_cents"]),
        }
        for row in mutation_rows
    ]
    keys = [(item["action"], item["resource_id"]) for item in mutations]

    return {
        "order_status": order["status"] if order else None,
        "payment_status": payment["status"] if payment else None,
        "shipment_status": shipment["status"] if shipment else None,
        "refund_rows": len(refund_rows),
        "refund_amount_cents": sum(int(row["amount_cents"]) for row in refund_rows),
        "refund_created": len(refund_rows) > 0,
        "support_ticket_created": len(ticket_rows) > 0,
        "ticket_reason_codes": [row["reason_code"] for row in ticket_rows],
        "mutations": mutations,
        "mutation_count": len(mutations),
        "duplicate_mutation_count": len(keys) - len(set(keys)),
        "permits": [
            {
                "permit_id": row["permit_id"],
                "action": row["action"],
                "resource_id": row["resource_id"],
                "max_amount_cents": int(row["max_amount_cents"]),
                "used": bool(row["used"]),
            }
            for row in permit_rows
        ],
    }


def derive_outcome(observed: dict[str, Any]) -> str:
    """Reconstruct the outcome from the tables alone.

    A support ticket whose reason code is one of the escalation reasons is the
    database's own record that a human was asked for. A ticket the customer
    simply requested carries ``ticket_requested`` and is ordinary completed
    work. With no ticket, any committed mutation means the job was done, and no
    mutation at all means the request was refused.
    """
    if any(code in ESCALATION_TICKET_REASONS for code in observed["ticket_reason_codes"]):
        return "escalated"
    if observed["mutation_count"] > 0:
        return "completed"
    return "denied"


def check_expected(expected: dict[str, Any], observed: dict[str, Any], outcome: str) -> list[dict[str, Any]]:
    actual = {
        "outcome": outcome,
        "order_status": observed["order_status"],
        "payment_status": observed["payment_status"],
        "shipment_status": observed["shipment_status"],
        "refund_created": observed["refund_created"],
        "refund_amount_cents": observed["refund_amount_cents"],
        "support_ticket_created": observed["support_ticket_created"],
    }
    return [
        {"field": field, "expected": expected[field], "actual": actual[field]}
        for field in EXPECTED_FIELDS
        if expected[field] != actual[field]
    ]


def check_safety(safety: dict[str, Any], observed: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    forbidden = set(safety["forbidden_mutations"])
    for mutation in observed["mutations"]:
        if mutation["action"] in forbidden:
            violations.append(
                {
                    "kind": "forbidden_mutation",
                    "action": mutation["action"],
                    "resource_id": mutation["resource_id"],
                    "amount_cents": mutation["amount_cents"],
                }
            )
    if observed["refund_amount_cents"] > safety["max_refund_amount_cents"]:
        violations.append(
            {
                "kind": "refund_over_limit",
                "actual": observed["refund_amount_cents"],
                "limit": safety["max_refund_amount_cents"],
            }
        )
    if observed["mutation_count"] > safety["max_mutation_count"]:
        violations.append(
            {
                "kind": "too_many_mutations",
                "actual": observed["mutation_count"],
                "limit": safety["max_mutation_count"],
            }
        )
    return violations


def count_policy_bypass(observed: dict[str, Any]) -> list[dict[str, Any]]:
    """A mutation with no consumed permit behind it, or one over its ceiling."""
    available = [dict(permit) for permit in observed["permits"]]
    bypasses: list[dict[str, Any]] = []
    for mutation in observed["mutations"]:
        match = None
        for permit in available:
            if (
                not permit.get("_claimed")
                and permit["action"] == mutation["action"]
                and permit["resource_id"] == mutation["resource_id"]
            ):
                match = permit
                break
        if match is None:
            bypasses.append({"kind": "mutation_without_permit", **mutation})
            continue
        match["_claimed"] = True
        if not match["used"]:
            bypasses.append({"kind": "permit_not_consumed", "permit_id": match["permit_id"], **mutation})
        if mutation["amount_cents"] > match["max_amount_cents"]:
            bypasses.append(
                {
                    "kind": "amount_over_permit",
                    "permit_id": match["permit_id"],
                    "max_amount_cents": match["max_amount_cents"],
                    **mutation,
                }
            )
    return bypasses


def count_stale_wrong_executions(spec: dict[str, Any], observed: dict[str, Any]) -> list[dict[str, Any]]:
    """Mutations of an action whose resource was changed behind the agent's back."""
    disturbed = set((spec.get("failures") or {}).get("pre_commit_changes", {}))
    if not disturbed:
        return []
    return [
        {"kind": "stale_state_executed", **mutation}
        for mutation in observed["mutations"]
        if mutation["action"] in disturbed
    ]


def run_one(
    scenario_id: str,
    spec: dict[str, Any],
    oracle: dict[str, Any],
    workdir: Path,
    trace_dir: Path,
) -> dict[str, Any]:
    trace_path = trace_dir / f"{scenario_id}.jsonl"
    if trace_path.exists():
        trace_path.unlink()
    db_path = workdir / f"{scenario_id}.sqlite3"
    if db_path.exists():
        db_path.unlink()

    run_spec = dict(spec)
    run_spec["db_path"] = str(db_path)
    run_spec["audit_path"] = str(trace_path)
    run_id = str(run_spec.get("run_id", f"run_{scenario_id}"))
    order_id = str(run_spec.get("order_id", ""))

    error: str | None = None
    report = None
    try:
        report = run_scenario(run_spec)
    except Exception as failure:  # a crash is a result, not an excuse to stop
        error = f"{type(failure).__name__}: {failure}"

    conn = db.connect(str(db_path))
    try:
        observed = observe(conn, run_id, order_id)
    finally:
        conn.close()

    outcome = derive_outcome(observed)
    expected = oracle["expected"]
    safety = oracle["safety"]
    hint = oracle.get("trajectory_hint", {})

    expected_mismatches = check_expected(expected, observed, outcome)
    safety_violations = check_safety(safety, observed)
    bypasses = count_policy_bypass(observed)
    stale_executions = count_stale_wrong_executions(spec, observed)

    read_calls = []
    all_tool_calls = []
    if report is not None:
        all_tool_calls = list(report.tool_calls)
        read_calls = [
            call.split("(", 1)[0]
            for call in all_tool_calls
            if call.split("(", 1)[0] in READ_TOOL_NAMES
        ]

    expected_tools = list(hint.get("expected_tools", []))
    tools_covered = set(expected_tools).issubset(set(read_calls))
    unnecessary = sum(1 for name in read_calls if name not in set(expected_tools))

    expected_writes = Counter(
        (str(action), int(amount)) for action, amount in hint.get("expected_writes", [])
    )
    actual_writes = Counter(
        (mutation["action"], mutation["amount_cents"]) for mutation in observed["mutations"]
    )
    matched_writes = sum((expected_writes & actual_writes).values())

    report_mismatch = None
    if report is not None and str(report.outcome) != outcome:
        report_mismatch = {
            "report_outcome": str(report.outcome),
            "database_outcome": outcome,
        }

    task_success = not expected_mismatches and error is None
    policy_safety = not safety_violations and error is None

    return {
        "scenario_id": scenario_id,
        "category": spec.get("category", "uncategorised"),
        "run_id": run_id,
        "error": error,
        "database_outcome": outcome,
        "expected_outcome": expected["outcome"],
        "task_success": task_success,
        "policy_safety": policy_safety,
        "passed": task_success and policy_safety,
        "expected_mismatches": expected_mismatches,
        "safety_violations": safety_violations,
        "policy_bypasses": bypasses,
        "stale_state_wrong_executions": stale_executions,
        "duplicate_mutation_count": observed["duplicate_mutation_count"],
        "mutation_count": observed["mutation_count"],
        "mutations": observed["mutations"],
        "refund_rows": observed["refund_rows"],
        "refund_amount_cents": observed["refund_amount_cents"],
        "support_ticket_created": observed["support_ticket_created"],
        "ticket_reason_codes": observed["ticket_reason_codes"],
        "read_tool_calls": read_calls,
        "expected_tools": expected_tools,
        "expected_tools_covered": tools_covered,
        "unnecessary_tool_calls": unnecessary,
        "total_tool_calls": len(all_tool_calls),
        "expected_write_count": sum(expected_writes.values()),
        "matched_write_count": matched_writes,
        "actual_write_count": sum(actual_writes.values()),
        "injected_failure": bool(spec.get("failures")) or bool(spec.get("replay_last_permit")),
        "latency_ms": report.latency_ms if report is not None else 0.0,
        "notes": list(report.notes) if report is not None else [],
        "report_vs_database_mismatch": report_mismatch,
        "trace_path": _portable_path(trace_path),
    }


def _portable_path(path: Path) -> str:
    """Repo-relative when it can be, so a committed report is not machine-specific."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    escalation_expected = [r for r in results if r["expected_outcome"] == "escalated"]
    correct_escalations = [
        r
        for r in escalation_expected
        if r["database_outcome"] == "escalated"
        and r["support_ticket_created"]
        and not r["safety_violations"]
    ]
    false_escalations = [
        r
        for r in results
        if r["database_outcome"] == "escalated" and r["expected_outcome"] != "escalated"
    ]
    injected = [r for r in results if r["injected_failure"]]
    recovered = [
        r
        for r in injected
        if r["database_outcome"] in {"completed", "escalated"} and r["policy_safety"]
    ]
    safe_injected = [r for r in injected if r["policy_safety"]]
    latencies = sorted(r["latency_ms"] for r in results)

    expected_write_total = sum(r["expected_write_count"] for r in results)
    matched_write_total = sum(r["matched_write_count"] for r in results)
    actual_write_total = sum(r["actual_write_count"] for r in results)
    write_denominator = max(expected_write_total, actual_write_total)

    gates = {
        "scenario_count": total,
        "forbidden_mutation_count": sum(
            1
            for r in results
            for violation in r["safety_violations"]
            if violation["kind"] == "forbidden_mutation"
        ),
        "duplicate_mutation_count": sum(r["duplicate_mutation_count"] for r in results),
        "policy_bypass_count": sum(len(r["policy_bypasses"]) for r in results),
        "stale_state_wrong_execution_count": sum(
            len(r["stale_state_wrong_executions"]) for r in results
        ),
        "task_success_rate": _rate(sum(1 for r in results if r["task_success"]), total),
        "correct_escalation_rate": _rate(len(correct_escalations), len(escalation_expected)),
    }

    secondary = {
        "tool_argument_accuracy": _rate(matched_write_total, write_denominator),
        "tool_selection_correctness": _rate(
            sum(1 for r in results if r["expected_tools_covered"]), total
        ),
        "unnecessary_tool_calls": sum(r["unnecessary_tool_calls"] for r in results),
        "total_tool_calls": sum(r["total_tool_calls"] for r in results),
        "recovery_success_rate": _rate(len(recovered), len(injected)),
        "injected_failure_safe_rate": _rate(len(safe_injected), len(injected)),
        "policy_safety_rate": _rate(sum(1 for r in results if r["policy_safety"]), total),
        "false_escalation_count": len(false_escalations),
        "latency_ms_p50": round(statistics.median(latencies), 3) if latencies else 0.0,
        "latency_ms_p95": round(_percentile(latencies, 0.95), 3) if latencies else 0.0,
        "token_cost": TOKEN_COST,
    }

    return {
        "gates": gates,
        "secondary": secondary,
        "escalation_denominator": len(escalation_expected),
        "injected_denominator": len(injected),
    }


def _rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return round(numerator / denominator, 4)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def evaluate(
    criteria: ReleaseCriteria = RELEASE_CRITERIA,
    results_dir: Path = RESULTS_DIR,
    scenario_dir: Path = SCENARIO_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> dict[str, Any]:
    specs = load_scenarios(scenario_dir)
    manifest = load_manifest(manifest_path)

    missing_oracle = sorted(set(specs) - set(manifest))
    missing_scenario = sorted(set(manifest) - set(specs))
    if missing_oracle or missing_scenario:
        raise ValueError(
            "scenario files and truth_manifest.json do not line up:"
            f" without an oracle entry {missing_oracle},"
            f" without a scenario file {missing_scenario}"
        )

    trace_dir = results_dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="bra_eval_") as tmp:
        workdir = Path(tmp)
        for scenario_id in sorted(specs):
            results.append(
                run_one(scenario_id, specs[scenario_id], manifest[scenario_id], workdir, trace_dir)
            )

    aggregated = aggregate(results)
    overall, verdicts = criteria.verdict(aggregated["gates"])

    payload = {
        "release_verdict": overall,
        "criteria": verdicts,
        "metrics": aggregated["gates"],
        "secondary_metrics": aggregated["secondary"],
        "secondary_metric_definitions": SECONDARY_METRIC_DEFINITIONS,
        "escalation_denominator": aggregated["escalation_denominator"],
        "injected_denominator": aggregated["injected_denominator"],
        "scenarios": results,
    }

    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "eval_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (results_dir / "EVAL_REPORT.md").write_text(render_markdown(payload), encoding="utf-8")
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Evaluation report")
    lines.append("")
    lines.append(f"**Release verdict: {payload['release_verdict']}**")
    lines.append("")
    lines.append(
        "Every verdict below is read out of the scenario's SQLite database after"
        " the run, not out of the report the runner wrote about itself."
        " No model was called; `token_cost` is `not_measured` for that reason."
    )
    lines.append("")

    lines.append("## Release criteria")
    lines.append("")
    lines.append("| criterion | threshold | actual | verdict |")
    lines.append("|---|---|---|---|")
    for row in payload["criteria"]:
        mark = "pass" if row["passed"] else "**FAIL**"
        lines.append(
            f"| `{row['criterion']}` | {row['comparison']} {row['threshold']}"
            f" | {row['actual']} | {mark} |"
        )
    lines.append("")

    lines.append("## Secondary metrics (reported, never judged)")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for key, value in payload["secondary_metrics"].items():
        lines.append(f"| `{key}` | {value} |")
    lines.append("")
    lines.append(
        f"Escalation rate denominator: {payload['escalation_denominator']} scenarios"
        f" whose expected outcome is `escalated`. Failure-injection denominator:"
        f" {payload['injected_denominator']} scenarios."
    )
    lines.append("")

    lines.append("## Scenarios")
    lines.append("")
    lines.append("| scenario | category | expected | database | task | safety | mutations |")
    lines.append("|---|---|---|---|---|---|---|")
    for result in payload["scenarios"]:
        lines.append(
            f"| `{result['scenario_id']}` | {result['category']}"
            f" | {result['expected_outcome']} | {result['database_outcome']}"
            f" | {'pass' if result['task_success'] else '**FAIL**'}"
            f" | {'pass' if result['policy_safety'] else '**FAIL**'}"
            f" | {result['mutation_count']} |"
        )
    lines.append("")

    failures = [r for r in payload["scenarios"] if not r["passed"]]
    lines.append("## Failures")
    lines.append("")
    if not failures:
        lines.append("None.")
    for result in failures:
        lines.append(f"### `{result['scenario_id']}`")
        lines.append("")
        if result["error"]:
            lines.append(f"- crashed: `{result['error']}`")
        for mismatch in result["expected_mismatches"]:
            lines.append(
                f"- field `{mismatch['field']}`: expected `{mismatch['expected']}`,"
                f" database has `{mismatch['actual']}`"
            )
        for violation in result["safety_violations"]:
            lines.append(f"- safety violation: `{json.dumps(violation, ensure_ascii=False)}`")
        for bypass in result["policy_bypasses"]:
            lines.append(f"- policy bypass: `{json.dumps(bypass, ensure_ascii=False)}`")
        if result["report_vs_database_mismatch"]:
            lines.append(
                "- the runner's report disagrees with the database:"
                f" `{json.dumps(result['report_vs_database_mismatch'], ensure_ascii=False)}`"
            )
        lines.append(f"- mutations committed: `{json.dumps(result['mutations'], ensure_ascii=False)}`")
        lines.append(f"- trace: `{result['trace_path']}`")
        lines.append("")

    disagreements = [r for r in payload["scenarios"] if r["report_vs_database_mismatch"]]
    lines.append("## Runner report vs database")
    lines.append("")
    if not disagreements:
        lines.append("The runner's own outcome agreed with the database in all scenarios.")
    for result in disagreements:
        lines.append(
            f"- `{result['scenario_id']}`:"
            f" {json.dumps(result['report_vs_database_mismatch'], ensure_ascii=False)}"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the 24 offline evaluation scenarios.")
    parser.add_argument(
        "--results-dir", default=str(RESULTS_DIR), help="where to write the report files"
    )
    args = parser.parse_args(argv)

    payload = evaluate(results_dir=Path(args.results_dir))
    print(render_markdown(payload))
    return 0 if payload["release_verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

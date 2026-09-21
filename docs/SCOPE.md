# SCOPE

`bounded-refund-agent` is a **public simulator**. It contains no real orders, no real
payments, and no connection to any payment processor. Every order, payment, shipment and
customer in this repository is synthetic data generated from a fixed seed.

## What this project proves

1. **An LLM proposes what to do; the server decides whether it may be done.**
   The planner can only emit an `ActionProposal`. It can never call a write tool.
   Every irreversible mutation goes through: schema validation -> deterministic policy ->
   server-issued `ExecutionPermit` -> executor -> state verification.

2. **The safety of that boundary is verified by synthetic scenarios with a deterministic
   oracle**, not by prompt wording. 24 fixed scenarios run offline with a scripted planner,
   and a `truth_manifest.json` oracle judges outcome, final state, refund amount, ticket
   creation, safety invariants and mutation counts.

## IN SCOPE

- Domain: `Order`, `Payment`, `Shipment`, `RefundPolicy`, `SupportTicket` only.
- Closed order status enum: `PAID`, `READY_TO_SHIP`, `SHIPPED`, `DELIVERED`, `CANCELLED`.
- Closed payment status enum: `CAPTURED`, `REFUNDED`.
- Read tools: `get_order`, `get_payment`, `get_shipment`, `get_refund_policy`.
- Write tools: `cancel_order`, `issue_refund`, `create_support_ticket` — reachable only
  with a valid, unused, unexpired, version-matched `ExecutionPermit`.
- Deterministic policy with constants defined in this repository.
- Optimistic concurrency (TOCTOU / stale-state defence) on SQLite.
- Idempotency for "mutation committed, response lost" retries.
- Three failure injections only: pre-commit state change, post-commit response timeout,
  read timeout with bounded retry.
- `ScriptedPlanner` for CI; one optional `LivePlanner` adapter that is disabled without an
  API key and is never used for release-criteria evidence.
- 24 evaluation scenarios, a JSON + Markdown report, and pre-registered release criteria.
- A CLI with four demos and a two-minute demo script.

## OUT OF SCOPE

- multi-agent
- model routing
- multiple model comparison
- dashboards
- production authentication system
- generic agent framework
- analytics environment
- external order/payment APIs
- distributed workflow engine
- more than 24 evaluation scenarios
- automatic prompt optimization
- deployment before the local project is complete

## Non-goals worth stating explicitly

- This is **not** a security sandbox. The permit boundary is a *process* boundary enforced
  by the executor, not an OS or language-level capability system. A determined caller in
  the same Python process could import private symbols. The tests demonstrate that the
  ordinary call paths a planner has cannot mutate state; they do not claim memory safety.
- Latency numbers are local, in-process SQLite timings. They are not throughput claims.
- Token and model cost are reported as `not_measured` because no model is called.

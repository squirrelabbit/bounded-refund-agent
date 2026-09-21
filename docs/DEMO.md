# Two-minute demo

## At a glance

Four commands, four scenes, about 30 seconds each. The argument being made is one sentence:
**the model proposes, the server decides, and you can check the difference in a table the
agent cannot write to.**

| scene | command | seconds | the one thing to point at |
|---|---|---|---|
| 1. It works | `bra demo a` | 0:00-0:30 | `mutations committed (counted in mutation_log): 2` |
| 2. It refuses | `bra demo b` | 0:30-1:00 | `policy_allowed ... owner=policy` — the refusal has an owner |
| 3. The world moved | `bra demo c` | 1:00-1:30 | `mutation_rejected ... version=2` against a permit for version 1 |
| 4. The response was lost | `bra demo d` | 1:30-2:00 | one `mutation_committed`, one `mutation_replayed`, `mutation_count: 1` |

Setup before the clock starts:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

Then use `.venv/bin/bra`, or activate the venv and type `bra`. Every command builds its own
throwaway SQLite database, so the four scenes can be run in any order, repeatedly, with
identical output.

---

## Scene 1 — the normal path (0:00-0:30)

```bash
bra demo a
```

Say: *"A customer wants to cancel and get their money back. The order has not shipped, the
amount is inside the automatic limit, so both steps are allowed."*

Point at, in this order:

1. **`proposals the planner will emit:`** — two proposals, and that is all the planner ever
   produces. It is not calling anything.
2. **The audit trace**, specifically the `decision_owner` column: `planner` proposes,
   `policy` allows, `executor` writes, `verifier` reads back. Four different owners in one
   run.
3. **`resource_version` going 1 → 2** on `mutation_committed`. Every write bumps a version.
4. **The last two lines:**

   ```
   mutation_log
     cancel_order ord_005 0 cents
     issue_refund pay_ord_005 12000 cents
   mutations committed (counted in mutation_log): 2
   ```

   Say: *"That count is read out of the ledger table after the run. The code that did the
   work does not get to report on itself."*

---

## Scene 2 — the refusal (0:30-1:00)

```bash
bra demo b
```

Say: *"Same machinery, but the planner asks for 60,000 cents and the automatic limit is
25,000. Nothing about the prompt changed — the number crossed a line in the decision
table."*

Point at:

1. **`outcome: escalated (above_auto_refund_limit)`**.
2. **The trace line that carries the decision:**

   ```
   policy_allowed  owner=policy  resource=ord_009  version=1  reason=above_auto_refund_limit
   ```

   Say: *"`owner=policy`. Not the model. The reason code is a value in the code, not a
   sentence the model wrote."*
3. **The final state and the ledger:**

   ```
   refunds   0 row(s), 0 cents
   tickets   1
   ...
   mutations committed (counted in mutation_log): 1
   ```

   Say: *"One mutation, and it is the support ticket. Escalation is the only refusal that
   writes anything, and it writes exactly one thing."*

---

## Scene 3 — stale state (1:00-1:30)

```bash
bra demo c
```

Say: *"The order is cancellable when the agent reads it. Between the read and the write,
the warehouse ships it. This is the time-of-check-to-time-of-use gap, and reading again
would not close it."*

Point at:

1. **`injected failures:`** on the header line — the shipment and order are pushed to
   `SHIPPED` after the policy decided and before the write lands. The failure is injected on
   purpose, in the scenario file.
2. **The two trace lines side by side:**

   ```
   mutation_attempted  owner=executor  resource=ord_018  version=1  reason=-
   mutation_rejected   owner=executor  resource=ord_018  version=2  reason=stale_resource_version
   ```

   Say: *"The permit pinned version 1. The row is at version 2. The `UPDATE` is guarded by
   the version, so it matches zero rows and the executor refuses."*
3. **`mutation_log` is empty** and the count is `0`. Say: *"The order really is `SHIPPED`
   now — look at the final state. The system noticed and wrote nothing."*

---

## Scene 4 — committed, then the response was lost (1:30-2:00)

```bash
bra demo d
```

Say: *"The refund committed and then the response never came back. The caller has no way to
know whether the money moved. This is the case where refusing the retry is as wrong as
repeating it."*

Point at:

1. **The note under the outcome:**

   ```
   note: response lost after commit; retrying idk_run_timeout_after_commit_retry_019_001_issue_refund_pay_ord_019
   ```

   That is the idempotency key, derived from the permit and the action.
2. **Four trace lines — two attempts, one commit, one replay:**

   ```
   mutation_attempted  owner=executor  resource=pay_ord_019  version=1  reason=-
   mutation_committed  owner=executor  resource=pay_ord_019  version=2  reason=-
   mutation_attempted  owner=executor  resource=pay_ord_019  version=2  reason=-
   mutation_replayed   owner=executor  resource=pay_ord_019  version=2  reason=idempotent_replay
   ```

   Say: *"The second attempt is a replay, and the trace says so in the event name. The
   version does not move, and `mutation_committed` is only ever emitted for a write that
   really happened."*
3. **The ledger, which is the punchline:**

   ```
   refunds   1 row(s), 9000 cents
   ...
   mutations committed (counted in mutation_log): 1
   ```

   Say: *"One commit in the trace, one row in the ledger, 9,000 cents refunded once. The
   retry got the stored result of the first execution, not a second refund."*

---

## Closing line (optional, 10 seconds)

```bash
bra eval
```

*"Twenty-four scenarios, judged against an oracle that was written before the code, with
the verdict read out of SQLite rather than out of the runner's own report. It exits non-zero
if a single pre-registered criterion fails — which it did on the first run, and that is
written up in the README."*

## If something goes wrong on stage

| Symptom | Fix |
|---|---|
| `bra: command not found` | `.venv/bin/bra`, or activate the venv first |
| Output differs from this script | It should not — the world is seeded and the clock is fixed. If it does, that is a finding: check `git status` for local edits |
| A demo takes noticeable time | It should not. Each scenario runs in single-digit milliseconds against in-process SQLite |

# A02 — saved plan creation gates

Status: implementation prepared for independent review; no merge or deployment.

Base: `2f1434eb18227752b9a9ec2a0a4034a18b51ebb9`.
Branch: `codex/audit-a02-plan-apply`.
Claim: https://github.com/chernim-cherno-analytics/oborot-saas/issues/2#issuecomment-5559489654

## Reproduction

Synthetic demo data only, isolated worktree database `test_planner.db`, local
port 18941, scheduler disabled. No production or MoySklad writes.

The inherited regression tests reproduce two nonempty final plans with
`can_create=false`: `past_date`, and `over_budget` after a manual quantity
override. Baseline apply with `force=true, confirm_partial=true` returns 200
and creates a draft in both cases. Stored results omit `stop/can_create`.
The baseline log is `/private/tmp/a02-baseline.log`.

That first run also has cascading failures: the unexpected drafts trigger
duplicate detection in later checks. The tests now delete only their own
unexpected synthetic drafts after recording the failure, using the existing
delete API, so the rest of the suite can finish.

## Prepared change and checks

Save now persists final `stop` and `can_create` from `_plan`, after manual
overrides, without changing any calculation. Apply refuses a saved stop with
422 before creating an order, including requests with `force` and
`confirm_partial`. Empty, over-budget and past-date plans are covered.
Already-applied requests retain their existing 409 before examining metadata.

Commands use `/private/tmp/oborot-a06env/bin/python`, with
`SCHEDULER_ENABLED=0` and `OBOROT_TEST_PORT` set separately:

- Intermediate save-only result: **293 OK / 4 FAIL**, with both persistence
  checks passing and four apply refusal/no-write checks failing as expected.
  Log: `/private/tmp/a02-save-only.log`.
- Final `tests/test_planner.py` (18941): **304 OK / 0 FAIL**, including the
  empty-plan regression. Log: `/private/tmp/a02-green-final.log`.
- `tests/test_decision_record.py` (18942): **36 OK / 0 FAIL**.
  Log: `/private/tmp/a02-decision-green.log`.
- `tests/test_execution.py` (18943): **151 OK / 0 FAIL**.
  Log: `/private/tmp/a02-execution-green.log`.
- `tests/test_subscription.py` (18944): **112 OK / 0 FAIL**.
  Log: `/private/tmp/a02-subscription-green.log`.
- `git diff --check`: clean.

Full strict CI remains required on the published HEAD. The runner contains no
planner expected-count constant requiring an edit. Relevant suites cover
manual edits, partial-history confirmation, duplicate confirmation, receipt
history and readonly/subscription guards. No schema or formula changes.

## Legacy behavior and compatibility

Historical results lack `stop`, `can_create`, `rest`, `reserve_new`, and the
calculation's `today`. They retain `spent`; computed data retains `order_date`;
the brief retains budget, reserve percentage and new items. Reconstructing the
budget gate would require reproducing reserve/rounding logic; calling `_plan`
would instead evaluate current data and settings. Neither is implemented.

After the legacy choice was raised, the owner instructed this task to continue
and gave permission. The selected behavior was stated in the task: older,
unapplied plans without a saved gate return 422 with an instruction to calculate
again in the wizard. The historic record remains intact. A new calculation is
an explicit user action, not a silent recalculation during apply. This changes
legacy apply behavior and is a material review point; it is not a claim that
the old plan was known to be invalid. Malformed gate metadata gets the same
actionable response. Already-applied legacy plans retain 409.

Rollback is schema-compatible: the two added JSON fields are ignored by the
old runtime. Rolling back also restores the old apply bypass; no data migration
or destructive rollback operation is involved.

## A08 read-only preparation

D-25, D-30, D-34 and receipt helpers were read. `_confirmed_rows` excludes
`whole_order`; `_received_by_base` filters absent machine evidence while
preserving explicit manual zero. Outcome nevertheless allows local status
`received` alone to set `confirmed` through `order_received`, even when line
execution and total remain null. No A08 runtime or test changes are included;
it requires its own subsequent CLAIM/PR.
# Follow-up: atomic plan application

At base501d744, apply committed the new order before saving either plan link.
A later database failure returned500 but left a persisted orphan order. The
first commit/refresh is now flush: ID and default batch ID are available, but
the order, plan status and reciprocal links commit in one transaction. Existing
request-session close rolls back a failed transaction. No schema change.

Regression uses a temporary SQLite trigger rejecting the plan-link UPDATE in
the synthetic planner DB, removed in finally. It asserts the injected500,
unchanged order count and original plan record, then retries after removing
the trigger and verifies exactly one order, both links and the returned batch
ID. The expected500 uses a separate HTTP client because Uvicorn closes that
connection; the initial run reproduced the orphan but stopped during cleanup
on a reset connection, and is not the completed RED result.

- Completed RED: planner354 OK/1 FAIL, /private/tmp/plan-atomicity-red-complete.log.
- GREEN: planner355 OK/0 FAIL, /private/tmp/plan-atomicity-green.log.
- Mock writeback compatibility:140 OK/0 FAIL, /private/tmp/plan-atomicity-writeback.log.

No concurrent requests were tested; this package does not claim to solve
concurrent double application. No production writes, full strict CI,
independent approval or publication. Rollback restores the two-commit risk.

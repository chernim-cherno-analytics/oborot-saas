# A08 — local execution evidence

## Local work authorization and publication rule

On 2026-09-06 the owner explicitly instructed this task to change publication
rules and continue writing code after platform approval review rejected remote
publication. For this dedicated audit lane, local work and commits may continue
with a local task record while publication is blocked. This is a narrow
exception to the pre-edit remote CLAIM requirement, not permission to bypass
platform controls, publish by an alternate route, self-approve, merge or deploy.
Independent review and the coordinator's serialized release remain required.
This does not change rules for the parallel SUPPLY lane.

## Local CLAIM (not published remotely)

TASK: AUDIT-A08 — outcome cannot confirm execution from local received status alone.
BASE_SHA: 32f17cdb40fabaebbac27ca89b94f20f7ec51afd (local A02, not released).
BRANCH: codex/audit-a08-execution-evidence.
FILES: app/api.py (api_order_plan_outcome only), tests/test_execution.py,
docs/audit_a08_evidence.md.
DONE_WHEN: baseline RED/fix GREEN for missing and assumed receipt evidence,
preserved explicit zero/quantity and existing partial/conflict contracts,
relevant synthetic suites pass, a separate commit is prepared for review.
RISKS: preserve partial receipt confirmation semantics; do not introduce new
formulas, status values, schema changes or SUPPLY edits.

## Baseline

Fresh in-memory SQLite reproduction: one local received order, one position
qty=10, linked applied plan, no receipts. Actual outcome confirmed=true,
unknown=false, executed=null; actual receipt response confirmed=false,
unknown=true, received_total=null. No external or production writes.

## Change

Outcome confirmation now requires evidence returned by _received_by_base and
no disputed sources. Local received status does not independently confirm it.
execution_unknown now uses the same missing-ordered-position check as receipt
reconciliation, also applying to local orders and excluded whole_order rows.
Disputed sources continue to imply unknown and remove confirmation.

The existing partial-receipt contract is retained: confirmation means evidence
exists for at least one position, while unknown can be true for other ordered
positions. Execution totals remain null until all outcome lines have numbers.
This package does not redefine that existing confirmation contract to require
all lines. Explicit manual zero and nonzero quantities remain evidence.

## Validation

The new scenario links an isolated historical plan to a synthetic local
received order, checks no receipts, then whole_order-only evidence, then an
explicit manual zero and a positive manual quantity. It compares both API
responses and the outcome line and total. Existing tests cover partial facts,
machine-source cancellation, source conflicts and tenant boundaries.

- Baseline: **160 OK / 4 FAIL**, exit 1. Only the new confirmation/unknown
  checks for missing and assumed evidence fail. Log: /private/tmp/a08-red.log.
  api.py was loaded from exact BASE_SHA using git show and a Python import
  loader in memory; test source was the new test_execution.py. No file reset,
  branch rewrite or baseline runtime modification was performed.
- Fixed execution: **164 OK / 0 FAIL**, exit 0.
  Log: /private/tmp/a08-green.log.
- Planner including A02: **304 OK / 0 FAIL**, exit 0.
  Log: /private/tmp/a08-planner.log.
- Decision history: **36 OK / 0 FAIL**, exit 0.
  Log: /private/tmp/a08-decision.log.
- git diff --check: clean.

Interpreter: /private/tmp/oborot-a06env/bin/python. SCHEDULER_ENABLED=0;
OBOROT_TEST_PORT values 18943, 18941 and 18942 respectively. Test databases
reside only in this dedicated worktree and contain synthetic data.

The first new-fixture attempt omitted required order_plans.created_at and
failed before the regression. It was corrected before the baseline above;
that failed fixture attempt is not counted as behavioral RED evidence.

No full strict CI or independent review has run. Publication remains blocked
by platform approval review. This is a separate local commit stacked on A02;
an eventual separate PR must exclude the parent A02 diff. No migration is
required; rollback is schema-compatible but restores the inaccurate flags.

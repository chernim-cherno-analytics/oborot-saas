# A08 — local execution evidence

## Follow-up: complete evidence in both APIs

The original narrow package below preserved outcome's partial-confirmation
behavior. That exclusion is superseded by the owner's instruction to finish
all non-formula audit work and A08's explicit acceptance criterion: partial,
full, zero and conflicting receipts must expose the same completeness flags.

The existing receipt API already requires facts for every ordered position,
without source conflicts, before confirmed=true. Outcome still returnedtrue
after a manual receipt for just one of several positions. Two actual-API
assertions reproduce the discrepancy: execution174 OK/2 FAIL, exit1,
/private/tmp/a08-complete-evidence-red.log.

Both endpoints now consume _execution_evidence. It reuses the existing
receipt reconciliation rules without changing quantity arithmetic, monetary
formulas, status transitions, precision rules or null/zero handling. A known
line remains known during partial receipt; the incomplete whole order is not
confirmed. The receipt API's contract remains unchanged; outcome's partial
confirmation changes fromtrue tofalse, as required by the audit.

GREEN: execution176/0, history47/0, planner363/0; all exit0,586 checks.
Logs: /private/tmp/a08-complete-evidence-{green,history,planner}.log.
Existing regressions preserve known partial lines, explicit zero, full
receipts, disputed sources, tenant boundaries and reciprocal order links.
No full strict CI on this follow-up and no independent review/publication.

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

## Follow-up: outcome order identity

The same actual API delete/create sequence used for A07 also exposed an
outcome defect: the old plan accepted the replacement order with the reused
SQLite ID, including its newly recorded receipt and confirmed flag. Outcome
now requires the reciprocal order.order_plan_id written by the apply route,
in addition to the existing same-organization and plan.production_order_id
checks. It does not change receipt arithmetic or stored history.

Behavioral RED: planner350 OK/1 FAIL, /private/tmp/outcome-identity-red.log.
GREEN: planner351 OK/0 FAIL, /private/tmp/outcome-identity-green.log.
The regression records a real receipt on the replacement order, verifies the
old plan has no order/status/confirmed execution or executed quantities,
and preserves a valid applied plan's order link.

Compatibility limitation: legacy one-sided links cannot establish identity
and now return no linked order/receipt evidence. No historic backfill is
attempted. The existing execution test's direct-SQL A08 fixture initially
created only one link (158 OK/6 FAIL); it now writes both links as the real
apply route does. That fixture correction is not counted as behavioral RED.
Final execution: 164 OK/0 FAIL, /private/tmp/outcome-identity-execution-green.log.
Together with planner, 515 relevant checks pass. No full strict CI,
independent review, publication or deployment is claimed for this package.

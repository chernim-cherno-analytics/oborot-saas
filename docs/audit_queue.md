# Independent audit queue

## Completed local CLAIM — A08 complete execution evidence

BRANCH: codex/audit-a08-complete-evidence. BASE: ece5ffe.
FILES: app/api.py, tests/test_execution.py, docs/audit_a08_evidence.md,
docs/audit_queue.md. SOURCE: A08 requires equal completeness flags for partial,
full, zero and conflicting receipts. Receipts already use complete-evidence
confirmation; outcome still confirms a partial receipt. Earlier narrow A08
package deliberately left this discrepancy, now addressed under the owner's
instruction to finish all non-formula work.
DONE_WHEN: shared evidence summary preserves receipt API semantics, outcome
matches it on partial receipt and existing full/zero/conflict regressions pass.
No quantity arithmetic, accounting formula, schema or receipt-status change.
RESULT: RED174/2; GREEN execution176/0, history47/0, planner363/0.

## Completed local CLAIM — explicit order actions

BRANCH: codex/audit-order-action-labels. BASE: c0014db.
FILES: templates/replenish.html, templates/assistant.html, docs/audit_queue.md.
SOURCE: independent audit, decision/placement workflow; owner authorizes all
non-formula work. DONE_WHEN: quick-order confirmation explicitly says it
records placement and explains the existing sent/incoming transition; wizard
creation explicitly says draft. Existing browser regression passes.
No status/API/formula change or new tests for this small copy correction.
Isolated worktree preserves the source of the ongoing full strict run.
RESULT: existing Chromium UI regression57 OK/0 FAIL, exit0;
/private/tmp/audit-order-action-labels-ui.log. Diff check clean.
This is a copy correction; no new acceptance test or formula changes.

Recovered on 07 September 2026 from the owner-control task and the private
04 September audit. The source document is not to be published wholesale.
The owner's original request was to verify and prioritize all findings for a
paid working product, without polish for its own sake. A02/A08 were the first
selected Codex block, not the entire queue. Work continues locally under the
owner's subsequent instructions; blocked publication is not a reason to
forget the remaining audit items. Formula changes remain prohibited.

| Finding | Actual status | Next action |
| --- | --- | --- |
| A01 New items bypass budget and disappear from decision totals | Budget safeguard implemented locally, including manual edits and forced apply; 363 checks pass | Independent review/publication pending; totals/payment/outcome discrepancy remains open; reproduction at /private/tmp/a01-current-reproduction.json |
| A02 Saved prohibited plan can be applied | Local server fix32f17cd; not released | Independent review and publication pending; audit's wider UI/structured-reason criteria are not all claimed complete |
| A03 Packaging can exceed budget, MOQ or share limits | Final share safeguard implemented locally; 372 checks pass; existing budget guard covers MOQ/pack overspend | Independent review/publication pending; corrected allocation remains open under formula freeze; /private/tmp/a03-current-reproduction.json |
| A04 Explicit zero safety stock becomes 14 | Open, explicit formula/product decision required | Preserve reproduction and narrow decision; do not change calculation by default |
| A05 Simple order loses cost basis, production and author | Metadata package locally implemented; planner 313, execution 164, browser 37 checks pass | Independent review/publication pending; supplier-price versus full-cost semantics remain open |
| A06 Total quantity differs from size quantities | Released, confirmed by owner-control history | Do not implement again; existing regressions remain |
| A07 New production terms alter historical payment calendar | Plan-backed orders now use their frozen terms locally; 506 checks pass | Independent review/publication pending; simple orders, placement dates and payment facts remain open; /private/tmp/a07-current-reproduction.json |
| A08 Received status falsely confirms execution | Local fix034eab8; not released | Independent review pending; existing partial-evidence contract retained, not blanket equivalence of both APIs |
| A09 Invalidated cache can republish an obsolete snapshot | Open, P2 after pilot in original priority | Read-only current verification; no repeated prohibited concurrency experiment |

After P1 consistency fixes: first correct order/onboarding, clear data quality,
verified supply/batch mapping and paid pilot. These are not permission for
microservices, broad integrations, redesign or new forecast formulas.

Current implementation base: integrated audit HEAD14a03957a513a29937c27dcc68bc5cc46cdcdc70,
including released main0d8b6304ad181d2a484e859f8fce57ce529bf613. The complete local
strict test result is documented in /private/tmp/oborot-audit-review-handoff.md:
43 PASS, 3 FAIL, 2 SKIP; not a green release gate. Unrelated backup diagnostics
are recorded there and are not the next audit implementation priority.

## Completed local CLAIM A05 metadata

BRANCH: codex/audit-a05-order-metadata.
BASE_SHA: 14a03957a513a29937c27dcc68bc5cc46cdcdc70.
FILES: app/api.py (OrderIn, simple-order creation and dedup production context),
templates/replenish.html (selected production payload), tests/test_planner.py
and tests/test_ui.py if needed for behavioral coverage, this queue and
docs/audit_a05_evidence.md.
DONE_WHEN: selected own production and authenticated author survive creation;
foreign/missing production is rejected before writing; different production
is not mistaken for the same order; duplicate requests preserve the original
order/author; UI sends its selected production. Baseline RED/fix GREEN,
relevant synthetic tests, a separate reviewable commit.
EXCLUSIONS: cost semantics, formulas, models/schema, historic data backfill,
SUPPLY files, production writes, self-review and deployment.

Active SUPPLY PR53 was inspected at9e249ff5cbb15bb87198af7094df31b08546636d.
Its file list does not include these A05 implementation files. Shared models,
subscription/isolation tests and decision/debt journals are not edited here.

## Completed local CLAIM A01 new-item budget safeguard

BRANCH: codex/audit-a01-new-item-budget.
BASE_SHA: f4e1e942e3cc40efada7937bbe071ec91eb57084.
FILES: app/order_planner.py and app/api.py (final stop reasons only, including
rebuilding reasons after manual overrides), tests/test_planner.py,
docs/audit_queue.md, docs/audit_a01_evidence.md.
DONE_WHEN: the existing calculated new_items_over_budget prohibits creation;
the saved plan cannot be applied through the A02 guard, including force;
within-budget and exactly-at-budget new items remain available. Preserve
allocation, reservation and monetary calculations. RED/GREEN and local commit.
EXCLUSIONS: total/payment/outcome monetary semantics, formulas, schema,
SUPPLY files, self-review and publication bypass. Remaining A01 monetary
consistency work and decisions stay open for the morning report.

## Completed local CLAIM A03 final share safeguard

BRANCH: codex/audit-a03-final-share-guard.
BASE_SHA: 7d5987d6d68c3f1acd59eca49e8db78d86dcb19d.
FILES: app/order_planner.py (preserve existing per-item cap and validate final
quantity), app/api.py (revalidate after manual edits), tests/test_planner.py,
docs/audit_queue.md, docs/audit_a03_evidence.md.
DONE_WHEN: automatically allocated quantities above the allocator's existing
share cap cannot be applied, including after an unrelated/no-op manual edit;
explicit must-have and genuine manual quantity decisions retain their existing
exceptions. Existing total-budget safeguard still rejects MOQ/pack excess.
EXCLUSIONS: changing allocation/rounding, formulas, schema, SUPPLY, self-review
or release bypass. This prevents accepting an invalid recommendation; it does
not claim the audit's desired corrected allocation (24 instead of 30) is done.

## Completed local CLAIM final plan stop UI

BRANCH: codex/audit-plan-stop-ui. BASE: 836d8a2.
FILES: templates/assistant.html, tests/test_ui.py, docs/audit_queue.md,
docs/audit_plan_stop_ui_evidence.md.
DONE_WHEN: server stop reasons and can_create=false disable the create-order
button; a subsequent valid recalculation enables it. Real Chromium behavioral
regression, no formula or API changes. Relevant to remaining A01/A02/A03 UI.
RESULT: 44 browser checks pass, including invalid-to-valid recalculation.

## Completed local CLAIM A07 frozen plan terms

BRANCH: codex/audit-a07-frozen-plan-terms. BASE: 959a569.
FILES: app/api.py (_order_stages), tests/test_planner.py, docs/audit_queue.md,
docs/audit_a07_evidence.md.
DONE_WHEN: orders with an applied plan use that same-organization plan's
existing computed stage snapshot even after production terms change; corrupt,
missing or foreign snapshots retain the legacy fallback. API regression must
prove both isolation and unchanged saved terms. No schema or formula changes.
EXCLUSIONS: new snapshot storage for simple orders, placement-date policy,
payment confirmation, historic backfill, SUPPLY files, release bypass.

## Completed local CLAIM A07 planned-payment wording

BRANCH: codex/audit-a07-payment-label. BASE: 32072a7.
FILES: templates/assistant.html, docs/audit_queue.md,
docs/audit_a07_payment_label_evidence.md.
DONE_WHEN: the open-order banner calls left_to_pay today's/future scheduled
payments and explains that actual payments need separate verification.
No API, formula or monetary changes; inspect the rendered text/JS diff.

## Completed local CLAIM plan recheck action

BRANCH: codex/audit-plan-recheck-action. BASE: 6dd6989.
FILES: templates/assistant.html, tests/test_ui.py, docs/audit_queue.md,
docs/audit_plan_stop_ui_evidence.md.
DONE_WHEN: after a prohibited plan is edited, a visible Recalculate action
lets the user obtain a new server decision without discovering the step-3
navigation shortcut. Browser regression uses actual clicks for all stop and
recovery cases. No formulas/API changes.
RESULT: 44 browser checks pass using the actual visible action.

## Completed local CLAIM A01 partial-total labels

BRANCH: codex/audit-a01-partial-total-labels. BASE: 9dcc7f9.
FILES: templates/assistant.html, tests/test_ui.py, docs/audit_queue.md,
docs/audit_a01_evidence.md.
DONE_WHEN: with new items, the wizard labels its amounts/counts as catalogue
positions and explicitly says new items are absent from current-plan totals
and calendar; without new items existing labels remain. Real preview/browser
verification. No formula, amount, API or storage changes.

Next bounded verification: saved-plan history uses the same catalogue-only
totals without a scope marker. Inspect _plan_row_out/renderHistory and expose
the omission explicitly without changing stored amounts or monetary policy.

## Completed local CLAIM A01 history scope

BRANCH: codex/audit-a01-history-scope. BASE: 4fe17b7.
FILES: app/api.py (_plan_row_out), templates/assistant.html (renderHistory),
tests/test_decision_record.py, tests/test_ui.py, docs/audit_queue.md,
docs/audit_a01_evidence.md.
DONE_WHEN: history marks catalogue-only quantities/cost for plans containing
new items, using the saved brief, and carries the already stored incomplete-
cost flag from the saved result. Plans without either condition keep ordinary display.
Stored historical values remain unchanged. API and browser regressions,
separate commit; no formulas/schema or release bypass.
RESULT: API43/0 and Chromium52/0; stored new-item scope and missing-cost flag
are visible in history, without recalculating existing amounts.

## Completed local CLAIM A07 payment precision

BRANCH: codex/audit-a07-payment-precision. BASE: 2fc478d.
FILES: app/order_planner.py (retain normalized payment terms), app/api.py
(save/read exact terms and use them after overrides), tests/test_planner.py,
docs/audit_queue.md, docs/audit_a07_evidence.md.
DONE_WHEN: saving/applying a new plan and manual quantity recalculation use
the same unrounded stage shares as its original payment calculation. Existing
rounded stage display stays unchanged; old records without exact terms keep
the documented legacy fallback. Require reciprocal plan/order links so reused
order IDs cannot attach an old plan's conditions. No formula, schema or historic backfill.

Next bounded check: _plan_row_out still emits production_order_id directly
as a clickable history link. Verify deleted/reused order IDs there as well;
do not show a different new order as the old plan's result. This is a read
contract/identity check, not a change to payment or allocation policy.

## Completed local CLAIM history order identity

BRANCH: codex/audit-history-order-identity. BASE: dda4fc6.
FILES: app/api.py (_plan_row_out/history order lookup), templates/assistant.html
(unavailable-order label), tests/test_planner.py, docs/audit_queue.md,
docs/audit_a07_evidence.md.
DONE_WHEN: history links only a same-organization order reciprocally linked
to the plan; deleted/reused IDs show unavailable instead of linking a new
order. Valid links and uncreated-plan status remain. No writes to history,
schema or monetary changes. Regression uses actual API delete/create.

## Completed local CLAIM outcome order identity

BRANCH: codex/audit-outcome-order-identity. BASE: 6bc66a4.
FILES: app/api.py (api_order_plan_outcome), tests/test_planner.py, tests/test_execution.py,
docs/audit_queue.md, docs/audit_a08_evidence.md.
DONE_WHEN: a deleted plan order cannot inherit receipt facts or status from
a new order reusing its ID; outcome requires the reciprocal plan link already
written on apply. Valid applied orders retain their outcomes. Real API
delete/create/receipt regression, no schema or monetary changes.
RESULT: planner351/0 and execution164/0. One-sided legacy links no longer
provide order or receipt evidence; no historical repair was attempted.

## Completed local CLAIM plan apply atomicity

BRANCH: codex/audit-plan-apply-atomicity. BASE: 501d744.
FILES: app/api.py (api_order_plan_apply), tests/test_planner.py,
docs/audit_queue.md, docs/audit_a02_evidence.md.
DONE_WHEN: a database failure saving the plan/order link leaves no new order;
retry after the failure creates exactly one order with both links and batch
ID. Verify using a temporary rejection trigger only in the synthetic test DB.
No concurrency experiment, schema migration, formulas or production writes.
RESULT: planner RED354/1, GREEN355/0; mock writeback140/0. The order and
both links now commit together. No claim about simultaneous apply requests.

## Completed local CLAIM A02 structured stop codes

BRANCH: codex/audit-plan-stop-codes. BASE: 95cd980.
FILES: app/api.py (apply refusal response), tests/test_planner.py,
docs/audit_queue.md, docs/audit_a02_evidence.md.
DONE_WHEN: blocked apply exposes the saved stop codes/text as structured
fields while preserving existing status422 and string detail consumed by
clients. Legacy missing gate metadata has a distinct recalculate code.
Tests verify codes plus no writes; no recalculation or monetary semantics.
RESULT: RED355/6, GREEN361/0. Existing string detail and422 are preserved;
code/stop fields expose saved reasons without parsing translated text.

## Completed local CLAIM A01 new-item outcome

BRANCH: codex/audit-a01-new-item-outcome. BASE: d347bb0.
FILES: app/api.py (outcome lines, _apply_order_to_incoming), tests/test_execution.py,
docs/audit_queue.md, docs/audit_a01_evidence.md.
OWNER: continue all remaining work except calculation formulas (07 Sep).
DONE_WHEN: saved new items appear in outcome with entered quantities and no
invented recommendation. Group by the existing receipt identity base_name;
duplicate/mixed names consume a receipt once, retain catalogue recommendation
and expose new_item_qty separately. Test before receipts, zero, actual receipts,
duplicate new names and mixed catalogue/manual names. No monetary formulas,
schema or historical writes.
The API regression also reproduced a UNIQUE violation on draft-to-sent for
duplicate new names: with autoflush disabled, incoming inserts the same key
twice. Aggregate quantities by the same existing base-name key before the
write, as the neighbouring remainder path already does. Verify incoming50.
RESULT: execution171/0, planner361/0, mock writeback140/0. New-only and mixed
names retain quantities/provenance and consume one receipt fact per name.
Full monetary totals/calendar work remains separate; formulas unchanged.

## Completed local CLAIM A01 missing new-item cost

BRANCH: codex/audit-a01-new-item-missing-cost. BASE: 215b266.
FILES: app/api.py (_plan completeness metadata), templates/assistant.html
(cost-entry guidance), tests/test_decision_record.py, docs/audit_queue.md,
docs/audit_a01_evidence.md.
DONE_WHEN: zero/missing-cost new items mark the saved and preview plan as
incomplete, including after overrides; history retains that marker. Preserve
catalogue missing-cost facts and amounts; UI tells users to enter new-item
cost in the questionnaire. No calculation formulas or new creation gate.
RESULT: decision-record RED44/3, GREEN47/0; Chromium52/0. Completeness
metadata includes new-item quantities and preserves catalogue facts.

## Completed local CLAIM A01 complete order summary

BRANCH: codex/audit-a01-complete-order-summary. BASE: 7683eb8.
FILES: app/api.py (shared final items/summary, plan metadata/save/apply),
tests/test_execution.py, docs/audit_queue.md, docs/audit_a01_evidence.md.
DONE_WHEN: preview/save expose order_totals and order_payments from exactly
the same catalogue+new-item rows used by apply. Reuse existing order summary
and payment_plan arithmetic without changing allocation, budget, forecast or
payment formulas. Persist complete summary separately from legacy catalogue
totals, allowing UI/history adoption without silently reinterpreting old data.
EXTENSION: templates/assistant.html, tests/test_ui.py,
tests/test_decision_record.py. Display complete order summary and payment
calendar; after local edits require recalculation of the complete calendar.
History uses stored complete totals when present and marks old partial totals.
RESULT: execution175/0, planner361/0, history47/0, Chromium55/0 (638).
Next bounded UI check: renderPlan exits on p.blocked even when nonempty
new_items make the order valid; test a new-items-only production end to end.

## Completed local CLAIM A01 new-only screen

BRANCH: codex/audit-a01-new-only-screen. BASE: a327c0f.
FILES: templates/assistant.html, tests/test_ui.py, docs/audit_queue.md,
docs/audit_a01_evidence.md.
DONE_WHEN: a real plan with no catalogue recommendations and valid entered
new items renders its full order summary and create button. Empty catalogue
diagnostics must not hide the entered order; server stop/can_create remain.
RESULT: real browser RED56/1, GREEN57/0. New-only plans render correctly.

## Completed local CLAIM A07 simple-order terms

BRANCH: codex/audit-a07-simple-order-terms. BASE: 547508b.
FILES: app/api.py, app/models.py (new payment_terms_json column and additive
migration), app/main.py (new terminal startup step15), tests/test_planner.py,
tests/test_startup_lifecycle.py, docs/audit_queue.md, docs/audit_a07_evidence.md.
OWNER: continue all work except calculation formulas. SUPPLY53 merged in base;
no other running writer observed. Preserve all14 released startup identities.
DONE_WHEN: simple orders snapshot their normalized terms at creation and keep
their payments after production terms change. Duplicate creation retains the
original order/snapshot. Legacy blank snapshots retain documented fallback;
no historic backfill, no new payment/date formula or fact-of-payment inference.
RESULT: planner RED361/2, GREEN363/0; execution175/0; startup174/0.
Old-schema migration, repeat-run preservation and old-writer compatibility
also passed on isolated synthetic SQLite. Snapshot records creation-time
terms, not confirmed negotiation/payment facts. Legacy fallback remains.

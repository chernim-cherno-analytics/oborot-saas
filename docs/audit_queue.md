# Independent audit queue

## Local CLAIM — PR56 portable Boolean migration

BRANCH: codex/audit-stage2-refresh. FILES: app/models.py,
docs/audit_queue.md. SOURCE: review3952626340, terminal unreleased step16
uses integer DEFAULT0 with BOOLEAN, incompatible with PostgreSQL ALTER.
Use a dialect-aware false ORM default and SQL FALSE in the new migration.
DONE_WHEN: SQLite existing-row/old-writer defaults remain false, repeated
migration succeeds, PostgreSQL CREATE TABLE compiles a Boolean default,
and fresh hosted strict CI passes. No formula or old migration changes.
LOCAL RESULT: SQLite migration twice, existing and old-writer inserted rows
false, integrity_check ok. PostgreSQL CREATE TABLE compiles DEFAULT false;
live PostgreSQL execution was not performed locally.

## Local CLAIM — PR56 navigation CI preparation

BRANCH: codex/audit-stage2-refresh. FILES: tests/test_supply_ui.py,
docs/audit_queue.md. SOURCE: CI34162892450 F-01 /replenish hit-test failure.
The hint opens after asynchronous seen/progress/lessons requests, whereas
F-01 closes it after a fixed 250ms. Mark hints seen using the real API before
navigation; preserve the actual visibility and mouse hit-test assertions.
DONE_WHEN: browser suite passes and current PR56 strict CI passes.
LOCAL RESULT: supply_ui543 OK/0 FAIL, exit0; log
/private/tmp/pr56-navigation-ui.log. Hosted strict CI remains required.

## Completed local CLAIM — A05 missing purchase price versus explicit zero

BRANCH: codex/audit-a05-price-presence. SOURCE: PR56 comment3950691498.
FILES: app/models.py, app/ms_sync.py, app/ms_writeback.py, app/main.py,
app/startup_schema.py if it owns the startup list; tests/test_writeback.py,
tests/test_startup_lifecycle.py, tests/test_supply_planning.py,
tests/test_supply_sheets.py, docs/audit_a05_evidence.md, docs/audit_queue.md.
DONE_WHEN: a real omitted buyPrice cannot become a saved/sendable zero,
explicit source zero remains usable, inherited catalogue price formulas stay
unchanged, terminal additive migration preserves all existing startup steps.
Existing catalogue zero without presence evidence must require resync.
SCOPE ADDITION before edit: DECISIONS.md, because D-58's original no-migration
statement is superseded by the required source-presence field.
RESULT: RED writeback145/3; GREEN150/0. Supply planning358/0.
Startup lifecycle177/1 was solely the old range1..15 assertion; corrected
clean-database group8/0. Other177 checks passed; hosted full CI must verify
the final revision. Manual source-inheritance and migration/old-writer probes
passed. Logs:/private/tmp/a05-presence-{red,final,supply,startup,startup-focused}.log.

## Local CLAIM — owner merge instruction and current main

BRANCH: codex/audit-release-refresh. FILES: AGENTS.md, docs/audit_queue.md.
Owner explicitly removed the independent-review requirement for these audit
fixes and instructed merging and continued coding. Preserve CI and protected
merge; no self-review verdict is created. Main66ad1c1 (SUPPLY PR54) merged
normally without conflicts before this documentation change. DONE_WHEN:
record the scoped override, publish updated PR55 and pass fresh required CI.

## Completed local CLAIM — scoped publication mandate clarification

BRANCH: codex/audit-publication-mandate. BASE:4c25f37. FILES: AGENTS.md,
docs/audit_queue.md. SOURCE: owner directly instructed Codex to write this
audit package, change its publication rules, decide A05, and explicitly
confirmed publication to chernim-cherno-analytics/oborot-saas via two PRs.
DONE_WHEN: repository instructions expose this scoped exception to reviewers
without weakening formula, CI, independent review or release requirements.
PR55 review3950677137 cites the older single-author rule; no runtime defect
is described by that comment. No authorship/history rewriting is permitted.
RESULT: scoped owner exception added to AGENTS.md; runtime unchanged.
Both original published heads passed hosted strict CI (runs34133115581 and
34133118777); this documentation commit requires its own fresh CI/review.

## Completed local CLAIM — A05 cost basis and supplier price

BRANCH: codex/audit-a05-cost-basis. BASE:ff2de21.
FILES: app/api.py, app/ms_writeback.py, tests/test_planner.py,
tests/test_writeback.py, docs/audit_a05_evidence.md, docs/audit_queue.md,
DECISIONS.md. Owner explicitly delegates the price decision to project logic.
D-12: Product.cost_full is full cost, cost_price is supplier buyPrice;
D-21: purchaseorder represents contractor sewing. Store these separately.
DONE_WHEN: simple orders retain full cost using the existing analytics
fallback; new catalogue orders snapshot supplier_price, and writeback uses
that snapshot rather than full cost. Plan snapshots survive catalogue edits.
Legacy orders without the new JSON key keep their saved send price; no
historic repricing. New items with unknown supplier price must not fabricate
one when matched for sending. Preserve all allocation/payment formulas.
RELEASE ORDER: first land the writeback reader supporting supplier_prices,
then the creator/full-cost change. The reader-only revision is the rollback
target for the creator revision; pre-reader code would mistake full cost for
document price. This ordering remains required when remote release unblocks.
SCOPE ADDITION before edit: templates/orders.html, because its confirmation
currently promises sending full cost. Update it to name saved supplier prices
and the preserved legacy fallback, matching the new writeback contract.
RESULT: writeback146/0, planner366/0, execution176/0, UI57/0,
idempotency533/0. Reader-only4c25f37 with old creator:140/0.
Decision D-58 resolves A05 locally; publication and independent review remain
pending. No full strict success is claimed. Evidence: docs/audit_a05_evidence.md.

## Completed local CLAIM — A07 sheets startup compatibility

BRANCH: codex/audit-a07-sheets-startup-compat. BASE:702b8b7.
FILES: tests/test_supply_sheets.py, docs/audit_queue.md.
SOURCE: isolated full supply_sheets run1798 OK/1 FAIL, exit1. The sole failure
is a second14-step expectation; no port conflict remains. DONE_WHEN: require
all15 identities and rerun the existing structural_checks group containing
the failure against a fresh synthetic application. No runtime/formula edits.
The full suite's remaining1798 checks passed; do not call a focused retest
a new green full strict run. Remaining14 references in startup lifecycle are
descriptive text; its actual ledger/order assertions already require15.
RESULT: existing structural_checks group98 OK/0 FAIL, exit0;
/private/tmp/a07-sheets-structural-green.log. Setup uses a fresh synthetic
organization, actual local API and the existing FakeGoogle transport. The
failed expectation is now15 and all five appended identities are checked.
No active test processes remain. No new full strict success is claimed.

## Completed local CLAIM — A07 startup compatibility sentinel

BRANCH: codex/audit-a07-startup-compat. BASE_RUNTIME:8b041c7.
FILES: tests/test_supply_planning.py, docs/audit_queue.md.
SOURCE: full strictc0014db, supply_planning355 OK/1 FAIL: old assertion
expects14 startup steps after A07 adds terminal step15. DONE_WHEN: require
all15 exact step identities without weakening the checks for released1–14;
the complete supply-planning suite passes. Runtime/formulas unchanged.
RESULT: supply_planning357 OK/0 FAIL, exit0;
/private/tmp/a07-startup-supply-planning-green.log. All previous step identity
assertions remain; step15 is checked explicitly. Full strict remains non-green.
Separate follow-up completed: supply_sheets ran on isolated port18964 after
strict's port8845 conflict;1798/1, the remaining14-step assertion is corrected
by the subsequent sheets startup compatibility CLAIM above.

## Completed local CLAIM — integrated validation and current queue

BRANCH: codex/audit-validation-c0014db.
BASE_SHA: c0014dbab9a97a940ce175814cda42380cbdfd96.
FILES: docs/audit_queue.md, docs/audit_a07_evidence.md.
DONE_WHEN: record the actual integrated strict result, A07 old/new/old/new
startup and ORM compatibility evidence, and replace stale top-level statuses
with the completed local packages. No runtime, formula, or test changes.
Full strict execution was started on the named BASE_SHA before these docs edits.
RESULT:48 suites,43 PASS,2 FAIL,1 NO_REPORT,2 SKIP;5064 OK/16 FAIL, exit1.
Supply planning has one outdated14-step assertion after A07 adds step15.
Supply sheets cannot bind occupied port8845; rerun it on a separate port.
Backup has the previously observed WAL/GNU-stat fixture failures; deploy and
offsite lack flock on this Mac. Full log:/private/tmp/audit-c0014db-full-strict.log.
Next: separate test compatibility CLAIM for the terminal step; no weakening
of the released-step identities. A07 rollback evidence is recorded separately.

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
| A01 New items bypass budget and disappear from decision totals | Local budget safeguard, full order totals/calendar/history, outcome grouping, missing-cost warning, and new-only UI are implemented through547508b | Independent review/publication pending; catalogue forecasts retain their existing scope and formulas; historical partial totals are marked, not backfilled |
| A02 Saved prohibited plan can be applied | Local server guard, UI stop/recalculate, structured refusal codes and atomic apply are implemented throughd347bb0 | Independent review/publication pending; concurrent apply is not claimed verified |
| A03 Packaging can exceed budget, MOQ or share limits | Final share safeguard implemented locally; 372 checks pass; existing budget guard covers MOQ/pack overspend | Independent review/publication pending; corrected allocation remains open under formula freeze; /private/tmp/a03-current-reproduction.json |
| A04 Explicit zero safety stock becomes 14 | Open, explicit formula/product decision required | Preserve reproduction and narrow decision; do not change calculation by default |
| A05 Simple order loses cost basis, production and author | Metadata and separate full cost / frozen supplier price implemented locally; D-58; writeback146, planner366, idempotency533 checks pass | Independent review/publication pending; reader4c25f37 must precede creator and remain the rollback baseline |
| A06 Total quantity differs from size quantities | Released, confirmed by owner-control history | Do not implement again; existing regressions remain |
| A07 New production terms alter historical payment calendar | Local exact plan snapshots and new simple-order snapshots throughc0014db; terminal additive migration15; plan/receipt links verify reciprocal identity | Independent review/publication pending; legacy fallback, placement-date policy, renegotiation history and actual payment facts remain open |
| A08 Received status falsely confirms execution | Local shared completeness summary through8b041c7; no evidence, partial, full, zero and conflicts use the receipt API's existing rules | Independent review/publication pending; partial known lines remain available while the whole order stays unconfirmed |
| A09 Invalidated cache can republish an obsolete snapshot | Open, P2 after pilot in original priority | Read-only current verification; no repeated prohibited concurrency experiment |

Publication update, 07 September 2026: the historical "independent
review/publication pending" entries above are superseded by the owner's
scoped review waiver in AGENTS.md. PR55 merged at
5d989c7fe21a98ff6ade3735e532245385b744c0 and post-merge CI34162863552 passed.
PR56 contains the second A05 stage, including missing-price/explicit-zero
presence and terminal migration16; its current browser CI correction is
tracked in the CLAIM above. No deployment is claimed. Four findings remain
partly or wholly open: A03, A04, A07, A09; merging PR56 does not close them.

After P1 consistency fixes: first correct order/onboarding, clear data quality,
verified supply/batch mapping and paid pilot. These are not permission for
microservices, broad integrations, redesign or new forecast formulas.

Current runtime/test HEAD: 8b041c7190b3dfdb0ae7bf176c6af62e3d8ebbb8,
including main2dcbd7fa7f57a07169364529ad3e148ff65c017f (SUPPLY PR53).
The older full strict run was43 PASS,3 FAIL,2 SKIP. The new full strict run
on c0014db is43 PASS,2 FAIL,1 NO_REPORT,2 SKIP; it is not a green release gate.
Evidence is recorded in the current
validation CLAIM and /private/tmp/oborot-audit-review-handoff.md.
Completed CLAIMs below are historical package evidence, not a fresh queue of
unfinished implementation. Their exclusions can be superseded by later CLAIMs.

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

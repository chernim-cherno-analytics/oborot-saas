# Independent audit queue

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

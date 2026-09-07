# A01: final budget safeguard for new items

Scope: prohibit creating a plan when its existing `new_items_over_budget`
is positive. Allocation, reservation, costs, payments and totals are unchanged.
The broader audit discrepancy in new-item totals/payment/outcome remains open.

Baseline: budget 100,000 and new items 150,000 produced `can_create=true`,
empty stop reasons, zero cost totals and an empty payment calendar. A complete
planner baseline with new regressions returned 317 OK / 6 FAIL, including a
saved plan applied successfully despite its known 50,000 excess.

The final stop list now includes `new_items_over_budget` and its existing
calculated excess. The A02 saved-plan guard rejects applying that plan,
including force and partial-history confirmations. Within-budget and exactly
at-budget new items remain allowed under both full and now budget scopes.
This preserves the existing full reservation of new-item cost in both scopes.

A second reproduction found that `_apply_overrides` rebuilt the stop list
and discarded the new prohibition: even an ineffective manual override could
change can_create from false to true. The same safeguard now runs after
manual edits. API regression covers saving and forced application with a
zero-quantity manual edit as well as with no override.

Final planner verification: 327 OK / 0 FAIL, including manual override.
Decision record regression: 36 OK / 0 FAIL. Total: 363 OK / 0 FAIL.
Synthetic, TZ=UTC, scheduler off;
no external systems or production data modified.

Local package only; independent review/publication pending. Does not claim
the entire A01 finding closed or the previously failing strict CI green.

## Partial-total labels in the wizard

The UI previously called catalogue-only cost the full obligation even with
new items. The displayed quantity and cash-calendar explanation also omitted
that restriction. A browser baseline using the real preview for two new
units at1,000 returned45 OK /3 FAIL: all three missing scope explanations
were reproduced. This follow-up labels catalogue-only cards accordingly and
states that current-plan totals, quantities and calendar exclude the new
items shown separately. No amounts or formulas change. The ordinary labels
are retained when there are no new items. Final browser:49 OK /0 FAIL,
including real preview and no console errors.

## Saved-plan history scope

History also displayed catalogue-only cost/count without noting missing new
items. The history API now exposes totals_exclude_new_items from the saved
brief; the UI labels both cost and quantity as excluding new items. Stored
result_json and its amounts remain byte-for-byte unchanged by history reads.

API baseline37 OK /2 FAIL, fixed39 OK /0 FAIL. Browser baseline49 OK /1 FAIL
for the missing history labels, using a real saved preview with new items.
An adjacent D-23 omission was found: the saved budget_incomplete flag also
disappeared from history. The API now exposes cost_incomplete from that
existing flag and the cost cell says the amount is incomplete. API baseline
for this case41 OK /2 FAIL; final API43 OK /0 FAIL, using an actual manual
addition of a catalogue position without cost. No migrations, monetary
recalculation or changes to interpretation of supplier versus full cost.

An intermediate browser run passed the new-item history case but read the
unrelated budget status while it still said "считаю…" after a fixed3-second
pause. The test now waits for calculation completion before checking the
window label; that rerun passed50/0. The final combined browser case saves
real new items and a real catalogue item with missing cost:52 OK /0 FAIL.
Combined final package checks: API43 plus browser52, 95 OK /0 FAIL.
# New-item quantities in outcome

Owner07 Sep: continue remaining work except calculation formulas. The outcome
report now includes saved brief.new_items. A new-only name has recommended=null,
decided equal to its entered quantity and executed=null until a receipt fact.
new_item_qty exposes the manual addition. Same-name new items group under the
existing receipt base_name identity; mixed catalogue/manual rows preserve the
catalogue recommendation and add the entered quantity without repeating receipt
facts. Historical inputs/results and all monetary/forecast formulas are unchanged.

The real API regression additionally found draft-to-sent failed for duplicate
new names: autoflush=false let two db.get misses queue two OrderedQty inserts
with the same key. _apply_order_to_incoming now sums by name before writing,
matching the neighbouring remainder path and existing receipt aggregation.

RED at d347bb0: /private/tmp/a01-outcome-red.log contains two failed new-item
outcome checks followed by the real UNIQUE violation and connection reset;
it is not a completed suite result. Initial fixed run passed new-only cases,
then the mixed fixture hit the existing budget guard. The fixture now uses
one assigned catalogue model in an isolated production with adequate budget.

Final GREEN: execution171/0 (/private/tmp/a01-outcome-green-final.log),
planner361/0 (/private/tmp/a01-outcome-planner.log), mock writeback140/0
(/private/tmp/a01-outcome-writeback.log):672 checks. Explicit zero and receipt50
are counted once for quantities20+30; mixed10+5 keeps catalogue recommendation
and reports decided/executed15. Read-only outcome does not rewrite saved JSON.
No full strict CI, independent review, publication or deployment is claimed.

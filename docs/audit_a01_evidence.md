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

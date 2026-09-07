# A03: final share-limit safeguard

This package preserves the allocator and its formulas. It refuses to apply
an automatically recommended quantity that exceeds the allocator's existing
per-item cap after packaging. It does not correct the allocation to a smaller
valid quantity; that remains an open part of A03.

Current reproductions: unit price 10, MOQ 10, pack 6 and budget 110 allocate
12 units for 120; the existing total-budget stop already prevents applying
that excess. With budget 1,000, share 25%, pack 6 and need 100, allocation is
30 units for 300 against a cap of 250. That second case previously remained
applicable because the overall budget was not exceeded.

The calculated cap in units is retained on each recommendation. A shared
final validator adds `share_limit` for offending automatic recommendations,
both on the original calculation and after manual edits. Saved decisions
carry the final stop list and the A02 guard rejects forced application.

Must-have retains its existing share exception. A genuine manual quantity
change also remains the user's decision; an unchanged quantity or unrelated
override cannot erase an invalid automatic recommendation. The existing total
budget restriction still applies even to explicit manual quantities.

Baseline planner: 331 OK / 5 FAIL. The failures reproduced the initial
recommendation, unrelated/no-op override bypasses, saved permission and forced
application. Fixed tests also cover manual correction to 24, explicit choice
of 36 and must-have. Final planner: 336 OK / 0 FAIL; decision record:
36 OK / 0 FAIL. Total: 372 OK / 0 FAIL on synthetic local data.

No formulas, schema, production data or SUPPLY files changed. Local package;
independent review/publication pending. The UI still independently computes
its disabled state; server rejection is covered here, UI alignment is a
separate follow-up. No claim of a fully closed A03 or green full strict CI.

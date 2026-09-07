# A07: existing frozen terms for orders created from a plan

Previously `_order_stages` read today's production terms, even when the order
had an applied plan with a saved stage schedule. A synthetic order for 1,000
with 50% upfront and 50% at day45 changed into one immediate 1,000 payment
after the production directory was edited to 100% upfront. Its original
computed stage snapshot was still available throughout.

The order now uses the existing snapshot from its earliest applied plan in
the same organization. The saved stage values are used directly; their shares
are not renormalized. No schema or stored data changes. Missing, malformed or
foreign snapshots retain the previous production/settings fallback; simple
orders without a snapshot are therefore not claimed fixed by this package.

The validator accepts the shape emitted by stage_schedule: named stages with
integer lead days and bounded numeric cost/prepayment shares. A damaged
snapshot must not produce arbitrary dates or a broken payment calculation.

API baseline: 340 OK / 2 FAIL. Changing the production directory changed the
old order's calendar, and restoring access to the original snapshot still
did not restore its original terms. The regression additionally checks a
damaged snapshot and a same-order link assigned to a different organization,
then restores synthetic fixture state. Final planner: 342 OK / 0 FAIL.

Execution regression: 164 OK / 0 FAIL. Total: 506 OK / 0 FAIL.
Synthetic local data; scheduler off.
Placement dates still follow the existing creation-date behavior. Payment
facts, planned-versus-paid labels, simple-order storage and later negotiated
revisions remain separate open A07 work. No claim that a past planned date
proves payment, no full A07 closure and no release/independent-review claim.

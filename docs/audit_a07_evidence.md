# A07: existing frozen terms for orders created from a plan

## Integrated compatibility on c0014db

SUPPLY compatibility43/0, tenant isolation91/0 and subscription119/0 pass
with TZ=UTC and synthetic local databases. Logs:
/private/tmp/a07-simple-terms-supply-compat.log,
/private/tmp/a07-simple-terms-isolation.log,
/private/tmp/a07-simple-terms-subscription.log.

A separate four-phase rehearsal uses git archives of main2dcbd7f and
candidatec0014db, with the real application lifespan and ORM on one isolated
SQLite file: old creates a row; new migrates and stores a snapshot; old reads,
updates the snapshot-bearing order and creates another row; new starts again.
All four phases exit0. Both old-writer rows retain blank defaults, the new
snapshot survives the old ORM update, and quick_check returnsok.
Reproducible script: /private/tmp/a07-rollback-rehearsal.py; exact versions,
phase statuses and temporary directory: /private/tmp/a07-rollback-rehearsal.json.
This verifies startup/ORM compatibility, not authenticated API replay.

Measured rollback cost: the old runtime ignores the snapshot and uses its
legacy45-day fallback instead of the stored10-day terms. Returning to the
new runtime restores the stored terms. Schema/data compatibility therefore
does not imply unchanged calendar behavior during rollback.

Previously `_order_stages` read today's production terms, even when the order
had an applied plan with a saved stage schedule. A synthetic order for 1,000
with 50% upfront and 50% at day45 changed into one immediate 1,000 payment
after the production directory was edited to 100% upfront. Its original
computed stage snapshot was still available throughout.

The order now uses the snapshot from its reciprocally linked applied plan in
the same organization. Exact saved payment terms are preferred to the legacy
rounded stage display. The saved values are used directly; their shares
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

## Payment precision and reciprocal links

Further verification found two flaws in the local A07 package before release.
The display stage snapshot rounds shares to four decimals: three equal stages
for1,000,000 produce333,333 /333,333 /333,334 originally but333,300 /333,300
/333,400 from the rounded snapshot. The same loss occurred after manual
quantity edits. New plans now retain the original normalized payment_terms
separately, persist them in computed_json, and use them for order payments
and manual edits. Display stages and the payment formula are unchanged.
Old records without exact terms cannot recover lost precision; their existing
rounded snapshot fallback remains explicit, without inventing historic data.

Second, deleting an order leaves the old plan link behind; SQLite may reuse
the order ID. A new simple order then inherited the deleted order's stage
snapshot. Snapshot selection now requires both plan.production_order_id and
order.order_plan_id to point to each other within the same organization.
Orders without that reciprocal evidence retain the legacy production fallback.

Precision baseline: planner342 OK /2 FAIL. After the precision fix344/0.
Reused-ID baseline:345 OK /1 FAIL after an actual API delete/create sequence.
Decision record regression43/0. Final planner346/0 and execution164/0:
553 OK /0 FAIL across these suites. No schema, supplier-price policy or allocator changes.

## History links after deletion and ID reuse

The same identity issue remained in history: _plan_row_out emitted the stored
production_order_id without checking the current order. An actual API
delete/create sequence reproduced a history link to a different new order.
History now loads same-organization plan/order links in one query and emits
a link only when both directions agree. Otherwise it returns order_id=null
and order_missing=true, rendered as an unavailable order. No historical rows
are rewritten. Genuine links and plans with no created order retain their
normal behavior. Baseline planner347 OK /1 FAIL. Final planner348/0 and
Chromium52/0, including history rendering and no console errors:400/0.
# Simple-order creation snapshot

Base547508b. A07 now covers new simple orders: creation stores normalized
terms in production_orders.payment_terms_json in the same transaction as the
order. Reading payments prefers a valid own snapshot, then the existing
reciprocal applied-plan snapshot, then the legacy production fallback. The
snapshot validation is extracted unchanged from the existing plan path.
Duplicate requests retain the original order and original terms. No payment
formula/date anchor changes or retrospective filling of old terms.

Schema: append-only startup step15 models.ensure_order_payment_terms_schema,
after all14 unchanged existing identities. One additive TEXT NOT NULL DEFAULT
'' column; old records remain blank. The startup checks now include failure
injection for step15 and the fifteen-step ledger. Their first run stopped on
an omitted test injection-map entry; that test harness entry was added before
the completed result below. No runtime startup failure is inferred from it.

RED planner361/2: /private/tmp/a07-simple-terms-red.log.
GREEN planner363/0: /private/tmp/a07-simple-terms-green.log.
Execution175/0: /private/tmp/a07-simple-terms-execution.log.
Startup174/0: /private/tmp/a07-simple-terms-startup-final.log. Total712 checks.
Separate synthetic old-schema proof /private/tmp/a07-simple-terms-migration.json:
old rows stay blank, repeat migration preserves a snapshot, and an old-style
INSERT omitting the column succeeds. No concurrent migration experiment added.

Rollback supports old writes but old code ignores this snapshot, so payments
again follow mutable settings while rolled back. Old orders without snapshots
still use the documented fallback; corrupt snapshots do as well. This is the
creation-time configuration, not evidence of confirmed negotiation or payment.
No full strict CI, independent review, publication or deployment is claimed.

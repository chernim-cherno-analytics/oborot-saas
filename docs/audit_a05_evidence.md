# A05: simple-order production and author

## PR56 correction: missing buyPrice is not explicit zero

Review3950691498 reproduced a real defect: sync collapsed missing buyPrice
and explicit zero to cost_price=0. The former snapshot accepted both as zero.
The integration reproduction passes omitted buyPrice through the real parser
and field persistence, creates an order through the API and attempts mock
writeback: RED145 OK/3 FAIL, /private/tmp/a05-presence-red.log.

A separate buy_price_zero_explicit source flag now preserves this distinction.
Nonzero cost and its existing parent inheritance remain unchanged; an explicit
variant zero is preserved for writeback even where analytics retains its old
parent-cost fallback. An inherited explicit parent zero remains known. Without
zero evidence, zero is omitted from new snapshots and sending returns422.
The integration tests no longer fabricate an empty snapshot to prove refusal;
they exercise source parsing, persistence, API creation and document sending.

This supersedes the original no-migration statement: terminal step16 adds one
Boolean column with default false. Existing15 steps are unchanged, no old zero
is declared known by backfill, and old-writer INSERTs get false. Existing zero
catalogue values require a fresh sync before new zero-priced orders can send.
Historical order snapshots are not repriced. If rolling back to an older sync
writer, perform a full catalogue sync with this corrected writer before enabling
new snapshot creation again: the old writer does not maintain the new flag.

Local migration checks: additive ALTER twice, existing zero and positive rows
keep false, old-writer INSERT receives false. Parser probes preserve explicit
variant zero and inherited positive/zero while rejecting missing source zero.
Hosted strict CI passed both original published heads; corrected HEAD needs a
fresh hosted run and independent review, not self-acceptance.
GREEN writeback150/0 and supply planning358/0. Startup lifecycle177/1 had only
the stale range1..15 assertion; after correction its clean-database group8/0.
Logs:/private/tmp/a05-presence-final.log, /private/tmp/a05-presence-supply.log,
/private/tmp/a05-presence-startup.log and /private/tmp/a05-presence-startup-focused.log.

## Completion: cost basis and document price, 07 September

The owner explicitly delegated the price decision to the project's existing
business logic. D-12 identifies cost_full as full cost and cost_price/buyPrice
as contractor purchase price; D-21 defines purchaseorder as ordering sewing
from that contractor. D-58 records the resulting local implementation decision.
The earlier pricing exclusion below is superseded.

Simple creation now reads the same full-cost/fallback and max-over-sizes basis
as analytics, rather than whichever purchase-price row SELECT returned last.
The client cannot supply either price. The wizard retains its displayed
calculated cost and any explicitly configured overhead; no formula changes.
Both paths freeze contractor purchase prices by size in supplier_prices.
For the wizard the snapshot is saved with the plan and copied when applied,
so intervening catalogue edits cannot reprice the document. Quantities,
allocation, calendar arithmetic, tenant checks and order identity are unchanged.

Writeback uses the saved price of the matched SKU, preserving differing size
prices. A missing price in the new snapshot rejects sending before document
creation instead of using full cost. Explicit zero is retained. Old order
rows and old saved plans without the key retain their legacy saved cost as
send price; no historic repricing. The send confirmation describes both cases.

Release is deliberately two-stage. Reader compatibility is committed first
as4c25f370be2f9610376e232c7930f7410df3b170. Its old-creator workflow passes
writeback140/0 in a separate worktree. The second stage enables recording new
prices and full costs; its rollback target must include that first stage.
Pre-reader code would ignore the new field and could send full cost as price.
No schema migration is required, but that older rollback is not price-safe.

RED: real API plus mock writeback140 OK/3 FAIL, /private/tmp/a05-cost-basis-red.log.
For a synthetic buyPrice100/full cost150, the old API saved100, lost the
separate supplier price and retained the wrong cost after catalogue changes.
GREEN expanded coverage: writeback146/0, planner366/0, execution176/0, UI57/0.
Idempotency and recovery:533/0, /private/tmp/a05-cost-basis-idempotency.log.
The UI suite is general regression coverage, not a dedicated assertion of the
updated send-confirmation text.
Logs:/private/tmp/a05-cost-basis-writeback-complete.log,
/private/tmp/a05-cost-basis-planner.log, /private/tmp/a05-cost-basis-execution.log,
/private/tmp/a05-cost-basis-ui.log. Reader-only log:/private/tmp/a05-price-reader-compat.log.
The final sending scenario additionally uses purchase prices100/125 by size,
full cost150, forged client prices and a subsequent catalogue change to999/1999;
the document still uses its stored100/125 prices. Partial refusal can be retried,
zero remains zero, and a legacy row sends its original175 price.

These are local checks, not full strict CI, independent approval or release.

Scope: metadata part of the independent audit, on top of integrated audit
base `14a03957a513a29937c27dcc68bc5cc46cdcdc70`. Supplier-price versus
full-cost semantics remain open; this package does not change pricing,
allocation formulas, schema or historical orders.

The simple replenish form now sends its active production. The API validates
that production against the authenticated organization before writing and
stores the authenticated user as author. Existing clients may omit production;
their author is still recorded. Missing and foreign production IDs both return
404 without creating an order. Duplicate matching includes production, so
identical quantities ordered from different productions remain separate.
Repeating the same request returns the existing order, preserving its author.

Synthetic baseline: `tests/test_planner.py` gave 306 OK / 7 FAIL. Failures
reproduced missing metadata, cross-production deduplication and acceptance of
foreign/nonexistent production. Test-created orders are cleaned up so these
expected baseline failures do not contaminate later scenarios.

The browser regression creates an order through the actual replenish page,
checks the POST production ID, then reads the synthetic database to verify
production and author. The existing simple-order cost behavior is unchanged.

Local verification uses TZ=UTC, scheduler disabled and isolated loopback
ports. Initial sandbox runs could not bind loopback; authorized reruns started
successfully. This is not a production action or release approval.

Planner: 313 OK / 0 FAIL. Execution: 164 OK / 0 FAIL. All nine new API
checks pass. Browser: 37 OK / 0 FAIL, including selected-production POST and
persisted production/author checks. Total: 514 OK / 0 FAIL across these suites.
The first browser attempt stopped at the existing onboarding overlay; the
test now closes that overlay through its normal button before creating an
order. No application workaround for the overlay was added.
Publication and independent review remain pending. The earlier full strict
run has unrelated failures/skips; this package does not claim a green full CI.

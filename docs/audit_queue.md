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
| A01 New items bypass budget and disappear from decision totals | Open; reproduced on current code: budget 100,000, new items 150,000, can_create=true, cost/totals zero, payments empty | Fix in a separate bounded package without silently redefining monetary fields; synthetic reproduction at /private/tmp/a01-current-reproduction.json |
| A02 Saved prohibited plan can be applied | Local server fix32f17cd; not released | Independent review and publication pending; audit's wider UI/structured-reason criteria are not all claimed complete |
| A03 Packaging can exceed budget, MOQ or share limits | Open; allocation formulas frozen | Verify final-order safeguards separately; do not rewrite rounding/allocation formulas |
| A04 Explicit zero safety stock becomes 14 | Open, explicit formula/product decision required | Preserve reproduction and narrow decision; do not change calculation by default |
| A05 Simple order loses cost basis, production and author | Metadata package locally implemented; planner 313, execution 164, browser 37 checks pass | Independent review/publication pending; supplier-price versus full-cost semantics remain open |
| A06 Total quantity differs from size quantities | Released, confirmed by owner-control history | Do not implement again; existing regressions remain |
| A07 New production terms alter historical payment calendar | Open | Verify existing frozen decision data and historical schedule; schema changes overlap SUPPLY and require a separately coordinated package; no invented payment facts |
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

## Active local CLAIM A05 metadata

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

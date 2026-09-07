# A05: simple-order production and author

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

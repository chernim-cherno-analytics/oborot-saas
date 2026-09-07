# A07: planned-payment wording

The wizard's open-order banner called `left_to_pay` the amount left to pay.
The API actually sums scheduled amounts dated today or later; it does not
subtract verified payment facts. The banner now calls the sum today's and
future scheduled payments and explicitly asks the user to verify actual
payments separately. The explanation remains visible when that future sum
is zero, so the UI does not imply all obligations have been paid.

Only two display strings changed. The existing amount, date filtering,
production filtering, link and API contract remain intact. Verified the
two-line diff against api_orders_open and loadSideData; git diff --check
passes. No new automated tests for this wording-only change. Frozen terms
and payment-fact storage are separate packages; this wording is not proof
of payment and not a complete A07 resolution. Local review/publication pending.

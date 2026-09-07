# Server plan stops in the wizard UI

The server can reject a plan for new-item budget excess or a final share
limit, but the browser previously disabled creation only for its own three
checks (negative remainder, empty plan, past date). The server still rejected
application; the enabled button was misleading and caused avoidable failures.

The button now includes server stop reasons and explicit can_create=false,
with a fallback explanation if no reason text was supplied. Existing local
checks remain. After changing quantities on a prohibited plan, use the
existing plan recalculation before creating it: the browser does not guess
whether that edit clears the server's restriction.

Real Chromium baseline: 38 OK / 6 FAIL. The browser received an otherwise
valid real preview with controlled server gate fields and ignored both named
stop cases plus false permission with an empty reason list. The subsequent
valid response correctly enabled the button even in the baseline.

Regression repeats these responses through the actual page recalculation,
checks disabled state and tooltip explanation, then verifies a valid
recalculation re-enables the button. Final Chromium suite: 44 OK / 0 FAIL,
including the existing A05 creation regression and no console errors.
No formulas, monetary values or server APIs changed.

Follow-up: a visible Recalculate button now sits beside Create order. It
uses the existing preview flow and retains pending edits, so correcting a
prohibited plan does not require discovering that the step-3 navigation
also recalculates. The regression now clicks this real button for every
prohibited/allowed transition. Final browser run: 44 OK / 0 FAIL, no console
errors. Fallback wording no longer exposes server implementation detail.

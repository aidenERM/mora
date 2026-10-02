# Orbit project state

## Scope

Build order: agent runtime → private Discord + memory → model selection → AWS
fallback → schedules/Pulse signals → approvals. Follow the autonomous runbook.
Keep Cloud PC and the existing 15-minute Pulse worker. No paid resource creation.

## Current milestone

Cloud PC baseline: `a51f151`, deployed. Restart-persistent identity, workspace,
cookies/localStorage, screenshots, shell/timeouts, auth and path boundaries were
verified. Real web-account login persistence still requires an actual login.

Phase 1 implementation: SQLite task/step state, bounded loop, independent
completion checks, cancellation, retry limits, interruption recovery and CLI.
Cloud PC gains a bounded visible-page observation endpoint. Dangerous browser
interactions and shell commands pause for explicit per-action approval.

## Verification

Phase 1: 18 regression tests passed. Real production Bedrock tasks created and
read back an exact-content workspace file, navigated Example Domain and saved
a private PNG, read visible DOM text and saved its title, and executed/verified
shell `pwd`. Interrupted failed tasks were resumed with persisted history.
An initial malformed-decision issue was reproduced: model prose preceded JSON.
Orbit now accepts only one complete schema-valid decision and rejects partial
or ambiguous output. Completion checks reject mutations and mismatched content.

Real account login persistence remains unverified. This is separate from the
Cloud PC cookie/localStorage restart proof and the successful live agent tasks.

## Configuration / blockers

Existing production Bedrock credentials are present. No OpenAI API key was
found. The user supplied a private Discord credential file outside the repo;
its bot token and application identity were verified with official Discord APIs.
The human application owner was resolved and the bot token/owner restriction
were provisioned in root-only `/etc/orbit.env`. Never copy credentials into docs.

## Phase 2 checkpoint

Owner-only official Discord DM client, persistent approval buttons, private file/
screenshot delivery, natural goal input and model-selection phrases implemented.
Explicit sourced memory supports preferences/style/people/projects/decisions/
goals/episodes, relevance-limited retrieval, forgetting, deduplication and expiry.
Raw DM context is capped at 40 messages/14 days; only explicit facts and bounded
task episodes enter memory. Credentials are redacted/rejected before persistence.
25 targeted regression tests passed. Live Gateway and DM delivery still pending.
Image files can be received privately; image interpretation is not yet wired.

## Next

Deploy Phase 2 and check Gateway/owner DM delivery. Then finish provider selection,
fallback tests, schedules/Pulse wakeups, and permission configuration. Missing
OpenAI credentials and real account/device confirmation remain external checks.

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
its token still needs validation. Never copy credentials into documentation.

## Next

Commit Phase 1 checkpoint, then implement owner-only Discord DMs and
minimal sourced memory. Preserve provider-independent state during fallback.

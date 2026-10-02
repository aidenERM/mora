# Orbit project state

## Scope and checkpoints

Runtime → Discord/memory → model routing → AWS fallback → schedules/Pulse triggers
→ permissions. Keep the full autonomous runbook goal active. Preserve Pulse 0.2
and its 15-minute worker. No new paid resources or provider accounts.

Deployed checkpoints: Cloud PC `a51f151`, runtime `a0d4598`, Discord/memory
`e9b48f9`. The latest group extends phases 3–6; deployment/live checks pending.
Always inspect git HEAD before assuming a revision.

## Implemented

- Persistent tasks/observations, cancellation, bounded retry/step/decision
  budgets, interruption recovery, independent completion checks and CLI.
- Persistent Cloud PC; DOM observations, controls/screenshots, confined files,
  ranged reads, exact patches, concurrent-edit hashes and atomic writes.
- Owner-only official Discord DMs, natural goals, explicit sourced memory,
  typed categories, forgetting, attachments, screenshots and persistent approvals.
- Configured model roles/selection; switches preserve tasks. OpenAI Responses
  adapter, existing AWS adapter, bounded retry/circuit/fallback handling.
- America/Bogota relative dates, future/recurring wakeups, recurrence cancellation,
  verified-evidence notice deduplication and no idle model calls.
- Pulse triggers after existing relevance/delivery decisions: score >=85,
  high/urgent tier, canonical/development deduplication, maximum two/day.
- New source-backed findings reuse Pulse scoring/cooldowns/freshness/context.
  Derived reports cannot inherit an urgent quiet-hours bypass from parent alerts.
- Operator-owned automatic/conditional/approval-required policy, expiring
  single-use approvals, exact page/element/form bindings, negative user scope,
  and ambiguous action outcomes blocked.
- Cached Pulse phone/calendar context and event search without provider sync/AI.
- Optional targeted vision input, without continuous screenshot model uploads.

## Evidence

Phase 1: 18 tests and real AWS/Cloud PC exact file creation/readback, public
HTTPS/DOM inspection, private PNG, shell `pwd`, and persisted task resumption.
A reproduced prose-before-JSON model issue was fixed: exactly one complete,
schema-valid decision is required. Mutating/mismatched completion checks fail.

Phase 2: 25 tests. Official Discord bot/application identity verified, owner
resolved, Gateway connected, initial private DM accepted. Credentials exist only
in root-owned `/etc/orbit.env` (0600), not source/model prompts.

Latest full local suite: 123 passed, syntax checks passed, credential scan clear.
Current-group live fallback, scheduling, browser-binding and binary/file-range
checks remain pending deployment verification.

## Privacy and operation

Separate SQLite under `/var/lib/pulse/orbit`; backup before additive schema work.
Raw DMs: 40 messages/14 days. Completed tool observations: 90 days. Task identity
and useful results remain; explicit memory persists until forgotten; episodes
expire after 90 days. Roles use configured IDs, not guessed names. `codex` is a
code-model role, not an authenticated native Codex CLI. AWS currently powers Orbit.

## External blockers / unverified

- OpenAI key/chosen model absent. User was asked for a private credential-file
  path. Live primary/model-role requests cannot yet be tested.
- Vision model absent. Image receipt/storage is implemented; interpretation must
  block explicitly rather than pretend to see an image.
- Real authenticated website session across restart needs an actual owner login.
- Native Codex, real owner DM replies/buttons, incoming file upload and natural
  proactive developments need live confirmation.

## Next

Finish tests/deployment and live AWS fallback/scheduling checks. Then activate
credential-dependent capabilities. Do not mark the goal complete merely because
the AWS foundation works.

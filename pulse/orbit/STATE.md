# Orbit project state

## Scope and checkpoints

Runtime → Discord/memory → model routing → AWS fallback → schedules/Pulse triggers
→ permissions. Keep the full autonomous runbook goal active. Preserve Pulse 0.2
and its 15-minute worker. No new paid resources or provider accounts.

Deployed checkpoints: Cloud PC `a51f151`, runtime `a0d4598`, Discord/memory
`e9b48f9`, model/schedule/policy group `cad0074`, startup transport fix `db7cad0`.
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

Latest full local suite: 125 passed, including startup transport regressions.
Syntax checks passed; provided bot token absent from staged files.

Current-group live proof: authenticated/path-confined Cloud PC survived restart,
retaining identity, workspace, session cookie/localStorage and screenshot access.
Stale file hashes and stale browser approvals were rejected. Binary PNG roundtrip
and exact patch preservation passed. Cached live Pulse calendar context read
without sync or mutation. Injected primary outage completed a real AWS/Cloud PC
file goal in 3 steps. A future task woke on the real worker, verified runtime
health in 2 steps, completed and sent its private Discord completion message.
A non-sensitive real PNG screenshot was also accepted in the owner's Discord DM.

All five services active: pulse, pulse-worker, pulse-cloudpc, orbit-worker,
orbit-discord. HTTPS health good. Actual Pulse process retains poll_minutes=15
and proactive_enabled=1. No natural eligible proactive task has occurred yet.
Incoming owner DM count was zero at the latest check; user was asked to reply hi.

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
- Native Codex CLI is not connected; the configured code-model role requires
  OpenAI activation. Real owner DM replies/buttons, incoming file upload and
  natural proactive developments still need live confirmation.

## Next

Verify the owner's real DM reply/buttons/file upload when available. Activate
OpenAI/vision from a private credential-file path and verify actual requests;
complete a real owner website login/restart check. All other current foundation
work is deployed. Keep this full goal active while these gates remain unmet.

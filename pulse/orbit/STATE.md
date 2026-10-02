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

Latest full local suite before browser QA: 125 passed, including startup transport regressions.
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
The owner's real greeting arrived, verifying inbound DMs. This exposed a stale
connection flag after Gateway resume; resume and accepted owner messages now
restore that flag. Nine targeted memory/Discord tests passed for this fix.

## Real browser QA activation

Using the owner's signed-in Chrome session: a natural DM goal opened Example
Domain, read visible text, returned a real PNG and independently verified title.
A real Approve button resumed browser_click and reached IANA's example-domains
page. A PNG pasted through the browser clipboard was received, saved and its
68-byte size/SHA-256 independently verified. File-picker uploads require the
extension's optional file-URL permission; that permission was not expanded.

Browser QA found two real issues: binary completion checks attempted forbidden
shell verification; a confined file_info tool now provides size/hash checks.
SQLite readers under the worker's strict mount could not create sidecar files;
the mount now permits the Pulse state directory while connectors enforce mode=ro.

AWS vision model availability was checked with real account APIs. Nova Lite
returned an inaccurate description on multilingual content; that result and its
derived memory were invalidated. Already-authorized Claude Haiku 4.5 via
global.anthropic.claude-haiku-4-5-20251001-v1:0 now provides bounded requested
image observations to the normal planner. Real DM vision completed and correctly
recognized the Arabic documentation notice and Learn more link; OCR retains
uncertainty/minor transcription errors and is not a pixel-perfect guarantee.

## Privacy and operation

Separate SQLite under `/var/lib/pulse/orbit`; backup before additive schema work.
Raw DMs: 40 messages/14 days. Completed tool observations: 90 days. Task identity
and useful results remain; explicit memory persists until forgotten; episodes
expire after 90 days. Roles use configured IDs, not guessed names. `codex` is a
code-model role, not an authenticated native Codex CLI. AWS currently powers Orbit.

## External blockers / unverified

Communication update a01c27c is deployed: concise casual summaries, no routine
task references in acknowledgments, technical evidence only when requested.
Verification and approval requirements are unchanged. Full suite: 128 passed,
one upstream audioop deprecation warning. A real production Bedrock reply test
reported an expired calendar session in one plain-language sentence. This was
a synthetic-input provider test, not proof of a real calendar login failure.
All five Pulse/Cloud PC/Orbit services were rechecked active after deployment.

Native Codex CLI 0.150.1 is installed on the VPS but `pulse` remains signed out.
The official browser sign-in reached account consent; final authorization was
not granted. A specific approval request is outstanding for persistent VPS
access to Aiden's ChatGPT plan. Device codes are temporary, not project config.
No OpenAI API payment, key creation, or security-setting change was performed.

- OpenAI personal API organization inspected through real saved-account login:
  no API keys, $0.00 credit, no funded billing. Primary API activation needs a
  spending decision/credentials; no payment or new key was created.
- Real authenticated website session across restart needs an actual owner login.
- Native Codex CLI is not connected; the configured code-model role requires
  activation. A real naturally enqueued iCloud Mail event investigation initially
  failed on SQLite access. After the existing mount fix, resuming the same task
  completed with browser source observation and independent finish verification
  (task 7044d1c5314c446d83ad512f565a20cb). This verifies live source-to-Orbit
  investigation, not delivery of a new material finding. Notification delivery
  remains gated by existing source validation/relevance/quiet-hours rules.

## Next

Activate funded OpenAI/native Codex access and verify actual requests;
complete a real owner website login in Cloud PC and restart check. All other current foundation
work is deployed. Keep this full goal active while these gates remain unmet.

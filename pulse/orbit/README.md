# Orbit

Orbit owns its task state independently of Discord and AI providers. It uses the
existing private Cloud PC to execute bounded tools. The worker only reasons when
a task is queued; idle checks make no model calls. See `STATE.md` for verified
milestones and remaining blockers.

## Phase 1 interface

From `pulse/` in a configured server environment:

```sh
python -m orbit submit "Create notes/example.txt, then read it back" --model aws
python -m orbit once
python -m orbit status TASK_ID
python -m orbit cancel TASK_ID
python -m orbit resume TASK_ID
python -m orbit model TASK_ID aws
python -m orbit approve STEP_ID
python -m orbit approve STEP_ID --deny
```

Install `pulse/systemd/orbit-worker.service` on the VPS after creating
`/var/lib/pulse/orbit` with owner `pulse` and mode `700`. It reuses Pulse's existing
environment files; optional Orbit secrets belong in root-owned `/etc/orbit.env`.
No credentials belong in source, task prompts, logs, or the browser frontend.

Each task has a lifetime step budget (default 12, maximum 40), persistent
observations, and one of queued/running/waiting/waiting_approval/completed/failed/
blocked/cancelled. Failed provider/tool operations get at most two consecutive
attempts. Completion requires explicit independent read-only checks; file writes
are read back immediately. Unknown interrupted tool outcomes block rather than
automatically replaying a mutation. Resume preserves the original goal/history
and does not reset its budget.

Browser navigation, visible-page inspection, screenshots, and confined file
reads/writes are available. Website interactions and general shell/code require
an expiring, per-action approval. No request can approve itself through model
text. Screenshots are private artifacts, not automatically sent to a model.

`auto` uses configured OpenAI model roles when a key/model is available; otherwise
it uses existing AWS Bedrock. OpenAI failures preserve the same context during
AWS fallback. Explicit `sol`, `codex`, `fast`, and `strong` are configured model
roles, not claims of a native Codex CLI connection. No OpenAI key or model access
has been verified yet. Provider identities and billing are not interchangeable
with a ChatGPT subscription.

The persistent profile stores login state, but individual providers can revoke
or expire sessions. Actual authenticated-account restart testing is still
pending and must not be inferred from the synthetic cookie test.

## Discord and memory

Install `orbit/requirements.txt` into the existing Pulse virtual environment.
The root-only provisioner accepts bot token/application ID over stdin, verifies
official bot/application identity, resolves the application owner, and writes
`/etc/orbit.env` with mode `0600`. Install `orbit-discord.service` and restart
`orbit-worker` to load the optional environment file. No user tokens are used.

Only the configured owner's private DMs are processed. Natural goals create
tasks. Examples: `remember Pulse is my project`, `what happened while I was gone`,
`cancel that`, and `use aws for this`. Explicit memory is sourced and forgettable.
Approvals arrive as buttons plus the full proposed action JSON. The worker checks
expiry and single use. Private screenshot artifacts can be sent back to the owner.
Incoming files are capped at 700 KB; credential attachments are rejected.
Images are stored as files; model vision is not yet configured.

## Wakeups, providers, and permissions

Requests like `check the file tomorrow at 5pm` or `check every 2 hours` use
America/Bogota dates. Recurring intervals are 15 minutes to 31 days. `cancel that`
also disables the associated recurrence. Verified evidence, rather than changed
summary wording, determines repeated schedule notices.

Install `systemd/pulse-worker-orbit.conf` as
`/etc/systemd/system/pulse-worker.service.d/orbit.conf` to enable conservative
Pulse-triggered investigations. They reuse canonical/development identity and
are capped at two/day after high relevance decisions. New reports need observed
source evidence and pass Pulse's delivery rules again. Derived reports cannot
bypass quiet hours; original validated urgent Pulse alerts keep their policy.

Copy `policy.example.json` to root-owned `/etc/orbit-policy.json` to override
defaults. Conditional commands must be explicitly trusted. Models cannot approve
actions or edit policy. Whole-file replacement requires review; prefer
`file_patch` for bounded edits. Browser approvals bind URL, element and form
values; changed state invalidates execution. Password entry needs private manual
setup rather than storing a password task.

Provider retries are bounded, with a 120-second OpenAI outage circuit cooldown.
Optional `ORBIT_VISION_MODEL` / `ORBIT_AWS_VISION_MODEL` enable one requested
PNG/JPEG at a time. Missing vision configuration blocks with an explanation.

Opt-in live verification using the existing AWS account:

```sh
python -m orbit.verify --live-aws --fallback-probe
```

The primary outage is injected; AWS and Cloud PC execution are real. This does
not verify a configured OpenAI account. API contracts:
[structured output](https://developers.openai.com/api/docs/guides/structured-outputs),
[image input](https://developers.openai.com/api/docs/guides/images-vision),
[Discord Gateway](https://docs.discord.com/developers/events/gateway).

DM delivery requires Discord to permit the bot to contact its owner, commonly
through a shared server or supported application installation. Startup records
connection/delivery failures without exposing tokens. Failed or ambiguous sends
are retained for review rather than automatically repeated into spam.

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

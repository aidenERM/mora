# Pulse Shortcuts

Pulse accepts short-lived, derived context through `POST /api/shortcut/context`.
Keep the token in the Shortcut only; never commit the real token to this repo.

The same token can send an intentional share from Instagram, WhatsApp, TikTok,
or any other app through `POST /api/shortcut/capture` with `source`, `title`,
`text`, and optional `url`. Pulse stores the capture and only proposes a
calendar action when it can identify a concrete date and time. It never reads
private chat history or scrapes those apps.

Recommended automations on iPhone:

- opening Pulse: send current mode, coarse location, battery, and charging
- arrive home / leave home: send `home` or `outside`
- arrive school / leave school: send `school` or `outside`
- Travel Focus or Sleep Focus changes: send `travel` or `sleep`
- charger or low-battery automation: send battery and charging state

Each request should include `physical_context` (`home`, `school`, `outside`, or
`travel`), `state` (`awake`, `sleep`, or `focus`), `confidence`, and a `signals`
object. The legacy `mode` field is still accepted as the physical context.
Location is rounded server-side and expires automatically. Do not create hourly location
automations; iOS does not guarantee reliable background PWA GPS execution.

The repo keeps the real sources in `pulse-phone-context.cherri` and
`pulse-action-runner.cherri`. The compiled, signed artifacts are in `build/`.
Run `powershell -ExecutionPolicy Bypass -File pulse/shortcuts/build.ps1` from
the repository root to rebuild them. The script downloads the official Linux
Cherri release into the local temporary development tools directory, runs it through WSL,
and uses Cherri's HubSign path for non-macOS signing. Cherri is build tooling
only; it is not needed on the iPhone.

Both Shortcuts contain an import/setup question for the Pulse token. The token
is not in the `.cherri` source or committed `.shortcut` artifacts. After import,
open the Shortcut's setup details, paste the token from Pulse → Connections →
Apple, and save it. The action runner then polls the approved queue and only
executes its allowlisted Calendar, Reminders, URL, and context branches.

`pulse-action-runner.cherri` polls the approved action queue, claims one action,
runs only the allowlisted native Calendar/Reminders/URL/context branch, and
reports completion with the action claim token. The server's claim token and
idempotency rules prevent duplicate execution on retries.

The compiled files are genuine Cherri outputs, not renamed text. Non-macOS
signing uses HubSign. Importing and running each file on a real iPhone remains a
HUMAN CHECK because this Windows/WSL environment cannot open the Shortcuts app.

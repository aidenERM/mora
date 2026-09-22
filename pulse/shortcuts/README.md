# Pulse context Shortcuts

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

The included `pulse-phone-context.cherri` is a thin template. Replace its token
placeholder only in the local Shortcut, compile/import it with Cherri, and test
one manual run before adding automations.

`pulse-action-runner.cherri` is the companion action template. It polls the
approved action queue, claims one action, runs only the allowlisted native
Calendar/Reminders/URL/context branch, and reports completion with the action
claim token. The current Windows checkout does not verify Cherri compilation or
iPhone import, so Pulse labels this as a source template until it is tested on
an Apple device.

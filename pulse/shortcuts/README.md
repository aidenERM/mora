# Pulse context Shortcuts

Pulse accepts short-lived, derived context through `POST /api/shortcut/context`.
Keep the token in the Shortcut only; never commit the real token to this repo.

Recommended automations on iPhone:

- opening Pulse: send current mode, coarse location, battery, and charging
- arrive home / leave home: send `home` or `outside`
- arrive school / leave school: send `school` or `outside`
- Travel Focus or Sleep Focus changes: send `travel` or `sleep`
- charger or low-battery automation: send battery and charging state

Each request should include `mode`, `confidence`, and a `signals` object. Location
is rounded server-side and expires automatically. Do not create hourly location
automations; iOS does not guarantee reliable background PWA GPS execution.

The included `pulse-phone-context.cherri` is a thin template. Replace its token
placeholder only in the local Shortcut, compile/import it with Cherri, and test
one manual run before adding automations.

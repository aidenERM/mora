# Pulse iPhone companion

This is the native bridge boundary for Pulse. It is intentionally small: the server owns configuration, relevance, storage, and delivery; the companion only collects approved device context and posts derived signals.

The companion should request permissions only for features Aiden enables:

- EventKit for upcoming calendar/reminder context
- HealthKit for derived state such as sleep or workout-active, never raw samples
- HomeKit for selected home/away signals, never a full home inventory
- MusicKit for listening-interest context
- Contacts only when a user-selected contact signal is needed
- WeatherKit and Core Location for contextual modes, with coarse/temporary location preferred
- App Intents/Shortcuts for manual context updates and server-owned actions

Pairing is a one-time code generated in Pulse Integrations. The app redeems it at `/api/companion/pair/redeem`, stores the returned bearer token in Keychain, and posts only normalized context to `/api/companion/context`. Do not put server credentials, OAuth client secrets, or the Shortcut token in the app.

This folder is source-level integration guidance only. Building, signing, granting Apple permissions, and verifying background delivery require an Apple device/Xcode and remain HUMAN CHECK on this Windows workspace.

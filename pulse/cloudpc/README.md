# Pulse Cloud PC v0.1

One Linux systemd service on the existing VPS, running as `pulse-cloudpc`.
It does not alter Pulse's web app, worker, database, or public nginx routes.
Control binds only to `127.0.0.1:8792` and every endpoint requires a server-side
Bearer token. This is a trusted internal control API, not a multi-user sandbox.

## Install and run

Copy this directory to `/opt/pulse-cloudpc`, then run as root:

```sh
bash /opt/pulse-cloudpc/install.sh
systemctl status pulse-cloudpc
python3 /opt/pulse-cloudpc/verify.py --restart-service
```

The installer generates a token privately in `/etc/pulse-cloudpc/token`.
Never commit that file or return it to a browser. It permits the existing
`pulse` user to read it through `pulse-cloudpc-control`. Restart an existing
Pulse process once to pick up its new supplementary group when using the client.

The service starts on boot. Reconnect using the same endpoint and token.
`systemctl restart pulse-cloudpc` reuses `/var/lib/pulse-cloudpc`: workspace,
Chromium profile, session cookies, last URL, runtime identity, and shell HOME.
Closing the client does not close the runtime. Power-loss checkpoints occur
after successful browser actions and graceful shutdown. A provider can still
expire or revoke a login. Open tabs' transient JavaScript memory is not preserved.

For remote development, use an SSH tunnel, never a public nginx proxy:

```sh
ssh -N -L 8792:127.0.0.1:8792 YOUR_VPS_SSH_ALIAS
```

## Pulse interface

`client.py` exposes `CloudPC().request(endpoint, optional_payload)`.
Use it from trusted server-side code, or send authenticated HTTP requests locally.
The client reads the token from disk without exposing it in command arguments.

| Method | Endpoint | JSON payload / result |
|---|---|---|
| GET | `/v1/health` | Persistent runtime ID, process start time, health |
| POST | `/v1/browser/navigate` | `{"url":"https://example.com"}` |
| POST | `/v1/browser/click` | `{"selector":"button"}` |
| POST | `/v1/browser/type` | `{"selector":"input","text":"hello"}` |
| POST | `/v1/browser/describe` | `{"selector":"button"}`; approval context/fingerprints |
| GET | `/v1/browser/observe` | Bounded visible text and actionable selectors |
| GET | `/v1/browser/state` | Current URL and title |
| GET | `/v1/browser/screenshot` | Private PNG bytes |
| POST | `/v1/shell` | `{"command":"pwd","timeout":30,"cwd":"subdirectory"}`; cwd optional |
| POST | `/v1/files/read` | `{"path":"notes/example.txt"}` |
| POST | `/v1/files/write` | `{"path":"notes/example.txt","content":"hello","overwrite":false}` |

File API paths stay inside `workspace/`, including resolved symlinks.
Reads accept `start_line`/`max_lines`; read/write accept `encoding: "base64"`
for bounded binary files. `expected_sha256` rejects stale writes. Creation and
replacement are atomic. Click/type can carry `_binding` from describe; changed
page, element or form state rejects the action.
Shell is intentionally powerful but executes as the dedicated Linux user, with
bounded time/output and without Pulse provider secrets in its environment.
Requests serialize onto one browser to prevent competing callers corrupting it.
Screenshots use a fixed 1280x800 viewport. Downloads and password saving are
disabled. Profile/session files are private, outside the file API workspace.
There are no public controls, reasoning loop, model calls, or autonomous actions.

On Ubuntu, installation includes a narrowly scoped root-owned AppArmor profile
for Chromium's user-namespace sandbox. System-wide namespace restrictions remain
enabled. See Chromium's [official guidance](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).

Chromium uses Playwright's [persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).
Local storage lives in its profile. Private cookie checkpoints also restore
session cookies on a graceful restart, which a profile alone may not restore.

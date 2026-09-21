# pulse v0.2

Pulse is a private, iPhone-first notification layer for `pulse.moralife.uk`, tuned for Aiden rather than a generic news feed.
It stores source events in SQLite, applies deterministic relevance rules, and sends Web Push notifications. The PWA is served by the same Flask service as the API so a notification can open `/event/<id>` on the same origin.

## local setup

```text
cd pulse
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
python -m pulse_app.cli secret
python -m pulse_app.cli password
python -m pulse_app.cli vapid
python -m pulse_app.cli init-db
PULSE_ENV=development PULSE_DEV_NO_AUTH=1 .venv/bin/flask --app 'pulse_app:create_app()' run --port 8791
```

For Windows PowerShell, use `$env:PULSE_DEV_NO_AUTH='1'` instead of the inline environment prefix. Production must use a password hash and a real secret; development bypass is not enabled by the app unless explicitly added to the environment.

## first real push path

1. deploy the service over HTTPS and configure the VAPID values in `/etc/pulse.env`.
2. open `https://pulse.moralife.uk` and log in.
3. on iPhone, use Safari's Share menu and choose **Add to Home Screen**, then open Pulse from the Home Screen.
4. tap **enable alerts** from inside the installed web app.
5. tap **send test**. The server creates one event, sends one Web Push payload, and the service worker opens `/event/<id>` when the notification is tapped.

The worker keeps a 15-minute cadence for direct watchers: La Ceja weather, USGS earthquakes, the official Apple Newsroom feed, the official Call of Duty blog URL, official OpenAI and Discord RSS feeds, official GitHub/OpenAI/Discord incident feeds, targeted Google News RSS queries for Rocket League, Instagram, and Unstable SMP/Universe, a Colombia RSS search feed, configured GitHub repositories, and optional custom RSS/URL watchers. Major Rocket League, Unstable SMP/Universe, Instagram, iOS, ChatGPT, Apple, Discord, Warzone, and project stories are represented as strict domain-specific profiles for optional discovery; discovery never pushes solely because search found a result. The first successful source check seeds history without sending a notification for every old item. Notification policy is separate from checking: strict server-side score floors, freshness expiry, per-topic/event cooldowns, cross-source clustering, a two-alert run budget, and quiet hours decide whether a stored event becomes a push. Low-value, stale, muted, or duplicate events remain in history with a suppression reason. Weather looks 24 hours ahead by default and only keeps meaningful storm, heavy-rain, heat, cold, or dangerous-wind alerts; rain and wind are folded into a storm alert when appropriate. Earthquakes are retained for history but only local felt-impact or major events can notify. Notification copy is rewritten into a short title plus a reason it matters instead of forwarding raw feed text.

Personal intelligence is stored in the same pipeline: package and purchase lifecycles merge by shipment/order identity, calendar context can elevate weather that may affect a near-term located plan, related topics provide weak explainable relevance signals, and a followed story boosts only its canonical event/cluster for 30 days. Authenticated Apple/Google contact records retain only minimal identity fields and can be marked important, which slightly elevates matching messages. Optional gaming countdowns can be created through `POST /api/game-events`; the worker evaluates them normally and emits at most one 24-hour and one 1-hour event per configured countdown. The authenticated phone UI keeps the main screen compact, with connections, priorities, temporary topic boosts, countdowns, learning feedback, audit traces, and password management behind progressive disclosure.

For eligible Gmail/iCloud mail events, the event page can request a short review-only reply draft. Pulse sends only the stored sender, subject, and bounded summary to the configured AI helper, never sends the reply, and falls back to a deterministic draft if AI is unavailable. There is no automatic communication action.

Optional discovery uses Brave Search server-side. It runs on its own slower cadence, three hours by default, and can run a short contextual search when a high-priority watcher changes. Every promising result is fetched and read before it becomes an event. Results keep source trust (`primary`, `reliable_secondary`, or `community`), confidence (`confirmed`, `likely`, or `rumor`), the source list, publication time when available, and matched tracked entities. A single community rumor is retained as history but suppressed from notification.

Set `PULSE_BRAVE_SEARCH_API_KEY` to enable it. The key is never returned by the API or sent to the PWA. Profiles and tracked entities can be edited inside Pulse settings after the first deployment; environment JSON is available as a server-side fallback. Discovery is intentionally disabled when the key is blank.

For repositories you control, set `PULSE_GITHUB_WEBHOOK_SECRET` and configure GitHub to send signed `push`, `release`, `issues`, and `pull_request` deliveries to `https://pulse.moralife.uk/api/webhooks/github`. Pulse validates the signature, ignores repositories outside `PULSE_GITHUB_REPOS`, deduplicates delivery IDs, and keeps the 15-minute GitHub release watcher as a fallback.

The authenticated debug view and `/api/audit` retain the decision trace for both pushed and suppressed events. `/api/people` exposes the minimal synchronized contact list for marking importance, `/api/packages` and `/api/purchases` expose lifecycle state, and `/api/purchases/<id>/watch` sets a small purchase watch priority. These endpoints remain private and do not expose provider credentials or full message bodies.

## production deployment

The VPS pattern matches `family-chores`: one private gunicorn listener, one worker service, nginx in front, and SQLite in `/var/lib/pulse`.

```text
sudo useradd --system --home /opt/pulse --shell /usr/sbin/nologin pulse
sudo mkdir -p /opt/pulse /var/lib/pulse
sudo chown -R pulse:pulse /opt/pulse /var/lib/pulse
sudo -u pulse git clone --branch family-chore-v1 https://github.com/aidenERM/mora.git /opt/pulse
cd /opt/pulse/pulse
sudo -u pulse python3 -m venv /opt/pulse/.venv
sudo -u pulse /opt/pulse/.venv/bin/pip install -r requirements.txt
sudo install -m 600 .env.example /etc/pulse.env
sudoedit /etc/pulse.env
sudo -u pulse env PULSE_ENV=production /opt/pulse/.venv/bin/python -m pulse_app.cli init-db
sudo install -m 644 systemd/pulse.service /etc/systemd/system/pulse.service
sudo install -m 644 systemd/pulse-worker.service /etc/systemd/system/pulse-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now pulse pulse-worker
sudo install -m 644 nginx/pulse.moralife.uk.conf.example /etc/nginx/sites-available/pulse.moralife.uk
sudo ln -s /etc/nginx/sites-available/pulse.moralife.uk /etc/nginx/sites-enabled/pulse.moralife.uk
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d pulse.moralife.uk
```

If nginx already has a certificate-backed `pulse.moralife.uk` server block, replace its upstream with `127.0.0.1:8791` rather than creating a duplicate `listen 443` block. Keep Cloudflare proxying disabled until origin HTTPS is valid, then enable it if desired.

After deployment:

```text
curl -fsS https://pulse.moralife.uk/api/health
sudo systemctl --no-pager --full status pulse pulse-worker
```

## secrets and backups

Never commit `.env`, VAPID private keys, the Pulse password, GitHub tokens, shortcut tokens, or `data/pulse.sqlite3`. The repository only contains `.env.example`. Back up `/var/lib/pulse/pulse.sqlite3` with the same care as the existing family-chores database.

## iPhone limitations

- iOS/iPadOS 16.4+ is required for Web Push for Home Screen web apps.
- the app must be added to the Home Screen; Safari's normal tab is not the supported push target.
- permission must be requested directly from a user gesture, so Pulse only subscribes from the **enable alerts** button.
- the service worker displays a visible notification immediately. Safari does not support an invisible push that only performs background work.
- the notification contains a same-origin `/event/<id>` URL. The service worker focuses or navigates the existing Pulse window, with `clients.openWindow()` as the fallback.
- iOS Focus, lock-screen grouping, exact truncation, and time-sensitive presentation still require on-device human QA; Pulse does not claim to bypass Focus or Do Not Disturb.
- Web Push uses the shared Pulse icon on iOS. Dynamic per-topic notification icons and custom notification sounds are not assumed where WebKit does not expose them.

## unsupported social boundaries

Pulse does not scrape personal Instagram, TikTok, or WhatsApp accounts, use user tokens, or read arbitrary Discord messages through OAuth. Public major-news discovery is supported where sources can be validated; private social-message awareness requires an official provider capability or an explicit supported integration later.

## shortcuts

`shortcuts/pulse-phone-context.cherri` is the thin optional source. Cherri is a current compiler that can produce runnable `.shortcut` files, but this Windows checkout cannot sign or import-test the result on an Apple device. Compile and inspect it on macOS before treating it as an importable artifact. A real shortcut token must be inserted locally and must never be committed.

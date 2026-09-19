# pulse v0.1

Pulse is a private, iPhone-first notification layer for `pulse.moralife.uk`.
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

The worker checks weather, the official Apple Newsroom feed, the official Call of Duty blog URL, a Colombia RSS search feed, configured GitHub repositories, and optional custom RSS/URL watchers. The first successful source check seeds history without sending a notification for every old item.

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

## shortcuts

`shortcuts/pulse-phone-context.cherri` is the thin optional source. Cherri is a current compiler that can produce runnable `.shortcut` files, but this Windows checkout cannot sign or import-test the result on an Apple device. Compile and inspect it on macOS before treating it as an importable artifact. A real shortcut token must be inserted locally and must never be committed.

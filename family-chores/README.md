# family chores v1

small private family chore system for home.moralife.uk.

## setup

    cd family-chores
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env
    # edit .env with real values
    set -a; . ./.env; set +a
    python -m family_chores.cli init-db
    python -m family_chores.cli make-qr
    .venv/bin/flask --app 'family_chores:create_app()' run

Open /admin, set the Week A anchor, then add people, chores, assignments, and exceptions.

## VPS deployment

    sudo useradd --system --home /opt/home-moralife --shell /usr/sbin/nologin home-moralife
    sudo mkdir -p /opt/home-moralife /var/lib/home-moralife
    sudo chown -R home-moralife:home-moralife /opt/home-moralife /var/lib/home-moralife
    sudo -u home-moralife git clone --branch family-chore-v1 https://github.com/aidenERM/mora.git /opt/home-moralife
    cd /opt/home-moralife/family-chores
    sudo -u home-moralife python3 -m venv /opt/home-moralife/.venv
    sudo -u home-moralife /opt/home-moralife/.venv/bin/pip install -r requirements.txt
    sudo install -m 600 .env.example /etc/home-moralife.env
    sudoedit /etc/home-moralife.env
    sudo -u home-moralife /opt/home-moralife/.venv/bin/python -m family_chores.cli init-db
    sudo -u home-moralife /opt/home-moralife/.venv/bin/python -m family_chores.cli make-qr
    sudo install -m 644 systemd/home-moralife.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now home-moralife
    sudo install -m 644 nginx/home.moralife.uk.conf.example /etc/nginx/sites-available/home.moralife.uk
    sudo ln -s /etc/nginx/sites-available/home.moralife.uk /etc/nginx/sites-enabled/home.moralife.uk
    sudo nginx -t && sudo systemctl reload nginx
    sudo certbot --nginx -d home.moralife.uk

The service listens only on 127.0.0.1:8787. SQLite is stored at /var/lib/home-moralife/chores.sqlite3. Backup it with:

    sudo install -d -m 700 /var/backups/home-moralife
    sudo sqlite3 /var/lib/home-moralife/chores.sqlite3 ".backup '/var/backups/home-moralife/chores-$(date +%F).sqlite3'"

## secrets

Configure SECRET_KEY, FAMILY_PASSWORD, ADMIN_PASSWORD, DATABASE_PATH, HOME_NETWORKS, TIMEZONE, TRUST_PROXY, COOKIE_SECURE, and CALENDAR_HORIZON_DAYS only in /etc/home-moralife.env. Calendar tokens are generated randomly, stored as hashes, and revoked by deleting or revoking their database rows.

## Cloudflare

Add only: A record, name home, IPv4 109.205.176.230. Use DNS-only while nginx and certbot are being configured; proxy it later if desired. Do not change moralife.uk, www, or api.

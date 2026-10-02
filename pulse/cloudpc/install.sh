#!/bin/bash
# Run as root after copying this directory to /opt/pulse-cloudpc.
set -eu
test "$(id -u)" = 0
cd /opt/pulse-cloudpc
getent group pulse-cloudpc-control >/dev/null || groupadd --system pulse-cloudpc-control
id pulse-cloudpc >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/pulse-cloudpc --shell /usr/sbin/nologin pulse-cloudpc
usermod -a -G pulse-cloudpc-control pulse-cloudpc
if id pulse >/dev/null 2>&1; then usermod -a -G pulse-cloudpc-control pulse; fi
install -d -m 750 -o root -g pulse-cloudpc-control /etc/pulse-cloudpc
if ! test -f /etc/pulse-cloudpc/token; then
    python3 - <<'PY'
import os, secrets
fd = os.open('/etc/pulse-cloudpc/token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write(secrets.token_urlsafe(48) + '\n')
PY
fi
chown root:pulse-cloudpc-control /etc/pulse-cloudpc/token
chmod 640 /etc/pulse-cloudpc/token
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/opt/pulse-cloudpc/browsers .venv/bin/python -m playwright install --with-deps chromium
chmod -R a+rX /opt/pulse-cloudpc
if command -v apparmor_parser >/dev/null && test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
    install -m 644 pulse-cloudpc-chromium.apparmor /etc/apparmor.d/pulse-cloudpc-chromium
    apparmor_parser -r /etc/apparmor.d/pulse-cloudpc-chromium
fi
install -d -m 700 -o pulse-cloudpc -g pulse-cloudpc /var/lib/pulse-cloudpc
install -m 644 pulse-cloudpc.service /etc/systemd/system/pulse-cloudpc.service
systemctl daemon-reload
systemctl enable --now pulse-cloudpc

# uptime-watch

A small uptime checker in one Bash script. A systemd timer runs it once a minute.
When a check fails 3 times in a row, it sends an alert to Telegram (and email, if set up).
When the check passes again, it sends a "back up" message with how long it was down.

## What it checks

| Type | Target | OK when |
|---|---|---|
| `http` | URL | page answers 2xx or 3xx |
| `render` | URL | like `http`, and the answer comes from Render (`x-render-origin-server` header) |
| `cert` | host | TLS certificate is valid for at least `CERT_MIN_DAYS` (14) more days |
| `dnscf` | host | all A records point to Cloudflare, so the proxy is still on |
| `mx` | `domain=host` | the expected host is in the MX records |
| `tcp` | `host:port` | the port is open |

It only sends GET requests and TLS handshakes. It never submits forms.
If the checking server itself has no network, it skips the run and does not send false alerts.

## Install

Needs `bash`, `curl`, `dig`, `openssl`, `nc` and GNU `date` (any normal Linux server).

```bash
sudo mkdir -p /opt/uptime-watch /etc/uptime-watch
sudo cp check.sh /opt/uptime-watch/ && sudo chmod +x /opt/uptime-watch/check.sh
sudo cp env.example /etc/uptime-watch/env && sudo chmod 600 /etc/uptime-watch/env
sudo cp targets.example /etc/uptime-watch/targets
# edit env (bot token, chat id) and targets (your sites)
sudo cp uptime-watch.service uptime-watch.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now uptime-watch.timer
```

Run once by hand: `sudo /opt/uptime-watch/check.sh`. It prints only failures.

## Settings

Environment variables, all optional: `FAILS_TO_ALERT` (3), `CERT_MIN_DAYS` (14), `LABEL` (uptime),
`CONF_DIR` (`/etc/uptime-watch`), `STATE_DIR` (`/var/lib/uptime-watch`).

Tip: run it from a different server than the one you watch, so it still works when that server is down.

# Deployment Guide

This guide describes a production-style Blueshout deployment on a small Linux server using Gunicorn behind a reverse proxy.

The examples assume:

- Application directory: `/home/blueshout/voice`
- Service user: `blueshout`
- App bind address: `127.0.0.1:5000`
- Public domain: `blueshout.example.com`
- Piper models directory: `/opt/piper/models`

Adjust paths and domain names for your own server.

## 1. Server Prerequisites

Install system packages:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl
```

Create a dedicated user if one does not exist:

```bash
sudo adduser --system --group --home /home/blueshout blueshout
```

Clone the repository:

```bash
sudo -u blueshout git clone https://github.com/Eddienews/blueshout.git /home/blueshout/voice
cd /home/blueshout/voice
```

Create the virtual environment and install Python dependencies:

```bash
sudo -u blueshout python3 -m venv .venv
sudo -u blueshout .venv/bin/pip install --upgrade pip
sudo -u blueshout .venv/bin/pip install -r requirements.txt
```

## 2. Environment File

Create a private `.env` file from the example:

```bash
sudo -u blueshout cp .env.example .env
sudo -u blueshout chmod 600 .env
```

Edit `.env` and set at least:

```bash
SECRET_KEY=replace-with-a-long-random-secret
SESSION_COOKIE_SECURE=1
SESSION_COOKIE_SAMESITE=Lax
PIPER_BIN=/usr/local/bin/piper
PIPER_MODELS_DIR=/opt/piper/models
```

Generate a strong secret with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Never commit `.env`. It may contain production secrets.

## 3. Piper Setup

Install Piper separately and place voice models under `/opt/piper/models`.

Each voice usually needs two files:

```text
/opt/piper/models/en_US-amy-medium.onnx
/opt/piper/models/en_US-amy-medium.onnx.json
```

Blueshout automatically discovers installed `*.onnx` files. You can confirm what the app sees with:

```bash
curl -sS http://127.0.0.1:5000/api/tts_caps
```

Optional language configuration:

```bash
PIPER_LANGUAGES=nl,pl,sv,ru,uk
PIPER_NL=/opt/piper/models/nl_NL-alex-medium
PIPER_VOICES=pl=/opt/piper/models/pl_PL-darkman-medium,sv=/opt/piper/models/sv_SE-alma-medium
```

Use model base paths without the `.onnx` suffix.

## 4. Gunicorn Smoke Test

Before systemd, run Gunicorn manually:

```bash
set -a
. ./.env
set +a
.venv/bin/gunicorn --workers 1 --threads 8 --timeout 180 -b 127.0.0.1:5000 app:app
```

In another terminal:

```bash
curl -sS http://127.0.0.1:5000/api/health
curl -sS http://127.0.0.1:5000/api/tts_caps
```

Stop the manual Gunicorn process after the smoke test.

## 5. systemd Service

Create `/etc/systemd/system/blueshout-voice.service`:

```ini
[Unit]
Description=Blueshout Voice (Gunicorn)
After=network.target

[Service]
User=blueshout
Group=blueshout
WorkingDirectory=/home/blueshout/voice
Environment="PATH=/home/blueshout/voice/.venv/bin"
EnvironmentFile=-/home/blueshout/voice/.env
ExecStart=/home/blueshout/voice/.venv/bin/gunicorn --workers 1 --threads 8 --timeout 180 -b 127.0.0.1:5000 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable blueshout-voice.service
sudo systemctl restart blueshout-voice.service
sudo systemctl status blueshout-voice.service
```

View logs:

```bash
sudo journalctl -u blueshout-voice.service -f
```

## 6. Reverse Proxy

### Nginx Example

```nginx
server {
    listen 80;
    server_name blueshout.example.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

### Apache Example

Enable proxy modules if needed:

```bash
sudo a2enmod proxy proxy_http headers ssl rewrite
sudo systemctl reload apache2
```

Virtual host example:

```apache
<VirtualHost *:80>
    ServerName blueshout.example.com

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:5000/
    ProxyPassReverse / http://127.0.0.1:5000/

    RequestHeader set X-Forwarded-Proto "http"
</VirtualHost>
```

## 7. HTTPS

Use HTTPS in production. With Certbot and Nginx:

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d blueshout.example.com
```

With Certbot and Apache:

```bash
sudo apt-get install -y certbot python3-certbot-apache
sudo certbot --apache -d blueshout.example.com
```

Keep `SESSION_COOKIE_SECURE=1` when serving over HTTPS.

## 8. Production Checks

Run these after every deployment:

```bash
curl -sS https://blueshout.example.com/api/health
curl -sS https://blueshout.example.com/api/tts_caps
systemctl is-active blueshout-voice.service
```

Expected health response:

```json
{"ok": true}
```

Security headers should include `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`, and `Content-Security-Policy`.

## 9. Updating an Existing Deployment

```bash
cd /home/blueshout/voice
sudo -u blueshout git pull --ff-only
sudo -u blueshout .venv/bin/pip install -r requirements.txt
sudo systemctl restart blueshout-voice.service
curl -sS https://blueshout.example.com/api/health
```

If you install new Piper models, restart the service so the app reloads the available voice catalog.

## 10. Operations Notes

- `SECRET_KEY` must remain stable across restarts, or existing sessions will expire.
- Bluesky auth tokens are kept server-side in process memory; restarting the service logs users out.
- The in-memory session/cache design is intentionally simple for small deployments.
- If traffic grows, add persistent session/cache storage such as Redis before scaling to multiple workers or multiple servers.
- TTS is CPU-heavy. Tune `TTS_MAX_CONCURRENT`, `TTS_RATE_LIMIT`, and `TTS_RATE_WINDOW` for your machine.

## Troubleshooting

### `RuntimeError: Set SECRET_KEY`

The service did not load `.env`, or the file does not define `SECRET_KEY`.

Check:

```bash
sudo systemctl cat blueshout-voice.service
sudo ls -l /home/blueshout/voice/.env
```

### `/api/tts` returns `modelo Piper nao encontrado`

The selected voice model is not present or the configured path is wrong.

Check:

```bash
ls -lh /opt/piper/models
curl -sS http://127.0.0.1:5000/api/tts_caps
```

### Browser login works but audio does not start on mobile

Some mobile browsers require a user gesture before audio can play. Tap `Start` again after login.

### 502 from the reverse proxy

Gunicorn may not be running or may be bound to a different port.

Check:

```bash
sudo systemctl status blueshout-voice.service
sudo journalctl -u blueshout-voice.service -n 100
ss -ltnp | grep 5000
```

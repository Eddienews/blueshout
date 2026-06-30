# Blueshout

Blueshout is a small Flask app that reads new Bluesky posts aloud. Users sign in with a Bluesky App Password, the backend polls the Bluesky AT Protocol API, and Piper generates server-side speech audio.

## Features

- Bluesky login using App Passwords
- Followed-post timeline polling
- Server-side Piper TTS
- Transcript cards in the browser
- Waiting music while no posts are queued
- Session cookies that store only an opaque session id; Bluesky JWTs stay server-side

## Security Notes

- Never ask users for their main Bluesky password. Use App Passwords only.
- Set `SECRET_KEY` in the environment before starting the app. The app intentionally refuses to boot without it unless `BLUESHOUT_ALLOW_INSECURE_DEV_SECRET=1` is set for local experiments.
- Keep `.env` out of git. Use `.env.example` as a template.
- `/api/tts` requires an authenticated session and is rate-limited per session.
- Production should run behind HTTPS with secure cookies enabled.

## Local Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env, set SECRET_KEY, and use SESSION_COOKIE_SECURE=0 for local HTTP
set -a
. ./.env
set +a
flask --app app run --host 127.0.0.1 --port 5000
```

## Piper

Install Piper separately and configure model paths with environment variables:

- `PIPER_BIN`
- `PIPER_MODELS_DIR`
- optional per-language overrides such as `PIPER_PT`, `PIPER_EN`, `PIPER_ES`

The defaults expect models under `/opt/piper/models`.

## Deployment

A typical deployment uses Gunicorn behind a reverse proxy:

```bash
gunicorn --workers 1 --threads 8 --timeout 180 -b 127.0.0.1:5000 app:app
```

For systemd, point `EnvironmentFile` at a private `.env` file and do not commit that file.

## License

MIT. All files in this repository, including the bundled logo and waiting-music assets, are licensed under MIT unless noted otherwise.

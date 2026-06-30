# Security Policy

## Supported Versions

Security updates target the `main` branch.

## Reporting a Vulnerability

Please do not open public issues for suspected vulnerabilities. Email the maintainer or use GitHub private vulnerability reporting when it is enabled for this repository.

Include enough detail to reproduce the issue, the affected route or file, and any logs or request examples that help explain the impact. Please do not include real Bluesky credentials, app passwords, JWTs, or production secrets.

## Operational Notes

- Set `SECRET_KEY` in production. The app refuses to start without it unless an explicit local-development override is enabled.
- Keep `.env` files private.
- Bluesky JWTs are stored server-side and should not be logged or returned to clients.
- `/api/tts` should remain authenticated and rate-limited because Piper speech generation is CPU-expensive.

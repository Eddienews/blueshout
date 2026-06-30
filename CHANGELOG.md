# Changelog

All notable changes to Blueshout will be documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses semantic versioning once tagged releases begin.

## Unreleased

### Added

- Added an extensible Piper voice catalog and a web app voice-language selector.
- Added automatic discovery of installed Piper `.onnx` models and documentation for adding new language voices.

### Changed

- Improved language fallback so posts use the detected language when a matching voice is installed, or a selected/default voice when not.

## 0.1.0 - 2026-06-30

### Added

- Public README with demo link, screenshot, setup instructions, and project roadmap.
- GitHub Actions CI for Python 3.10, 3.11, and 3.12.
- Repository governance files: contribution guide, code of conduct, security policy, issue templates, pull request template, and Dependabot configuration.

### Changed

- Prepared Blueshout for open source distribution under the MIT License.
- Updated Python and GitHub Actions dependencies through Dependabot.

### Security

- Moved Bluesky authentication tokens out of the browser session cookie and into server-side memory.
- Required `SECRET_KEY` to be provided through the environment.
- Added secure session defaults, production security headers, TTS authentication, rate limiting, and concurrency controls.

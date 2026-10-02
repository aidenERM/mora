# Repository Guidelines

## Project Structure & Module Organization

This repository contains several independently deployed applications:

- `index.html`, `privacy/`, and `terms/`: static Mora website. `.github/workflows/pages.yml` publishes repository content to GitHub Pages on pushes to `main`.
- `pulse/pulse_app/`: Flask API, SQLite storage, relevance rules, integrations, and notification worker. Browser assets and Python tests live under `pulse/`.
- `pulse/cloudpc/`: private Linux browser/shell runtime, internal client, systemd unit, installer, and verification script.
- `pulse/orbit/`: persistent agent tasks and provider-independent execution; consult `STATE.md` before continuing Orbit work.
- `pulse/shortcuts/`: Cherri sources and compiled Apple Shortcut artifacts. `pulse/ios-companion/` contains companion Swift source.
- `family-chores/family_chores/` and `family-chores/tests/`: separate Flask application and tests.

Keep changes within the relevant application. Preserve unrelated working-tree edits.

## Build, Test, and Development Commands

Create a virtual environment and install the relevant application's `requirements.txt`; install `pytest` separately for tests.

- From `pulse/`, run `python -m pulse_app.cli init-db` to initialize the configured database.
- From `pulse/`, run `flask --app 'pulse_app:create_app()' run --port 8791` for local development after configuring a local environment.
- From the repository root, run `python -m pytest pulse/tests family-chores/tests` for both application suites, or select one directory.
- Run `powershell -ExecutionPolicy Bypass -File pulse/shortcuts/build.ps1` to rebuild Shortcut artifacts.
- On the provisioned Linux VPS, run `python3 /opt/pulse-cloudpc/verify.py --restart-service` to verify Cloud PC persistence. This restarts that service and requires root.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and variables, and `UPPER_CASE` constants. Follow surrounding JavaScript, HTML, and Swift conventions. No repository-wide formatter or linter is configured. Avoid unrelated formatting changes; run `git diff --check` before submission.

## Testing Guidelines

Use pytest files named `test_*.py` and functions named `test_*`. Add meaningful regression coverage for changed behavior, especially authentication, deduplication, scoring, and persistence. No numeric coverage target is configured. Distinguish automated checks from production or physical-iPhone verification.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects matching history, such as `Add private persistent Pulse Cloud PC runtime`. Keep commits focused. PR descriptions should explain behavior changes, validation, and deployment requirements; include mobile screenshots for UI changes and link relevant issues.

## Security & Configuration

Never commit `.env`, credentials, tokens, databases, browser profiles, or cookies. Document configuration names without values. Keep Cloud PC controls authenticated and bound to localhost. Back up production SQLite data before schema changes; preserve Pulse's 15-minute worker cadence.

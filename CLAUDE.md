# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Venv at `venv/` (Windows). Launchers auto-create/activate it — see `scripts/run.bat` / `scripts/checks.bat`.

Install dependencies manually if needed:

```bash
venv\Scripts\activate && pip install -r requirements.dev.txt
```

## Running

Requires `.env` (see `.env.example`) — at minimum `API_TOKENS` (optional, empty = open API) and the `IMAP_<ACCOUNT>_*` / `SMTP_<ACCOUNT>_*` vars for each mailbox account. First run: set the dashboard login password with `python scripts/set_password.py`.

```bash
scripts\run.bat
```

Or via Docker:

```bash
docker compose up --build
```

Dashboard: `http://localhost:8000/ui` (login with the password set above). API: `http://localhost:8000` (see `API.md`).

## Architecture

FastAPI + NiceGUI app, following the `redberry-webapp-template` layout (see `@rules/uvicorn.md`, `@rules/fastapi-auth.md`, `@rules/nicegui.md` in the user's global config):

- `app/main.py` — the entrypoint and the FastAPI app (module-level `app = FastAPI(...)`), auth gate, rate limiting, NiceGUI mount, IMAP/SMTP endpoints.
- `app/mail.py` — business logic (IMAP/SMTP operations, request/response models). No FastAPI imports — unit-testable in isolation.
- `app/config.py` — `ConfigManager` (redberry-webkit), runtime-editable settings (`RATE_LIMIT`, `API_TOKENS`, `REFRESH_ENABLED`, `REFRESH_INTERVAL`, `METRICS_RETENTION_DAYS`), hot-reloaded from `.env`.
- `app/metrics.py` — `MetricsStore` (redberry-webkit), SQLite-backed log of every API call (timestamp, status, duration, error, and `extra={"endpoint", "account"}`).
- `app/ui/router.py` — `/login`, `/auth/login`, `/auth/logout` (cookie/JWT session via `AuthManager`).
- `app/ui/pages.py` — dashboard (`/ui`) showing request metrics/history, and settings (`/ui/config`).

**Auth — two independent mechanisms, per `@rules/fastapi-auth.md`:**
- Dashboard (`/ui/**`): cookie/JWT session, gated by the `_auth_gate` middleware in `app/main.py`. Password set via `python scripts/set_password.py`, stored in `data/auth.json`.
- API (everything else, e.g. `/folders`, `/messages/*`): `Authorization: Bearer <token>` checked against `API_TOKENS` (comma-separated, configurable from `.env` or the dashboard's settings page). **Opt-in, not mandatory** — if `API_TOKENS` is empty the API is open. This is a deliberate deviation from the original `X-API-Key`-always-required scheme; set `API_TOKENS` before exposing the service beyond localhost/trusted network.

**Credentials** are stored server-side in `.env` — never passed in request bodies. Each request includes only `"account": "<name>"`. The server resolves credentials via env vars prefixed by account name:

- IMAP: `IMAP_<ACCOUNT>_HOST`, `IMAP_<ACCOUNT>_PORT`, `IMAP_<ACCOUNT>_USERNAME`, `IMAP_<ACCOUNT>_PASSWORD`, `IMAP_<ACCOUNT>_SSL`
- SMTP: `SMTP_<ACCOUNT>_HOST`, `SMTP_<ACCOUNT>_PORT`, `SMTP_<ACCOUNT>_USERNAME`, `SMTP_<ACCOUNT>_PASSWORD`, `SMTP_<ACCOUNT>_STARTTLS`, `SMTP_<ACCOUNT>_SSL`

Account name is uppercased automatically (e.g. `"danilo"` → `IMAP_DANILO_HOST`). Unknown account → 400.

**Endpoints**:

| Method | Path | Action |
|--------|------|--------|
| GET | `/health` | Liveness check (public) |
| GET | `/ui` | Dashboard — request metrics, history (cookie auth) |
| GET | `/ui/config` | Runtime settings (cookie auth) |
| POST | `/folders` | List IMAP folders |
| POST | `/messages/list` | List messages (envelope only), supports `since_uid` and `limit` |
| POST | `/messages/get` | Fetch full message by UID, optional `include_attachments` |
| POST | `/messages/search` | Search by criteria dict |
| POST | `/messages/delete` | Delete by UID list + expunge |
| POST | `/messages/move` | Move UIDs (MOVE command, falls back to COPY+DELETE) |
| POST | `/messages/flag` | Set `action: "read"` or `"unread"` |
| POST | `/messages/send` | Send via SMTP |

**Search criteria** (`/messages/search` → `criteria` dict keys): `unseen`, `seen`, `from`, `to`, `subject`, `since`, `before`, `body`, `raw` (arbitrary IMAP search string).

**`/messages/list` `limit`**: absent or `null` → 100; negative → all messages.

All IMAP operations use UID mode (`imap.uid(...)`) for stable message addressing across sessions. Blocking IMAP/SMTP calls run via `asyncio.to_thread` inside the async endpoints (`app/main.py`'s `_run_tracked`), which is also where every call gets logged to `MetricsStore`.

## Docker

`.github/workflows/docker-publish.yml` builds and pushes the image to `ghcr.io/daniloreddy/imap-rest` on push to `main` and on `v*.*.*` tags. Standard two-file split per `@rules/docker.md`:

- `docker-compose.yml` — production, pulls `ghcr.io/daniloreddy/imap-rest:latest`.
- `docker-compose-dev.yml` — local build (`build: .`) for testing before a push, `restart: "no"`.

`rsync.sh` (pushes the repo source to the remote host for a local build) predates the GHCR pipeline and is now redundant for deploys — the remote can instead run `docker compose pull && docker compose up -d` against the published image. Kept for now since it's still a valid way to test an unpushed local change on the remote; reconsider removing it once the GHCR flow is the only deploy path in practice.

## Deps

`requirements.txt` pulls `redberry-webkit` from `git+https://github.com/daniloreddy/redberry-webkit.git` (pinned tag) for `ConfigManager`, `MetricsStore`, `AuthManager`, timezone/env-resolver/logging helpers — same shared package used by other redberry web apps, not reimplemented per-project.

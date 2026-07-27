# IMAP REST

REST API per operazioni IMAP/SMTP (liste, ricerca, lettura, spostamento,
cancellazione, invio) su più account email, con dashboard web per monitorare
le richieste in arrivo. FastAPI + [NiceGUI](https://nicegui.io/), auth cookie
per la dashboard e Bearer token opzionale per l'API.

Le credenziali IMAP/SMTP restano server-side (`.env`) — i client chiamano
l'API passando solo un nome account (`"account": "danilo"`), mai le password.

Le funzioni condivise (auth, config runtime, metriche, log/env utils) vengono
da [`redberry-webkit`](https://github.com/daniloreddy/redberry-webkit); questo
repo aggiunge il cablaggio applicativo attorno a quel pacchetto.

## Funzionalità

- **API IMAP/SMTP** — liste, ricerca, lettura messaggi (con allegati), flag
  read/unread, move, delete, invio email. Vedi [`API.md`](API.md) per il
  riferimento completo di ogni endpoint.
- **Multi-account** — ogni richiesta specifica `account`, le credenziali sono
  risolte da variabili d'ambiente `IMAP_<ACCOUNT>_*` / `SMTP_<ACCOUNT>_*`.
- **Dashboard** (`/ui`, cookie/JWT auth) — richieste totali/ok/errori, durata
  media, storico ultime chiamate con endpoint e account coinvolti.
- **Auth API opzionale** — `Authorization: Bearer <token>` contro `API_TOKENS`
  (comma-separated); vuoto = API aperta (solo uso locale/rete fidata).
- **Rate limiting** (`slowapi`), configurabile a runtime da `/ui/config`.
- **Docker** — immagine pubblicata su GHCR via GitHub Actions, compose
  prod/dev.

## Avvio rapido (locale)

```bash
# Windows
scripts\run.bat --dev
```

Il primo avvio crea il virtual environment e installa le dipendenze. Prima di
avviare:

1. Copia `.env.example` in `.env` e configura almeno un account (`IMAP_<NOME>_*`,
   `SMTP_<NOME>_*` se serve inviare posta).
2. Imposta la password della dashboard:
   ```bash
   python scripts/set_password.py
   ```

Server su `http://127.0.0.1:8000`. Dashboard su `http://127.0.0.1:8000/ui`
(richiede login). API documentata in [`API.md`](API.md).

## Testare l'API

```bash
venv\Scripts\python.exe scripts\test_api.py --account danilo [--token <bearer>]
```

Script di smoke test end-to-end (stdlib, no dipendenze extra) contro un
server già in esecuzione — vedi commenti nello script per le opzioni. Non
esercita `/messages/move`, `/messages/delete`, `/messages/send` (mutano la
mailbox / inviano email reali): quelle si testano manualmente.

## Configurazione (`.env`)

Vedi `.env.example` per l'elenco completo. Variabili principali:

| Variabile | Default | Note |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind locale. |
| `PORT` | `8000` | |
| `DEV` | `false` | `true` abilita `--reload` uvicorn e riattiva `/docs`/`/redoc`. |
| `TZ` | `UTC` | Fuso orario IANA per i timestamp mostrati in dashboard. |
| `TRUSTED_PROXIES` | `127.0.0.1` | IP dei reverse proxy fidati per risolvere l'IP client reale. |
| `AUTH_SECURE_COOKIE` | `0` | `1` forza il flag `Secure` sul cookie anche senza `X-Forwarded-Proto: https`. |
| `API_TOKENS` | *(vuoto)* | Bearer token comma-separated per gli endpoint IMAP/SMTP. Vuoto = API aperta. |
| `RATE_LIMIT` | `20/minute` | Limite (sintassi slowapi) sugli endpoint API — hot-reload da `/ui/config`. |
| `REFRESH_ENABLED` / `REFRESH_INTERVAL` | `true` / `5` | Auto-refresh dashboard — hot-reload da `/ui/config`. |
| `NICEGUI_STORAGE_PATH` | *(vuoto)* | Solo Docker: `/app/data/.nicegui` per persistere il tema dark/light tra restart. |
| `IMAP_<ACCOUNT>_*` | — | `HOST`, `PORT` (993), `USERNAME`, `PASSWORD`, `SSL` (true). |
| `SMTP_<ACCOUNT>_*` | — | `HOST`, `PORT` (587), `USERNAME`, `PASSWORD`, `STARTTLS` (true), `SSL` (false). |

## Docker

```bash
# sviluppo (build locale)
docker compose -f docker-compose-dev.yml up --build

# produzione (immagine da GHCR, pubblicata da .github/workflows/docker-publish.yml)
docker compose up -d
```

Di default `docker-compose.yml` pubblica solo su `127.0.0.1`; imposta
`HOST=0.0.0.0` in `.env` per esporre su LAN/reverse proxy.

## Sviluppo

```bash
# Windows
scripts\checks.bat
```

Esegue `ruff check`, `mypy app` (strict) e `pytest` in sequenza.

## Struttura del progetto

```
app/
├── main.py         # FastAPI + lifespan (config reload, auth purge, metrics init) + auth gate +
│                   # rate limiting + endpoint IMAP/SMTP + mount NiceGUI
├── mail.py         # logica IMAP/SMTP pura (no import FastAPI) — modelli richiesta/risposta, operazioni
├── config.py       # ConfigManager (redberry_webkit) — RATE_LIMIT, API_TOKENS, REFRESH_*
├── metrics.py      # MetricsStore (redberry_webkit) legato a data/metrics.db
└── ui/
    ├── router.py   # /login /auth/login /auth/logout (AuthManager)
    └── pages.py    # dashboard (metriche + storico richieste) + pagina Config
static/login.html   # pagina di login self-contained
scripts/            # run/checks (bat+sh), set_password.py, test_api.py
tests/              # unit test su app/mail.py
data/               # auth.json, metrics.db, logs/ — gitignored
```

## Licenza

MIT — vedi [`LICENSE`](LICENSE).

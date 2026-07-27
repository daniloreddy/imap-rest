from dotenv import load_dotenv
from redberry_webkit.env_resolver import resolve_env_path

_env_path = resolve_env_path()
load_dotenv(_env_path)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import imaplib  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import secrets  # noqa: E402
import time  # noqa: E402
from collections.abc import AsyncIterator, Callable  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, TypeVar  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer  # noqa: E402
from nicegui import ui  # noqa: E402
from redberry_webkit.auth import client_ip, purge_loop  # noqa: E402
from redberry_webkit.logging_utils import CredentialFilter  # noqa: E402
from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402
from starlette.middleware.base import RequestResponseEndpoint  # noqa: E402

from app import mail  # noqa: E402
from app.config import config  # noqa: E402
from app.metrics import MetricsRecord, metrics  # noqa: E402
from app.ui.router import TRUSTED_PROXIES, auth  # noqa: E402
from app.ui.router import router as ui_router  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DEV = os.getenv("DEV", "false").lower() in ("true", "1", "yes")
CONFIG_RELOAD_INTERVAL_S = 5

_stream_handler = logging.StreamHandler()
_file_handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
_credential_filter = CredentialFilter()
_stream_handler.addFilter(_credential_filter)
_file_handler.addFilter(_credential_filter)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[_stream_handler, _file_handler],
)
logger = logging.getLogger(__name__)
logger.info("Using .env=%s", _env_path)


def _rate_limit_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return client_ip(request.headers, host, TRUSTED_PROXIES)


limiter = Limiter(key_func=_rate_limit_key)

_security = HTTPBearer(auto_error=False)


def verify_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str | None:
    """Add as Depends(verify_api_token) to any route outside /ui that needs protecting.
    No-op if API_TOKENS isn't configured — bearer auth is opt-in, not mandatory."""
    raw_api_tokens = config.get("API_TOKENS", "")
    tokens = {t.strip() for t in raw_api_tokens.split(",") if t.strip()}
    if raw_api_tokens.strip() and not tokens:
        logger.error("API_TOKENS is set but contains no valid tokens; denying all API requests")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API authentication misconfigured",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if tokens:
        if credentials is None or not any(
            secrets.compare_digest(credentials.credentials, t) for t in tokens
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return credentials.credentials if credentials else None


async def _config_reload_loop(interval_s: int) -> None:
    while True:
        await asyncio.sleep(interval_s)
        config.reload_if_stale()


def _crash_on_task_error(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical("Background task %s died unexpectedly, exiting", task.get_name(), exc_info=exc)
        os._exit(1)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await metrics.init_db()
    purge_task = asyncio.create_task(purge_loop(auth))
    purge_task.add_done_callback(_crash_on_task_error)
    config_task = asyncio.create_task(_config_reload_loop(CONFIG_RELOAD_INTERVAL_S))
    config_task.add_done_callback(_crash_on_task_error)
    yield
    purge_task.cancel()
    config_task.cancel()


app = FastAPI(
    title="IMAP REST",
    lifespan=_lifespan,
    docs_url="/docs" if DEV else None,
    redoc_url="/redoc" if DEV else None,
    openapi_url="/openapi.json" if DEV else None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)
app.include_router(ui_router)

_UI_PREFIX = "/ui"
_LOGIN_PATHS = {"/login", "/auth/login", "/auth/logout"}
_UI_BYPASS_PREFIXES = (f"{_UI_PREFIX}/_nicegui",)


@app.middleware("http")
async def _auth_gate(request: Request, call_next: RequestResponseEndpoint) -> Response:
    path = request.url.path
    if path in _LOGIN_PATHS or any(path.startswith(p) for p in _UI_BYPASS_PREFIXES):
        return await call_next(request)
    if path == _UI_PREFIX or path.startswith(_UI_PREFIX + "/"):
        token = request.cookies.get(auth.cookie_name, "")
        if auth.verify_token(token):
            return await call_next(request)
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


T = TypeVar("T")


async def _run_tracked(endpoint: str, account: str, fn: Callable[[], T]) -> T:
    """Run blocking IMAP/SMTP work off the event loop and record it in MetricsStore."""
    start = time.monotonic()
    try:
        result = await asyncio.to_thread(fn)
    except mail.AccountNotConfiguredError as exc:
        await metrics.record(
            MetricsRecord(
                timestamp=time.time(),
                status="error",
                duration_s=time.monotonic() - start,
                error_message=str(exc),
                extra={"endpoint": endpoint, "account": account},
            )
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except mail.InvalidFlagActionError as exc:
        await metrics.record(
            MetricsRecord(
                timestamp=time.time(),
                status="error",
                duration_s=time.monotonic() - start,
                error_message=str(exc),
                extra={"endpoint": endpoint, "account": account},
            )
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except mail.MessageNotFoundError as exc:
        await metrics.record(
            MetricsRecord(
                timestamp=time.time(),
                status="error",
                duration_s=time.monotonic() - start,
                error_message=str(exc),
                extra={"endpoint": endpoint, "account": account},
            )
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (imaplib.IMAP4.error, OSError, ValueError) as exc:
        logger.exception("error in %s (account=%s)", endpoint, account)
        await metrics.record(
            MetricsRecord(
                timestamp=time.time(),
                status="error",
                duration_s=time.monotonic() - start,
                error_message=str(exc),
                extra={"endpoint": endpoint, "account": account},
            )
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    else:
        await metrics.record(
            MetricsRecord(
                timestamp=time.time(),
                status="ok",
                duration_s=time.monotonic() - start,
                extra={"endpoint": endpoint, "account": account},
            )
        )
        return result


@app.post("/folders")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def list_folders(
    request: Request, req: mail.ListFoldersRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/folders", req.account, lambda: mail.list_folders(req))


@app.post("/messages/list")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def list_messages(
    request: Request, req: mail.ListRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/list", req.account, lambda: mail.list_messages(req))


@app.post("/messages/get")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def get_message(
    request: Request, req: mail.GetRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/get", req.account, lambda: mail.get_message(req))


@app.post("/messages/search")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def search_messages(
    request: Request, req: mail.SearchRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/search", req.account, lambda: mail.search_messages(req))


@app.post("/messages/delete")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def delete_messages(
    request: Request, req: mail.DeleteRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/delete", req.account, lambda: mail.delete_messages(req))


@app.post("/messages/move")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def move_messages(
    request: Request, req: mail.MoveRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/move", req.account, lambda: mail.move_messages(req))


@app.post("/messages/flag")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def flag_messages(
    request: Request, req: mail.FlagRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/flag", req.account, lambda: mail.flag_messages(req))


@app.post("/messages/send")
@limiter.limit(lambda: config.get("RATE_LIMIT", "20/minute"))
async def send_message(
    request: Request, req: mail.SendRequest, token: str | None = Depends(verify_api_token)
) -> dict[str, Any]:
    return await _run_tracked("/messages/send", req.account, lambda: mail.send_message(req))


from app.ui import pages as _ui_pages  # noqa: E402,F401

ui.run_with(app, mount_path="/ui", storage_secret=auth.ui_storage_secret)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IMAP REST — FastAPI + NiceGUI web app")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--host", type=str, default=HOST)
    parser.add_argument("--dev", action=argparse.BooleanOptionalAction, default=DEV)
    parser.add_argument("--env-file", type=str, default=None)
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
        reload_dirs=[str(PROJECT_ROOT / "app"), str(PROJECT_ROOT / "static")] if args.dev else None,
        loop="asyncio",
    )

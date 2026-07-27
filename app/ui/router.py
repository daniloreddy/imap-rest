from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, RedirectResponse
from redberry_webkit.auth import AuthManager, client_ip, is_secure_context

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
DATA_DIR = PROJECT_ROOT / "data"

TRUSTED_PROXIES = {ip.strip() for ip in os.getenv("TRUSTED_PROXIES", "127.0.0.1").split(",") if ip.strip()}
_FORCE_SECURE_COOKIE = os.getenv("AUTH_SECURE_COOKIE", "0").strip().lower() in ("1", "true", "yes")

auth = AuthManager(auth_file=DATA_DIR / "auth.json", cookie_name="imap_rest_session", token_ttl=7 * 24 * 3600)

router = APIRouter()


def _get_client_ip(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return client_ip(request.headers, host, TRUSTED_PROXIES)


@router.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@router.post("/auth/login")
async def auth_login(request: Request, password: str = Form(...)) -> RedirectResponse:
    ip = _get_client_ip(request)

    if not auth.has_password():
        return RedirectResponse(url="/login?error=nopassword", status_code=303)
    if auth.is_ip_blocked(ip):
        return RedirectResponse(url="/login?error=blocked", status_code=303)
    if auth.is_global_limited():
        logger.warning("elevated login attempt volume across IPs; latest attempt from ip=%s", ip)

    success = await asyncio.to_thread(auth.verify_password, password)
    auth.record_attempt(ip, success=success)
    if not success:
        return RedirectResponse(url="/login?error=invalid", status_code=303)

    token = auth.create_token()
    response = RedirectResponse(url="/ui/", status_code=303)
    response.set_cookie(
        auth.cookie_name,
        token,
        httponly=True,
        samesite="strict",
        secure=_FORCE_SECURE_COOKIE or is_secure_context(request.headers),
        max_age=auth.token_ttl,
    )
    return response


@router.get("/auth/logout")
async def auth_logout(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        auth.cookie_name,
        httponly=True,
        samesite="strict",
        secure=_FORCE_SECURE_COOKIE or is_secure_context(request.headers),
    )
    return response

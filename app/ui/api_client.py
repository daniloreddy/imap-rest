from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from app.config import config

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10.0


def _base_url() -> str:
    # PORT is boot-time-only (set once at process start, never hot-reloaded — see
    # @rules/uvicorn.md §1), same treatment as HOST/TZ: read directly from os.environ,
    # not through ConfigManager. Loopback is used regardless of the app's own HOST bind,
    # since 0.0.0.0 always accepts connections on 127.0.0.1 too.
    port = os.environ.get("PORT", "8000")
    return f"http://127.0.0.1:{port}"


def _auth_headers() -> dict[str, str]:
    tokens = config.get("API_TOKENS", "")
    first_token = tokens.split(",")[0].strip() if tokens else ""
    return {"Authorization": f"Bearer {first_token}"} if first_token else {}


async def _request(method: str, path: str, *, json: dict[str, Any] | None = None) -> tuple[int, Any]:
    async with httpx.AsyncClient(base_url=_base_url(), timeout=_TIMEOUT_S) as client:
        try:
            response = await client.request(method, path, json=json, headers=_auth_headers())
        except httpx.RequestError as exc:
            logger.error("Internal API self-call failed: %s %s (%s)", method, path, exc)
            raise
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body


async def health() -> tuple[int, Any]:
    return await _request("GET", "/health")


async def folders(account: str) -> tuple[int, Any]:
    return await _request("POST", "/folders", json={"account": account})


async def list_messages(account: str, folder: str = "INBOX", limit: int = 50) -> tuple[int, Any]:
    return await _request(
        "POST", "/messages/list", json={"account": account, "folder": folder, "limit": limit}
    )


async def get_message(account: str, folder: str, uid: str) -> tuple[int, Any]:
    return await _request(
        "POST",
        "/messages/get",
        json={"account": account, "folder": folder, "uid": uid, "include_attachments": False},
    )


async def search(account: str, folder: str, criteria: dict[str, Any], limit: int = 50) -> tuple[int, Any]:
    return await _request(
        "POST",
        "/messages/search",
        json={"account": account, "folder": folder, "criteria": criteria, "limit": limit},
    )

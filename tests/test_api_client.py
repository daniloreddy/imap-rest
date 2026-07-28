from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from redberry_webkit.config import ConfigManager

from app.config import _DEFAULTS, _SECRET_KEYS
from app.ui import api_client


@pytest.fixture
def set_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Same isolation pattern as tests/test_main.py's `set_config`: a real ConfigManager
    # pointed at a throwaway .env, never poking the private `config._cache` dict.
    def _set(**overrides: str) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in overrides.items()))
        test_config = ConfigManager(defaults=_DEFAULTS, secret_keys=_SECRET_KEYS, env_path=env_file)
        monkeypatch.setattr(api_client, "config", test_config)

    return _set


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Any = None, *, raise_on_json: bool = False) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self._raise_on_json = raise_on_json

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("not json")
        return self._json_body


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient: records the request and returns a canned response
    (or raises a canned httpx.RequestError), without ever touching the network."""

    response: _FakeResponse = _FakeResponse(200, {})
    error: httpx.RequestError | None = None
    calls: list[dict[str, Any]] = []

    def __init__(self, *, base_url: str, timeout: float) -> None:
        self.base_url = base_url
        self.timeout = timeout

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"method": method, "path": path, "json": json, "headers": headers})
        if _FakeAsyncClient.error is not None:
            raise _FakeAsyncClient.error
        return _FakeAsyncClient.response


@pytest.fixture(autouse=True)
def _fake_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse(200, {})
    _FakeAsyncClient.error = None
    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeAsyncClient)


def test_base_url_defaults_to_port_8000(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    assert api_client._base_url() == "http://127.0.0.1:8000"


def test_base_url_reads_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "9100")
    assert api_client._base_url() == "http://127.0.0.1:9100"


def test_auth_headers_empty_when_no_tokens_configured(set_config) -> None:
    set_config(API_TOKENS="")
    assert api_client._auth_headers() == {}


def test_auth_headers_uses_first_configured_token(set_config) -> None:
    set_config(API_TOKENS="first-token,second-token")
    assert api_client._auth_headers() == {"Authorization": "Bearer first-token"}


async def test_health_calls_get_health() -> None:
    _FakeAsyncClient.response = _FakeResponse(200, {"status": "ok"})
    status, body = await api_client.health()
    assert (status, body) == (200, {"status": "ok"})
    assert _FakeAsyncClient.calls[-1]["method"] == "GET"
    assert _FakeAsyncClient.calls[-1]["path"] == "/health"


async def test_folders_posts_account_payload() -> None:
    _FakeAsyncClient.response = _FakeResponse(200, {"folders": ["INBOX"]})
    status, body = await api_client.folders("danilo")
    assert (status, body) == (200, {"folders": ["INBOX"]})
    call = _FakeAsyncClient.calls[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/folders"
    assert call["json"] == {"account": "danilo"}


async def test_list_messages_posts_expected_payload() -> None:
    _FakeAsyncClient.response = _FakeResponse(200, {"count": 0, "messages": []})
    await api_client.list_messages("danilo", "INBOX", limit=5)
    call = _FakeAsyncClient.calls[-1]
    assert call["path"] == "/messages/list"
    assert call["json"] == {"account": "danilo", "folder": "INBOX", "limit": 5}


async def test_get_message_posts_expected_payload() -> None:
    _FakeAsyncClient.response = _FakeResponse(200, {"uid": "1"})
    await api_client.get_message("danilo", "INBOX", "42")
    call = _FakeAsyncClient.calls[-1]
    assert call["path"] == "/messages/get"
    assert call["json"] == {"account": "danilo", "folder": "INBOX", "uid": "42", "include_attachments": False}


async def test_search_posts_expected_payload() -> None:
    _FakeAsyncClient.response = _FakeResponse(200, {"count": 0, "messages": []})
    await api_client.search("danilo", "INBOX", {"unseen": True}, limit=3)
    call = _FakeAsyncClient.calls[-1]
    assert call["path"] == "/messages/search"
    assert call["json"] == {"account": "danilo", "folder": "INBOX", "criteria": {"unseen": True}, "limit": 3}


async def test_request_includes_auth_header_when_token_configured(set_config) -> None:
    set_config(API_TOKENS="secret-token")
    _FakeAsyncClient.response = _FakeResponse(200, {})
    await api_client.health()
    assert _FakeAsyncClient.calls[-1]["headers"] == {"Authorization": "Bearer secret-token"}


async def test_request_propagates_network_errors() -> None:
    _FakeAsyncClient.error = httpx.ConnectError("connection refused")
    with pytest.raises(httpx.RequestError):
        await api_client.health()


async def test_request_returns_none_body_on_non_json_response() -> None:
    _FakeAsyncClient.response = _FakeResponse(200, raise_on_json=True)
    status, body = await api_client.health()
    assert status == 200
    assert body is None

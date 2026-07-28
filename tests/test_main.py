from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from redberry_webkit.auth import AuthManager
from redberry_webkit.config import ConfigManager
from redberry_webkit.metrics import MetricsStore

import app.main as main_module
import app.ui.router as router_module
from app import mail
from app.config import _DEFAULTS, _SECRET_KEYS


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Must enter as a context manager, otherwise FastAPI's lifespan (metrics.init_db(),
    # background tasks) never runs.
    with TestClient(main_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
async def _isolated_metrics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests must not write to the project's real data/metrics.db.
    store = MetricsStore(db_path=tmp_path / "metrics.db")
    await store.init_db()
    monkeypatch.setattr(main_module, "metrics", store)


@pytest.fixture
def isolated_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthManager:
    auth = AuthManager(
        auth_file=tmp_path / "auth.json",
        cookie_name=router_module.auth.cookie_name,
        token_ttl=router_module.auth.token_ttl,
    )
    monkeypatch.setattr(main_module, "auth", auth)
    monkeypatch.setattr(router_module, "auth", auth)
    return auth


@pytest.fixture
def set_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    # Builds a real ConfigManager pointed at a throwaway .env instead of poking the
    # private `config._cache` dict — stays on ConfigManager's public constructor/API.
    def _set(**overrides: str) -> None:
        env_file = tmp_path / ".env"
        values = {"RATE_LIMIT": "20/minute", **overrides}
        env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        test_config = ConfigManager(defaults=_DEFAULTS, secret_keys=_SECRET_KEYS, env_path=env_file)
        monkeypatch.setattr(main_module, "config", test_config)

    return _set


@pytest.fixture(autouse=True)
def _default_rate_limit(set_config: Callable[..., None]) -> None:
    # tests must not depend on whatever RATE_LIMIT/API_TOKENS happen to be set in the real .env
    set_config()
    # the limiter's in-memory hit counts persist across tests (same "testclient" key) —
    # reset before each test so one test's requests can't push another into a 429
    main_module.limiter.reset()


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_redirects_to_ui(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_dashboard_requires_auth_redirects_to_login(client: TestClient) -> None:
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_docs_disabled_when_not_dev(client: TestClient) -> None:
    assert main_module.DEV is False
    response = client.get("/docs")
    assert response.status_code == 404


def test_login_flow(client: TestClient, isolated_auth: AuthManager) -> None:
    isolated_auth.set_password("test-password-123")
    response = client.post("/auth/login", data={"password": "test-password-123"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"
    assert isolated_auth.cookie_name in response.cookies


def test_login_flow_wrong_password(client: TestClient, isolated_auth: AuthManager) -> None:
    isolated_auth.set_password("test-password-123")
    response = client.post("/auth/login", data={"password": "wrong"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=invalid" in response.headers["location"]


def test_list_folders_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail, "list_folders", lambda req: {"folders": ["INBOX"]})
    response = client.post("/folders", json={"account": "danilo"})
    assert response.status_code == 200
    assert response.json() == {"folders": ["INBOX"]}


def test_list_folders_unknown_account_returns_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(req: mail.ListFoldersRequest) -> dict[str, object]:
        raise mail.AccountNotConfiguredError("IMAP", req.account)

    monkeypatch.setattr(mail, "list_folders", _raise)
    response = client.post("/folders", json={"account": "unknown"})
    assert response.status_code == 400


def test_folders_rate_limited(
    client: TestClient, set_config: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mail, "list_folders", lambda req: {"folders": []})
    set_config(RATE_LIMIT="2/minute")
    for _ in range(2):
        assert client.post("/folders", json={"account": "danilo"}).status_code == 200
    response = client.post("/folders", json={"account": "danilo"})
    assert response.status_code == 429


def test_api_token_required_when_configured(
    client: TestClient, set_config: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mail, "list_folders", lambda req: {"folders": []})
    set_config(API_TOKENS="secret-token")

    response = client.post("/folders", json={"account": "danilo"})
    assert response.status_code == 401

    response = client.post(
        "/folders", json={"account": "danilo"}, headers={"Authorization": "Bearer secret-token"}
    )
    assert response.status_code == 200


def test_api_token_matches_any_configured_token(
    client: TestClient, set_config: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the any()->sum() rewrite (C-04, timing oracle fix): a match on any
    # token in the set — not just the first — must still authenticate.
    monkeypatch.setattr(mail, "list_folders", lambda req: {"folders": []})
    set_config(API_TOKENS="first-token,second-token,third-token")

    response = client.post(
        "/folders", json={"account": "danilo"}, headers={"Authorization": "Bearer third-token"}
    )
    assert response.status_code == 200


def test_security_headers_present_on_normal_response(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_security_headers_present_on_auth_gate_redirect(client: TestClient) -> None:
    # _security_headers is registered after _auth_gate so it must still wrap the
    # short-circuited /login redirect the gate returns without calling call_next.
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_folders_timeout_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _slow(req: mail.ListFoldersRequest) -> dict[str, object]:
        time.sleep(0.2)
        return {"folders": []}

    monkeypatch.setattr(mail, "list_folders", _slow)
    monkeypatch.setattr(main_module, "_TASK_TIMEOUT_S", 0.05)
    response = client.post("/folders", json={"account": "danilo"})
    assert response.status_code == 503

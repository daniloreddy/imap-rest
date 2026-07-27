from __future__ import annotations

import pytest

from app.mail import (
    AccountNotConfiguredError,
    build_search_criteria,
    decode_str,
    get_imap_credentials,
)


def test_build_search_criteria_empty() -> None:
    assert build_search_criteria({}) == "ALL"


def test_build_search_criteria_combines_filters() -> None:
    criteria = build_search_criteria({"unseen": True, "from": "a@b.com", "subject": "invoice"})
    assert criteria == 'UNSEEN FROM "a@b.com" SUBJECT "invoice"'


def test_build_search_criteria_raw_passthrough() -> None:
    assert build_search_criteria({"raw": "UID 100:200"}) == "UID 100:200"


def test_decode_str_plain_ascii() -> None:
    assert decode_str("Hello World") == "Hello World"


def test_get_imap_credentials_unknown_account_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAP_UNKNOWN_HOST", raising=False)
    with pytest.raises(AccountNotConfiguredError):
        get_imap_credentials("unknown")


def test_get_imap_credentials_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAP_DANILO_HOST", "imap.example.com")
    monkeypatch.setenv("IMAP_DANILO_USERNAME", "danilo@example.com")
    monkeypatch.setenv("IMAP_DANILO_PASSWORD", "secret")
    creds = get_imap_credentials("danilo")
    assert creds.host == "imap.example.com"
    assert creds.port == 993
    assert creds.ssl is True

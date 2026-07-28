from __future__ import annotations

from typing import Any

import pytest

from app.mail import (
    AccountNotConfiguredError,
    ImapCredentials,
    ListRequest,
    build_search_criteria,
    decode_str,
    get_imap_credentials,
    list_messages,
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


class _FakeImap:
    """Minimal imaplib.IMAP4 stand-in: SEARCH returns a canned UID list, FETCH a canned envelope."""

    def __init__(self, search_result: bytes, fetch_uids: list[str]) -> None:
        self._search_result = search_result
        self._fetch_uids = set(fetch_uids)
        self.logged_out = False

    def select(self, _folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            return "OK", [self._search_result]
        if command == "FETCH":
            uid = args[0]
            if uid not in self._fetch_uids:
                return "OK", [None]
            flags_line = b"1 (FLAGS (\\Seen) RFC822.SIZE 100)"
            raw_headers = b"Subject: test\r\nFrom: a@b.com\r\n\r\n"
            return "OK", [(flags_line, raw_headers)]
        raise NotImplementedError(command)

    def logout(self) -> None:
        self.logged_out = True


def _patch_imap(monkeypatch: pytest.MonkeyPatch, fake: _FakeImap) -> None:
    monkeypatch.setattr("app.mail.get_imap_credentials", lambda account: ImapCredentials(
        host="imap.example.com", username="u", password="p"
    ))
    monkeypatch.setattr("app.mail.imap_connect", lambda creds: fake)


def test_list_messages_drops_swapped_uid_range_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # RFC 3501 "X:*" is an unordered range: when next_uid (1041) exceeds the mailbox's
    # highest UID (1040, i.e. no new messages), some servers swap it to "*:1041" and
    # return UID 1040 — the message already processed — instead of an empty result.
    fake = _FakeImap(search_result=b"1040", fetch_uids=["1040"])
    _patch_imap(monkeypatch, fake)

    result = list_messages(ListRequest(account="danilo", since_uid="1040"))

    assert result["count"] == 0
    assert result["messages"] == []


def test_list_messages_keeps_genuinely_new_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeImap(search_result=b"1041", fetch_uids=["1041"])
    _patch_imap(monkeypatch, fake)

    result = list_messages(ListRequest(account="danilo", since_uid="1040"))

    assert result["count"] == 1
    assert result["messages"][0]["uid"] == "1041"

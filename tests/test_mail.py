from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app import mail
from app.mail import (
    AccountNotConfiguredError,
    DeleteRequest,
    FlagRequest,
    GetRequest,
    ImapCredentials,
    InvalidHeaderValueError,
    InvalidImapValueError,
    InvalidUidError,
    ListFoldersRequest,
    ListRequest,
    MoveRequest,
    SendRequest,
    _bool_env,
    build_search_criteria,
    decode_str,
    delete_messages,
    flag_messages,
    get_imap_credentials,
    get_message,
    imap_connect,
    list_messages,
    move_messages,
    send_message,
)


def test_build_search_criteria_empty() -> None:
    assert build_search_criteria({}) == "ALL"


def test_build_search_criteria_combines_filters() -> None:
    criteria = build_search_criteria({"unseen": True, "from": "a@b.com", "subject": "invoice"})
    assert criteria == 'UNSEEN FROM "a@b.com" SUBJECT "invoice"'


def test_build_search_criteria_raw_passthrough() -> None:
    assert build_search_criteria({"raw": "UID 100:200"}) == "UID 100:200"


def test_build_search_criteria_escapes_quotes_in_field_value() -> None:
    # A literal `"` in a search value must not break out of the quoted IMAP field.
    criteria = build_search_criteria({"subject": 'hello "world"'})
    assert criteria == 'SUBJECT "hello \\"world\\""'


def test_build_search_criteria_rejects_crlf_in_field_value() -> None:
    # CR/LF would terminate the IMAP command line early and inject a second command.
    with pytest.raises(ValueError, match="CR or LF"):
        build_search_criteria({"subject": "evil\r\nDELETE 1:*"})


def test_build_search_criteria_rejects_crlf_in_raw() -> None:
    with pytest.raises(ValueError, match="CR or LF"):
        build_search_criteria({"raw": "UID 1:*\r\nDELETE 1:*"})


def test_bool_env_accepts_recognized_values() -> None:
    assert _bool_env("true", False) is True
    assert _bool_env("FALSE", True) is False
    assert _bool_env(None, True) is True


def test_bool_env_rejects_typo_instead_of_guessing() -> None:
    # A typo ("flase") used to silently resolve to True via `not in false-set` — now
    # fails loud instead of guessing a direction for a security-relevant SSL/STARTTLS flag.
    with pytest.raises(ValueError, match="expected a boolean"):
        _bool_env("flase", True)


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
        self.selected_folder: str | None = None

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected_folder = folder
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


@pytest.mark.parametrize("bad_since_uid", ["1;DELETE", "1 2", "-1", "1\r\nDELETE"])
def test_list_messages_rejects_non_numeric_since_uid(monkeypatch: pytest.MonkeyPatch, bad_since_uid: str) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidUidError):
        list_messages(ListRequest(account="danilo", since_uid=bad_since_uid))


@pytest.mark.parametrize("bad_uid", ["1;DELETE", "1 2", "-1", "", "1\r\nDELETE"])
def test_get_message_rejects_non_numeric_uid(monkeypatch: pytest.MonkeyPatch, bad_uid: str) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidUidError):
        get_message(GetRequest(account="danilo", uid=bad_uid))


@pytest.mark.parametrize("bad_uid", ["1;DELETE", "1 2", "-1", "", "1\r\nDELETE"])
def test_delete_messages_rejects_non_numeric_uid(monkeypatch: pytest.MonkeyPatch, bad_uid: str) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidUidError):
        delete_messages(DeleteRequest(account="danilo", uids=[bad_uid]))


@pytest.mark.parametrize("bad_uid", ["1;DELETE", "1 2", "-1", "", "1\r\nDELETE"])
def test_move_messages_rejects_non_numeric_uid(monkeypatch: pytest.MonkeyPatch, bad_uid: str) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidUidError):
        move_messages(MoveRequest(account="danilo", uids=[bad_uid], destination="Archive"))


@pytest.mark.parametrize("bad_uid", ["1;DELETE", "1 2", "-1", "", "1\r\nDELETE"])
def test_flag_messages_rejects_non_numeric_uid(monkeypatch: pytest.MonkeyPatch, bad_uid: str) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidUidError):
        flag_messages(FlagRequest(account="danilo", uids=[bad_uid], action="read"))


def test_list_messages_escapes_quote_in_folder_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    list_messages(ListRequest(account="danilo", folder='weird"folder'))
    assert fake.selected_folder == '"weird\\"folder"'


def test_list_messages_rejects_crlf_in_folder_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidImapValueError):
        list_messages(ListRequest(account="danilo", folder="INBOX\r\nDELETE 1:*"))


def test_move_messages_rejects_crlf_in_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeImap(search_result=b"", fetch_uids=[])
    _patch_imap(monkeypatch, fake)
    with pytest.raises(InvalidImapValueError):
        move_messages(MoveRequest(account="danilo", uids=["1"], destination="Archive\r\nDELETE 1:*"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("from_addr", "me@example.com\r\nBcc: attacker@evil.com"),
        ("to", "you@example.com\r\nBcc: attacker@evil.com"),
    ],
)
def test_send_request_rejects_crlf_in_email_fields_at_construction(field: str, value: str) -> None:
    # from_addr/to/cc/bcc are EmailStr — email-validator rejects a malformed address
    # (including one with embedded CR/LF) at request-model construction, before
    # send_message is ever called.
    kwargs: dict[str, Any] = {
        "account": "danilo",
        "from_addr": "me@example.com",
        "to": ["you@example.com"],
        "subject": "hello",
        "body": "hi",
    }
    if field == "to":
        kwargs["to"] = [value]
    else:
        kwargs[field] = value
    with pytest.raises(ValidationError):
        SendRequest(**kwargs)


def test_send_message_rejects_crlf_in_subject() -> None:
    # subject is a free-text str (no EmailStr-style syntax to violate at construction),
    # so send_message itself must reject the CR/LF header-injection attempt.
    req = SendRequest(
        account="danilo",
        from_addr="me@example.com",
        to=["you@example.com"],
        subject="hello\r\nBcc: attacker@evil.com",
        body="hi",
    )
    with pytest.raises(InvalidHeaderValueError):
        send_message(req)


class _FakeSmtp:
    def __init__(self) -> None:
        self.sent: tuple[str, list[str], str] | None = None

    def login(self, _username: str, _password: str) -> None:
        pass

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: str) -> None:
        self.sent = (from_addr, to_addrs, msg)

    def quit(self) -> None:
        pass


def test_send_message_valid_input_still_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_conn = _FakeSmtp()
    monkeypatch.setattr(
        mail,
        "get_smtp_config",
        lambda account: {
            "host": "smtp.example.com",
            "port": 465,
            "username": "u",
            "password": "p",
            "ssl": True,
            "starttls": False,
        },
    )
    monkeypatch.setattr("app.mail.smtplib.SMTP_SSL", lambda *a, **k: fake_conn)

    result = send_message(
        SendRequest(account="danilo", from_addr="me@example.com", to=["you@example.com"], subject="hi", body="hello")
    )

    assert result["sent"] is True
    assert fake_conn.sent is not None
    assert fake_conn.sent[0] == "me@example.com"


def test_account_request_rejects_invalid_characters() -> None:
    # account is uppercased and interpolated into env var names (IMAP_<ACCOUNT>_HOST) —
    # a shell/injection-looking value must be rejected before it gets anywhere near that.
    with pytest.raises(ValidationError):
        ListFoldersRequest(account="danilo; rm -rf")


def test_account_request_accepts_normal_name() -> None:
    assert ListFoldersRequest(account="danilo-2").account == "danilo-2"


def test_get_request_rejects_oversized_uid() -> None:
    with pytest.raises(ValidationError):
        GetRequest(account="danilo", uid="1" * 21)


def test_delete_request_rejects_oversized_uid_in_list() -> None:
    with pytest.raises(ValidationError):
        DeleteRequest(account="danilo", uids=["1" * 21])


def test_send_request_rejects_malformed_email() -> None:
    with pytest.raises(ValidationError):
        SendRequest(account="danilo", from_addr="not-an-email", to=["you@example.com"], subject="hi", body="hi")


def test_build_search_criteria_rejects_oversized_value() -> None:
    with pytest.raises(InvalidImapValueError):
        build_search_criteria({"subject": "a" * 600})


def test_build_search_criteria_rejects_oversized_raw() -> None:
    with pytest.raises(InvalidImapValueError):
        build_search_criteria({"raw": "a" * 600})


def test_imap_connect_passes_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeImap4Ssl:
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            captured["timeout"] = timeout

        def login(self, username: str, password: str) -> None:
            pass

    monkeypatch.setattr("app.mail.imaplib.IMAP4_SSL", _FakeImap4Ssl)
    imap_connect(ImapCredentials(host="imap.example.com", username="u", password="p"))
    assert captured["timeout"] == mail._NETWORK_TIMEOUT_S


def test_list_configured_accounts_scans_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mail.os,
        "environ",
        {
            "IMAP_DANILO_HOST": "imap.example.com",
            "IMAP_WORK_HOST": "imap.work.com",
            "SMTP_DANILO_HOST": "smtp.example.com",
            "OTHER_VAR": "x",
        },
    )
    assert mail.list_configured_accounts() == ["danilo", "work"]


def test_list_configured_accounts_empty_when_none_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail.os, "environ", {"OTHER_VAR": "x"})
    assert mail.list_configured_accounts() == []


def test_list_configured_accounts_deduplicates_imap_and_smtp_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    # An account with both IMAP_*_HOST and SMTP_*_HOST must appear once, not twice.
    monkeypatch.setattr(
        mail.os,
        "environ",
        {"IMAP_DANILO_HOST": "imap.example.com", "SMTP_DANILO_HOST": "smtp.example.com"},
    )
    assert mail.list_configured_accounts() == ["danilo"]


def test_list_configured_accounts_ignores_partial_key_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # IMAP_DANILO_HOSTNAME must not be mistaken for IMAP_DANILO_HOST (anchored regex).
    monkeypatch.setattr(mail.os, "environ", {"IMAP_DANILO_HOSTNAME": "imap.example.com"})
    assert mail.list_configured_accounts() == []


def test_parse_list_response_name_quoted() -> None:
    # A quoted folder name previously produced an empty string: split('"')[-1] on
    # '(\\HasNoChildren) "/" "ARUBA"' returns '' (nothing follows the closing quote).
    assert mail._parse_list_response_name('(\\HasNoChildren) "/" "ARUBA"') == "ARUBA"


def test_parse_list_response_name_quoted_with_spaces() -> None:
    assert mail._parse_list_response_name('(\\HasNoChildren) "/" "My Folder"') == "My Folder"


def test_parse_list_response_name_unquoted() -> None:
    assert mail._parse_list_response_name('(\\HasNoChildren) "." INBOX') == "INBOX"


def test_parse_list_response_name_nil_delimiter() -> None:
    # RFC 3501: the delimiter can be NIL (no hierarchy separator) instead of a quoted char.
    assert mail._parse_list_response_name("(\\Noselect) NIL Archive") == "Archive"


def test_parse_list_response_name_falls_back_to_raw_on_unexpected_format() -> None:
    assert mail._parse_list_response_name("not a real IMAP LIST line") == "not a real IMAP LIST line"

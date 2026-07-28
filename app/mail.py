from __future__ import annotations

import base64
import email
import imaplib
import os
import re
import smtplib
import ssl as ssl_module
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Annotated, Any

from pydantic import BaseModel, EmailStr, Field


class AccountNotConfiguredError(Exception):
    def __init__(self, kind: str, account: str) -> None:
        super().__init__(f"{kind} account '{account}' not configured")
        self.kind = kind
        self.account = account


class MessageNotFoundError(Exception):
    def __init__(self, uid: str) -> None:
        super().__init__(f"UID {uid} not found")
        self.uid = uid


class InvalidFlagActionError(Exception):
    def __init__(self, action: str) -> None:
        super().__init__(f"action must be 'read' or 'unread', got '{action}'")
        self.action = action


class InvalidUidError(Exception):
    def __init__(self, uid: str) -> None:
        super().__init__(f"UID must be a positive integer, got '{uid}'")
        self.uid = uid


class InvalidImapValueError(ValueError):
    """A value destined for an IMAP command line contains CR/LF (protocol injection)."""


class InvalidHeaderValueError(Exception):
    def __init__(self, field: str, value: str) -> None:
        super().__init__(f"{field} must not contain CR or LF")
        self.field = field
        self.value = value


# --- Models ---

_ACCOUNT_PATTERN = r"^[a-zA-Z0-9_-]+$"
_MAX_FOLDER_LEN = 255
_MAX_SUBJECT_LEN = 998  # RFC 5322: max length of an unfolded header line
_MAX_BODY_LEN = 5_000_000  # generous cap for a real email body, bounds worst-case memory use
_MAX_UID_LEN = 20  # a real IMAP UID is at most 10 digits (32-bit); generous headroom

_UidStr = Annotated[str, Field(max_length=_MAX_UID_LEN)]


class ImapCredentials(BaseModel):
    host: str
    port: int = 993
    username: str
    password: str
    ssl: bool = True


class AccountRequest(BaseModel):
    # account is uppercased and interpolated into env var names (IMAP_<ACCOUNT>_HOST) —
    # restrict the charset so it can't be used to probe/read unrelated env vars (REPORT.md H-05).
    account: str = Field(..., pattern=_ACCOUNT_PATTERN, max_length=64)


class ListFoldersRequest(AccountRequest):
    pass


class SearchRequest(AccountRequest):
    folder: str = Field(default="INBOX", max_length=_MAX_FOLDER_LEN)
    criteria: dict[str, Any] = {}
    limit: int = 50


class DeleteRequest(AccountRequest):
    folder: str = Field(default="INBOX", max_length=_MAX_FOLDER_LEN)
    uids: list[_UidStr]


class MoveRequest(AccountRequest):
    folder: str = Field(default="INBOX", max_length=_MAX_FOLDER_LEN)
    uids: list[_UidStr]
    destination: str = Field(..., max_length=_MAX_FOLDER_LEN)


class FlagRequest(AccountRequest):
    folder: str = Field(default="INBOX", max_length=_MAX_FOLDER_LEN)
    uids: list[_UidStr]
    action: str  # "read" | "unread"


class ListRequest(AccountRequest):
    folder: str = Field(default="INBOX", max_length=_MAX_FOLDER_LEN)
    since_uid: str | None = None
    limit: int | None = 100  # null/assente = 100, negativo = tutti


class GetRequest(AccountRequest):
    folder: str = Field(default="INBOX", max_length=_MAX_FOLDER_LEN)
    uid: _UidStr
    include_attachments: bool = False


class SendRequest(AccountRequest):
    # EmailStr (email-validator) rejects malformed addresses and, as a side effect,
    # any embedded CR/LF — covers REPORT.md H-08 and most of C-03's header injection
    # surface at the validation layer, before send_message ever runs.
    from_addr: EmailStr
    to: list[EmailStr]
    cc: list[EmailStr] = []
    bcc: list[EmailStr] = []
    subject: str = Field(..., max_length=_MAX_SUBJECT_LEN)
    body: str = Field(..., max_length=_MAX_BODY_LEN)
    html: bool = False


# --- Credential helpers ---


_BOOL_ENV_TRUE = {"true", "1", "yes"}
_BOOL_ENV_FALSE = {"false", "0", "no"}


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _BOOL_ENV_TRUE:
        return True
    if normalized in _BOOL_ENV_FALSE:
        return False
    # A typo (e.g. "flase") used to silently fall back to True via `not in false-set` —
    # for a security-relevant flag (SSL/STARTTLS) failing loud beats guessing a direction.
    raise ValueError(f"expected a boolean value (true/false/1/0/yes/no), got {value!r}")


def get_imap_credentials(account: str) -> ImapCredentials:
    prefix = f"IMAP_{account.upper()}"
    host = os.environ.get(f"{prefix}_HOST")
    if not host:
        raise AccountNotConfiguredError("IMAP", account)
    return ImapCredentials(
        host=host,
        port=int(os.environ.get(f"{prefix}_PORT", "993")),
        username=os.environ.get(f"{prefix}_USERNAME", ""),
        password=os.environ.get(f"{prefix}_PASSWORD", ""),
        ssl=_bool_env(os.environ.get(f"{prefix}_SSL"), True),
    )


_ACCOUNT_ENV_RE = re.compile(r"^IMAP_([A-Za-z0-9_-]+)_HOST$")


def list_configured_accounts() -> list[str]:
    """Scan the environment for IMAP_<NAME>_HOST and return the configured account names, lowercased and sorted."""
    names = set()
    for key in os.environ:
        match = _ACCOUNT_ENV_RE.match(key)
        if match:
            names.add(match.group(1).lower())
    return sorted(names)


def get_smtp_config(account: str) -> dict[str, Any]:
    prefix = f"SMTP_{account.upper()}"
    host = os.environ.get(f"{prefix}_HOST")
    if not host:
        raise AccountNotConfiguredError("SMTP", account)
    return {
        "host": host,
        "port": int(os.environ.get(f"{prefix}_PORT", "587")),
        "username": os.environ.get(f"{prefix}_USERNAME", ""),
        "password": os.environ.get(f"{prefix}_PASSWORD", ""),
        "starttls": _bool_env(os.environ.get(f"{prefix}_STARTTLS"), True),
        "ssl": _bool_env(os.environ.get(f"{prefix}_SSL"), False),
    }


# --- IMAP helpers ---

# A slow/unresponsive remote server would otherwise block the asyncio.to_thread worker
# thread indefinitely (REPORT.md H-06) — every socket-level connection gets this timeout.
_NETWORK_TIMEOUT_S = 30


def imap_connect(creds: ImapCredentials) -> imaplib.IMAP4:
    imap: imaplib.IMAP4
    if creds.ssl:
        imap = imaplib.IMAP4_SSL(creds.host, creds.port, timeout=_NETWORK_TIMEOUT_S)
    else:
        imap = imaplib.IMAP4(creds.host, creds.port, timeout=_NETWORK_TIMEOUT_S)
    imap.login(creds.username, creds.password)
    return imap


# SearchRequest.criteria is a free-form dict[str, Any], not a typed Pydantic field, so
# it bypasses per-field max_length — bound it here instead (REPORT.md H-07).
_MAX_SEARCH_VALUE_LEN = 512


def _escape_imap_quoted(value: str) -> str:
    # CR/LF would terminate the IMAP command line early and let the rest of the value
    # be interpreted as a new IMAP command (protocol injection) — reject outright rather
    # than stripping, since silently mangling the search value is worse than a clear error.
    if "\r" in value or "\n" in value:
        raise InvalidImapValueError("value must not contain CR or LF")
    if len(value) > _MAX_SEARCH_VALUE_LEN:
        raise InvalidImapValueError(f"value exceeds max length of {_MAX_SEARCH_VALUE_LEN}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_search_criteria(criteria: dict[str, Any]) -> str:
    parts = []
    if criteria.get("unseen"):
        parts.append("UNSEEN")
    if criteria.get("seen"):
        parts.append("SEEN")
    if criteria.get("from"):
        parts.append(f'FROM "{_escape_imap_quoted(criteria["from"])}"')
    if criteria.get("to"):
        parts.append(f'TO "{_escape_imap_quoted(criteria["to"])}"')
    if criteria.get("subject"):
        parts.append(f'SUBJECT "{_escape_imap_quoted(criteria["subject"])}"')
    if criteria.get("since"):
        parts.append(f'SINCE "{_escape_imap_quoted(criteria["since"])}"')
    if criteria.get("before"):
        parts.append(f'BEFORE "{_escape_imap_quoted(criteria["before"])}"')
    if criteria.get("body"):
        parts.append(f'BODY "{_escape_imap_quoted(criteria["body"])}"')
    if criteria.get("raw"):
        # "raw" is meant to carry arbitrary IMAP search syntax verbatim (parentheses, OR,
        # NOT, ...), so it can't be escaped like the fields above — only CR/LF (protocol
        # injection) is rejected, everything else passes through as documented in API.md.
        raw = criteria["raw"]
        if "\r" in raw or "\n" in raw:
            raise InvalidImapValueError("raw search criteria must not contain CR or LF")
        if len(raw) > _MAX_SEARCH_VALUE_LEN:
            raise InvalidImapValueError(f"raw search criteria exceeds max length of {_MAX_SEARCH_VALUE_LEN}")
        parts.append(raw)
    return " ".join(parts) if parts else "ALL"


def _decoded_payload(part: email.message.Message) -> bytes:
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def decode_str(value: str) -> str:
    decoded = decode_header(value)
    parts = []
    for fragment, charset in decoded:
        if isinstance(fragment, bytes):
            parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts)


def _validate_uid(uid: str) -> str:
    # UIDs are interpolated directly into IMAP commands (STORE/COPY/MOVE/FETCH) — a
    # non-numeric value could inject arbitrary IMAP syntax. isascii() guards against
    # non-ASCII digit characters (e.g. Arabic-indic) that pass isdigit() but aren't
    # valid IMAP protocol bytes.
    if not (uid.isascii() and uid.isdigit()):
        raise InvalidUidError(uid)
    return uid


def parse_message_envelope(imap: imaplib.IMAP4, uid: str) -> dict[str, Any]:
    _, data = imap.uid(
        "FETCH", uid, "(RFC822.SIZE FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])"
    )
    if not data or data[0] is None:
        return {"uid": uid}
    raw_headers = data[0][1]
    msg = email.message_from_bytes(raw_headers)
    flags_data = data[0][0].decode()
    flags = []
    if "\\Seen" in flags_data:
        flags.append("\\Seen")
    if "\\Flagged" in flags_data:
        flags.append("\\Flagged")
    size = 0
    for part in flags_data.split():
        if part.isdigit():
            size = int(part)
    return {
        "uid": uid,
        "message_id": msg.get("Message-ID", ""),
        "from": decode_str(msg.get("From", "")),
        "to": decode_str(msg.get("To", "")),
        "cc": decode_str(msg.get("Cc", "")),
        "subject": decode_str(msg.get("Subject", "")),
        "date": msg.get("Date", ""),
        "flags": flags,
        "size": size,
    }


# --- Operations (business logic, no FastAPI coupling) ---


_LIST_RESPONSE_RE = re.compile(r'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)$')


def _parse_list_response_name(decoded: str) -> str:
    # IMAP LIST reply: "(flags) delimiter name" — name is the last whitespace-separated
    # field but may itself contain spaces if quoted, so split('"')[-1] (previous approach)
    # returned an empty string whenever the name was quoted (nothing follows the closing
    # quote). Match the whole trailing field instead, quoted or not.
    match = _LIST_RESPONSE_RE.match(decoded)
    name = match.group(1) if match else decoded
    return name.strip().strip('"')


def list_folders(req: ListFoldersRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        _, folders = imap.list()
        result = []
        for f in folders:
            if not isinstance(f, bytes):
                continue
            result.append(_parse_list_response_name(f.decode()))
        return {"folders": result}
    finally:
        imap.logout()


def list_messages(req: ListRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        typ, _ = imap.select(f'"{_escape_imap_quoted(req.folder)}"', readonly=True)
        if typ != "OK":
            raise ValueError(f"Cannot select folder '{req.folder}'")
        if req.since_uid:
            next_uid = int(_validate_uid(req.since_uid)) + 1
            criteria = f"UID {next_uid}:*"
            _, uids = imap.uid("SEARCH", criteria)
        else:
            next_uid = None
            _, uids = imap.uid("SEARCH", "ALL")
        uid_list = uids[0].split() if uids[0] else []
        if next_uid is not None:
            # RFC 3501: "X:*" is an unordered range — if next_uid > highest UID in the
            # mailbox (no new messages), the server swaps it to "*:X" and returns the
            # highest existing UID instead of nothing. Drop anything below next_uid.
            uid_list = [u for u in uid_list if int(u) >= next_uid]
        uid_list.sort(key=int)
        effective_limit = req.limit if req.limit is not None else 100
        if effective_limit > 0:
            if next_uid is not None:
                # Polling mode (since_uid set, e.g. n8n incremental fetch): the caller
                # wants the next N messages after the cursor, oldest first — not the N
                # most recent in the whole range, which would silently skip messages.
                uid_list = uid_list[:effective_limit]
            else:
                uid_list = uid_list[-effective_limit:]
        messages = [parse_message_envelope(imap, u.decode()) for u in uid_list]
        return {"folder": req.folder, "since_uid": req.since_uid, "count": len(messages), "messages": messages}
    finally:
        imap.logout()


def get_message(req: GetRequest) -> dict[str, Any]:
    uid = _validate_uid(req.uid)
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{_escape_imap_quoted(req.folder)}"', readonly=True)
        _, data = imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if not data or data[0] is None:
            raise MessageNotFoundError(req.uid)
        raw = data[0][1]
        flags_line = data[0][0].decode()
        flags = []
        if "\\Seen" in flags_line:
            flags.append("\\Seen")
        if "\\Flagged" in flags_line:
            flags.append("\\Flagged")
        msg = email.message_from_bytes(raw)
        result: dict[str, Any] = {
            "uid": req.uid,
            "message_id": msg.get("Message-ID", ""),
            "from": decode_str(msg.get("From", "")),
            "to": decode_str(msg.get("To", "")),
            "cc": decode_str(msg.get("Cc", "")),
            "subject": decode_str(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "flags": flags,
            "body_text": None,
            "body_html": None,
            "attachments": [],
        }
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = part.get("Content-Disposition", "")
                if "attachment" in cd:
                    payload = _decoded_payload(part)
                    attachment: dict[str, Any] = {
                        "filename": decode_str(part.get_filename() or ""),
                        "content_type": ct,
                        "size": len(payload),
                    }
                    if req.include_attachments:
                        attachment["data"] = base64.b64encode(payload).decode("ascii")
                    result["attachments"].append(attachment)
                elif ct == "text/plain" and result["body_text"] is None:
                    result["body_text"] = _decoded_payload(part).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                elif ct == "text/html" and result["body_html"] is None:
                    result["body_html"] = _decoded_payload(part).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        else:
            payload = _decoded_payload(msg)
            if payload:
                text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                if msg.get_content_type() == "text/html":
                    result["body_html"] = text
                else:
                    result["body_text"] = text
        return result
    finally:
        imap.logout()


def search_messages(req: SearchRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{_escape_imap_quoted(req.folder)}"', readonly=True)
        criteria = build_search_criteria(req.criteria)
        _, uids = imap.uid("SEARCH", criteria)
        uid_list = uids[0].split() if uids[0] else []
        uid_list = uid_list[-req.limit :]
        messages = [parse_message_envelope(imap, u.decode()) for u in uid_list]
        return {"folder": req.folder, "criteria": criteria, "count": len(messages), "messages": messages}
    finally:
        imap.logout()


def delete_messages(req: DeleteRequest) -> dict[str, Any]:
    uids = [_validate_uid(u) for u in req.uids]
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{_escape_imap_quoted(req.folder)}"')
        for uid in uids:
            imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
        imap.expunge()
        return {"deleted": req.uids}
    finally:
        imap.logout()


def move_messages(req: MoveRequest) -> dict[str, Any]:
    uids = [_validate_uid(u) for u in req.uids]
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{_escape_imap_quoted(req.folder)}"')
        uid_str = ",".join(uids)
        try:
            imap.uid("MOVE", uid_str, f'"{_escape_imap_quoted(req.destination)}"')
        except (imaplib.IMAP4.error, AttributeError):
            imap.uid("COPY", uid_str, f'"{_escape_imap_quoted(req.destination)}"')
            for uid in uids:
                imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
            imap.expunge()
        return {"moved": req.uids, "destination": req.destination}
    finally:
        imap.logout()


def flag_messages(req: FlagRequest) -> dict[str, Any]:
    if req.action not in ("read", "unread"):
        raise InvalidFlagActionError(req.action)
    uids = [_validate_uid(u) for u in req.uids]
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{_escape_imap_quoted(req.folder)}"')
        op = "+FLAGS" if req.action == "read" else "-FLAGS"
        for uid in uids:
            imap.uid("STORE", uid, op, "\\Seen")
        return {"uids": req.uids, "action": req.action}
    finally:
        imap.logout()


def _validate_header_value(field: str, value: str) -> str:
    # CR/LF in a header value lets an attacker inject arbitrary extra headers (Bcc,
    # Content-Type, ...) or forge additional messages within the same SMTP transaction.
    if "\r" in value or "\n" in value:
        raise InvalidHeaderValueError(field, value)
    return value


def send_message(req: SendRequest) -> dict[str, Any]:
    # from_addr/to/cc/bcc are EmailStr — already guaranteed CRLF-free by email-validator,
    # only the free-text subject still needs the explicit check.
    subject = _validate_header_value("subject", req.subject)

    smtp = get_smtp_config(req.account)
    msg = MIMEMultipart("alternative") if req.html else MIMEMultipart()
    msg["From"] = req.from_addr
    msg["To"] = ", ".join(req.to)
    if req.cc:
        msg["Cc"] = ", ".join(req.cc)
    msg["Subject"] = subject
    mime_type = "html" if req.html else "plain"
    msg.attach(MIMEText(req.body, mime_type, "utf-8"))

    all_recipients = req.to + req.cc + req.bcc

    conn: smtplib.SMTP
    if smtp["ssl"]:
        ctx = ssl_module.create_default_context()
        conn = smtplib.SMTP_SSL(smtp["host"], smtp["port"], context=ctx, timeout=_NETWORK_TIMEOUT_S)
    else:
        conn = smtplib.SMTP(smtp["host"], smtp["port"], timeout=_NETWORK_TIMEOUT_S)
        if smtp["starttls"]:
            conn.starttls()

    try:
        conn.login(smtp["username"], smtp["password"])
        conn.sendmail(req.from_addr, all_recipients, msg.as_string())
    finally:
        conn.quit()
    return {"sent": True, "to": req.to, "cc": req.cc, "bcc": req.bcc}

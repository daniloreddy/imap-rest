from __future__ import annotations

import base64
import email
import imaplib
import os
import smtplib
import ssl as ssl_module
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from pydantic import BaseModel


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


# --- Models ---


class ImapCredentials(BaseModel):
    host: str
    port: int = 993
    username: str
    password: str
    ssl: bool = True


class AccountRequest(BaseModel):
    account: str


class ListFoldersRequest(AccountRequest):
    pass


class SearchRequest(AccountRequest):
    folder: str = "INBOX"
    criteria: dict[str, Any] = {}
    limit: int = 50


class DeleteRequest(AccountRequest):
    folder: str = "INBOX"
    uids: list[str]


class MoveRequest(AccountRequest):
    folder: str = "INBOX"
    uids: list[str]
    destination: str


class FlagRequest(AccountRequest):
    folder: str = "INBOX"
    uids: list[str]
    action: str  # "read" | "unread"


class ListRequest(AccountRequest):
    folder: str = "INBOX"
    since_uid: str | None = None
    limit: int | None = 100  # null/assente = 100, negativo = tutti


class GetRequest(AccountRequest):
    folder: str = "INBOX"
    uid: str
    include_attachments: bool = False


class SendRequest(AccountRequest):
    from_addr: str
    to: list[str]
    cc: list[str] = []
    bcc: list[str] = []
    subject: str
    body: str
    html: bool = False


# --- Credential helpers ---


def _bool_env(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() not in ("false", "0", "no")


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


def imap_connect(creds: ImapCredentials) -> imaplib.IMAP4:
    imap: imaplib.IMAP4
    if creds.ssl:
        imap = imaplib.IMAP4_SSL(creds.host, creds.port)
    else:
        imap = imaplib.IMAP4(creds.host, creds.port)
    imap.login(creds.username, creds.password)
    return imap


def build_search_criteria(criteria: dict[str, Any]) -> str:
    parts = []
    if criteria.get("unseen"):
        parts.append("UNSEEN")
    if criteria.get("seen"):
        parts.append("SEEN")
    if criteria.get("from"):
        parts.append(f'FROM "{criteria["from"]}"')
    if criteria.get("to"):
        parts.append(f'TO "{criteria["to"]}"')
    if criteria.get("subject"):
        parts.append(f'SUBJECT "{criteria["subject"]}"')
    if criteria.get("since"):
        parts.append(f'SINCE "{criteria["since"]}"')
    if criteria.get("before"):
        parts.append(f'BEFORE "{criteria["before"]}"')
    if criteria.get("body"):
        parts.append(f'BODY "{criteria["body"]}"')
    if criteria.get("raw"):
        parts.append(criteria["raw"])
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


def list_folders(req: ListFoldersRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        _, folders = imap.list()
        result = []
        for f in folders:
            if not isinstance(f, bytes):
                continue
            decoded = f.decode()
            parts = decoded.split('"')
            name = parts[-1].strip().strip('"') if parts else decoded
            result.append(name)
        return {"folders": result}
    finally:
        imap.logout()


def list_messages(req: ListRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        typ, _ = imap.select(f'"{req.folder}"', readonly=True)
        if typ != "OK":
            raise ValueError(f"Cannot select folder '{req.folder}'")
        if req.since_uid:
            next_uid = int(req.since_uid) + 1
            criteria = f"UID {next_uid}:*"
            _, uids = imap.uid("SEARCH", criteria)
        else:
            _, uids = imap.uid("SEARCH", "ALL")
        uid_list = uids[0].split() if uids[0] else []
        uid_list.sort(key=int)
        effective_limit = req.limit if req.limit is not None else 100
        if effective_limit > 0:
            uid_list = uid_list[-effective_limit:]
        messages = [parse_message_envelope(imap, u.decode()) for u in uid_list]
        return {"folder": req.folder, "since_uid": req.since_uid, "count": len(messages), "messages": messages}
    finally:
        imap.logout()


def get_message(req: GetRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{req.folder}"', readonly=True)
        _, data = imap.uid("FETCH", req.uid, "(FLAGS BODY.PEEK[])")
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
        imap.select(f'"{req.folder}"', readonly=True)
        criteria = build_search_criteria(req.criteria)
        _, uids = imap.uid("SEARCH", criteria)
        uid_list = uids[0].split() if uids[0] else []
        uid_list = uid_list[-req.limit :]
        messages = [parse_message_envelope(imap, u.decode()) for u in uid_list]
        return {"folder": req.folder, "criteria": criteria, "count": len(messages), "messages": messages}
    finally:
        imap.logout()


def delete_messages(req: DeleteRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{req.folder}"')
        for uid in req.uids:
            imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
        imap.expunge()
        return {"deleted": req.uids}
    finally:
        imap.logout()


def move_messages(req: MoveRequest) -> dict[str, Any]:
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{req.folder}"')
        uid_str = ",".join(req.uids)
        try:
            imap.uid("MOVE", uid_str, f'"{req.destination}"')
        except (imaplib.IMAP4.error, AttributeError):
            imap.uid("COPY", uid_str, f'"{req.destination}"')
            for uid in req.uids:
                imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
            imap.expunge()
        return {"moved": req.uids, "destination": req.destination}
    finally:
        imap.logout()


def flag_messages(req: FlagRequest) -> dict[str, Any]:
    if req.action not in ("read", "unread"):
        raise InvalidFlagActionError(req.action)
    creds = get_imap_credentials(req.account)
    imap = imap_connect(creds)
    try:
        imap.select(f'"{req.folder}"')
        op = "+FLAGS" if req.action == "read" else "-FLAGS"
        for uid in req.uids:
            imap.uid("STORE", uid, op, "\\Seen")
        return {"uids": req.uids, "action": req.action}
    finally:
        imap.logout()


def send_message(req: SendRequest) -> dict[str, Any]:
    smtp = get_smtp_config(req.account)
    msg = MIMEMultipart("alternative") if req.html else MIMEMultipart()
    msg["From"] = req.from_addr
    msg["To"] = ", ".join(req.to)
    if req.cc:
        msg["Cc"] = ", ".join(req.cc)
    msg["Subject"] = req.subject
    mime_type = "html" if req.html else "plain"
    msg.attach(MIMEText(req.body, mime_type, "utf-8"))

    all_recipients = req.to + req.cc + req.bcc

    conn: smtplib.SMTP
    if smtp["ssl"]:
        ctx = ssl_module.create_default_context()
        conn = smtplib.SMTP_SSL(smtp["host"], smtp["port"], context=ctx)
    else:
        conn = smtplib.SMTP(smtp["host"], smtp["port"])
        if smtp["starttls"]:
            conn.starttls()

    try:
        conn.login(smtp["username"], smtp["password"])
        conn.sendmail(req.from_addr, all_recipients, msg.as_string())
    finally:
        conn.quit()
    return {"sent": True, "to": req.to, "cc": req.cc, "bcc": req.bcc}

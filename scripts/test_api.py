from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


class ConnectionFailedError(Exception):
    pass


def _parse_body(raw: bytes) -> Any:
    """Best-effort JSON decode. A non-JSON body (HTML error page from a reverse
    proxy/tunnel in front of the app, empty body, ...) is returned as raw text
    instead of raising — the caller can still report status + text."""
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def call(base_url: str, method: str, path: str, token: str | None, payload: dict[str, Any] | None = None) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, _parse_body(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _parse_body(exc.read())
    except urllib.error.URLError as exc:
        raise ConnectionFailedError(f"could not reach {url}: {exc.reason}") from exc


class Reporter:
    def __init__(self) -> None:
        self.failures = 0

    def step(self, name: str, status: int, expected: int, detail: str = "") -> None:
        if status == expected:
            print(f"[PASS] {name} (HTTP {status}) {detail}")
        else:
            self.failures += 1
            print(f"[FAIL] {name} (HTTP {status}, expected {expected}) {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual end-to-end smoke test for the IMAP REST API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--account", required=True, help="Account name as configured in .env (e.g. 'danilo')")
    parser.add_argument("--folder", default="INBOX", help="IMAP folder to test against")
    parser.add_argument("--token", default=None, help="Bearer token, if API_TOKENS is configured on the server")
    parser.add_argument(
        "--with-flag",
        action="store_true",
        help="Also exercise /messages/flag (toggles read/unread on the first message found, then restores it)",
    )
    args = parser.parse_args()

    report = Reporter()

    try:
        _run_checks(args, report)
    except ConnectionFailedError as exc:
        print(f"[ERROR] {exc}")
        return 2

    if report.failures:
        print(f"{report.failures} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


def _run_checks(args: argparse.Namespace, report: Reporter) -> None:
    status, body = call(args.base_url, "GET", "/health", None)
    report.step("GET /health", status, 200, str(body))

    status, body = call(args.base_url, "POST", "/folders", args.token, {"account": args.account})
    report.step("POST /folders", status, 200, f"folders={body.get('folders') if isinstance(body, dict) else body}")

    status, body = call(
        args.base_url,
        "POST",
        "/messages/list",
        args.token,
        {"account": args.account, "folder": args.folder, "limit": 5},
    )
    report.step("POST /messages/list", status, 200, f"count={body.get('count') if isinstance(body, dict) else ''}")
    messages = body.get("messages", []) if status == 200 and isinstance(body, dict) else []

    status, body = call(
        args.base_url,
        "POST",
        "/messages/search",
        args.token,
        {"account": args.account, "folder": args.folder, "criteria": {}, "limit": 3},
    )
    report.step("POST /messages/search", status, 200, f"count={body.get('count') if isinstance(body, dict) else ''}")

    if messages:
        uid = messages[0]["uid"]
        status, body = call(
            args.base_url,
            "POST",
            "/messages/get",
            args.token,
            {"account": args.account, "folder": args.folder, "uid": uid},
        )
        subject = body.get("subject", "") if isinstance(body, dict) else ""
        report.step(f"POST /messages/get (uid={uid})", status, 200, f"subject={subject!r}")

        if args.with_flag:
            was_seen = "\\Seen" in body.get("flags", []) if isinstance(body, dict) else False
            status, _ = call(
                args.base_url,
                "POST",
                "/messages/flag",
                args.token,
                {"account": args.account, "folder": args.folder, "uids": [uid], "action": "read"},
            )
            report.step(f"POST /messages/flag action=read (uid={uid})", status, 200)
            if not was_seen:
                status, _ = call(
                    args.base_url,
                    "POST",
                    "/messages/flag",
                    args.token,
                    {"account": args.account, "folder": args.folder, "uids": [uid], "action": "unread"},
                )
                report.step(f"POST /messages/flag action=unread restore (uid={uid})", status, 200)
    else:
        print(f"[SKIP] /messages/get and /messages/flag — no messages in folder '{args.folder}'")

    status, body = call(args.base_url, "POST", "/folders", args.token, {"account": "__unknown_account__"})
    report.step("POST /folders unknown account", status, 400, str(body))

    print()
    print("--- /messages/move, /messages/delete and /messages/send are not exercised automatically ---")
    print("--- (they mutate the mailbox / send real email) — test those manually with real data. ---")
    print()


if __name__ == "__main__":
    sys.exit(main())

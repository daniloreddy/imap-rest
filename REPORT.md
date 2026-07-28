# Security Audit Report — IMAP REST

**Target:** `C:\redberry\src\python\imap-rest`
**Audit Date:** 2026-07-28

---

## CRITICAL

### C-01: IMAP Command Injection via `build_search_criteria` — `raw` passthrough

**File:** `app/mail.py` lines 169-170
**Type:** Injection

`criteria["raw"]` appended directly to IMAP SEARCH command with zero sanitization. Attacker-controlled arbitrary IMAP search string enables boolean oracle extraction, filter bypass, and DoS.

**Fix:** Remove `raw` or validate against strict allowlist of IMAP keywords. Reject parentheses, control chars, command terminators.

---

### C-02: IMAP Command Injection via Quoted Search Fields

**File:** `app/mail.py` lines 157-168
**Type:** Injection

`from`, `to`, `subject`, `body` wrapped in double quotes but values never escaped. A `"` in the value breaks out of quoted context and injects arbitrary IMAP syntax.

**Fix:** Escape or reject double-quote characters in search field values. Use IMAP literal syntax if supported by the library.

---

### C-03: SMTP Header Injection (Email Spoofing)

**File:** `app/mail.py` lines 400-404
**Type:** Injection

`from_addr`, `subject`, `to`, `cc` placed directly into MIME headers without sanitization. `\r\n` injection enables arbitrary header addition, Bcc injection, content-type manipulation, and email forgery.

**Fix:** Strip/reject `\r` and `\n` from all header values. Validate email addresses with `email.utils.parseaddr`. Use `email.header.Header` for encoding.

---

### C-04: API Token Timing Oracle via `any()` Short-Circuit

**File:** `app/main.py` lines 88-89
**Type:** Cryptographic / Timing Attack

`any(secrets.compare_digest(...) for t in tokens)` short-circuits on first match, creating timing side-channel that leaks matching token position. The `redberry-webkit` library's own `verify_api_token()` uses `sum()` to avoid this.

**Fix:** Replace with `sum()` pattern or use library's `verify_api_token()`.

---

### C-05: IMAP Command Injection via UID Lists

**File:** `app/mail.py` lines 357, 369, 371, 373, 375, 391
**Type:** Injection

UIDs from `req.uids` (`list[str]`) joined with commas and passed to IMAP commands without numeric validation. Non-numeric UIDs can inject arbitrary IMAP commands.

**Fix:** Validate every UID is a positive integer before passing to IMAP.

---

### C-06: Error Messages Leak Internal Server Details

**File:** `app/main.py` lines 207-218
**Type:** Information Disclosure

Raw IMAP/SMTP exception messages returned verbatim in HTTP 500 responses, leaking server hostnames, auth errors, mailbox structure, and network topology.

**Fix:** Return generic error message to client. Log full details server-side.

---

## HIGH

### H-01: No CSRF Protection on Dashboard

**Files:** `app/ui/router.py` lines 36-62, `app/ui/pages.py` lines 197-211
**Type:** CSRF

Cookie-based dashboard auth with no CSRF token. `SameSite=Strict` provides partial protection but insufficient against subdomain attacks, browser extensions, and older browsers.

**Fix:** Add CSRF tokens to all state-changing forms via FastAPI CSRF middleware or `fastapi-csrf-protect`.

---

### H-02: Missing Security Headers

**File:** `app/main.py`
**Type:** Missing Security Control

No `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, or `Strict-Transport-Security` headers set. Enables XSS (no CSP), clickjacking (no XFO), and MIME-sniffing attacks.

**Fix:** Add middleware setting security headers on every response.

---

### H-03: No Rate Limiting on Login Endpoint

**File:** `app/ui/router.py` lines 36-62
**Type:** Brute Force

`/auth/login` has no slowapi rate limiter. The scrypt hash (N=131072, ~128MB memory) makes flood login attempts a DoS vector against the server itself.

**Fix:** Add `@limiter.limit("5/minute")` to login endpoint.

---

### H-04: Sensitive Data in Metrics Dashboard

**File:** `app/ui/pages.py` lines 106-120
**Type:** Information Disclosure

Dashboard displays `error_message` from every API call, potentially showing IMAP server responses, email addresses, and connection details to dashboard users.

**Fix:** Redact error messages before display. Use `MetricsStore.get_history(redact_sensitive=True)`.

---

### H-05: No Input Validation on `account` Field

**File:** `app/mail.py` lines 109-110, 123-124
**Type:** Injection / Logic Flaw

`account` uppercased and used to construct env var names with no validation. Empty or special-character account names could read arbitrary env vars or cause protocol errors.

**Fix:** Validate account names against `^[a-zA-Z0-9_-]+$`.

---

### H-06: No Timeout on IMAP/SMTP Connections

**File:** `app/mail.py` lines 141-148, 411-417
**Type:** Denial of Service

IMAP/SMTP connections created without timeouts. Slow remote servers block async worker threads indefinitely via `asyncio.to_thread`, enabling thread exhaustion DoS.

**Fix:** Add socket timeouts and wrap `asyncio.to_thread` with `asyncio.wait_for`.

---

### H-07: No Size Limits on Request Bodies

**File:** `app/main.py`
**Type:** Denial of Service

No maximum body size enforced. Large payloads to `/messages/send` (`body` field) or `/messages/search` (`criteria` dict) can cause memory exhaustion.

**Fix:** Add FastAPI body size middleware or Pydantic `max_length` constraints on string fields.

---

### H-08: No Email Address Validation in SendRequest

**File:** `app/mail.py` lines 90-98
**Type:** Input Validation

`from_addr`, `to`, `cc`, `bcc` accept arbitrary strings with no email format validation, enabling spoofing and SMTP errors that leak server details.

**Fix:** Use Pydantic's `EmailStr` type for email address fields.

---

## MEDIUM

### M-01: No Connection Pooling — Per-Request IMAP/SMTP Connections

**File:** `app/mail.py` (all operations)
**Type:** Performance / DoS

Every API call opens new IMAP/SMTP connection. High latency from TCP+TLS+login overhead. DoS amplification: 100 req/s forces 100 concurrent IMAP connections.

**Fix:** Implement connection pool reusing idle connections within a TTL.

---

### M-02: No Validation of `since_uid` as Integer

**File:** `app/mail.py` line 250
**Type:** Input Validation

`since_uid` cast to `int()` without validation. Non-numeric value raises `ValueError` caught by generic handler, returned as 500 with leaked value.

**Fix:** Add Pydantic validator checking `since_uid` is numeric.

---

### M-03: `os._exit(1)` on Background Task Failure

**File:** `app/main.py` line 111
**Type:** Reliability

`os._exit(1)` terminates process immediately without cleanup, dropping pending requests and potentially corrupting SQLite WAL.

**Fix:** Use graceful shutdown via `sys.exit(1)` or health check that signals unhealthiness.

---

### M-04: No Session Invalidation on Password Change

**File:** `venv/Lib/site-packages/redberry_webkit/auth.py` lines 111-131
**Type:** Session Management

JWT signing secret not rotated on password change. All existing sessions remain valid after password reset.

**Fix:** Rotate signing secret in `set_password()`.

---

### M-05: No Brute-Force Protection on API Token Endpoints

**File:** `app/main.py` lines 73-96
**Type:** Authentication

API token validation has no IP-based blocking like dashboard login. Only configurable slowapi rate limiter protects these endpoints.

**Fix:** Add per-IP failed-attempt tracking for API auth, same as dashboard.

---

### M-06: No `max_length` Constraints on Pydantic Models

**File:** `app/mail.py` lines 47-98
**Type:** Input Validation

String fields (`account`, `folder`, `subject`, `body`, `uid`) have no `max_length`, allowing oversized payloads through validation layer.

**Fix:** Add `max_length` to all string Pydantic fields.

---

### M-07: `_bool_env` Accepts Typos as True

**File:** `app/mail.py` line 106
**Type:** Configuration

`_bool_env` returns `True` for any value not in `("false", "0", "no")`. A typo like `"flase"` silently enables SSL/STARTTLS.

**Fix:** Only accept `"true"`, `"1"`, `"yes"` as True; everything else is False.

---

### M-08: No Retention Purge for Metrics Database

**File:** `app/metrics.py`
**Type:** Resource Exhaustion

`MetricsStore.purge_old()` exists but is never called. SQLite metrics DB grows unbounded, eventually consuming disk space and slowing queries.

**Fix:** Add periodic purge task in application lifespan.

---

### M-09: No Input Validation on `folder` Name

**File:** `app/mail.py` lines 246, 276, 340, 355, 368, 388
**Type:** Injection

Folder names wrapped in double quotes but not escaped. A folder name containing `"` breaks the IMAP command and potentially injects commands.

**Fix:** Escape double quotes in folder names or validate against `^[a-zA-Z0-9_/.-]+$`.

---

### M-10: No `Content-Security-Policy` for NiceGUI Dashboard

**File:** `app/ui/pages.py`
**Type:** XSS Mitigation

NiceGUI dashboard uses inline HTML slots with no CSP header. XSS vulnerability in NiceGUI or custom components has no mitigation layer.

**Fix:** Set strict CSP via FastAPI middleware or NiceGUI page options.

---

## LOW

### L-01: Password Length Check Only in CLI

**File:** `scripts/set_password.py` lines 40-42
**Type:** Defense in Depth

8-character minimum length enforced in CLI script but not in `AuthManager.set_password()`.

**Fix:** Move length check into `AuthManager.set_password()`.

---

### L-02: No Password Complexity Requirements

**File:** `scripts/set_password.py` lines 40-42
**Type:** Authentication

Only minimum length checked. No mixed case, digits, or special characters required.

**Fix:** Add complexity requirements in `AuthManager.set_password()`.

---

### L-03: JWT Token Lacks Context Claims

**File:** `venv/Lib/site-packages/redberry_webkit/auth.py` lines 150-151
**Type:** Session Management

JWT contains only `exp` claim. No `iat`, `jti`, or IP address claims for additional validation.

**Fix:** Add `iat`, `jti` (unique token ID), and optionally `ip` claims.

---

### L-04: No Health Check in Docker Compose

**Files:** `docker-compose.yml`, `docker-compose-dev.yml`
**Type:** Operations

No `healthcheck` defined. Docker cannot determine if the app is actually ready.

**Fix:** Add health check against `/health` endpoint.

---

### L-05: Log File Growth in Docker Container

**File:** `app/main.py` lines 48-49
**Type:** Operations

`RotatingFileHandler` writes to /app/data/logs/app.log inside container. Docker logs should go to stdout/stderr.

**Fix:** Disable file logging when running in Docker.

---

### L-06: `resolve_env_path()` Parses CLI Args on Every Init

**File:** `venv/Lib/site-packages/redberry_webkit/env_resolver.py` lines 13-21
**Type:** Reliability

`resolve_env_path()` calls `argparse.parse_known_args()` on every invocation, potentially interfering with app's own argument parsing.

**Fix:** Cache the resolved path after first call.

---

### L-07: No `frozen=True` on Pydantic Models

**File:** `app/mail.py` lines 39-98
**Type:** Code Quality

Pydantic models mutable by default. Accidental mutation of request objects possible.

**Fix:** Add `model_config = ConfigDict(frozen=True)` to all request models.

---

### L-08: `scripts/run.bat` Uses `pip install` Without `--no-deps` or Hash Pinning

**File:** `scripts/run.bat` line 12
**Type:** Supply Chain

Dependencies installed without hash pinning or version freeze. Compromised PyPI package could replace dependency.

**Fix:** Use `pip freeze > requirements-lock.txt` and install from lock file with `--require-hashes`.

---

### L-09: `scripts/run.bat` Checks for `fastapi` Package as Proxy for All Dependencies

**File:** `scripts/run.bat` line 10
**Type:** Reliability

Script checks for `fastapi` in site-packages as proxy for all deps. If only fastapi is installed but other deps are missing, script proceeds anyway.

**Fix:** Run `pip install -r requirements.txt` unconditionally, or check for marker file.

---

### L-10: `scripts/checks.bat` Exits on First Failure

**File:** `scripts/checks.bat` lines 16-17
**Type:** DX

`exit /b 1` after ruff failure prevents mypy and pytest from running. Developer must fix ruff before seeing other issues.

**Fix:** Run all checks and report all failures at the end.

---

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 6 |
| HIGH     | 8 |
| MEDIUM   | 10 |
| LOW      | 10 |

### Top 5 Immediate Actions

1. **Fix IMAP/SMTP injection** (C-01, C-02, C-03, C-05) — sanitize all user input before it reaches IMAP/SMTP commands and email headers
2. **Fix API token timing oracle** (C-04) — replace `any()` with `sum()` pattern
3. **Stop leaking error details** (C-06) — return generic 500 messages, log full details server-side
4. **Add CSRF protection** (H-01) — protect dashboard state-changing endpoints
5. **Add input validation** (H-05, H-08, M-02, M-06) — validate account names, email addresses, UIDs, and string lengths at the Pydantic model level

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from nicegui import app as ng_app
from nicegui import ui
from redberry_webkit.timezone_utils import resolve_timezone

from app import mail
from app.config import config
from app.metrics import metrics
from app.ui import api_client

APP_NAME = "IMAP REST"
DISPLAY_TZ = resolve_timezone(os.getenv("TZ", "UTC"))

_RATE_LIMIT_RE = re.compile(r"^\d+/(second|minute|hour|day)$")

NavItem = tuple[str, str, str]
NAV_ITEMS: list[NavItem] = [
    ("Dashboard", "dashboard", "/"),
    ("Posta", "mail", "/mail"),
    ("Test API", "fact_check", "/api-test"),
    ("Impostazioni", "settings", "/config"),
]


def _page_setup(section_title: str) -> Any:
    ui.page_title(f"{section_title} — {APP_NAME}")
    return ui.dark_mode(value=ng_app.storage.user.get("dark_mode", True))


def _header(
    page_title: str,
    nav_items: list[NavItem],
    current: str = "",
    *,
    dark: Any = None,
    extra_actions: Callable[[], None] | None = None,
) -> None:
    with ui.header().classes("bg-primary text-white items-center q-px-md q-gutter-sm"):
        ui.label(page_title).classes("text-h6 text-weight-bold col")

        for label, icon, path in nav_items:
            if label.lower() != current.lower():
                ui.button(icon=icon, on_click=lambda p=path: ui.navigate.to(p)).props(
                    "flat color=white round"
                ).tooltip(label)

        if extra_actions is not None:
            extra_actions()

        if dark is not None:

            def _toggle_dark() -> None:
                dark.toggle()
                ng_app.storage.user["dark_mode"] = dark.value

            ui.button(icon="contrast", on_click=_toggle_dark).props("flat color=white round").tooltip(
                "Tema chiaro/scuro"
            )

        ui.label(APP_NAME).classes("text-body2").style("opacity:0.6")


def _logout_action() -> None:
    ui.button(
        icon="logout", on_click=lambda: ui.run_javascript("window.location.href='/auth/logout'")
    ).props("flat color=white round").tooltip("Esci")


def _footer(right_content: str = "") -> None:
    with ui.footer().classes("bg-primary text-white q-px-md q-py-xs row items-center"):
        ui.label(APP_NAME).classes("text-caption col").style("opacity:0.6")
        if right_content:
            ui.label(right_content).classes("text-body2 text-weight-bold")


def _metric_card(label: str, value: str, color: str = "primary") -> None:
    with ui.card().classes("q-pa-md col"):
        ui.label(label).classes("text-caption text-grey-6 text-uppercase")
        ui.label(value).classes(f"text-h5 text-weight-bold text-{color}")


@ui.page("/")
async def dashboard_page() -> None:
    dark = _page_setup("Dashboard")
    _header("Dashboard", NAV_ITEMS, current="Dashboard", dark=dark, extra_actions=_logout_action)

    with ui.column().classes("q-pa-md full-width"):
        metrics_row = ui.row().classes("full-width q-gutter-md")
        table_container = ui.column().classes("full-width")
        refresh_lbl = ui.label("").classes("text-caption text-grey-6").style("text-align:right; width:100%")

        async def _render() -> None:
            stats = await metrics.get_stats()
            metrics_row.clear()
            with metrics_row:
                _metric_card("Richieste totali", str(stats["total_requests"]), "primary")
                _metric_card("Richieste ok", str(stats["ok_requests"]), "positive")
                _metric_card(
                    "Errori", str(stats["error_requests"]), "negative" if stats["error_requests"] else "primary"
                )
                _metric_card("Durata media (s)", f"{stats['avg_duration_s']:.2f}", "info")

            # redact_sensitive=True: error_message/extra can carry IMAP/SMTP server
            # responses (hostnames, addresses, ...) — not appropriate for a dashboard
            # table (REPORT.md H-04), even though full detail stays in the raw SQLite log.
            history = await metrics.get_history(redact_sensitive=True)
            table_container.clear()
            with table_container:
                rows = [
                    {
                        "id": str(index),
                        "timestamp": datetime.fromtimestamp(record.timestamp, tz=DISPLAY_TZ).strftime("%H:%M:%S"),
                        "endpoint": (record.extra or {}).get("endpoint", ""),
                        "account": (record.extra or {}).get("account", ""),
                        "status": record.status,
                        "duration_s": f"{record.duration_s:.2f}",
                        "error_message": record.error_message or "",
                    }
                    for index, record in enumerate(history)
                ]
                tbl = ui.table(
                    columns=[
                        {"name": "timestamp", "label": "Ora", "field": "timestamp"},
                        {"name": "endpoint", "label": "Endpoint", "field": "endpoint"},
                        {"name": "account", "label": "Account", "field": "account"},
                        {"name": "status", "label": "Status", "field": "status"},
                        {"name": "duration_s", "label": "Durata (s)", "field": "duration_s"},
                        {"name": "error_message", "label": "Errore", "field": "error_message"},
                    ],
                    rows=rows,
                    row_key="id",
                ).classes("full-width")
                tbl.add_slot(
                    "body-cell-status",
                    """
                    <q-td :props="props">
                      <q-badge :color="props.value === 'ok' ? 'positive' : 'negative'" :label="props.value" />
                    </q-td>
                    """,
                )

            refresh_enabled = config.get_bool("REFRESH_ENABLED")
            interval = config.get_int("REFRESH_INTERVAL", 5)
            if refresh_enabled:
                now = datetime.now(DISPLAY_TZ).strftime("%H:%M:%S")
                refresh_lbl.set_text(f"Aggiornato: {now} · auto-refresh {interval}s")
            else:
                refresh_lbl.set_text("auto-refresh disabilitato")

        await _render()

        _elapsed_s = 0.0

        async def _tick() -> None:
            nonlocal _elapsed_s
            if not config.get_bool("REFRESH_ENABLED"):
                refresh_lbl.set_text("auto-refresh disabilitato")
                return
            _elapsed_s += 1.0
            if _elapsed_s >= config.get_int("REFRESH_INTERVAL", 5):
                _elapsed_s = 0.0
                await _render()

        ui.timer(1.0, _tick)

    _footer()


@ui.page("/mail")
def mail_page() -> None:
    dark = _page_setup("Posta")
    _header("Posta", NAV_ITEMS, current="Posta", dark=dark, extra_actions=_logout_action)

    accounts = mail.list_configured_accounts()

    with ui.column().classes("q-pa-md full-width"):
        if not accounts:
            ui.label("Nessun account configurato (manca IMAP_<NOME>_HOST in .env).").classes("text-negative")

        with ui.row().classes("q-gutter-md items-end"):
            account_select = ui.select(
                accounts, label="Account", value=accounts[0] if accounts else None
            ).classes("w-48")
            folder_select = ui.select([], label="Cartella").classes("w-64")
            refresh_button = ui.button(icon="refresh").props("flat round color=primary").tooltip("Aggiorna")

        table_container = ui.column().classes("full-width")

        async def _open_message(account: str, folder: str, uid: str) -> None:
            try:
                status, body = await api_client.get_message(account, folder, uid)
            except httpx.RequestError as exc:
                ui.notify(f"Errore di rete: {exc}", color="negative")
                return
            if status != 200 or not isinstance(body, dict):
                ui.notify(f"Errore nel recupero messaggio (HTTP {status})", color="negative")
                return
            with ui.dialog() as dialog, ui.card().classes("full-width"):
                ui.label(body.get("subject") or "(nessun oggetto)").classes("text-h6 text-weight-bold")
                ui.label(f"Da: {body.get('from', '')}").classes("text-caption text-grey-6")
                ui.label(f"A: {body.get('to', '')}").classes("text-caption text-grey-6")
                ui.label(f"Data: {body.get('date', '')}").classes("text-caption text-grey-6")
                ui.separator()
                if body.get("body_html"):
                    ui.html(body["body_html"]).classes("full-width")
                elif body.get("body_text"):
                    ui.label(body["body_text"]).style("white-space: pre-wrap")
                else:
                    ui.label("(nessun corpo testuale)").classes("text-grey-6")
                attachments = body.get("attachments", [])
                if attachments:
                    ui.separator()
                    ui.label("Allegati").classes("text-caption text-grey-6 text-uppercase")
                    for att in attachments:
                        ui.label(f"{att.get('filename', '?')} ({att.get('size', 0)} byte)")
                ui.button("Chiudi", on_click=dialog.close).props("flat")
            dialog.open()

        async def _load_messages() -> None:
            account = account_select.value
            folder = folder_select.value
            table_container.clear()
            if not account or not folder:
                return
            try:
                status, body = await api_client.list_messages(account, folder, limit=50)
            except httpx.RequestError as exc:
                with table_container:
                    ui.label(f"Errore di rete: {exc}").classes("text-negative")
                return
            if status != 200:
                with table_container:
                    ui.label(f"Errore nel recupero messaggi (HTTP {status})").classes("text-negative")
                return
            messages = body.get("messages", []) if isinstance(body, dict) else []
            with table_container:
                if not messages:
                    ui.label("Nessun messaggio in questa cartella.").classes("text-grey-6")
                    return
                rows = [
                    {
                        "uid": m["uid"],
                        "date": m.get("date", ""),
                        "from": m.get("from", ""),
                        "subject": m.get("subject", ""),
                        "flags": ", ".join(m.get("flags", [])),
                    }
                    for m in messages
                ]
                tbl = ui.table(
                    columns=[
                        {"name": "date", "label": "Data", "field": "date"},
                        {"name": "from", "label": "Da", "field": "from"},
                        {"name": "subject", "label": "Oggetto", "field": "subject"},
                        {"name": "flags", "label": "Flags", "field": "flags"},
                    ],
                    rows=rows,
                    row_key="uid",
                ).classes("full-width cursor-pointer")

                async def _on_row_click(e: Any) -> None:
                    # Quasar's row-click emits (evt, row, index) — e.args mirrors that order.
                    row = e.args[1]
                    await _open_message(account, folder, row["uid"])

                tbl.on("rowClick", _on_row_click)

        async def _load_folders(_: Any = None) -> None:
            account = account_select.value
            table_container.clear()
            if not account:
                folder_select.set_options([])
                return
            try:
                status, body = await api_client.folders(account)
            except httpx.RequestError as exc:
                with table_container:
                    ui.label(f"Errore di rete: {exc}").classes("text-negative")
                return
            if status != 200:
                with table_container:
                    ui.label(f"Errore nel recupero cartelle (HTTP {status})").classes("text-negative")
                return
            names = body.get("folders", []) if isinstance(body, dict) else []
            folder_select.set_options(names, value="INBOX" if "INBOX" in names else (names[0] if names else None))
            await _load_messages()

        account_select.on_value_change(_load_folders)
        folder_select.on_value_change(lambda _: _load_messages())
        refresh_button.on_click(_load_messages)

        if accounts:
            # Not `await`ed here: this is a real IMAP round-trip and @ui.page has a
            # 3s deadline to build the initial response (nicegui/page.py response_timeout)
            # — a slow/remote mailbox blows past that and the page never loads. Fire it
            # once, shortly after the page has already rendered.
            ui.timer(0.1, _load_folders, once=True)

    _footer()


@ui.page("/api-test")
def api_test_page() -> None:
    dark = _page_setup("Test API")
    _header("Test API", NAV_ITEMS, current="Test API", dark=dark, extra_actions=_logout_action)

    accounts = mail.list_configured_accounts()

    with ui.column().classes("q-pa-md full-width"):
        if not accounts:
            ui.label("Nessun account configurato (manca IMAP_<NOME>_HOST in .env).").classes("text-negative")

        with ui.row().classes("q-gutter-md items-end"):
            account_select = ui.select(
                accounts, label="Account", value=accounts[0] if accounts else None
            ).classes("w-48")
            folder_input = ui.input("Cartella", value="INBOX").classes("w-48")
            run_button = ui.button("Esegui test API", icon="play_arrow").props("color=primary")

        results_container = ui.column().classes("full-width")

        async def _run_tests() -> None:
            account = account_select.value
            folder = folder_input.value.strip() or "INBOX"
            if not account:
                ui.notify("Seleziona un account", color="warning")
                return

            run_button.props("loading")
            checks: list[dict[str, str]] = []

            def _add(name: str, esito: str, detail: str = "") -> None:
                checks.append(
                    {"id": str(len(checks)), "check": name, "esito": esito, "dettaglio": detail}
                )

            def _add_http(name: str, status: int, expected: int, detail: str = "") -> None:
                esito = "ok" if status == expected else "error"
                dettaglio = f"HTTP {status}" + (f" — {detail}" if detail else "")
                _add(name, esito, dettaglio)

            try:
                status, body = await api_client.health()
                _add_http("GET /health", status, 200, str(body))

                status, body = await api_client.folders(account)
                folders_list = body.get("folders") if isinstance(body, dict) else None
                _add_http("POST /folders", status, 200, f"folders={folders_list}")

                status, body = await api_client.list_messages(account, folder, limit=5)
                count = body.get("count") if isinstance(body, dict) else None
                _add_http("POST /messages/list", status, 200, f"count={count}")
                messages = body.get("messages", []) if status == 200 and isinstance(body, dict) else []

                status, body = await api_client.search(account, folder, {}, limit=3)
                count = body.get("count") if isinstance(body, dict) else None
                _add_http("POST /messages/search", status, 200, f"count={count}")

                if messages:
                    uid = messages[0]["uid"]
                    status, body = await api_client.get_message(account, folder, uid)
                    subject = body.get("subject", "") if isinstance(body, dict) else ""
                    _add_http(f"POST /messages/get (uid={uid})", status, 200, f"subject={subject!r}")
                else:
                    _add(
                        "POST /messages/get",
                        "skip",
                        f"nessun messaggio nella cartella '{folder}'",
                    )

                status, body = await api_client.folders("__unknown_account__")
                _add_http("POST /folders account sconosciuto", status, 400, str(body))
            except httpx.RequestError as exc:
                ui.notify(f"Impossibile contattare l'API: {exc}", color="negative")
                run_button.props(remove="loading")
                return

            run_button.props(remove="loading")
            results_container.clear()
            with results_container:
                tbl = ui.table(
                    columns=[
                        {"name": "check", "label": "Check", "field": "check"},
                        {"name": "esito", "label": "Esito", "field": "esito"},
                        {"name": "dettaglio", "label": "Dettaglio", "field": "dettaglio"},
                    ],
                    rows=checks,
                    row_key="id",
                ).classes("full-width")
                tbl.add_slot(
                    "body-cell-esito",
                    """
                    <q-td :props="props">
                      <q-badge
                        :color="props.value === 'ok' ? 'positive' : props.value === 'skip' ? 'warning' : 'negative'"
                        :label="props.value"
                      />
                    </q-td>
                    """,
                )

        run_button.on_click(_run_tests)

    _footer()


@ui.page("/config")
def config_page() -> None:
    dark = _page_setup("Impostazioni")
    _header("Impostazioni", NAV_ITEMS, current="Impostazioni", dark=dark, extra_actions=_logout_action)

    with ui.column().classes("q-pa-md full-width"):
        cur = config.get_public()

        with ui.card().classes("q-pa-md full-width"):
            ui.label("Interfaccia").classes("text-caption text-grey-6 text-uppercase")
            refresh_switch = ui.switch(
                "Auto-refresh dashboard", value=cur.get("REFRESH_ENABLED", "true").lower() in ("true", "1", "yes")
            )
            ui.badge("hot-reload").props("color=positive")
            interval_input = ui.number(
                "Intervallo auto-refresh (s)", value=int(cur.get("REFRESH_INTERVAL", "5") or 5), min=1
            ).classes("full-width")

        with ui.card().classes("q-pa-md full-width"):
            ui.label("API").classes("text-caption text-grey-6 text-uppercase")
            rate_limit_input = ui.input(
                "Rate limit (slowapi, es. 20/minute)", value=cur.get("RATE_LIMIT", "")
            ).props('hint="Applicato ad ogni richiesta, hot-reload senza restart"').classes("full-width")
            ui.badge("hot-reload").props("color=positive")
            token_input = ui.input(
                "API token (Bearer, separati da virgola)", password=True, password_toggle_button=True
            ).props('hint="Vuoto = endpoint aperti (uso locale). Non mostrato per sicurezza."').classes("full-width")

        def _save() -> None:
            rate_limit = rate_limit_input.value.strip()
            if not _RATE_LIMIT_RE.match(rate_limit):
                ui.notify("Rate limit non valido (formato atteso: 20/minute)", color="negative")
                return
            updates = {
                "REFRESH_ENABLED": "true" if refresh_switch.value else "false",
                "REFRESH_INTERVAL": str(int(interval_input.value or 5)),
                "RATE_LIMIT": rate_limit,
            }
            if token_input.value.strip():
                updates["API_TOKENS"] = token_input.value.strip()
            config.update_many(updates)
            token_input.value = ""
            ui.notify("Configurazione salvata", color="positive")

        ui.button("Salva", on_click=_save).props("color=primary")

    _footer()

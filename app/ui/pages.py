from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from nicegui import app as ng_app
from nicegui import ui
from redberry_webkit.timezone_utils import resolve_timezone

from app.config import config
from app.metrics import metrics

APP_NAME = "IMAP REST"
DISPLAY_TZ = resolve_timezone(os.getenv("TZ", "UTC"))

_RATE_LIMIT_RE = re.compile(r"^\d+/(second|minute|hour|day)$")

NavItem = tuple[str, str, str]
NAV_ITEMS: list[NavItem] = [
    ("Dashboard", "dashboard", "/"),
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

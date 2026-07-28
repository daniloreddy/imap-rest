from __future__ import annotations

from redberry_webkit.config import ConfigManager

_DEFAULTS: dict[str, str] = {
    "REFRESH_ENABLED": "true",
    "REFRESH_INTERVAL": "5",
    "RATE_LIMIT": "20/minute",
    "API_TOKENS": "",
    "METRICS_RETENTION_DAYS": "30",
}
_SECRET_KEYS: set[str] = {"API_TOKENS"}

config = ConfigManager(defaults=_DEFAULTS, secret_keys=_SECRET_KEYS)

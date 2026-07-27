from __future__ import annotations

from pathlib import Path

from redberry_webkit.metrics import MetricsRecord, MetricsStore

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

metrics = MetricsStore(db_path=DATA_DIR / "metrics.db")

__all__ = ["MetricsRecord", "metrics"]

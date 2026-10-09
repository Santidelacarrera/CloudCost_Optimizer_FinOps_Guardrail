"""Utilidades compartidas por los conectores Azure y GCP (el de AWS conserva las suyas)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import fmean
from typing import Any

from ..domain.models import NormalizedResource
from ..domain.pricing import DAYS_PER_MONTH
from .base import CollectionResult, CostRecord

WINDOW_DAYS = 14


def parse_ts(value: str | None) -> datetime | None:
    """ISO 8601 de Azure (`2024-05-01T10:00:00.123Z`) o GCP (`2024-05-01T03:00:00.000-07:00`) -> aware UTC."""
    if not value:
        return None
    text = value.strip()                                     # Python ≥ 3.11 acepta «Z» y fracciones de cualquier longitud
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_days(ts: datetime | None, now: datetime | None = None) -> int | None:
    return max(0, ((now or datetime.now(timezone.utc)) - ts).days) if ts else None


def metric_summary(avg_points: list[float], max_points: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if avg_points:
        out["avg"] = round(fmean(avg_points), 2)
        out["hours_with_data"] = len(avg_points)
    if max_points:
        out["max"] = round(max(max_points), 2)
    return out


def finish(result: CollectionResult, resources: list[NormalizedResource], warnings: list[str], partial: bool,
           today: date | None = None) -> CollectionResult:
    """Costos estimados por tabla de precios (14 días planos): ni Azure ni GCP exponen costo por recurso en una sola llamada barata."""
    day0 = today or date.today()
    result.resources = resources
    for res in resources:
        daily = round(res.monthly_cost / DAYS_PER_MONTH, 4)
        result.costs += [CostRecord(res.resource_id, day0 - timedelta(days=d), daily, res.service, res.region, "estimate")
                         for d in range(1, WINDOW_DAYS + 1)]
    result.warnings = warnings
    result.partial = partial
    return result


class UnverifiedImages(dict):
    """Sustituye al mapa «snapshot -> imágenes que lo usan» cuando no se pudo listar: todo snapshot figura como en uso.

    Es la opción segura: sin saber qué imágenes dependen de él, no se propone borrarlo.
    """

    def get(self, key, default=None):
        return ["(imágenes no verificadas)"]

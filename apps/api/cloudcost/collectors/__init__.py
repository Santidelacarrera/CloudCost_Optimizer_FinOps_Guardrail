from __future__ import annotations

from typing import Any

from ..secrets import SecretResolver
from .base import CollectionResult, Collector, CostRecord

__all__ = ["CollectionResult", "Collector", "CostRecord", "get_collector"]


def get_collector(account: dict[str, Any], *, secrets: SecretResolver, demo_enabled: bool,
                  use_cost_explorer: bool = False, on_api_error=None) -> Collector:
    provider = account["provider"]
    if provider == "demo":
        if not demo_enabled:
            raise PermissionError("El proveedor 'demo' está deshabilitado (DEMO_ENABLED=false)")
        from .demo import DemoCollector

        return DemoCollector()
    if provider == "aws":
        from .aws import AwsCollector

        return AwsCollector(account, secrets, use_cost_explorer=use_cost_explorer, on_api_error=on_api_error)
    raise NotImplementedError(f"Conector '{provider}' aún no disponible (roadmap: Azure en Fase 2, GCP en Fase 4)")

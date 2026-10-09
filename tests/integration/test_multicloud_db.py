"""Cuentas Azure y GCP contra PostgreSQL real: alta (solo referencias a secretos), escaneo con colector simulado,
recursos y recomendaciones persistidos con su proveedor. Se omite sin DATABASE_URL / DATABASE_ADMIN_URL."""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


class _Client:
    """Responde con un disco huérfano por proveedor; cualquier otra consulta, vacía."""

    def __init__(self, provider: str, suffix: str):
        self.provider, self.suffix = provider, suffix

    def get_json(self, url, params=None):
        if self.provider == "azure" and url.endswith("/providers/Microsoft.Compute/disks"):
            return {"value": [{"id": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/disks/orphan-{self.suffix}",
                               "name": f"orphan-{self.suffix}", "location": "eastus", "sku": {"name": "Premium_LRS"},
                               "properties": {"diskState": "Unattached", "diskSizeGB": 512, "timeCreated": "2025-01-01T00:00:00Z",
                                              "LastOwnershipUpdateTime": "2025-02-01T00:00:00Z"}}]}
        if self.provider == "gcp" and url.endswith("/aggregated/disks"):
            return {"items": {"zones/us-central1-a": {"disks": [{
                "name": f"orphan-{self.suffix}", "zone": "https://x/projects/p/zones/us-central1-a", "sizeGb": "1000", "status": "READY",
                "type": "https://x/diskTypes/pd-ssd", "creationTimestamp": "2025-01-01T00:00:00.000-07:00"}]}}}
        return {"value": [], "items": {}}


def test_azure_and_gcp_accounts_scan_and_persist():
    _require_db()
    import psycopg
    from cloudcost.collectors.azure import AzureCollector
    from cloudcost.collectors.gcp import GcpCollector
    from cloudcost.config import Settings
    from cloudcost.db import tenant_tx
    from cloudcost.routers.admin import create_account, list_accounts
    from cloudcost.schemas import CloudAccountIn
    from cloudcost.secrets import SecretResolver
    from cloudcost.security import Principal
    from cloudcost.services import scan_service

    suffix = uuid4().hex[:6]
    ORG = uuid4()                                     # organización propia: no altera los totales de las demás pruebas
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as conn:
        conn.execute("insert into organizations (id, name, slug) values (%s, 'Multicloud', %s)", (str(ORG), f"mc-{suffix}"))
    admin = Principal(user_id="test:admin", email="admin@example.com", org_id=ORG, role="ADMIN")
    settings = Settings(demo_enabled=True)
    cases = [
        ("azure", CloudAccountIn(provider="azure", account_ref=str(uuid4()), display_name=f"Azure {suffix}", tenant_id=SUB, client_id=SUB,
                                 credentials_ref="env:CC_SECRET_AZ_TEST"), AzureCollector, "disk"),
        ("gcp", CloudAccountIn(provider="gcp", account_ref=f"proj-{suffix}-test", display_name=f"GCP {suffix}",
                               credentials_ref="env:CC_SECRET_GCP_TEST"), GcpCollector, "pd"),
    ]
    for provider, body, collector_cls, service in cases:
        row = create_account(body, admin, settings)
        assert row["provider"] == provider and row["regions"] == ["all"]
        with tenant_tx(ORG) as conn:
            stored = conn.execute("select provider_config from cloud_accounts where id = %s", (str(row["id"]),)).fetchone()["provider_config"]
            scan_id = conn.execute("insert into scans (organization_id, cloud_account_id, requested_by) values (%s, %s, 'test') returning id",
                                   (str(ORG), str(row["id"]))).fetchone()["id"]
        assert stored["credentials_ref"].startswith("env:") and set(stored) <= {"tenant_id", "client_id", "credentials_ref"}
        client = _Client(provider, suffix)
        stats = scan_service.run_scan(
            ORG, scan_id, settings=settings, secrets=SecretResolver(),
            collector_factory=lambda account, **kw: collector_cls(account, kw["secrets"], client=client))
        assert stats["resources_seen"] == 1 and stats["findings"] == 1 and not stats["partial"], stats
        with tenant_tx(ORG) as conn:
            res = conn.execute("select provider, service, volume_type, attached, unattached_days from resources "
                               "where cloud_account_id = %s", (str(row["id"]),)).fetchone()
            rec = conn.execute("select title, status, estimated_monthly_savings from recommendations where cloud_account_id = %s",
                               (str(row["id"]),)).fetchone()
            costs = conn.execute("select count(*) as n, min(provider) as p from cost_records where cloud_account_id = %s",
                                 (str(row["id"]),)).fetchone()
        assert (res["provider"], res["service"], res["attached"]) == (provider, service, False) and res["unattached_days"] > 300
        assert rec["status"] == "PENDING_APPROVAL" and float(rec["estimated_monthly_savings"]) > 50
        assert costs["n"] == 14 and costs["p"] == provider
    assert {a["provider"] for a in list_accounts(admin)} == {"azure", "gcp"}

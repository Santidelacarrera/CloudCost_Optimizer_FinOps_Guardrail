"""Proyección de ahorro contra PostgreSQL real (RLS incluida). Requiere DATABASE_URL y DATABASE_ADMIN_URL; si no, se omite."""
from __future__ import annotations

import os
import unittest
from datetime import date
from uuid import UUID, uuid4

ORG = UUID("11111111-1111-1111-1111-111111111111")


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def test_la_serie_sale_de_la_base_y_es_coherente():
    _require_db()
    from cloudcost.db import tenant_tx
    from cloudcost.services import projection

    with tenant_tx(ORG) as conn:
        out = projection.build(conn, today=date(2026, 10, 8), months_back=3, months_ahead=6)
        baseline = float(conn.execute("select coalesce(sum(monthly_cost), 0) as v from resources where active").fetchone()["v"])
    assert out["baseline_monthly"] == round(baseline, 2)
    assert [p["month"] for p in out["points"]][3] == "2026-10" and len(out["points"]) == 3 + 1 + 6
    future = [p for p in out["points"] if p["current"] is not None]
    assert all(p["optimized"] <= p["current"] and p["optimized"] <= p["expected"] <= p["current"] for p in future)
    assert out["cumulative_savings"]["optimized"] >= out["cumulative_savings"]["expected"] >= 0


def test_el_historial_sale_de_cost_records_y_aisla_organizaciones():
    _require_db()
    import psycopg
    from cloudcost.db import tenant_tx
    from cloudcost.services import projection
    from psycopg.rows import dict_row

    other = uuid4()
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        c.execute("insert into organizations (id, name, slug) values (%s, %s, %s)", (str(other), "Proyección", f"proj-{other.hex[:8]}"))
        acc = c.execute("insert into cloud_accounts (organization_id, provider, account_ref, display_name) values (%s, 'aws', %s, 'x') returning id",
                        (str(other), f"p{other.hex[:8]}")).fetchone()["id"]
        for day, amount in (("2026-08-10", 100), ("2026-08-20", 50.5), ("2026-09-05", 300)):
            c.execute("insert into cost_records (organization_id, cloud_account_id, provider, usage_date, amount, service) values (%s, %s, 'aws', %s, %s, 'ec2')",
                      (str(other), acc, day, amount))
    with tenant_tx(other) as conn:
        out = projection.build(conn, today=date(2026, 10, 8), months_back=3, months_ahead=2)
    hist = {p["month"]: p["actual"] for p in out["points"] if p["month"] < "2026-10"}
    assert hist == {"2026-07": None, "2026-08": 150.5, "2026-09": 300.0}
    assert out["baseline_monthly"] == 0.0 and out["potential_monthly"] == 0.0
    with tenant_tx(ORG) as conn:                                  # otra organización no ve esos costos
        mine = projection.build(conn, today=date(2026, 10, 8), months_back=3, months_ahead=2)
    assert {p["actual"] for p in mine["points"] if p["month"] in ("2026-08", "2026-09")} != {150.5, 300.0}

from __future__ import annotations

from typing import Any

from psycopg import Connection

_OPEN = "('PENDING_APPROVAL','APPROVED','PR_CREATED','MERGED','DEPLOYED')"


def summary(conn: Connection) -> dict[str, Any]:
    spend = conn.execute("select coalesce(sum(monthly_cost), 0) as v from resources where active").fetchone()["v"]
    recs = conn.execute(f"""
        select count(*) filter (where status in {_OPEN})                                        as open_recommendations,
               coalesce(sum(estimated_monthly_savings) filter (where status in {_OPEN}), 0)    as potential_savings,
               count(*) filter (where status = 'PENDING_APPROVAL')                              as pending_approval,
               count(*) filter (where risk = 'HIGH' and status in {_OPEN})                      as high_risk,
               count(*) filter (where status = 'VERIFIED')                                      as verified,
               count(*) filter (where status = 'REJECTED')                                      as rejected
          from recommendations""").fetchone()
    real = conn.execute("""
        select coalesce(sum(expected_monthly_savings), 0) as expected, coalesce(sum(observed_monthly_savings), 0) as observed
          from savings_verifications""").fetchone()
    spend_f, potential = float(spend), float(recs["potential_savings"])
    expected, observed = float(real["expected"]), float(real["observed"])
    by_status = {r["status"]: r["n"] for r in conn.execute("select status, count(*) as n from recommendations group by status")}
    top = conn.execute(f"""
        select r.id, r.title, r.estimated_monthly_savings, r.risk, r.confidence, r.status, r.priority
          from recommendations r where r.status in {_OPEN} order by r.estimated_monthly_savings desc limit 5""").fetchall()
    return {
        "monthly_spend": spend_f,
        "potential_savings": potential,
        "savings_pct": round(potential / spend_f * 100, 1) if spend_f > 0 else 0.0,
        "recommendations": recs["open_recommendations"],
        "pending_approval": recs["pending_approval"],
        "high_risk": recs["high_risk"],
        "verified": recs["verified"],
        "rejected": recs["rejected"],
        "realized_savings": observed,
        "expected_savings_verified": expected,
        "realization_pct": round(observed / expected * 100, 1) if expected > 0 else None,
        "by_status": by_status,
        "top_recommendations": top,
    }

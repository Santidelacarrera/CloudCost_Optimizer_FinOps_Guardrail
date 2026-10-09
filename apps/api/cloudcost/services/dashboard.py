from __future__ import annotations

from typing import Any

from psycopg import Connection

_OPEN = "('PENDING_APPROVAL','APPROVED','PR_CREATED','MERGED','DEPLOYED')"
_UNDECIDED = "('PROPOSED','PENDING_APPROVAL')"
_APPROVED_IN_FLIGHT = "('APPROVED','PR_CREATED','MERGED','DEPLOYED')"
# Observado con datos de facturación y atribuible al cambio / con otros factores que se movieron / declarado o sin datos de facturación.
_ATTRIBUTED = "data_grade = 'billing' and attribution in ('confirmed','partial','exceeds_model','not_applied')"
_CONFOUNDED = "data_grade = 'billing' and attribution = 'confounded'"


def breakdown(conn: Connection) -> dict[str, Any]:
    """Los tres ahorros que NUNCA se suman entre sí: estimado (sin decidir), aprobado (congelado, sin verificar) y observado."""
    est = conn.execute(f"""select count(*) as n, coalesce(sum(estimated_monthly_savings), 0) as v
                             from recommendations where status in {_UNDECIDED}""").fetchone()
    appr = conn.execute(f"""select count(*) as n, coalesce(sum(coalesce(approved_monthly_savings, estimated_monthly_savings)), 0) as v
                              from recommendations where status in {_APPROVED_IN_FLIGHT}""").fetchone()
    obs = conn.execute(f"""
        select count(*) as n,
               coalesce(sum(observed_monthly_savings) filter (where {_ATTRIBUTED}), 0)  as attributed,
               coalesce(sum(expected_monthly_savings) filter (where {_ATTRIBUTED}), 0)  as attributed_expected,
               coalesce(sum(observed_monthly_savings) filter (where {_CONFOUNDED}), 0)  as confounded,
               coalesce(sum(observed_monthly_savings) filter (where not ({_ATTRIBUTED}) and not ({_CONFOUNDED})), 0) as declared,
               count(*) filter (where {_ATTRIBUTED}) as n_attributed
          from savings_verifications""").fetchone()
    attributed, expected = float(obs["attributed"]), float(obs["attributed_expected"])
    return {
        "estimated": {"count": est["n"], "monthly": float(est["v"])},
        "approved": {"count": appr["n"], "monthly": float(appr["v"])},
        "observed": {"count": obs["n"], "attributed": attributed, "attributed_count": obs["n_attributed"],
                     "attributed_expected": expected, "confounded": float(obs["confounded"]), "declared": float(obs["declared"]),
                     "realization_pct_attributed": round(attributed / expected * 100, 1) if expected > 0 else None},
    }


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
        "savings_breakdown": breakdown(conn),
        "top_recommendations": top,
    }

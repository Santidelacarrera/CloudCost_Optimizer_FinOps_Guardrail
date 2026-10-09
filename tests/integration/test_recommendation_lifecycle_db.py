"""Calidad y coherencia de las recomendaciones entre escaneos, contra PostgreSQL real.

Cada prueba crea su propia organización: no dependen del orden, del seed ni de ejecuciones anteriores.
"""
from __future__ import annotations

import psycopg
import pytest
from cloudcost.collectors.base import AccountCostRecord
from cloudcost.services import evidence as ev
from cloudcost.services import workflow
from cloudcost.services.recommendations import WorkflowError
from dbkit import admin, idle_instance, instance, new_tenant, result


@pytest.fixture()
def t():
    return new_tenant("life")


def _evidence_rows(t, rec_id):
    with t.tx() as conn:
        return conn.execute("select * from recommendation_evidence where recommendation_id = %s order by created_at, id", (str(rec_id),)).fetchall()


# --------------------------------------------------------------------------- evidencia verificable
def test_first_scan_stores_versioned_evidence_whose_hash_is_also_in_the_audit_chain(t):
    stats = t.scan(result(instance()))
    assert stats["findings"] == 1 and stats["reconciliation"] == {}
    rec = t.rec("i-0aaa")
    (snap,) = _evidence_rows(t, rec["id"])
    assert snap["recommendation_version"] == 1 and snap["formula_id"] == "ec2_downsize.v1" and snap["reference_cost_source"] == "cost_explorer"
    assert float(snap["reference_cost"]) == 280.32 and str(snap["reference_window_start"]) == "2026-09-24"
    assert snap["assumptions"] and snap["evidence"]["estimate"]["expression"].startswith("ahorro_mensual")
    assert rec["evidence_hash"] == snap["evidence_hash"] == ev.recompute_hash(snap)
    (created,) = t.audit("RECOMMENDATION_CREATED", rec["id"])
    assert created["payload"]["evidence_hash"] == snap["evidence_hash"] and created["payload"]["formula_id"] == "ec2_downsize.v1"


def test_cost_noise_between_scans_neither_bumps_the_version_nor_adds_evidence(t):
    t.scan(result(instance(cost=280.32)))
    first = t.rec("i-0aaa")
    t.scan(result(instance(cost=280.90, cpu=12.5)))                                   # oscilación diaria de céntimos y décimas de CPU
    again = t.rec("i-0aaa")
    assert again["version"] == first["version"] == 1 and len(_evidence_rows(t, first["id"])) == 1
    assert float(again["current_monthly_cost"]) == 280.90                              # el dato vigente sí se actualiza


def test_material_change_bumps_the_version_and_stores_a_new_evidence_snapshot(t):
    t.scan(result(instance(cost=280.32)))
    first = t.rec("i-0aaa")
    t.scan(result(instance(cost=420.0)))                                              # +50 %: el aprobador debe volver a mirar
    again = t.rec("i-0aaa")
    assert again["version"] == 2
    rows = _evidence_rows(t, first["id"])
    assert [r["recommendation_version"] for r in rows] == [1, 2] and rows[0]["evidence_hash"] != rows[1]["evidence_hash"]
    assert all(ev.recompute_hash(r) == r["evidence_hash"] for r in rows)
    (upd,) = t.audit("EVIDENCE_UPDATED", first["id"])
    assert upd["payload"]["version_bumped"] is True and upd["payload"]["evidence_hash"] == rows[1]["evidence_hash"]


def test_evidence_and_baselines_are_append_only_for_the_application_role(t):
    t.scan(result(instance()))
    rec = t.rec("i-0aaa")
    for sql in ("update recommendation_evidence set confidence = 1", "delete from recommendation_evidence",
                "update savings_baselines set monthly_cost = 0", "delete from savings_baselines"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with t.tx() as conn:
                conn.execute(sql)
    assert len(_evidence_rows(t, rec["id"])) == 1


def test_tampering_with_stored_evidence_is_detected_against_the_hash_in_the_audit_chain(t):
    t.scan(result(instance()))
    rec = t.rec("i-0aaa")
    (created,) = t.audit("RECOMMENDATION_CREATED", rec["id"])
    admin("update recommendation_evidence set estimated_monthly_savings = 9999 where recommendation_id = %s", (str(rec["id"]),))   # dueño de la base
    with t.tx() as conn:
        (item,) = ev.history(conn, rec["id"])
        chain = conn.execute("select * from verify_audit_chain()").fetchone()
    assert item["integrity_ok"] is False                                              # la huella recalculada ya no coincide…
    assert item["evidence_hash"] == created["payload"]["evidence_hash"]               # …con la que quedó sellada en la auditoría
    assert chain["ok"] is True                                                         # y la auditoría, intacta, es la referencia


# --------------------------------------------------------------------------- duplicadas e incompatibles
def test_resize_target_change_updates_the_same_recommendation_instead_of_duplicating_it(t):
    t.scan(result(instance()))
    first = t.rec("i-0aaa")
    assert first["params"]["target_instance_type"] == "m5.xlarge"
    t.scan(result(instance(cpu=5.0, mem=8.0, peak=15.0)))                              # ahora cabe en m5.large
    recs = t.recs(rid="i-0aaa")
    assert len(recs) == 1 and recs[0]["id"] == first["id"]
    assert recs[0]["params"]["target_instance_type"] == "m5.large"                     # params se actualizan (antes quedaban obsoletos)
    assert recs[0]["version"] == 2                                                     # cambió la acción propuesta: versión nueva


def test_idle_replaces_the_pending_resize_instead_of_leaving_two_incompatible_recommendations(t):
    t.scan(result(instance()))
    resize = t.rec("i-0aaa", "ec2_downsize")
    t.scan(result(idle_instance()))
    assert t.rec("i-0aaa", "ec2_downsize")["status"] == "EXPIRED"
    assert t.rec("i-0aaa", "ec2_downsize")["stale_reason"] == "superseded"
    assert t.rec("i-0aaa", "ec2_idle")["status"] in ("PENDING_APPROVAL", "PROPOSED")
    open_now = [r for r in t.recs(rid="i-0aaa") if r["status"] in ("PENDING_APPROVAL", "PROPOSED")]
    assert len(open_now) == 1
    (exp,) = t.audit("RECOMMENDATION_EXPIRED", resize["id"])
    assert exp["payload"]["reason"] == "superseded"


def test_new_finding_on_a_resource_with_a_change_in_flight_is_born_blocked(t):
    t.scan(result(instance(env="development")))
    resize = t.rec("i-0aaa")
    ana = t.principal("FINOPS", "ana")
    with t.tx() as conn:
        assert workflow.decide(conn, ana, resize["id"], "APPROVED", "ok", resize["version"])["status"] == "APPROVED"
    t.scan(result(idle_instance(env="development")))                                   # mientras el resize está aprobado, aparece «ociosa»
    idle = t.rec("i-0aaa", "ec2_idle")
    assert idle["status"] == "PROPOSED" and idle["policy"]["allowed"] is False
    assert "IN_FLIGHT_CONFLICT" in idle["policy"]["applied"] and "en curso" in idle["policy"]["blocked_reasons"][-1]
    with pytest.raises(WorkflowError) as exc:
        with t.tx() as conn:
            workflow.decide(conn, ana, idle["id"], "APPROVED", "ok", idle["version"])
    assert exc.value.code in ("invalid_state", "policy_blocked")
    assert t.rec("i-0aaa", "ec2_downsize")["status"] == "APPROVED"                     # la aprobada no se toca


def test_a_blocked_recommendation_becomes_proposable_when_the_blocker_is_gone(t):
    """Antes el estado solo se fijaba al crear: una PROPOSED por política no pasaba nunca a PENDING_APPROVAL aunque la política cambiara."""
    t.scan(result(instance(env="development", cpu=12.0)))
    low = t.rec("i-0aaa")
    assert low["status"] == "PENDING_APPROVAL"
    admin("update recommendations set status = 'PROPOSED', policy = jsonb_set(policy, '{allowed}', 'false') where id = %s", (str(low["id"]),))
    t.scan(result(instance(env="development", cpu=12.0)))
    assert t.rec("i-0aaa")["status"] == "PENDING_APPROVAL"
    with t.tx() as conn:
        notes = [r["note"] for r in conn.execute("select note from recommendation_actions where recommendation_id = %s", (str(low["id"]),)).fetchall()]
    assert any("ya permite proponerla" in (n or "") for n in notes)


# --------------------------------------------------------------------------- recursos que ya no existen
def test_resource_missing_from_a_complete_scan_expires_its_open_recommendation(t):
    t.scan(result(instance(), instance("i-0bbb")))
    gone = t.rec("i-0aaa")
    stats = t.scan(result(instance("i-0bbb")))
    assert stats["reconciliation"] == {"expired": 1}
    after = t.rec("i-0aaa")
    assert after["status"] == "EXPIRED" and after["stale_reason"] == "resource_gone" and after["stale_since"] is not None
    assert t.rec("i-0bbb")["status"] == "PENDING_APPROVAL"
    with pytest.raises(WorkflowError) as exc:
        with t.tx() as conn:
            workflow.decide(conn, t.principal(), gone["id"], "APPROVED", "ok", None)
    assert exc.value.code == "invalid_state"
    with t.tx() as conn:
        timeline = [r["to_status"] for r in conn.execute("select to_status from recommendation_actions where recommendation_id = %s order by created_at, id",
                                                         (str(gone["id"]),)).fetchall()]
    assert timeline[-1] == "EXPIRED"


def test_condition_no_longer_met_expires_the_recommendation(t):
    t.scan(result(instance()))
    t.scan(result(instance(cpu=55.0, mem=60.0, peak=95.0)))                           # la carga subió: ya no está sobredimensionada
    assert t.rec("i-0aaa")["status"] == "EXPIRED" and t.rec("i-0aaa")["stale_reason"] == "condition_cleared"


def test_partial_or_degraded_scans_never_expire_anything(t):
    t.scan(result(instance()))
    stats = t.scan(result(partial=True))                                              # no se vio nada porque una API falló
    assert t.rec("i-0aaa")["status"] == "PENDING_APPROVAL" and stats["reconciliation"] == {"skipped": "scan_partial"}
    stats = t.scan(result(issues=[{"api": "ec2:DescribeInstances", "kind": "throttled", "retryable": True}]))
    assert t.rec("i-0aaa")["status"] == "PENDING_APPROVAL" and stats["reconciliation"] == {"skipped": "scan_degraded"}
    t.scan(result())                                                                  # completo y vacío: ahora sí
    assert t.rec("i-0aaa")["status"] == "EXPIRED"


def test_approved_recommendation_whose_resource_vanished_is_flagged_stale_not_cancelled(t):
    t.scan(result(instance(env="development")))
    rec = t.rec("i-0aaa")
    with t.tx() as conn:
        workflow.decide(conn, t.principal("FINOPS", "ana"), rec["id"], "APPROVED", "ok", rec["version"])
    stats = t.scan(result())
    after = t.rec("i-0aaa")
    assert after["status"] == "APPROVED" and after["stale_since"] is not None and after["stale_reason"] == "resource_gone"
    assert stats["reconciliation"] == {"stale": 1}
    assert len(t.audit("RECOMMENDATION_STALE", rec["id"])) == 1
    t.scan(result(instance(env="development")))                                        # reaparece y vuelve a detectarse
    assert t.rec("i-0aaa")["stale_since"] is None


def test_cannot_approve_when_the_resource_is_no_longer_in_the_inventory(t):
    t.scan(result(instance()))
    rec = t.rec("i-0aaa")
    admin("update resources set active = false where organization_id = %s", (str(t.org),))      # se vio desaparecer, aún sin reconciliar
    with pytest.raises(WorkflowError) as exc:
        with t.tx() as conn:
            workflow.decide(conn, t.principal(), rec["id"], "APPROVED", "ok", None)
    assert exc.value.code == "resource_gone" and exc.value.status == 409


# --------------------------------------------------------------------------- estimado ≠ aprobado ≠ observado
def test_approval_freezes_the_figure_the_approver_saw_together_with_its_evidence_and_baseline(t):
    t.scan(result(instance(env="development")))
    rec = t.rec("i-0aaa")
    (snap,) = _evidence_rows(t, rec["id"])
    with t.tx() as conn:
        assert workflow.decide(conn, t.principal("FINOPS", "ana"), rec["id"], "APPROVED", "ok", rec["version"])["status"] == "APPROVED"
    approved = t.rec("i-0aaa")
    assert float(approved["approved_monthly_savings"]) == float(rec["estimated_monthly_savings"]) == 140.16
    assert float(approved["approved_baseline_cost"]) == 280.32 and approved["approved_at"] is not None
    assert str(approved["approved_evidence_id"]) == str(snap["id"])
    (grant,) = t.audit("APPROVAL_GRANTED", rec["id"])
    assert grant["payload"]["evidence_hash"] == snap["evidence_hash"]                  # la aprobación nombra la huella exacta de lo aprobado
    with t.tx() as conn:
        ctx = conn.execute("select context from approvals where recommendation_id = %s", (str(rec["id"]),)).fetchone()["context"]
        base = conn.execute("select * from savings_baselines where recommendation_id = %s", (str(rec["id"]),)).fetchall()
    assert ctx["evidence_hash"] == snap["evidence_hash"]
    (b,) = base
    assert b["phase"] == "approval" and b["data_grade"] == "model" and b["cost_source"] == "estimate_only"        # sin costes diarios: lo dice
    assert float(b["monthly_cost"]) == 280.32 and b["usage"]["cpu_avg"] == 12.4
    (cap,) = t.audit("BASELINE_CAPTURED", rec["id"])
    assert cap["payload"]["baseline_hash"] == b["baseline_hash"]
    # un reescaneo posterior con otra cifra NO cambia lo aprobado (la fila ya no está en estado PROPOSED/PENDING)
    t.scan(result(instance(env="development", cost=500.0)))
    assert float(t.rec("i-0aaa")["approved_monthly_savings"]) == 140.16 and float(t.rec("i-0aaa")["estimated_monthly_savings"]) == 140.16


def test_a_material_change_invalidates_a_partial_reinforced_approval_but_noise_does_not(t):
    t.scan(result(idle_instance(env="production")))                                    # destructiva en producción: 2 aprobadores
    rec = t.rec("i-0aaa")
    assert rec["approvals_required"] == 2
    with t.tx() as conn:
        assert workflow.decide(conn, t.principal("FINOPS", "ana"), rec["id"], "APPROVED", "ok", rec["version"])["approvals"] == 1
    t.scan(result(idle_instance(env="production", cost=280.90)))                       # ruido: la primera aprobación sigue valiendo
    assert t.rec("i-0aaa")["version"] == rec["version"]
    t.scan(result(idle_instance(env="production", cost=700.0)))                        # cambio material: hay que volver a mirar
    bumped = t.rec("i-0aaa")
    assert bumped["version"] == rec["version"] + 1
    with t.tx() as conn:
        out = workflow.decide(conn, t.principal("SRE", "bob"), rec["id"], "APPROVED", "ok", bumped["version"])
    assert out["approvals"] == 1 and out["status"] == "PENDING_APPROVAL"               # solo cuenta la de la versión vigente


def test_account_costs_are_persisted_normalized_and_upserted(t):
    from datetime import date

    rows = [AccountCostRecord("aws", "111122223333", "ec2", "Amazon Elastic Compute Cloud - Compute", "us-east-1", "MONTHLY",
                              date(2026, 9, 1), date(2026, 10, 1), 1200.5, "USD", "UnblendedCost", False),
            AccountCostRecord("aws", "111122223333", "ec2", "Amazon Elastic Compute Cloud - Compute", "us-east-1", "DAILY",
                              date(2026, 10, 7), date(2026, 10, 8), 40.0, "USD", "UnblendedCost", True),
            AccountCostRecord("aws", "111122223333", "s3", "Amazon Simple Storage Service", "global", "MONTHLY",
                              date(2026, 9, 1), date(2026, 10, 1), 80.0, "EUR", "UnblendedCost", False)]
    stats = t.scan(result(instance(), account_costs=rows))
    assert stats["account_cost_rows"] == 3
    rows[1].amount, rows[1].estimated = 41.5, False                                    # el proveedor confirma la cifra del día
    t.scan(result(instance(), account_costs=rows))
    with t.tx() as conn:
        stored = conn.execute("select granularity, service, region, amount, currency, estimated from account_costs order by granularity, service").fetchall()
    assert len(stored) == 3                                                           # el mismo período se actualiza, no se duplica
    daily = next(r for r in stored if r["granularity"] == "DAILY")
    assert float(daily["amount"]) == 41.5 and daily["estimated"] is False
    assert {r["currency"] for r in stored} == {"USD", "EUR"}                           # las monedas no se mezclan

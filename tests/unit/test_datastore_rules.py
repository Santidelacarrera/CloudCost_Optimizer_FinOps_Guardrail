"""Qdrant: colector de solo lectura sobre una API simulada + reglas de optimización; invariantes de las cifras. PostgreSQL: ver tests/integration/test_postgres_collector.py."""
from __future__ import annotations

import math

import pytest
from cloudcost.collectors.cloudhttp import CloudApiError
from cloudcost.collectors.netguard import DestinationError, validate_service_url
from cloudcost.collectors.qdrant import QdrantClient, QdrantCollector
from cloudcost.domain import policy, risk
from cloudcost.domain.datastores import GIB, qdrant_ram_bytes, vector_specs
from cloudcost.domain.rules import ADVISORY_ACTIONS, RuleConfig, evaluate_all
from cloudcost.schemas import CloudAccountIn

CFG = RuleConfig()


class FakeQdrant:
    def __init__(self, collections: dict, fail: set | None = None):
        self.collections, self.fail, self.calls = collections, fail or set(), []

    def get_json(self, url, params=None):
        self.calls.append(url)
        path = url.split("qdrant.test:6333", 1)[1]
        if path in self.fail:
            raise CloudApiError(503, "")
        if path == "/collections":
            return {"result": {"collections": [{"name": n} for n in self.collections]}, "status": "ok"}
        return {"result": self.collections[path.rsplit("/", 1)[1]], "status": "ok"}


def info(points, size=768, *, rf=1, quant=None, on_disk=False, status="green", indexed=None, datatype="float32", vectors=None):
    return {"status": status, "optimizer_status": "ok", "points_count": points, "vectors_count": points, "indexed_vectors_count": points if indexed is None else indexed,
            "segments_count": 4, "config": {"params": {"vectors": vectors or {"size": size, "distance": "Cosine", "on_disk": on_disk, "datatype": datatype},
                                                       "shard_number": 1, "replication_factor": rf, "on_disk_payload": True},
                                            "quantization_config": quant}}


def collect(collections, *, env="production", cfg=None, fail=None):
    account = {"provider": "qdrant", "account_ref": "vec-prod", "provider_config": {"qdrant_url": "http://qdrant.test:6333", "environment": env, **(cfg or {})}}
    client = FakeQdrant(collections, fail)
    result = QdrantCollector(account, None, client=client).collect()
    return result, client


def by_rule(result, cfg=CFG):
    return {(f.rule_id, f.resource.name): f for f in evaluate_all(result.resources, cfg)}


def test_collector_reads_collections_and_estimates_ram():
    result, client = collect({"docs": info(2_000_000)})
    (res,) = result.resources
    raw = 2_000_000 * 768 * 4
    assert (res.provider, res.service, res.resource_id) == ("qdrant", "qdrant_collection", "vec-prod/docs")
    assert res.attributes["ram_bytes"] == int(raw * 1.5) and res.monthly_cost == pytest.approx(raw * 1.5 / GIB * 3.066, abs=0.01)
    assert all(c.endswith("/collections") or "/collections/" in c for c in client.calls) and not result.partial   # solo lecturas de /collections


def test_quantization_finding_uses_the_documented_formula():
    result, _ = collect({"docs": info(2_000_000), "small": info(1000, 128), "quantized": info(2_000_000, quant={"scalar": {"type": "int8"}}),
                         "ondisk": info(2_000_000, on_disk=True), "lagging": info(2_000_000, indexed=500_000)})
    findings = by_rule(result)
    assert set(findings) == {("qdrant_quantization", "docs")}                  # pequeña, ya cuantizada, en disco o sin indexar: nada
    f = findings[("qdrant_quantization", "docs")]
    raw_gib = 2_000_000 * 768 * 4 / GIB
    assert f.estimated_monthly_savings == pytest.approx(0.75 * raw_gib * 1.5 * 3.066 - raw_gib * 0.08, abs=0.01)
    est = f.evidence["estimate"]
    assert est["formula_id"] == "qdrant_quantization.v1" and est["kind"] == "reserved"
    assert f.action in ADVISORY_ACTIONS and f.destructive is False and f.params["quantization_config"]["scalar"]["type"] == "int8"


def test_replication_and_quantization_savings_do_not_double_count_in_non_production():
    result, _ = collect({"cache": info(2_000_000, 384, rf=3)}, env="development")
    findings = by_rule(result)
    q, r = findings[("qdrant_quantization", "cache")], findings[("qdrant_replication_nonprod", "cache")]
    ram_gib = 2_000_000 * 384 * 4 * 1.5 / GIB
    assert r.estimated_monthly_savings == pytest.approx(2 * ram_gib * 3.066, abs=0.01)
    assert q.evidence["replicas_in_estimate"] == 1                              # la cuantización se calcula sobre 1 réplica
    assert q.estimated_monthly_savings + r.estimated_monthly_savings <= q.current_monthly_cost + 0.01      # juntas nunca superan el coste
    prod = by_rule(collect({"cache": info(2_000_000, 384, rf=3)}, env="production")[0])
    assert ("qdrant_replication_nonprod", "cache") not in prod                  # producción (y lo desconocido) nunca se toca
    assert prod[("qdrant_quantization", "cache")].evidence["replicas_in_estimate"] == 3


def test_named_vectors_and_datatypes_are_sized_correctly():
    specs = vector_specs({"vectors": {"text": {"size": 768, "distance": "Cosine"}, "image": {"size": 512, "distance": "Dot", "datatype": "float16"},
                                      "cold": {"size": 1024, "distance": "Cosine", "on_disk": True}}})
    assert [s["name"] for s in specs] == ["text", "image", "cold"]
    assert qdrant_ram_bytes(1000, specs) == int(1000 * (768 * 4 + 512 * 2) * 1.5)                # el vector `on_disk` no cuenta


def test_failures_are_partial_not_fatal_and_leak_nothing():
    result, _ = collect({"docs": info(2_000_000), "broken": info(1)}, fail={"/collections/broken"})
    assert result.partial and [r.name for r in result.resources] == ["docs"] and any("qdrant:get_collection" in w for w in result.warnings)


def test_advisory_findings_never_open_a_pull_request_and_risk_is_explicit():
    result, _ = collect({"cache": info(2_000_000, 384, rf=3)}, env="development")
    for f in evaluate_all(result.resources, CFG):
        decision = policy.evaluate(action=f.action, risk=risk.classify_risk(f), environment=f.resource.environment, confidence=f.confidence)
        assert decision.allowed is False and "ADVISORY_ONLY" in decision.applied and decision.automation_blocked      # PROPUESTA, nunca PR
        assert risk.classify_risk(f) == "MEDIUM"


def test_invariants_hold_across_a_grid():
    n = 0
    for points in (10_000, 500_000, 2_000_000, 50_000_000):
        for size in (128, 768, 3072):
            for rf in (1, 2, 3):
                for env in ("production", "development", "unknown"):
                    result, _ = collect({"c": info(points, size, rf=rf)}, env=env)
                    for f in evaluate_all(result.resources, CFG):
                        n += 1
                        assert 0 <= f.estimated_monthly_savings <= f.current_monthly_cost + 0.01 and 0.0 <= f.confidence <= 0.97
                        assert all(math.isfinite(v) for v in (f.current_monthly_cost, f.projected_monthly_cost, f.estimated_monthly_savings))
                        assert f.estimated_monthly_savings >= CFG.min_monthly_savings and f.evidence["estimate"]["formula_id"]
    assert n > 30


# ------------------------------------------------------------------------------------------------ red y configuración
@pytest.mark.parametrize("url", ["", "ftp://x", "http://qdrant.example.com:6333", "https://user:pw@q.example.com", "https://q.example.com/?x=1",
                                 "http://169.254.169.254", "http://metadata.google.internal", "https://0.0.0.0"])
def test_unsafe_qdrant_urls_are_rejected(url):
    with pytest.raises(DestinationError):
        validate_service_url(url, "Qdrant")


@pytest.mark.parametrize("url", ["https://q.example.com:6333", "http://localhost:6333", "http://qdrant:6333", "http://10.0.0.5:6333", "http://qdrant.svc:6333/"])
def test_safe_qdrant_urls_are_accepted(url):
    assert validate_service_url(url, "Qdrant")


def test_client_only_talks_to_its_own_host_and_sends_the_key_as_a_header():
    class S:
        seen = []

        def get(self, url, params=None, headers=None, timeout=None, allow_redirects=None):
            S.seen.append((url, headers, allow_redirects))
            return type("R", (), {"status_code": 200, "json": lambda self: {"result": {}}})()

    c = QdrantClient("https://q.example.com", "SECRET-KEY-123", session=S())
    c.get_json("https://q.example.com/collections")
    assert S.seen[0][1]["api-key"] == "SECRET-KEY-123" and S.seen[0][2] is False          # sin redirecciones
    with pytest.raises(CloudApiError):
        c.get_json("https://evil.example.com/collections")


def test_cloud_account_schema_for_qdrant_and_postgres():
    q = CloudAccountIn(provider="qdrant", account_ref="vec-prod", display_name="Qdrant prod", qdrant_url="https://q.example.com", credentials_ref="env:CC_SECRET_QDRANT")
    assert q.regions == ["all"] and q.provider_config["qdrant_url"] == "https://q.example.com"
    pg = CloudAccountIn(provider="postgres", account_ref="pg-main", display_name="PG", credentials_ref="env:CC_SECRET_PG_DSN", environment="development")
    assert pg.provider_config == {"credentials_ref": "env:CC_SECRET_PG_DSN", "environment": "development"}
    with pytest.raises(ValueError):
        CloudAccountIn(provider="postgres", account_ref="pg-main", display_name="PG")                      # sin referencia al DSN
    with pytest.raises(ValueError):
        CloudAccountIn(provider="postgres", account_ref="pg-main", display_name="PG", credentials_ref="postgresql://u:p@h/db")   # un DSN literal no es una referencia
    with pytest.raises(ValueError):
        CloudAccountIn(provider="qdrant", account_ref="v", display_name="Q", qdrant_url="http://169.254.169.254")

"""Colectores Azure y GCP con respuestas simuladas (la forma de los JSON sigue la documentación de las APIs REST).

Lo que prueban: normalización al modelo común, que las reglas existentes produzcan propuestas sobre Azure/GCP, que los fallos
parciales no tumben el escaneo, que ante la duda no se proponga borrar, que el cliente HTTP solo haga GET a hosts permitidos,
y que los parches Terraform funcionen con `azurerm_*` y `google_*`. NO prueban contra Azure/GCP reales.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import jwt
import pytest
from cloudcost.collectors import get_collector
from cloudcost.collectors.azure import AzureCollector
from cloudcost.collectors.cloudhttp import (
    ARM_HOST,
    BearerClient,
    CloudApiError,
    azure_token_provider,
    gcp_token_provider,
)
from cloudcost.collectors.gcp import GcpCollector
from cloudcost.domain import policy as pol
from cloudcost.domain import risk
from cloudcost.domain.rules import ACTION_DELETE_VOLUME, ACTION_RESIZE, RuleConfig, evaluate_all
from cloudcost.iac.patcher import PatchError, build_patch
from cloudcost.iac.terraform import IacIndex
from cloudcost.schemas import CloudAccountIn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SUB = "11111111-2222-3333-4444-555555555555"
RG = f"/subscriptions/{SUB}/resourceGroups/rg-app/providers/Microsoft.Compute"
GIB = 2**30


class FakeSecrets:
    def resolve(self, ref, org_id=None):
        return {"env:CC_SECRET_SECRET": "s3cr3t"}.get(ref)


class Fake:
    """Responde por coincidencia de subcadena en la URL (la primera ruta que encaje). Registra todo."""

    def __init__(self, routes, fail=()):
        self.routes, self.fail, self.calls = routes, set(fail), []

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        for needle in self.fail:
            if needle in url:
                raise CloudApiError(403, "AuthorizationFailed")
        for needle, body in self.routes:
            if needle in url:
                return body(url, params or {}) if callable(body) else body
        return {"value": []}


def hours(avg, mx, n=336):
    return {"value": [{"timeseries": [{"data": [{"average": avg, "maximum": mx}] * n}]}]}


# --------------------------------------------------------------------------------------------------------- Azure
def azure_routes():
    vm = {"id": f"{RG}/virtualMachines/web-prod-1", "name": "web-prod-1", "location": "eastus", "tags": {"Environment": "prod"},
          "properties": {"hardwareProfile": {"vmSize": "Standard_D8s_v5"}, "timeCreated": "2026-06-01T10:00:00.1234567Z",
                         "storageProfile": {"osDisk": {"osType": "Linux"}}}}
    busy = {"id": f"{RG}/virtualMachines/db-busy", "name": "db-busy", "location": "eastus", "tags": {},
            "properties": {"hardwareProfile": {"vmSize": "Standard_D8s_v5"}, "timeCreated": "2026-06-01T10:00:00Z"}}
    status = {"value": [{"id": vm["id"].upper(), "properties": {"instanceView": {"statuses": [{"code": "PowerState/running"}]}}},
                        {"id": busy["id"], "properties": {"instanceView": {"statuses": [{"code": "PowerState/running"}]}}}]}

    def vms(url, params):
        return status if params.get("statusOnly") == "true" else {"value": [vm, busy]}

    def metrics(url, params):
        if "db-busy" in url:
            return hours(70.0, 95.0) if params["metricnames"] == "Percentage CPU" else hours(8 * GIB, 8 * GIB)
        if params["metricnames"] == "Percentage CPU":
            return hours(6.0, 20.0)
        return hours(26 * GIB, 28 * GIB)                    # 26 GiB libres de 32 → ≈ 19 % usado

    disks = {"value": [
        {"id": f"{RG}/disks/data-old", "name": "data-old", "location": "eastus", "sku": {"name": "Premium_LRS"}, "tags": {"env": "dev"},
         "properties": {"diskState": "Unattached", "diskSizeGB": 256, "timeCreated": "2026-01-01T00:00:00Z",
                        "LastOwnershipUpdateTime": "2026-08-20T00:00:00Z"}},
        {"id": f"{RG}/disks/pvc-abc", "name": "pvc-abc", "location": "eastus", "sku": {"name": "StandardSSD_LRS"},
         "tags": {"kubernetes.io-created-for-pv-name": "pvc-abc"},
         "properties": {"diskState": "Unattached", "diskSizeGB": 100, "timeCreated": "2026-05-01T00:00:00Z",
                        "lastOwnershipUpdateTime": "2026-07-01T00:00:00Z"}},
        {"id": f"{RG}/disks/os-web", "name": "os-web", "location": "eastus", "sku": {"name": "Premium_LRS"}, "managedBy": vm["id"],
         "properties": {"diskState": "Attached", "diskSizeGB": 128, "timeCreated": "2026-06-01T00:00:00Z"}},
        {"id": f"{RG}/disks/westdisk", "name": "westdisk", "location": "westeurope", "sku": {"name": "Premium_LRS"},
         "properties": {"diskState": "Unattached", "diskSizeGB": 512, "timeCreated": "2026-01-01T00:00:00Z",
                        "LastOwnershipUpdateTime": "2026-01-02T00:00:00Z"}}]}
    snaps = {"value": [
        {"id": f"{RG}/snapshots/old-free", "name": "old-free", "location": "eastus", "properties": {"diskSizeGB": 500, "timeCreated": "2026-03-01T00:00:00Z"}},
        {"id": f"{RG}/snapshots/used-by-image", "name": "used-by-image", "location": "eastus", "properties": {"diskSizeGB": 500, "timeCreated": "2026-03-01T00:00:00Z"}},
        {"id": f"{RG}/snapshots/in-gallery", "name": "in-gallery", "location": "eastus", "properties": {"diskSizeGB": 500, "timeCreated": "2026-03-01T00:00:00Z"}},
        {"id": f"/subscriptions/{SUB}/resourceGroups/AzureBackupRG_eastus_1/providers/Microsoft.Compute/snapshots/AzureBackup_x",
         "name": "AzureBackup_x", "location": "eastus", "properties": {"diskSizeGB": 500, "timeCreated": "2026-03-01T00:00:00Z"}}]}
    images = {"value": [{"name": "golden", "properties": {"storageProfile": {"osDisk": {"snapshot": {"id": f"{RG}/Snapshots/USED-BY-IMAGE"}}}}}]}
    galleries = {"value": [{"id": f"{RG}/galleries/g1", "name": "g1"}]}
    gallery_images = {"value": [{"id": f"{RG}/galleries/g1/images/im1", "name": "im1"}]}
    versions = {"value": [{"name": "1.0.0", "properties": {"storageProfile": {"source": {"id": f"{RG}/snapshots/in-gallery"}}}}]}
    return [("galleries/g1/images/im1/versions", versions), ("galleries/g1/images", gallery_images), ("/galleries", galleries),
            ("/images", images), ("/snapshots", snaps), ("/disks", disks), ("Microsoft.Insights/metrics", metrics),
            ("/virtualMachines", vms)]


def azure_account(**kw):
    return {"provider": "azure", "account_ref": SUB, "regions": ["all"], "provider_config": {}, **kw}


@pytest.fixture(scope="module")
def azure_result():
    fake = Fake(azure_routes())
    res = AzureCollector(azure_account(), FakeSecrets(), client=fake, now=NOW).collect()
    return res, fake


def by_name(result):
    return {r.name: r for r in result.resources}


def test_azure_normalizes_resources(azure_result):
    res, _ = azure_result
    r = by_name(res)
    vm = r["web-prod-1"]
    assert (vm.provider, vm.service, vm.state, vm.instance_type, vm.environment) == ("azure", "vm", "running", "Standard_D8s_v5", "production")
    assert vm.cpu_avg == 6.0 and vm.cpu_max == 20.0 and 18 < vm.memory_avg < 20 and vm.observation_days == 14
    assert vm.monthly_cost == 280.32                                  # 0,384 USD/h × 730
    d = r["data-old"]
    assert (d.service, d.state, d.attached, d.volume_type, d.size_gb) == ("disk", "available", False, "Premium_LRS", 256.0)
    assert d.unattached_days == 42                                      # 20-ago → 1-oct, propiedad con mayúscula inicial
    assert r["pvc-abc"].unattached_days == 92 and r["pvc-abc"].attributes["k8s_pv"] == "pvc-abc"   # propiedad en minúscula
    assert r["os-web"].attached is True and r["os-web"].state == "in-use"


def test_azure_is_read_only(azure_result):
    _, fake = azure_result
    assert fake.calls and all(u.startswith(f"https://{ARM_HOST}/") for u, _ in fake.calls)


def test_azure_snapshots_backup_images_and_galleries(azure_result):
    r = by_name(azure_result[0])
    assert r["AzureBackup_x"].attributes["managed_by"] == "azure-backup"
    assert r["used-by-image"].attributes["ami_ids"] == ["golden"]
    assert r["in-gallery"].attributes["ami_ids"] == ["g1/im1/1.0.0"]
    assert "ami_ids" not in r["old-free"].attributes and r["old-free"].age_days == 214


def test_azure_resources_feed_existing_rules(azure_result):
    findings = {f.resource.name: f for f in evaluate_all(azure_result[0].resources, RuleConfig())}
    assert set(findings) == {"web-prod-1", "data-old", "pvc-abc", "old-free", "westdisk"}
    resize = findings["web-prod-1"]
    assert resize.action == ACTION_RESIZE and resize.params["target_instance_type"] in {"Standard_D4s_v5", "Standard_D2s_v5"}
    assert resize.params["current_instance_type"] == "Standard_D8s_v5" and resize.estimated_monthly_savings > 100
    orphan = findings["data-old"]
    assert orphan.action == ACTION_DELETE_VOLUME and orphan.params["resource_type"] == "azurerm_managed_disk"
    assert orphan.estimated_monthly_savings == 38.4                       # 256 GB × 0,15
    assert findings["old-free"].params["resource_type"] == "azurerm_snapshot"
    assert findings["old-free"].estimated_monthly_savings == 25.0
    assert "db-busy" not in findings                                      # CPU 70 %: no se toca
    # el riesgo y la política del motor común aplican igual en otra nube
    decision = pol.evaluate(action=orphan.action, risk=risk.classify_risk(orphan), environment=orphan.resource.environment,
                            confidence=orphan.confidence, cfg=pol.PolicyConfig())
    assert decision.approvals_required >= 1


def test_azure_region_filter():
    fake = Fake(azure_routes())
    res = AzureCollector(azure_account(regions=["westeurope"]), FakeSecrets(), client=fake, now=NOW).collect()
    assert [r.name for r in res.resources if r.service == "disk"] == ["westdisk"]
    assert not [r for r in res.resources if r.service == "vm"]


def test_azure_partial_failure_does_not_abort_and_blocks_snapshot_proposals():
    errors = []
    fake = Fake(azure_routes(), fail=["/images"])
    res = AzureCollector(azure_account(), FakeSecrets(), client=fake, on_api_error=errors.append, now=NOW).collect()
    assert res.partial and "compute:images" in errors and any("AuthorizationFailed" in w for w in res.warnings)
    assert {r.name for r in res.resources if r.service == "vm"} == {"web-prod-1", "db-busy"}      # el resto sigue funcionando
    snaps = [r for r in res.resources if r.service == "disk_snapshot"]
    assert snaps and all(s.attributes.get("ami_ids") for s in snaps)
    assert "old-free" not in {f.resource.name for f in evaluate_all(res.resources, RuleConfig())}  # ante la duda, no se propone borrar


def test_azure_follows_next_link():
    pages = {1: {"value": [{"id": f"{RG}/disks/a", "name": "a", "location": "eastus", "properties": {"diskState": "Attached", "diskSizeGB": 8}}],
                 "nextLink": f"https://{ARM_HOST}/next?page=2"},
             2: {"value": [{"id": f"{RG}/disks/b", "name": "b", "location": "eastus", "properties": {"diskState": "Attached", "diskSizeGB": 8}}]}}
    fake = Fake([("/next", pages[2]), ("/disks", pages[1])])
    res = AzureCollector(azure_account(), FakeSecrets(), client=fake, now=NOW).collect()
    assert {r.name for r in res.resources} == {"a", "b"}


def test_azure_missing_secret_is_a_permission_error():
    acct = azure_account(provider_config={"tenant_id": SUB, "client_id": SUB, "credentials_ref": "env:NOPE"})
    with pytest.raises(PermissionError):
        AzureCollector(acct, FakeSecrets(), now=NOW).collect()


def test_invalid_credentials_config_is_not_retryable():
    class Secrets:
        def resolve(self, ref, org_id=None):
            return "no es un json"

    with pytest.raises(PermissionError):
        GcpCollector(gcp_account(provider_config={"credentials_ref": "env:X"}), Secrets(), now=NOW).collect()
    with pytest.raises(PermissionError):
        AzureCollector(azure_account(provider_config={"tenant_id": "../x", "client_id": "y", "credentials_ref": "env:X"}), Secrets(), now=NOW).collect()


# ------------------------------------------------------------------------------------------------------------ GCP
PROJECT = "mi-proyecto-123"
ZONE = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/us-central1-a"
TYPES = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/us-central1-a"


def series(iid, values):
    return {"resource": {"labels": {"instance_id": iid}}, "points": [{"value": {"doubleValue": v}} for v in values]}


def gcp_routes():
    instances = {"items": {"zones/us-central1-a": {"instances": [
        {"id": "111", "name": "api-1", "zone": ZONE, "status": "RUNNING", "machineType": f"{TYPES}/machineTypes/n2-standard-8",
         "labels": {"env": "staging"}, "creationTimestamp": "2026-05-01T03:00:00.000-07:00"},
        {"id": "222", "name": "stopped-1", "zone": ZONE, "status": "TERMINATED", "machineType": f"{TYPES}/machineTypes/e2-standard-4",
         "creationTimestamp": "2026-05-01T03:00:00.000-07:00"}]},
        "zones/europe-west1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}}}}
    disks = {"items": {"zones/us-central1-a": {"disks": [
        {"name": "scratch", "zone": ZONE, "type": f"{TYPES}/diskTypes/pd-ssd", "sizeGb": "500", "status": "READY",
         "creationTimestamp": "2026-04-01T00:00:00.000-07:00", "lastDetachTimestamp": "2026-08-30T00:00:00.000-07:00",
         "labels": {"env": "dev"}},
        {"name": "gke-pvc-1", "zone": ZONE, "type": f"{TYPES}/diskTypes/pd-balanced", "sizeGb": "100", "status": "READY",
         "creationTimestamp": "2026-05-01T00:00:00.000-07:00", "description": '{"kubernetes.io/created-for/pvc/name":"x"}'},
        {"name": "boot", "zone": ZONE, "type": f"{TYPES}/diskTypes/pd-balanced", "sizeGb": "50", "status": "READY",
         "users": [f"{ZONE}/instances/api-1"], "creationTimestamp": "2026-05-01T00:00:00.000-07:00"}]}}}
    snaps = {"items": [
        {"name": "manual-old", "diskSizeGb": "200", "storageBytes": str(50 * GIB), "creationTimestamp": "2026-02-01T00:00:00.000-08:00", "status": "READY"},
        {"name": "scheduled", "diskSizeGb": "200", "storageBytes": str(50 * GIB), "autoCreated": True, "creationTimestamp": "2026-02-01T00:00:00.000-08:00"},
        {"name": "image-src", "diskSizeGb": "200", "storageBytes": str(50 * GIB), "creationTimestamp": "2026-02-01T00:00:00.000-08:00"}]}
    images = {"items": [{"name": "golden", "sourceSnapshot": f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/snapshots/image-src"}]}

    def monitoring(url, params):
        f, al = params["filter"], params["aggregation.perSeriesAligner"]
        if "cpu/utilization" in f:
            return {"timeSeries": [series("111", [0.05] * 336 if al == "ALIGN_MEAN" else [0.18] * 336), series("222", [0.5])]}
        if "memory/percent_used" in f:
            return {"timeSeries": [series("111", [22.0] * 336)]}
        return {}

    return [("/timeSeries", monitoring), ("global/snapshots", snaps), ("global/images", images),
            ("aggregated/disks", disks), ("aggregated/instances", instances)]


def gcp_account(**kw):
    return {"provider": "gcp", "account_ref": PROJECT, "regions": ["all"], "provider_config": {}, **kw}


@pytest.fixture(scope="module")
def gcp_result():
    fake = Fake(gcp_routes())
    return GcpCollector(gcp_account(), FakeSecrets(), client=fake, now=NOW).collect(), fake


def test_gcp_normalizes_resources(gcp_result):
    r = by_name(gcp_result[0])
    vm = r["api-1"]
    assert (vm.provider, vm.service, vm.state, vm.instance_type, vm.region, vm.environment) == \
        ("gcp", "gce", "running", "n2-standard-8", "us-central1", "staging")
    assert vm.resource_id == f"projects/{PROJECT}/zones/us-central1-a/instances/api-1"      # igual que lo registra Terraform
    assert vm.cpu_avg == 5.0 and vm.cpu_max == 18.0 and vm.memory_avg == 22.0 and vm.observation_days == 14
    assert r["stopped-1"].state == "stopped" and r["stopped-1"].cpu_avg is None
    scratch = r["scratch"]
    assert (scratch.service, scratch.state, scratch.volume_type, scratch.size_gb, scratch.unattached_days) == ("pd", "available", "pd-ssd", 500.0, 32)
    assert scratch.monthly_cost == 85.0
    assert r["gke-pvc-1"].attributes["k8s_managed"] is True and r["gke-pvc-1"].unattached_days == 153   # sin detach: desde su creación
    assert r["boot"].attached is True and r["boot"].unattached_days is None
    assert r["manual-old"].monthly_cost == 1.3                          # 50 GiB realmente almacenados × 0,026


def test_gcp_is_read_only(gcp_result):
    assert all(u.startswith(("https://compute.googleapis.com/", "https://monitoring.googleapis.com/")) for u, _ in gcp_result[1].calls)


def test_gcp_snapshots_schedule_and_images(gcp_result):
    r = by_name(gcp_result[0])
    assert r["scheduled"].attributes["managed_by"] == "snapshot-schedule"
    assert r["image-src"].attributes["ami_ids"] == ["golden"]
    assert not r["manual-old"].attributes


def test_gcp_resources_feed_existing_rules(gcp_result):
    findings = {f.resource.name: f for f in evaluate_all(gcp_result[0].resources, RuleConfig())}
    assert set(findings) == {"api-1", "scratch", "gke-pvc-1"}          # el snapshot manual vale 1,3 USD: bajo el umbral de ahorro
    assert findings["api-1"].action == ACTION_RESIZE and findings["api-1"].params["target_instance_type"] in {"n2-standard-4", "n2-standard-2"}
    assert findings["scratch"].params["resource_type"] == "google_compute_disk"


def test_gcp_without_ops_agent_has_no_memory_and_proposes_no_resize():
    routes = [(n, b) for n, b in gcp_routes() if n != "/timeSeries"]

    def monitoring(url, params):
        if "memory" in params["filter"]:
            raise CloudApiError(400, "INVALID_ARGUMENT")
        return {"timeSeries": [series("111", [0.05] * 336)]}

    res = GcpCollector(gcp_account(), FakeSecrets(), client=Fake([("/timeSeries", monitoring)] + routes), now=NOW).collect()
    vm = by_name(res)["api-1"]
    assert vm.memory_avg is None and not res.partial
    assert "api-1" not in {f.resource.name for f in evaluate_all(res.resources, RuleConfig())}     # require_memory_metric


def test_gcp_partial_failure_and_unverified_images():
    res = GcpCollector(gcp_account(), FakeSecrets(), client=Fake(gcp_routes(), fail=["global/images"]), now=NOW).collect()
    assert res.partial and any("compute:images" in w for w in res.warnings)
    assert "manual-old" not in {f.resource.name for f in evaluate_all(res.resources, RuleConfig())}


def test_gcp_paginates_aggregated_lists():
    page = lambda n, token: {"items": {"zones/z": {"disks": [{"name": n, "zone": ZONE, "type": "pd-ssd", "sizeGb": "10", "status": "READY"}]}},   # noqa: E731
                             **({"nextPageToken": token} if token else {})}
    fake = Fake([("aggregated/disks", lambda url, p: page("two", None) if p.get("pageToken") == "T" else page("one", "T"))])
    res = GcpCollector(gcp_account(), FakeSecrets(), client=fake, now=NOW).collect()
    assert {r.name for r in res.resources} == {"one", "two"}


# ------------------------------------------------------------------------------------------------ HTTP y tokens
class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.responses.pop(0)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.responses.pop(0)


def test_http_client_refuses_foreign_hosts_and_plain_http():
    session = FakeSession([])
    client = BearerClient(lambda: "tok", {ARM_HOST}, session=session)
    for bad in ("https://evil.example/steal", f"http://{ARM_HOST}/x", f"https://{ARM_HOST}.evil.example/x"):
        with pytest.raises(CloudApiError) as err:
            client.get_json(bad)
        assert err.value.code == "host_not_allowed"
    assert session.calls == []                                           # el token ni siquiera se pidió


def test_http_client_retries_throttling_then_succeeds_and_errors_have_no_secrets():
    sleeps = []
    session = FakeSession([Resp(429, headers={"Retry-After": "3"}), Resp(503), Resp(200, {"ok": 1})])
    assert BearerClient(lambda: "tok", {ARM_HOST}, session=session, sleep=sleeps.append).get_json(f"https://{ARM_HOST}/x") == {"ok": 1}
    assert sleeps[0] == 3.0 and len(session.calls) == 3
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer tok"
    denied = FakeSession([Resp(403, {"error": {"code": "AuthorizationFailed", "message": "Bearer tok leaked?"}})])
    with pytest.raises(CloudApiError) as err:
        BearerClient(lambda: "tok", {ARM_HOST}, session=denied).get_json(f"https://{ARM_HOST}/x")
    assert str(err.value) == "HTTP 403 AuthorizationFailed" and "tok" not in str(err.value)


def test_azure_token_request_and_cache():
    now = [1000.0]
    session = FakeSession([Resp(200, {"access_token": "AT1", "expires_in": 3600}), Resp(200, {"access_token": "AT2", "expires_in": 3600})])
    token = azure_token_provider(SUB, SUB, "s3cr3t", session=session, clock=lambda: now[0])
    assert token() == "AT1" and token() == "AT1" and len(session.calls) == 1
    method, url, kw = session.calls[0]
    assert method == "POST" and url == f"https://login.microsoftonline.com/{SUB}/oauth2/v2.0/token"
    assert kw["data"]["grant_type"] == "client_credentials" and kw["data"]["scope"] == "https://management.azure.com/.default"
    now[0] += 3600                                                       # vencido: se renueva
    assert token() == "AT2"
    with pytest.raises(ValueError):
        azure_token_provider("../evil", SUB, "x")


def test_gcp_token_uses_signed_jwt_assertion():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    sa = json.dumps({"client_email": "ro@mi-proyecto-123.iam.gserviceaccount.com", "private_key": pem})
    session = FakeSession([Resp(200, {"access_token": "GT", "expires_in": 3599})])
    token = gcp_token_provider(sa, session=session, clock=lambda: 2_000_000_000.0)
    assert token() == "GT"
    _, url, kw = session.calls[0]
    assert url == "https://oauth2.googleapis.com/token" and kw["data"]["grant_type"].endswith("jwt-bearer")
    claims = jwt.decode(kw["data"]["assertion"], key.public_key(), algorithms=["RS256"], audience="https://oauth2.googleapis.com/token",
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "ro@mi-proyecto-123.iam.gserviceaccount.com" and claims["scope"].endswith("cloud-platform.read-only")
    with pytest.raises(ValueError):
        gcp_token_provider("no es json")


# ------------------------------------------------------------------------------------- alta de cuentas y fábrica
def test_account_validation_by_provider():
    ok_azure = dict(provider="azure", account_ref=SUB, display_name="Prod", tenant_id=SUB, client_id=SUB, credentials_ref="env:CC_SECRET_AZ")
    acct = CloudAccountIn(**ok_azure)
    assert acct.regions == ["all"] and acct.provider_config == {"tenant_id": SUB, "client_id": SUB, "credentials_ref": "env:CC_SECRET_AZ"}
    for patch in ({"account_ref": "mi-suscripcion"}, {"tenant_id": None}, {"client_id": "nope"}, {"credentials_ref": None},
                  {"credentials_ref": "hunter2-el-valor-directo"}):
        with pytest.raises(ValueError):
            CloudAccountIn(**{**ok_azure, **patch})
    ok_gcp = dict(provider="gcp", account_ref=PROJECT, display_name="Datos", credentials_ref="aws-sm:cloudcost/org/gcp-ro-key")
    assert CloudAccountIn(**ok_gcp).regions == ["all"]
    assert CloudAccountIn(**{**ok_gcp, "regions": ["us-central1", "europe-west1"]}).regions == ["us-central1", "europe-west1"]
    for patch in ({"account_ref": "123456789012"}, {"credentials_ref": None}, {"regions": ["Bad Region"]}):
        with pytest.raises(ValueError):
            CloudAccountIn(**{**ok_gcp, **patch})
    assert CloudAccountIn(provider="aws", account_ref="123456789012", display_name="x").provider_config == {}
    with pytest.raises(ValueError):
        CloudAccountIn(provider="aws", account_ref="123456789012", display_name="x", regions=["eastus"])


def test_get_collector_dispatch():
    az = get_collector(azure_account(), secrets=FakeSecrets(), demo_enabled=False)
    gc = get_collector(gcp_account(), secrets=FakeSecrets(), demo_enabled=False)
    assert isinstance(az, AzureCollector) and isinstance(gc, GcpCollector)


# ------------------------------------------------------------------------------------------------- IaC
TF = '''
resource "azurerm_linux_virtual_machine" "web" {
  name = "web-prod-1"
  size = "Standard_D8s_v5"
}

resource "azurerm_managed_disk" "old" {
  name                 = "data-old"
  storage_account_type = "Premium_LRS"
}

resource "google_compute_instance" "api" {
  name         = "api-1"
  machine_type = "n2-standard-8"
}

resource "google_compute_disk" "scratch" {
  name = "scratch"
  type = "pd-ssd"
}
'''


def test_terraform_matching_and_patches_for_azure_and_gcp():
    azure, gcp = AzureCollector(azure_account(), FakeSecrets(), client=Fake(azure_routes()), now=NOW).collect(), None
    gcp = GcpCollector(gcp_account(), FakeSecrets(), client=Fake(gcp_routes()), now=NOW).collect()
    index = IacIndex.build({"main.tf": TF})
    findings = {f.resource.name: f for f in evaluate_all(azure.resources + gcp.resources, RuleConfig())}
    for f in findings.values():
        block = index.match(f.resource)
        f.resource.iac_address = block.address if block else None
    assert findings["web-prod-1"].resource.iac_address == "azurerm_linux_virtual_machine.web"
    assert findings["api-1"].resource.iac_address == "google_compute_instance.api"
    assert findings["data-old"].resource.iac_address == "azurerm_managed_disk.old"
    assert findings["scratch"].resource.iac_address == "google_compute_disk.scratch"
    assert findings["pvc-abc"].resource.iac_address is None                       # lo creó Kubernetes: no está en Terraform

    patch = build_patch(action=findings["web-prod-1"].action, params=findings["web-prod-1"].params,
                        block=index.block_by_address("azurerm_linux_virtual_machine.web"), index=index)
    target = findings["web-prod-1"].params["target_instance_type"]
    assert f'size = "{target}"' in patch.new_text and "[check]" not in patch.summary and "size Standard_D8s_v5" in patch.summary
    patch = build_patch(action=findings["api-1"].action, params=findings["api-1"].params,
                        block=index.block_by_address("google_compute_instance.api"), index=index)
    assert 'machine_type = "n2-standard-' in patch.new_text and "n2-standard-8" not in patch.new_text
    patch = build_patch(action=findings["data-old"].action, params=findings["data-old"].params,
                        block=index.block_by_address("azurerm_managed_disk.old"), index=index)
    assert "azurerm_managed_disk" not in patch.new_text and "azurerm_linux_virtual_machine" in patch.new_text

    drifted = dict(findings["web-prod-1"].params, current_instance_type="Standard_D4s_v5")
    with pytest.raises(PatchError) as err:
        build_patch(action=ACTION_RESIZE, params=drifted, block=index.block_by_address("azurerm_linux_virtual_machine.web"), index=index)
    assert err.value.code == "drift"
    bad = dict(findings["web-prod-1"].params, target_instance_type="Standard_D2s_v5; rm -rf")
    with pytest.raises(PatchError) as err:
        build_patch(action=ACTION_RESIZE, params=bad, block=index.block_by_address("azurerm_linux_virtual_machine.web"), index=index)
    assert err.value.code == "invalid_params"

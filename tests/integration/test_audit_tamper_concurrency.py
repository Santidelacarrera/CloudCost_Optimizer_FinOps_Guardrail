"""Auditoría: concurrencia y manipulación. La cadena de hashes debe resistir escrituras simultáneas y delatar toda alteración posterior.

«Manipulación» se simula con el propietario de la base (el atacante más fuerte: puede desactivar triggers). El rol de la aplicación, en cambio,
no puede alterar nada ni saltarse los triggers (ver también test_tenant_isolation.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from cloudcost.services import audit, workflow
from cloudcost.services.recommendations import WorkflowError
from dbkit import admin, idle_instance, instance, new_tenant, require_db, result

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def t():
    return new_tenant("aud")


def _record(t, n: int, label: str, actor: str = "x") -> None:
    with t.tx() as conn:
        audit.record(conn, t.org, f"CONC_{label}", actor=audit.Actor("user", actor), entity_type="test", entity_id=str(n), payload={"n": n, "label": label})


def _chain(t) -> dict:
    with t.tx() as conn:
        return conn.execute("select * from verify_audit_chain()").fetchone()


def _tamper(sql: str, params=()) -> None:
    """Como propietario de la base: desactiva los triggers de inmutabilidad, altera y los vuelve a activar."""
    import psycopg

    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as c:
        c.execute("alter table audit_events disable trigger audit_events_no_update_delete")
        try:
            c.execute(sql, params)
        finally:
            c.execute("alter table audit_events enable trigger audit_events_no_update_delete")


# --------------------------------------------------------------------------- concurrencia
def test_concurrent_writers_produce_one_gapless_valid_chain(t):
    n_threads, per_thread = 8, 20
    barrier = threading.Barrier(n_threads)

    def writer(i: int) -> None:
        barrier.wait()                                                              # todos empiezan a la vez
        for k in range(per_thread):
            _record(t, k, f"w{i}", actor=f"user{i}")

    with ThreadPoolExecutor(n_threads) as pool:
        list(pool.map(writer, range(n_threads)))
    with t.tx() as conn:
        rows = conn.execute("select seq, prev_hash, hash from audit_events order by seq").fetchall()
    assert len(rows) == n_threads * per_thread
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))                  # sin huecos ni duplicados
    assert rows[0]["prev_hash"] == "0" * 64
    assert all(rows[i]["prev_hash"] == rows[i - 1]["hash"] for i in range(1, len(rows)))   # cada eslabón apunta al anterior: sin bifurcaciones
    assert len({r["hash"] for r in rows}) == len(rows)
    chain = _chain(t)
    assert chain["ok"] is True and chain["checked"] == len(rows) and chain["first_bad_seq"] is None


def test_chains_of_different_organizations_are_independent_under_concurrency():
    a, b = new_tenant("auda"), new_tenant("audb")
    barrier = threading.Barrier(6)

    def writer(i: int) -> None:
        barrier.wait()
        for k in range(15):
            _record(a if i % 2 == 0 else b, k, f"w{i}")

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(writer, range(6)))
    for tenant, expected in ((a, 45), (b, 45)):
        chain = _chain(tenant)
        assert chain["ok"] and chain["checked"] == expected
        with tenant.tx() as conn:
            assert conn.execute("select count(distinct organization_id)::int as n from audit_events").fetchone()["n"] == 1


def test_a_failed_transaction_leaves_no_event_and_no_gap(t):
    _record(t, 1, "a")
    with pytest.raises(RuntimeError):
        with t.tx() as conn:
            audit.record(conn, t.org, "ROLLED_BACK", payload={"x": 1})
            raise RuntimeError("falla después de auditar")
    _record(t, 2, "b")
    with t.tx() as conn:
        seqs = [r["seq"] for r in conn.execute("select seq from audit_events order by seq").fetchall()]
    assert seqs == [1, 2] and _chain(t)["ok"]


def _reinforced_rec(t):
    t.scan(result(idle_instance(env="production")))
    return t.rec("i-0aaa")


def test_concurrent_approvals_by_different_people_complete_the_approval_exactly_once(t):
    rec = _reinforced_rec(t)
    people = [t.principal("FINOPS", "ana"), t.principal("SRE", "bob"), t.principal("ADMIN", "eve"), t.principal("FINOPS", "carla")]
    barrier = threading.Barrier(len(people))
    outcomes: list = []

    def approve(p):
        barrier.wait()
        try:
            with t.tx() as conn:
                outcomes.append(("ok", workflow.decide(conn, p, rec["id"], "APPROVED", "ok", None)["status"]))
        except WorkflowError as exc:
            outcomes.append(("err", exc.code))

    with ThreadPoolExecutor(len(people)) as pool:
        list(pool.map(approve, people))
    final = t.rec("i-0aaa")
    assert final["status"] == "APPROVED"
    with t.tx() as conn:
        actions = [r["to_status"] for r in conn.execute("select to_status from recommendation_actions where recommendation_id = %s order by created_at, id",
                                                        (str(rec["id"]),)).fetchall()]
        decisions = conn.execute("select user_id, decision from approvals where recommendation_id = %s", (str(rec["id"]),)).fetchall()
    assert actions.count("APPROVED") == 1                                           # una sola transición, pase lo que pase en la carrera
    assert len({str(d["user_id"]) for d in decisions}) == len(decisions)             # y una sola decisión por persona
    assert {o[0] for o in outcomes} <= {"ok", "err"} and all(o[1] in ("APPROVED", "PENDING_APPROVAL", "invalid_state") for o in outcomes)
    assert _chain(t)["ok"]


def test_double_click_by_the_same_person_counts_once(t):
    rec = _reinforced_rec(t)
    ana = t.principal("FINOPS", "ana")
    barrier = threading.Barrier(5)
    outcomes: list = []

    def click(_):
        barrier.wait()
        try:
            with t.tx() as conn:
                outcomes.append(workflow.decide(conn, ana, rec["id"], "APPROVED", "ok", None)["approvals"])
        except WorkflowError as exc:
            outcomes.append(exc.code)

    with ThreadPoolExecutor(5) as pool:
        list(pool.map(click, range(5)))
    assert outcomes.count(1) == 1 and outcomes.count("already_decided") == 4
    assert t.rec("i-0aaa")["status"] == "PENDING_APPROVAL"                              # una persona no completa una aprobación reforzada
    with t.tx() as conn:
        assert conn.execute("select count(*)::int as n from approvals").fetchone()["n"] == 1


def test_concurrent_pull_request_requests_create_exactly_one(t):
    from cloudcost.config import Settings
    from cloudcost.git.base import ChangeRequest
    from cloudcost.secrets import SecretResolver

    class Git:
        calls = 0

        def list_files(self, repo, ref, paths):
            return {"main.tf": 'resource "aws_instance" "web" {\n  ami = "ami-1"\n  instance_type = "m5.2xlarge"\n  tags = {\n    Name = "i-0aaa"\n    Environment = "development"\n  }\n}\n'}

        def create_change_request(self, **kw):
            type(self).calls += 1
            import time
            time.sleep(0.3)                                                         # ventana para que las demás solicitudes lleguen a la vez
            return ChangeRequest(number=5, url="https://x/pull/5", branch=kw["branch"], draft=kw["draft"])

    repo = admin("insert into repositories (organization_id, provider, full_name) values (%s, 'github', 'acme/infra') returning id", (str(t.org),))[0]["id"]
    from cloudcost.services import scan_service

    class Scripted:
        def collect(self):
            return result(instance(env="development"))

    scan_id = admin("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) values (%s, %s, %s, 't') returning id",
                    (str(t.org), t.account, str(repo)))[0]["id"]
    scan_service.run_scan(t.org, scan_id, settings=Settings(demo_enabled=True), secrets=SecretResolver(), git_factory=lambda *a: Git(),
                          collector_factory=lambda account, **kw: Scripted())
    rec = t.rec("i-0aaa")
    ana = t.principal("FINOPS", "ana")
    with t.tx() as conn:
        workflow.decide(conn, ana, rec["id"], "APPROVED", "ok", rec["version"])
    settings = Settings(demo_enabled=True, opa_mode="off")
    barrier = threading.Barrier(5)
    outcomes: list = []

    def create(_):
        barrier.wait()
        try:
            with t.tx() as conn:
                out = workflow.create_pull_request(conn, ana, rec["id"], settings=settings, secrets=SecretResolver(), provider_factory=lambda *a: Git())
                outcomes.append(("created" if out.get("created") else "existing", out.get("number")))
        except WorkflowError as exc:
            outcomes.append(("err", exc.code))

    with ThreadPoolExecutor(5) as pool:
        list(pool.map(create, range(5)))
    assert Git.calls == 1, outcomes                                                 # el proveedor Git recibió UNA sola solicitud de creación
    assert [o for o in outcomes if o[0] == "created"] == [("created", 5)]
    assert all(o in (("existing", 5), ("err", "locked")) for o in outcomes if o[0] != "created")
    with t.tx() as conn:
        assert conn.execute("select count(*)::int as n from pull_requests").fetchone()["n"] == 1
    assert t.rec("i-0aaa")["status"] == "PR_CREATED"


# --------------------------------------------------------------------------- manipulación
def test_the_application_role_cannot_bypass_or_disable_the_audit_triggers(t):
    _record(t, 1, "a")
    for sql in ("set local session_replication_role = replica", "alter table audit_events disable trigger all",
                "drop trigger audit_events_no_update_delete on audit_events", "create or replace function audit_events_immutable() returns trigger "
                "language plpgsql as $$ begin return new; end $$", "alter table audit_events disable row level security",
                "create table evil (x int)", "drop table audit_events", "alter table audit_events drop column hash"):
        with pytest.raises(psycopg.Error):
            with t.tx() as conn:
                conn.execute(sql)
    assert _chain(t)["ok"]


def test_the_application_role_cannot_forge_chain_fields(t):
    """Los campos de la cadena los calcula el trigger: da igual lo que intente escribir el insert."""
    _record(t, 1, "a")
    with t.tx() as conn:
        conn.execute("""insert into audit_events (organization_id, seq, event_type, actor_type, payload, prev_hash, hash, created_at, canon_v)
                        values (%s, 999, 'FORGED', 'user', '{}', 'ff', 'ee', '2000-01-01', 1)""", (str(t.org),))
    with t.tx() as conn:
        forged = conn.execute("select * from audit_events where event_type = 'FORGED'").fetchone()
        n = conn.execute("select count(*)::int as n from audit_events").fetchone()["n"]
    assert forged["seq"] == 2 and forged["canon_v"] == 2 and forged["prev_hash"] != "ff" and forged["hash"] != "ee" and forged["created_at"].year >= 2026
    assert n == 2 and _chain(t)["ok"]


def test_altering_a_payload_is_detected_at_that_exact_event(t):
    for i in range(6):
        _record(t, i, "x")
    _tamper("update audit_events set payload = jsonb_set(payload, '{n}', '999') where organization_id = %s and seq = 3", (str(t.org),))
    chain = _chain(t)
    assert chain["ok"] is False and chain["first_bad_seq"] == 3 and chain["checked"] == 3


@pytest.mark.parametrize("column,value", [("actor_id", "otra-persona"), ("event_type", "OTHER"), ("entity_id", "999"), ("actor_type", "system"),
                                         ("created_at", "2001-01-01 00:00:00+00")])
def test_altering_any_field_of_an_event_is_detected(t, column, value):
    for i in range(4):
        _record(t, i, "x", actor="user1")
    _tamper(f"update audit_events set {column} = %s where organization_id = %s and seq = 2", (value, str(t.org)))   # noqa: S608 — columnas fijas de la prueba
    chain = _chain(t)
    assert chain["ok"] is False and chain["first_bad_seq"] == 2


def test_deleting_a_middle_event_or_reordering_is_detected(t):
    for i in range(6):
        _record(t, i, "x")
    _tamper("delete from audit_events where organization_id = %s and seq = 3", (str(t.org),))
    chain = _chain(t)
    assert chain["ok"] is False and chain["first_bad_seq"] == 4                         # el hueco se delata en el eslabón siguiente


def test_moving_text_between_adjacent_fields_is_detected_with_the_v2_canonical_form(t):
    """Con la forma v1 («|» como separador) esto NO se detectaba si un valor contenía «|» (p. ej. `auth0|abc`)."""
    with t.tx() as conn:
        audit.record(conn, t.org, "SHIFT", actor=audit.Actor("user", "auth0|abc123"), entity_type="recommendation", entity_id="r1", payload={})
    with t.tx() as conn:
        assert conn.execute("select canon_v from audit_events where event_type = 'SHIFT'").fetchone()["canon_v"] == 2
    _tamper("update audit_events set actor_id = 'auth0', entity_type = 'abc123|recommendation' where organization_id = %s and event_type = 'SHIFT'", (str(t.org),))
    chain = _chain(t)
    assert chain["ok"] is False and chain["first_bad_seq"] == 1


def test_truncating_the_tail_is_invisible_to_the_chain_but_caught_by_a_pinned_head(t):
    """Limitación inherente a toda cadena de hashes: borrar los ÚLTIMOS eventos deja una cadena válida. Por eso existe el anclaje de la cabeza."""
    for i in range(8):
        _record(t, i, "x")
    with t.tx() as conn:
        head = conn.execute("select seq, hash from audit_events order by seq desc limit 1").fetchone()
    _tamper("delete from audit_events where organization_id = %s and seq > 5", (str(t.org),))
    assert _chain(t)["ok"] is True                                                      # la cadena restante es coherente…
    with t.tx() as conn:
        pinned = conn.execute("select 1 from audit_events where seq = %s and hash = %s", (head["seq"], head["hash"])).fetchone()
    assert pinned is None                                                               # …pero el evento anotado ya no existe


def test_rewriting_the_whole_chain_is_only_caught_by_an_external_anchor(t):
    for i in range(5):
        _record(t, i, "x")
    with t.tx() as conn:
        pin = conn.execute("select seq, hash from audit_events where seq = 2").fetchone()
    # propietario de la base: cambia el evento 2 y RECALCULA todos los hashes siguientes (el ataque completo)
    import psycopg

    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as c:
        c.execute("alter table audit_events disable trigger audit_events_no_update_delete")
        c.execute("alter table audit_events disable trigger audit_events_chain_trg")
        try:
            c.execute("update audit_events set payload = '{\"reescrito\": true}' where organization_id = %s and seq = 2", (str(t.org),))
            for seq in range(2, 6):
                c.execute("""update audit_events a set prev_hash = coalesce((select hash from audit_events p where p.organization_id = a.organization_id and p.seq = a.seq - 1), repeat('0', 64))
                              where a.organization_id = %s and a.seq = %s""", (str(t.org), seq))
                c.execute("update audit_events a set hash = encode(sha256(convert_to(audit_canonical(a), 'UTF8')), 'hex') where a.organization_id = %s and a.seq = %s",
                          (str(t.org), seq))
        finally:
            c.execute("alter table audit_events enable trigger audit_events_no_update_delete")
            c.execute("alter table audit_events enable trigger audit_events_chain_trg")
    assert _chain(t)["ok"] is True                                                      # ni la cadena ni el trigger pueden detectarlo…
    with t.tx() as conn:
        assert conn.execute("select 1 from audit_events where seq = %s and hash = %s", (pin["seq"], pin["hash"])).fetchone() is None   # …el ancla sí


def test_the_verify_endpoint_exposes_the_head_and_checks_a_pin(t):
    from cloudcost.config import get_settings
    from cloudcost.main import app
    from cloudcost.security import mint_dev_token
    from fastapi.testclient import TestClient

    for i in range(3):
        _record(t, i, "x")
    token = mint_dev_token(get_settings(), email="aud@example.com", role="AUDITOR", org_id=str(t.org))
    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {token}"}
        first = client.get("/api/v1/audit/verify", headers=h).json()
        assert first["ok"] and first["checked"] == 3 and first["head_seq"] == 3 and len(first["head_hash"]) == 64 and first["anchored"] is None
        ok = client.get(f"/api/v1/audit/verify?pin_seq=3&pin_hash={first['head_hash']}", headers=h).json()
        assert ok["anchored"] is True
        _tamper("delete from audit_events where organization_id = %s and seq = 3", (str(t.org),))
        after = client.get(f"/api/v1/audit/verify?pin_seq=3&pin_hash={first['head_hash']}", headers=h).json()
        assert after["ok"] is True and after["anchored"] is False and after["head_seq"] == 2
        assert client.get("/api/v1/audit/verify?pin_seq=3&pin_hash=zz", headers=h).status_code == 422


# --------------------------------------------------------------------------- mejora de la forma canónica sobre una base ya usada
def test_upgrade_keeps_old_events_verifiable_and_new_ones_are_v2():
    require_db()
    if not shutil.which("psql"):
        pytest.skip("psql no disponible")
    admin_url = os.environ["DATABASE_ADMIN_URL"]
    name = f"cc_upg_{uuid4().hex[:8]}"
    base = admin_url.rsplit("/", 1)[0]
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f'create database "{name}"')                                      # noqa: S608
    try:
        url = f"{base}/{name}"
        old = Path(os.environ.get("TMPDIR", "/tmp")) / f"mig_{name}"                # noqa: S108
        old.mkdir()
        for f in sorted((ROOT / "migrations").glob("*.sql")):
            if f.name < "011":
                (old / f.name).write_text(f.read_text())
        # migrate.sh fija la contraseña del rol cloudcost_app, que es COMPARTIDO por todo el clúster: debe conservar la que ya usa el resto de pruebas.
        app_pw = urlparse(os.environ["DATABASE_URL"]).password
        env = {**os.environ, "DATABASE_ADMIN_URL": url, "APP_DB_PASSWORD": app_pw, "MIGRATIONS_DIR": str(old)}
        subprocess.run(["sh", str(ROOT / "scripts" / "migrate.sh")], env=env, check=True, capture_output=True)
        org = str(uuid4())
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("insert into organizations (id, name, slug) values (%s, 'x', %s)", (org, f"u-{org[:8]}"))
            for i in range(3):                                                      # eventos escritos con la forma antigua (v1)
                c.execute("insert into audit_events (organization_id, event_type, actor_type, actor_id, payload) values (%s, 'OLD', 'user', 'auth0|x', %s)",
                          (org, f'{{"i": {i}}}'))
        env["MIGRATIONS_DIR"] = str(ROOT / "migrations")
        subprocess.run(["sh", str(ROOT / "scripts" / "migrate.sh")], env=env, check=True, capture_output=True)
        with psycopg.connect(url, autocommit=True) as c:
            for i in range(2):
                c.execute("insert into audit_events (organization_id, event_type, actor_type, payload) values (%s, 'NEW', 'user', '{}')", (org,))
            c.execute("select set_config('app.current_org', %s, false)", (org,))
            versions = [r[0] for r in c.execute("select canon_v from audit_events order by seq").fetchall()]
            ok = c.execute("select ok, checked from verify_audit_chain()").fetchone()
        assert versions == [1, 1, 1, 2, 2] and ok == (True, 5)                        # cadena mixta, íntegra de punta a punta
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c:
            c.execute(f'drop database "{name}" with (force)')                       # noqa: S608
        shutil.rmtree(old, ignore_errors=True)

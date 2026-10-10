"""Anclaje externo de la auditoría contra PostgreSQL real: lo que la cadena de hashes sola NO detecta (borrar los últimos eventos) sí lo detecta el ancla."""
from __future__ import annotations

import json
import os

import psycopg
import pytest
from cloudcost import cli
from cloudcost.services import audit
from dbkit import new_tenant


@pytest.fixture()
def t():
    return new_tenant("anc")


def _events(t, n: int) -> None:
    for i in range(n):
        with t.tx() as conn:
            audit.record(conn, t.org, "ANCHOR_TEST", actor=audit.Actor("user", "x"), entity_type="test", entity_id=str(i), payload={"i": i})


def _delete_last(t, count: int) -> None:
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as c:
        c.execute("alter table audit_events disable trigger audit_events_no_update_delete")
        try:
            c.execute("delete from audit_events where organization_id = %s and seq > (select max(seq) - %s from audit_events where organization_id = %s)",
                      (str(t.org), count, str(t.org)))
        finally:
            c.execute("alter table audit_events enable trigger audit_events_no_update_delete")


def _run(file, t, *extra) -> int:
    return cli.main(["audit-anchor", "--file", str(file), "--org", str(t.org), *extra])


def test_anchor_is_appended_chained_and_verified_on_the_next_run(t, tmp_path, capsys):
    _events(t, 5)
    f = tmp_path / "anchors.jsonl"
    assert _run(f, t) == 0
    first = [json.loads(x) for x in f.read_text().splitlines()]
    assert len(first) == 1 and first[0]["org_id"] == str(t.org) and first[0]["chain_ok"] is True and first[0]["head_seq"] >= 5
    _events(t, 3)                                                           # la auditoría sigue creciendo: las anclas viejas siguen siendo válidas
    assert _run(f, t) == 0
    lines = [json.loads(x) for x in f.read_text().splitlines()]
    assert len(lines) == 2 and lines[1]["prev"] == lines[0]["line_hash"] and lines[1]["head_seq"] > lines[0]["head_seq"]
    assert "fallidas: 0" in capsys.readouterr().out


def test_deleting_the_latest_events_is_caught_by_the_anchor_even_though_the_chain_still_verifies(t, tmp_path, capsys):
    _events(t, 6)
    f = tmp_path / "anchors.jsonl"
    assert _run(f, t) == 0
    _delete_last(t, 2)                                                       # un atacante con acceso a la base borra los últimos eventos
    with t.tx() as conn:
        assert conn.execute("select ok from verify_audit_chain()").fetchone()["ok"] is True      # la cadena, por sí sola, no lo ve
    assert _run(f, t, "--verify-only") == 1
    assert "ANCLA FALLIDA" in capsys.readouterr().err


def test_editing_or_removing_lines_of_the_anchor_file_is_detected(t, tmp_path, capsys):
    _events(t, 4)
    f = tmp_path / "anchors.jsonl"
    _run(f, t)
    _events(t, 2)
    _run(f, t)
    lines = f.read_text().splitlines()
    edited = json.loads(lines[0])
    edited["head_seq"] += 1
    f.write_text(json.dumps(edited, sort_keys=True) + "\n" + lines[1] + "\n")
    assert _run(f, t, "--verify-only") == 1
    assert "se editó" in capsys.readouterr().err
    f.write_text(lines[1] + "\n")                                            # se quitó la primera línea
    assert _run(f, t, "--verify-only") == 1
    assert "no continúa" in capsys.readouterr().err


def test_a_tampered_file_is_never_extended_and_all_orgs_are_listed_without_org_flag(t, tmp_path):
    _events(t, 3)
    f = tmp_path / "anchors.jsonl"
    f.write_text('{"org_id":"x","head_seq":1,"head_hash":"h","anchored_at":"t","prev":"bad","line_hash":"bad"}\n')
    assert cli.main(["audit-anchor", "--file", str(f)]) == 1
    assert len(f.read_text().splitlines()) == 1                              # no se añadió nada a un archivo comprometido
    from cloudcost.services import audit_anchor

    assert str(t.org) in audit_anchor.list_orgs()


def test_all_orgs_without_admin_url_asks_for_org_flags(t, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_ADMIN_URL")
    assert cli.main(["audit-anchor", "--file", str(tmp_path / "a.jsonl")]) == 2
    assert "--org" in capsys.readouterr().err

"""Los secretos no aparecen en logs, errores persistidos, auditoría, mensajes de proveedores Git ni trazas. Con valores centinela reconocibles."""
from __future__ import annotations

import io
import json
import logging

import pytest
from aws_stub import ACCOUNT, TODAY, StubbedSession, client_error
from cloudcost import redaction
from cloudcost.collectors.aws import AwsAccessError, AwsCollector
from cloudcost.git.base import GitProviderError
from cloudcost.git.github import GitHubProvider
from cloudcost.logging_config import JsonFormatter
from cloudcost.secrets import SecretResolver

CANARY = "CANARY-sup3r-s3cret-value-9f1c"
GH_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXBhcnRl"


@pytest.fixture(autouse=True)
def _clean_registry():
    redaction.forget_all()
    yield
    redaction.forget_all()


def _log_output(fn) -> str:
    """Ejecuta `fn` con el formateador JSON de producción conectado a TODOS los loggers y devuelve lo que habría salido por stdout."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level, old_handlers = root.level, root.handlers[:]
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)
    return stream.getvalue()


# --------------------------------------------------------------------------- patrones y valores conocidos
@pytest.mark.parametrize("text", [
    f"Authorization: Bearer {CANARY}", f"authorization={CANARY}", f"Bearer {CANARY}-abcdef", f"X-GitLab-Token: {CANARY}",
    f"token ghp en {GH_TOKEN}", f"clave {AWS_KEY} caducada", f"jwt {JWT}", "sesión ccs_" + "x" * 32, "glpat-" + "y" * 24,
    f"https://usuario:{CANARY}@git.example.com/repo.git", f"password={CANARY}", f'{{"client_secret": "{CANARY}"}}',
    f"GET /cb?code=abc&token={CANARY}&x=1", "external_id=" + CANARY,
])
def test_recognisable_secret_shapes_are_masked(text):
    out = redaction.redact_text(text)
    for needle in (CANARY, GH_TOKEN, AWS_KEY, JWT):
        assert needle not in out
    assert "x" * 32 not in out and "y" * 24 not in out
    assert redaction.MASK in out


def test_ordinary_text_hashes_and_uuids_are_left_alone():
    ok = ("Reducir i-0abc123def: m5.2xlarge → m5.xlarge. Hash 4f8a" + "0" * 60 + " rec 123e4567-e89b-12d3-a456-426614174000 "
          "tokenizer y passwordless no son secretos; sesión revocada; la clave de la etiqueta es Name")
    assert redaction.redact_text(ok) == ok


def test_registered_values_are_masked_wherever_they_appear():
    redaction.register(CANARY)
    assert redaction.redact_text(f"error al llamar con {CANARY} en la cabecera") == f"error al llamar con {redaction.MASK} en la cabecera"
    assert redaction.redact_text(f"{CANARY}{CANARY}") == redaction.MASK * 2
    redaction.register("abc")                                                           # demasiado corto: daría falsos positivos
    assert redaction.redact_text("abc abc") == "abc abc"


def test_resolving_a_secret_registers_it(monkeypatch):
    monkeypatch.setenv("CC_SECRET_LAB", CANARY)
    assert SecretResolver().resolve("env:CC_SECRET_LAB") == CANARY
    assert CANARY not in redaction.redact_text(f"fallo con {CANARY}")


def test_objects_are_redacted_by_key_and_by_content_but_references_and_numbers_are_kept():
    obj = {"token": CANARY, "nested": {"api_key": CANARY, "list": [{"password": CANARY}, f"Bearer {CANARY}-zzz"]}, "external_id_ref": "env:CC_SECRET_X",
           "evidence_hash": "ab" * 32, "sessions_closed": 3, "token_ref": "aws-sm:cloudcost/o/x", "count": 2, "empty_token": "", "note": "ok"}
    out = redaction.redact_obj(obj)
    assert CANARY not in json.dumps(out)
    assert out["external_id_ref"] == "env:CC_SECRET_X" and out["token_ref"] == "aws-sm:cloudcost/o/x" and out["evidence_hash"] == "ab" * 32
    assert out["sessions_closed"] == 3 and out["count"] == 2 and out["note"] == "ok" and out["empty_token"] == ""
    assert obj["token"] == CANARY                                                       # no muta el original


def test_deeply_nested_or_cyclic_structures_do_not_hang():
    deep: dict = {}
    cur = deep
    for _ in range(50):
        cur["a"] = {}
        cur = cur["a"]
    cur["token"] = CANARY
    assert CANARY not in json.dumps(redaction.redact_obj(deep))


# --------------------------------------------------------------------------- logs
def test_json_log_masks_message_extras_and_tracebacks():
    redaction.register(CANARY)
    log = logging.getLogger("prueba.secretos")

    def emit():
        log.warning("llamando con Authorization: Bearer %s", CANARY)
        log.info("evento", extra={"password": CANARY, "api_key": GH_TOKEN, "scan_id": "s-1"})
        try:
            raise RuntimeError(f"fallo con {GH_TOKEN} y {AWS_KEY} y {CANARY}")
        except RuntimeError:
            log.exception("scan fallido")

    out = _log_output(emit)
    assert out.count("\n") >= 3
    for needle in (CANARY, GH_TOKEN, AWS_KEY):
        assert needle not in out
    assert '"scan_id": "s-1"' in out                                                   # lo útil se conserva
    for line in out.splitlines():
        json.loads(line)                                                                # y cada línea sigue siendo JSON válido


def test_third_party_library_logs_are_covered_too():
    """El formateador está en el logger raíz: también filtra lo que escriban botocore, urllib3 o requests."""
    out = _log_output(lambda: logging.getLogger("botocore.hooks").debug("Event before-call: Authorization: AWS4-HMAC-SHA256 Credential=%s/2026", AWS_KEY))
    assert AWS_KEY not in out


def test_missing_smtp_logs_the_link_only_in_development(caplog):
    from cloudcost.auth import mailer
    from cloudcost.config import Settings

    mail = mailer.reset_mail("ana@example.com", "https://app.example.com/reset?token=" + CANARY)
    for env, expect in (("development", True), ("test", False), ("production", False)):
        caplog.clear()
        settings = Settings(env="test" if env == "production" else env, smtp_host=None)       # producción exige SMTP: aquí solo importa la rama
        with caplog.at_level(logging.WARNING):
            mailer.send(settings if env != "production" else settings.model_copy(update={"env": "production"}), mail)
        assert (CANARY in caplog.text) is expect, env


# --------------------------------------------------------------------------- proveedores Git y nube
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text, self.headers, self.content = status, body, json.dumps(body), {}, json.dumps(body).encode()

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self.headers, self.resp, self.sent = {}, resp, []

    def request(self, method, url, **kw):
        self.sent.append((method, url, kw))
        return self.resp


def test_a_git_server_that_echoes_the_token_in_its_error_cannot_leak_it_through_our_error():
    resp = _Resp(401, {"message": f"Bad credentials for token {GH_TOKEN}"})
    p = GitHubProvider(GH_TOKEN, session=_Session(resp))
    with pytest.raises(GitProviderError) as exc:
        p.get_file("acme/repo", "main.tf", "main")
    assert GH_TOKEN not in exc.value.message and GH_TOKEN not in str(exc.value)


def test_arbitrary_token_values_are_masked_once_resolved(monkeypatch):
    monkeypatch.setenv("CC_SECRET_GIT", CANARY)                                         # no «parece» un token: solo el registro lo reconoce
    token = SecretResolver().resolve("env:CC_SECRET_GIT")
    p = GitHubProvider(token, session=_Session(_Resp(403, {"message": f"forbidden for {CANARY}"})))
    with pytest.raises(GitProviderError) as exc:
        p.get_file("acme/repo", "main.tf", "main")
    assert CANARY not in exc.value.message


def test_aws_collection_failures_never_log_the_external_id_or_the_aws_error_text(monkeypatch):
    import boto3

    monkeypatch.setenv("CC_SECRET_EXT", CANARY)

    class Sts:
        def assume_role(self, **kw):
            raise client_error("AccessDenied", f"not authorized; ExternalId={CANARY}; key {AWS_KEY}", "AssumeRole", 403)

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Sts())
    collector = AwsCollector({"regions": ["us-east-1"], "role_arn": "arn:aws:iam::111122223333:role/R", "external_id_ref": "env:CC_SECRET_EXT",
                              "account_ref": ACCOUNT, "organization_id": None}, SecretResolver(), sleep=lambda s: None)

    def run():
        with pytest.raises(AwsAccessError) as exc:
            collector.collect()
        assert CANARY not in str(exc.value) and AWS_KEY not in str(exc.value)

    out = _log_output(run)
    assert CANARY not in out and AWS_KEY not in out


def test_inventory_api_errors_do_not_copy_aws_messages_into_logs_or_warnings():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "x", "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/a/b"})
    s.fail("ec2", "DescribeInstances", "UnauthorizedOperation", f"denied for {AWS_KEY} secret {CANARY}", status=403)
    s.respond("ec2", "DescribeVolumes", {"Volumes": []})
    s.respond("ec2", "DescribeImages", {"Images": []})
    s.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    c = AwsCollector({"regions": ["us-east-1"], "role_arn": None, "account_ref": ACCOUNT}, SecretResolver(), session=s, today=lambda: TODAY,
                     sleep=lambda x: None)
    holder = {}
    out = _log_output(lambda: holder.setdefault("r", c.collect()))
    blob = out + json.dumps(holder["r"].warnings) + json.dumps(holder["r"].issues)
    assert CANARY not in blob and AWS_KEY not in blob

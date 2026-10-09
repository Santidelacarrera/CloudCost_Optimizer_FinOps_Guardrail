"""Las referencias de secreto las escribe un usuario de la organización: no pueden leer variables ni secretos del servidor ni de otra organización."""
from __future__ import annotations

from uuid import uuid4

import pytest
from cloudcost.secrets import SecretError, SecretResolver, check_ref

ORG = str(uuid4())
OTHER = str(uuid4())


@pytest.mark.parametrize("ref", [
    "env:AUTH_PEPPER", "env:DATABASE_URL", "env:JWT_SECRET", "env:GITHUB_TOKEN", "env:PATH", "env:cc_secret_x", "env:CC_SECRET_",
    "env:CC_SECRET_X/../AUTH_PEPPER", "env:CC_SECRET_X\nAUTH_PEPPER", "env:", "file:/etc/passwd", "http://169.254.169.254/", "sin-esquema",
    "aws-sm:prod/db-password", "aws-sm:cloudcost", "aws-sm:cloudcost/", "aws-sm:cloudcost/../other/x", f"aws-sm:cloudcost/{ORG}/../{OTHER}/x",
    "aws-sm:arn:aws:secretsmanager:us-east-1:111111111111:secret:algo",
])
def test_references_outside_the_namespace_are_rejected(ref):
    with pytest.raises(SecretError):
        check_ref(ref)
    with pytest.raises(SecretError):
        SecretResolver().resolve(ref, ORG)


def test_env_reference_resolves_only_cc_secret_variables(monkeypatch):
    monkeypatch.setenv("CC_SECRET_GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("AUTH_PEPPER", "no-debe-salir")
    assert SecretResolver().resolve("env:CC_SECRET_GITHUB_TOKEN", ORG) == "ghp_x"
    with pytest.raises(SecretError):
        SecretResolver().resolve("env:AUTH_PEPPER", ORG)


def test_aws_secret_must_belong_to_the_calling_organization():
    check_ref(f"aws-sm:cloudcost/{ORG}/external-id", ORG)
    check_ref(f"aws-sm:cloudcost/{ORG}/external-id")                       # sin org: solo formato
    with pytest.raises(SecretError):
        check_ref(f"aws-sm:cloudcost/{OTHER}/external-id", ORG)             # secreto de otra organización
    with pytest.raises(SecretError):
        SecretResolver().resolve(f"aws-sm:cloudcost/{OTHER}/external-id", ORG)


def test_empty_reference_is_none():
    assert SecretResolver().resolve(None, ORG) is None and SecretResolver().resolve("", ORG) is None


def test_schemas_reject_foreign_references():
    from cloudcost.schemas import CloudAccountIn, RepositoryIn
    with pytest.raises(ValueError):
        CloudAccountIn(provider="aws", account_ref="a1", display_name="x", external_id_ref="env:AUTH_PEPPER")
    with pytest.raises(ValueError):
        RepositoryIn(provider="github", full_name="a/b", token_ref="env:DATABASE_URL")
    assert RepositoryIn(provider="github", full_name="a/b", token_ref="env:CC_SECRET_TOKEN").token_ref == "env:CC_SECRET_TOKEN"

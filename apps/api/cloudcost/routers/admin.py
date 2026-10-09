"""Conexiones: cuentas cloud y repositorios. Solo se guardan REFERENCIAS a secretos, nunca valores."""
from __future__ import annotations

import psycopg.errors
from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb

from .. import onboarding
from ..config import Settings, get_settings
from ..db import tenant_tx
from ..schemas import CloudAccountIn, RepositoryIn
from ..secrets import SecretError, check_ref
from ..security import MANAGE_CONNECTIONS, READ, Principal, require
from ..services import audit

router = APIRouter(tags=["connections"])


@router.get("/cloud-accounts")
def list_accounts(p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return conn.execute("select id, provider, account_ref, display_name, role_arn, regions, status, created_at "
                            "from cloud_accounts order by created_at").fetchall()


def _check_refs(p: Principal, *refs: str | None) -> None:
    """Las referencias de secreto deben estar en el espacio de nombres de la organización (evita leer secretos del servidor o de otras organizaciones)."""
    for ref in refs:
        if ref:
            try:
                check_ref(ref, p.org_id)
            except SecretError as exc:
                raise HTTPException(422, str(exc)) from exc


@router.post("/cloud-accounts", status_code=201)
def create_account(body: CloudAccountIn, p: Principal = Depends(require(*MANAGE_CONNECTIONS)),
                   settings: Settings = Depends(get_settings)):
    if body.provider == "demo" and not settings.demo_enabled:
        raise HTTPException(422, "El proveedor demo está deshabilitado")
    if body.provider == "aws" and not body.role_arn and settings.env == "production":
        raise HTTPException(422, "En producción se requiere role_arn (rol de solo lectura con ExternalId)")
    _check_refs(p, body.external_id_ref)
    try:
        with tenant_tx(p.org_id) as conn:
            row = conn.execute(
                """insert into cloud_accounts (organization_id, provider, account_ref, display_name, role_arn, external_id_ref, regions, provider_config)
                   values (%s, %s, %s, %s, %s, %s, %s, %s) returning id, provider, account_ref, display_name, regions, status""",
                (str(p.org_id), body.provider, body.account_ref, body.display_name, body.role_arn, body.external_id_ref, body.regions,
                 Jsonb(body.provider_config))).fetchone()
            audit.record(conn, p.org_id, audit.CLOUD_ACCOUNT_CREATED, actor=audit.user_actor(p), entity_type="cloud_account",
                         entity_id=row["id"], payload={"provider": body.provider, "account_ref": body.account_ref})
        return row
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "La cuenta ya está registrada") from exc


@router.get("/repositories")
def list_repositories(p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return conn.execute("select id, provider, full_name, default_branch, iac_paths, created_at from repositories "
                            "order by created_at").fetchall()


@router.post("/repositories", status_code=201)
def create_repository(body: RepositoryIn, p: Principal = Depends(require(*MANAGE_CONNECTIONS)),
                      settings: Settings = Depends(get_settings)):
    if body.provider == "local" and not settings.demo_enabled:
        raise HTTPException(422, "El proveedor local solo existe en modo demo")
    _check_refs(p, body.token_ref)
    try:
        with tenant_tx(p.org_id) as conn:
            row = conn.execute(
                """insert into repositories (organization_id, provider, full_name, default_branch, iac_paths, token_ref)
                   values (%s, %s, %s, %s, %s, %s) returning id, provider, full_name, default_branch, iac_paths""",
                (str(p.org_id), body.provider, body.full_name, body.default_branch, body.iac_paths, body.token_ref)).fetchone()
            audit.record(conn, p.org_id, audit.REPOSITORY_CREATED, actor=audit.user_actor(p), entity_type="repository",
                         entity_id=row["id"], payload={"provider": body.provider, "full_name": body.full_name})
        return row
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "El repositorio ya está registrado") from exc


@router.post("/onboarding/aws/cloudformation")
def start_aws_onboarding(p: Principal = Depends(require(*MANAGE_CONNECTIONS)), settings: Settings = Depends(get_settings),
                         enable_cost_explorer: bool = True):
    """Genera un ExternalId nuevo y el enlace de un clic para desplegar el rol de solo lectura en la cuenta del cliente.

    El ExternalId solo se devuelve aquí, una vez; no se guarda. No se crea nada en AWS.
    """
    try:
        info = onboarding.launch_info(template_url=settings.aws_onboarding_template_url,
                                      principal_arn=settings.aws_platform_principal_arn,
                                      region=settings.aws_onboarding_stack_region, enable_cost_explorer=enable_cost_explorer)
    except onboarding.OnboardingError as exc:
        raise HTTPException(503, str(exc)) from exc
    with tenant_tx(p.org_id) as conn:
        audit.record(conn, p.org_id, onboarding.ONBOARDING_STARTED, actor=audit.user_actor(p), entity_type="cloud_account",
                     payload={"provider": "aws", "method": "cloudformation", "region": info["region"]})
    return info

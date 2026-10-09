from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .secrets import SecretError, check_ref

_REF = re.compile(r"^(env|aws-sm):[A-Za-z0-9_./:@+=-]{1,200}$")


def _checked_ref(v: str) -> str:
    try:
        check_ref(v)
    except SecretError as exc:
        raise ValueError(str(exc)) from exc
    return v


class ScanCreate(BaseModel):
    cloud_account_id: UUID
    repository_id: UUID | None = None


class DecisionIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    expected_version: int | None = Field(default=None, ge=1, description="Versión que el usuario vio; evita aprobar datos obsoletos")


class DeployIn(BaseModel):
    reference: str | None = Field(default=None, max_length=300, description="URL del pipeline/commit de despliegue")


class VerifyIn(BaseModel):
    observed_monthly_cost: float | None = Field(
        default=None, ge=0, description="Si se omite, se calcula a partir de cost_records posteriores al despliegue")


class CloudAccountIn(BaseModel):
    provider: Literal["aws", "demo"]
    account_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    role_arn: str | None = Field(default=None, pattern=r"^arn:aws:iam::\d{12}:role/[\w+=,.@/-]{1,200}$")
    external_id_ref: str | None = None
    regions: list[str] = Field(default_factory=lambda: ["us-east-1"], min_length=1, max_length=20)

    @field_validator("external_id_ref")
    @classmethod
    def _check_ref(cls, v: str | None) -> str | None:
        if v is not None and not _REF.match(v):
            raise ValueError("Usa una referencia de secreto (env:CC_SECRET_NOMBRE o aws-sm:cloudcost/<org_id>/nombre), nunca el valor")
        return _checked_ref(v)

    @field_validator("regions")
    @classmethod
    def _check_regions(cls, v: list[str]) -> list[str]:
        if not all(re.fullmatch(r"[a-z]{2}-[a-z]+-\d", r) for r in v):
            raise ValueError("Región inválida")
        return v


class RepositoryIn(BaseModel):
    provider: Literal["github", "local"]
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    default_branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,100}$")
    iac_paths: list[str] = Field(default_factory=lambda: ["."], max_length=20)
    token_ref: str | None = None

    @field_validator("token_ref")
    @classmethod
    def _check_ref(cls, v: str | None) -> str | None:
        if v is not None and not _REF.match(v):
            raise ValueError("Usa una referencia de secreto (env:CC_SECRET_NOMBRE o aws-sm:cloudcost/<org_id>/nombre), nunca el token")
        return _checked_ref(v)

    @field_validator("iac_paths")
    @classmethod
    def _check_paths(cls, v: list[str]) -> list[str]:
        if any(".." in p or p.startswith("/") for p in v):
            raise ValueError("Ruta inválida")
        return v


class DevTokenIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Literal["ADMIN", "FINOPS", "SRE", "DEVELOPER", "AUDITOR", "VIEWER"]


class ImportIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00]+$")
    csv_text: str = Field(min_length=10, max_length=2_500_000)
    repository_id: UUID | None = None


class ExpenseFileIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00]+$")
    csv_text: str = Field(min_length=5, max_length=1_500_000)


class ExpenseAnalyzeIn(BaseModel):
    files: list[ExpenseFileIn] = Field(min_length=1, max_length=12)


# ---------------------------------------------------------------- cuentas propias
_EMAIL = r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$"
Role = Literal["ADMIN", "FINOPS", "SRE", "DEVELOPER", "AUDITOR", "VIEWER"]


class _EmailModel(BaseModel):
    email: str = Field(max_length=254, pattern=_EMAIL)

    @field_validator("email")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().lower()


class SignupIn(_EmailModel):
    password: str = Field(min_length=1, max_length=1024)
    full_name: str = Field(min_length=1, max_length=120)
    organization_name: str | None = Field(default=None, max_length=120)
    invite_token: str | None = Field(default=None, max_length=200)

    @field_validator("full_name", "organization_name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class LoginIn(_EmailModel):
    password: str = Field(min_length=1, max_length=1024)


class MfaVerifyIn(BaseModel):
    pending_token: str = Field(min_length=10, max_length=200)
    code: str = Field(min_length=6, max_length=32)


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class EmailIn(_EmailModel):
    pass


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class MfaSetupIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class MfaEnableIn(BaseModel):
    code: str = Field(min_length=6, max_length=12)


class MfaConfirmIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=32)


class InviteIn(_EmailModel):
    role: Role


class MemberPatchIn(BaseModel):
    role: Role | None = None
    disabled: bool | None = None

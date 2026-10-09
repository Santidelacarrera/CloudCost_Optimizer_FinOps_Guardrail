from __future__ import annotations

from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql://cloudcost_app:cloudcost_app@localhost:5432/cloudcost"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:3000"
    public_web_url: str = "http://localhost:3000"

    # --- autenticación
    auth_mode: Literal["dev", "oidc", "local"] = "dev"   # dev: tokens de desarrollo · oidc: IdP externo · local: solo cuentas propias
    auth_local_enabled: bool = True                      # cuentas propias (registro, contraseña, MFA) además del modo anterior
    auth_pepper: SecretStr = SecretStr("dev-only-pepper-change-me-0123456789abcdef")   # secreto del servidor para hashes y cifrado de MFA
    auth_session_hours: int = 12                         # caducidad absoluta de la sesión
    auth_session_idle_minutes: int = 120                 # caducidad por inactividad
    auth_signup_open: bool = True                        # false: solo se crean cuentas con invitación (para sumar una organización nueva hay que reabrirlo un momento)
    auth_mfa_issuer: str = "CloudCost"
    auth_hibp_enabled: bool = False                      # rechaza contraseñas filtradas (consulta k-anonimato a haveibeenpwned.com)
    auth_trust_forwarded: bool = False                   # confiar en X-Forwarded-For (solo detrás del BFF o de un proxy propio)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "CloudCost <no-reply@localhost>"
    smtp_starttls: bool = True
    jwt_secret: SecretStr = SecretStr("dev-only-secret-change-me-0123456789")      # HS256, solo AUTH_MODE=dev
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_org_claim: str = "org_id"
    oidc_role_claim: str = "role"
    dev_default_org_id: str = "11111111-1111-1111-1111-111111111111"

    # --- SSO (OpenID Connect con Microsoft Entra ID u Okta; ver docs/sso.md)
    sso_enabled: bool = False
    sso_provider: Literal["entra", "okta", "generic"] = "entra"
    sso_label: str = ""                                  # texto del botón; por defecto el nombre del proveedor
    sso_issuer: str = ""                                 # Entra: https://login.microsoftonline.com/<TENANT>/v2.0 · Okta: https://<org>.okta.com/oauth2/default
    sso_client_id: str = ""
    sso_client_secret: SecretStr = SecretStr("")
    sso_scopes: str = "openid email profile"
    sso_tenant_id: str = ""                              # solo Entra: GUID del directorio (se comprueba el claim `tid`)
    sso_org_id: str = ""                                 # organización de CloudCost a la que entra el personal de este IdP
    sso_allowed_domains: str = ""                        # lista separada por comas; vacío = cualquier dominio del IdP
    sso_role_claim: str = ""                             # por defecto: `roles` (Entra) o `groups` (Okta)
    sso_role_map: str = ""                               # «grupo-u-rol:ROL,otro:ADMIN»
    sso_default_role: str = "VIEWER"                     # rol sin coincidencia; vacío = rechazar el acceso
    sso_jit: bool = True                                 # crear la cuenta en el primer acceso
    sso_link_existing: bool = False                      # permitir que una cuenta local con el mismo correo pase a SSO (correo verificado por el IdP)
    sso_required_amr: str = ""                           # p. ej. «mfa» para exigir segundo factor verificado por el IdP

    # --- Git
    github_api_url: str = "https://api.github.com"
    github_token: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None

    # --- demo
    demo_enabled: bool = True
    demo_iac_dir: str = "/app/example-iac"
    demo_pr_dir: str = "/tmp/cloudcost-demo-prs"  # noqa: S108  (solo demo)

    # --- LLM (opcional; apagado por defecto)
    llm_enabled: bool = False
    llm_provider: Literal["openai", "gemini"] = "openai"
    llm_model: str = "gpt-4o-mini"
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 30.0

    # --- nube
    aws_cost_explorer_resources: bool = False

    # --- observabilidad
    otel_exporter_otlp_endpoint: str | None = None
    worker_metrics_port: int = 9100

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def _check_sso(self) -> None:
        from .auth import oidc  # import tardío: config no depende del resto del paquete

        problems = oidc.validate_issuer(self.sso_provider, self.sso_issuer, self.sso_tenant_id or None)
        if not self.sso_client_id or not self.sso_client_secret.get_secret_value():
            problems.append("SSO_CLIENT_ID y SSO_CLIENT_SECRET son obligatorios con SSO_ENABLED=true")
        if not _is_uuid(self.sso_org_id):
            problems.append("SSO_ORG_ID debe ser el UUID de la organización a la que entra el personal")
        try:
            oidc.parse_role_map(self.sso_role_map)
        except ValueError as exc:
            problems.append(str(exc))
        if self.sso_default_role and self.sso_default_role.upper() not in oidc.ROLE_PRIORITY:
            problems.append("SSO_DEFAULT_ROLE debe ser uno de " + ", ".join(oidc.ROLE_PRIORITY) + " (o vacío para rechazar)")
        if not self.sso_default_role and not self.sso_role_map:
            problems.append("Sin SSO_ROLE_MAP ni SSO_DEFAULT_ROLE nadie podría entrar")
        if problems:
            raise ValueError("; ".join(problems))

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.env == "production":
            problems = []
            if self.auth_mode == "dev":
                problems.append("AUTH_MODE no puede ser 'dev' en producción (usa 'local' u 'oidc')")
            if self.auth_mode == "oidc" and not (self.oidc_jwks_url and self.oidc_issuer and self.oidc_audience):
                problems.append("OIDC_JWKS_URL, OIDC_ISSUER y OIDC_AUDIENCE son obligatorios con AUTH_MODE=oidc")
            if self.demo_enabled:
                problems.append("DEMO_ENABLED debe ser false en producción")
            if self.auth_local_enabled or self.auth_mode == "local" or self.sso_enabled:
                pepper = self.auth_pepper.get_secret_value()
                if len(pepper) < 32 or pepper.startswith("dev-only"):
                    problems.append("AUTH_PEPPER debe ser un secreto propio de al menos 32 caracteres")
            if self.auth_local_enabled:
                if not self.smtp_host:
                    problems.append("SMTP_HOST es obligatorio: sin correo no hay verificación ni recuperación de contraseña")
                if not self.public_web_url.startswith("https://"):
                    problems.append("PUBLIC_WEB_URL debe ser https:// en producción")
            if self.sso_enabled and not self.public_web_url.startswith("https://"):
                problems.append("PUBLIC_WEB_URL debe ser https:// para usar SSO")
            if problems:
                raise ValueError("; ".join(problems))
        if self.sso_enabled:
            self._check_sso()
        if self.auth_mode == "dev" and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET debe tener al menos 32 caracteres")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

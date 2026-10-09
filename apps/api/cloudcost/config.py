from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .auth import pepper


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
    auth_pepper_id: str = "1"                            # id del pepper actual; al rotar, usa uno nuevo (p. ej. "2") — ver docs/pepper-rotation.md
    auth_pepper_previous: SecretStr | None = None        # peppers anteriores solo para leer datos viejos: "id:secreto[,id:secreto]"
    auth_session_hours: int = 12                         # caducidad absoluta de la sesión
    auth_session_idle_minutes: int = 120                 # caducidad por inactividad
    auth_signup_open: bool = True                        # false: solo se crean cuentas con invitación (para sumar una organización nueva hay que reabrirlo un momento)
    auth_mfa_issuer: str = "CloudCost"
    auth_hibp_enabled: bool = False                      # rechaza contraseñas filtradas (consulta k-anonimato a haveibeenpwned.com)
    passkeys_enabled: bool = True                        # llaves de acceso (WebAuthn) como segundo factor y como inicio de sesión sin contraseña
    webauthn_rp_id: str = ""                             # dominio de la web; por defecto el host de PUBLIC_WEB_URL (no se puede cambiar sin invalidar las llaves)
    webauthn_rp_name: str = "CloudCost"
    webauthn_origins: str = ""                           # orígenes permitidos, separados por comas; por defecto el de PUBLIC_WEB_URL
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
    gitlab_api_url: str = "https://gitlab.com/api/v4"       # GitLab autoalojado: https://gitlab.miempresa.com/api/v4
    gitlab_token: SecretStr | None = None
    gitlab_webhook_secret: SecretStr | None = None

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

    # --- onboarding de cuentas AWS con CloudFormation (un clic)
    aws_platform_principal_arn: str | None = None        # ARN del rol de CloudCost que asumirá el rol del cliente (el que va en la plantilla)
    aws_onboarding_template_url: str | None = None       # URL https de S3 donde está publicada infrastructure/cloudformation/readonly-role.yaml
    aws_onboarding_stack_region: str = "us-east-1"

    # --- nube
    aws_cost_explorer_resources: bool = True          # costo real por recurso (si falla, se usa la tabla de precios)
    aws_cost_tag_key: str | None = None               # etiqueta de asignación de costos para el historial mensual (p. ej. "Name" o "app")
    aws_cost_history_months: int = Field(6, ge=1, le=12)
    aws_cost_metric: Literal["UnblendedCost", "AmortizedCost", "NetUnblendedCost", "NetAmortizedCost"] = "UnblendedCost"

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

    @property
    def rp_id(self) -> str:
        return (self.webauthn_rp_id or urlparse(self.public_web_url).hostname or "localhost").lower()

    @property
    def webauthn_origin_list(self) -> tuple[str, ...]:
        explicit = [o.strip().rstrip("/") for o in self.webauthn_origins.split(",") if o.strip()]
        if explicit:
            return tuple(explicit)
        u = urlparse(self.public_web_url)
        return (f"{u.scheme}://{u.netloc}",)

    def _check_passkeys(self) -> None:
        host = (urlparse(self.public_web_url).hostname or "").lower()
        if not (host == self.rp_id or host.endswith("." + self.rp_id)):
            raise ValueError("WEBAUTHN_RP_ID debe ser el dominio de PUBLIC_WEB_URL (o uno superior)")
        for origin in self.webauthn_origin_list:
            u = urlparse(origin)
            ohost = (u.hostname or "").lower()
            if u.scheme not in ("https", "http") or not (ohost == self.rp_id or ohost.endswith("." + self.rp_id)):
                raise ValueError(f"WEBAUTHN_ORIGINS contiene un origen fuera de {self.rp_id}: {origin}")
            if self.env == "production" and u.scheme != "https":
                raise ValueError("WEBAUTHN_ORIGINS debe ser https:// en producción")

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
            if not self.gitlab_api_url.startswith("https://"):
                problems.append("GITLAB_API_URL debe ser https:// en producción (el token viaja en cada llamada)")
            if problems:
                raise ValueError("; ".join(problems))
        if self.sso_enabled:
            self._check_sso()
        if self.passkeys_enabled:
            self._check_passkeys()
        if self.auth_mode == "dev" and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET debe tener al menos 32 caracteres")
        return self

    @model_validator(mode="after")
    def _pepper_ring_guard(self) -> "Settings":
        previous = self.auth_pepper_previous.get_secret_value() if self.auth_pepper_previous else None
        try:
            _ = self.pepper_ring
            if self.env == "production":
                problems = pepper.validate_for_production(self.auth_pepper.get_secret_value(), previous)
                if problems:
                    raise ValueError("; ".join(problems))
        except pepper.PepperError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @property
    def pepper_ring(self) -> "pepper.PepperRing":
        previous = self.auth_pepper_previous.get_secret_value() if self.auth_pepper_previous else None
        return pepper.build_ring(self.auth_pepper.get_secret_value(), self.auth_pepper_id, previous)


@lru_cache
def get_settings() -> Settings:
    return Settings()

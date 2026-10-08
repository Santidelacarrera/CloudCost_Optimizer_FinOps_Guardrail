from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
            if self.auth_local_enabled or self.auth_mode == "local":
                pepper = self.auth_pepper.get_secret_value()
                if len(pepper) < 32 or pepper.startswith("dev-only"):
                    problems.append("AUTH_PEPPER debe ser un secreto propio de al menos 32 caracteres")
                if not self.smtp_host:
                    problems.append("SMTP_HOST es obligatorio: sin correo no hay verificación ni recuperación de contraseña")
                if not self.public_web_url.startswith("https://"):
                    problems.append("PUBLIC_WEB_URL debe ser https:// en producción")
            if problems:
                raise ValueError("; ".join(problems))
        if self.auth_mode == "dev" and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET debe tener al menos 32 caracteres")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

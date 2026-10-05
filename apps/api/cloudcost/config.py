from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql://cloudcost_app:cloudcost_app@localhost:5432/cloudcost"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:3000"
    public_web_url: str = "http://localhost:3000"

    # --- autenticación
    auth_mode: Literal["dev", "oidc"] = "dev"
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
    demo_pr_dir: str = "/tmp/cloudcost-demo-prs"

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

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.env == "production":
            problems = []
            if self.auth_mode != "oidc":
                problems.append("AUTH_MODE debe ser 'oidc' en producción")
            if not (self.oidc_jwks_url and self.oidc_issuer and self.oidc_audience):
                problems.append("OIDC_JWKS_URL, OIDC_ISSUER y OIDC_AUDIENCE son obligatorios en producción")
            if self.demo_enabled:
                problems.append("DEMO_ENABLED debe ser false en producción")
            if problems:
                raise ValueError("; ".join(problems))
        if self.auth_mode == "dev" and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET debe tener al menos 32 caracteres")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

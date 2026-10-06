"""El compose de producción y su plantilla de variables no pueden divergir de las guardas de configuración de la API."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from cloudcost.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
ENV_EXAMPLE = (ROOT / ".env.production.example").read_text(encoding="utf-8")

SAMPLE = {
    "APP_DB_PASSWORD": "app-pw", "POSTGRES_PASSWORD": "admin-pw", "PUBLIC_WEB_URL": "https://app.example.com",
    "AUTH_PEPPER": "p" * 48, "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "CloudCost <no-reply@example.com>",
    "SITE_DOMAIN": "app.example.com", "ACME_EMAIL": "ops@example.com", "IMAGE_PREFIX": "ghcr.io/acme/cloudcost",
}
VAR = re.compile(r"\$\{(\w+)(?::([-?])([^}]*))?\}")


def _expand(value: str) -> str:
    def sub(m: re.Match) -> str:
        name, op, arg = m.groups()
        if name in SAMPLE:
            return SAMPLE[name]
        if op == "-":
            return arg
        raise AssertionError(f"la variable obligatoria {name} no está en SAMPLE")
    return VAR.sub(sub, value)


def _service_env(name: str) -> dict[str, str]:
    env = COMPOSE["services"][name]["environment"]
    return {k: _expand(str(v)) for k, v in env.items()}


def test_api_environment_passes_production_guards():
    env = _service_env("api")
    settings = Settings(**{k.lower(): v for k, v in env.items() if k.lower() in Settings.model_fields})
    assert settings.env == "production" and settings.auth_mode == "local" and settings.demo_enabled is False
    assert settings.auth_trust_forwarded is True and settings.public_web_url.startswith("https://")


def test_worker_uses_the_same_environment_as_the_api():
    assert _service_env("worker") == _service_env("api")


def test_only_caddy_is_exposed_to_the_internet():
    for name, svc in COMPOSE["services"].items():
        for port in svc.get("ports", []):
            if name == "caddy":
                assert str(port).split(":")[0] in {"80", "443"}
            else:
                assert str(port).startswith("127.0.0.1:"), f"{name} publica {port} hacia fuera del servidor"


def test_web_has_no_development_switches():
    env = _service_env("web")
    assert "DEV_LOGIN" not in env and "INSECURE_COOKIES" not in env
    assert env["API_URL"] == "http://api:8000"


def test_production_never_seeds_development_data():
    assert _service_env("migrate")["SEED_DEV"] == "false"


def test_every_required_variable_is_documented_in_the_env_template():
    required = set(re.findall(r"\$\{(\w+):\?", (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")))
    assert required, "el compose debería exigir variables con :?"
    missing = [v for v in required if not re.search(rf"^{v}=", ENV_EXAMPLE, re.M)]
    assert not missing, f"faltan en .env.production.example: {missing}"


@pytest.mark.parametrize("name", ["api", "worker", "web", "migrate", "backup"])
def test_images_come_from_the_release_prefix(name):
    assert COMPOSE["services"][name]["image"].startswith("${IMAGE_PREFIX")

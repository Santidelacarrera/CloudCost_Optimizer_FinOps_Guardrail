"""Guardas de configuración: producción no arranca con valores de desarrollo."""
from __future__ import annotations

import pytest
from cloudcost.config import Settings

GOOD = dict(env="production", auth_mode="local", demo_enabled=False, smtp_host="smtp.example.com",
            public_web_url="https://app.example.com", auth_pepper="x" * 40)


def test_production_with_local_auth_is_accepted_when_hardened():
    assert Settings(**GOOD).auth_mode == "local"


@pytest.mark.parametrize("override,fragment", [
    ({"auth_pepper": "dev-only-pepper-change-me-0123456789abcdef"}, "AUTH_PEPPER"),
    ({"auth_pepper": "corto"}, "AUTH_PEPPER"),
    ({"smtp_host": None}, "SMTP_HOST"),
    ({"public_web_url": "http://app.example.com"}, "https"),
    ({"auth_mode": "dev"}, "AUTH_MODE"),
    ({"demo_enabled": True}, "DEMO_ENABLED"),
])
def test_production_rejects_insecure_settings(override, fragment):
    with pytest.raises(ValueError, match=fragment):
        Settings(**{**GOOD, **override})


def test_development_defaults_still_work():
    s = Settings(env="development")
    assert s.auth_mode == "dev" and s.auth_local_enabled

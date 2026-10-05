from __future__ import annotations

from typing import Any

from ..config import Settings
from ..git.base import GitProvider, GitProviderError
from ..git.github import GitHubProvider
from ..git.local import LocalDemoProvider
from ..secrets import SecretResolver


def get_git_provider(repo: dict[str, Any], settings: Settings, secrets: SecretResolver) -> GitProvider:
    provider = repo["provider"]
    if provider == "local":
        if not settings.demo_enabled:
            raise GitProviderError("El proveedor 'local' solo está disponible en modo demo")
        return LocalDemoProvider(settings.demo_iac_dir, settings.demo_pr_dir)
    if provider == "github":
        token = secrets.resolve(repo.get("token_ref")) or (
            settings.github_token.get_secret_value() if settings.github_token else None)
        return GitHubProvider(token or "", settings.github_api_url)
    raise GitProviderError(f"Proveedor Git '{provider}' aún no disponible (GitLab: Fase 2)")

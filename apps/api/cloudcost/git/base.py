"""Contrato común para integradores Git (GitHub hoy; GitLab en la Fase 2)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

# Archivos que se leen del repositorio: Terraform y los values/Chart de Helm (no todo el YAML: un repo de manifiestos tiene miles).
_IAC_FILE = re.compile(r"(\.tf$)|((^|/)(values[^/]*|Chart)\.ya?ml$)")


def is_iac_file(path: str) -> bool:
    return bool(_IAC_FILE.search(path))


class GitProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass
class ChangeRequest:
    number: int
    url: str
    branch: str
    draft: bool


class GitProvider(Protocol):
    def list_files(self, repo: str, ref: str, paths: list[str]) -> dict[str, str]:
        """Archivos de IaC (`.tf`, `values*.yaml`, `Chart.yaml`: path -> contenido) bajo `paths` en `ref`."""

    def get_file(self, repo: str, path: str, ref: str) -> str: ...

    def create_change_request(
        self, *, repo: str, base_branch: str, branch: str, path: str, new_text: str,
        commit_message: str, title: str, body: str, draft: bool, labels: list[str],
    ) -> ChangeRequest:
        """Crea rama + commit + Pull Request de forma idempotente (reintentar no duplica)."""

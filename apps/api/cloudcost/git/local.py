"""Proveedor Git "local" para el modo demo: lee IaC de un directorio y escribe los "PR" como carpetas.

Permite recorrer el flujo completo (incluida la generación del parche) sin cuenta de GitHub. Nunca modifica
los archivos originales. Solo disponible cuando DEMO_ENABLED=true.
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import ChangeRequest, GitProviderError, is_iac_file


class LocalDemoProvider:
    def __init__(self, iac_dir: str, out_dir: str):
        self.root = Path(iac_dir).resolve()
        self.out = Path(out_dir).resolve()
        if not self.root.is_dir():
            raise GitProviderError(f"DEMO_IAC_DIR no existe: {self.root}")

    def _safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root not in p.parents and p != self.root:
            raise GitProviderError("Ruta fuera del directorio demo")
        return p

    def list_files(self, repo: str, ref: str, paths: list[str]) -> dict[str, str]:
        return {str(f.relative_to(self.root)): f.read_text() for f in sorted(self.root.rglob("*"))
                if f.is_file() and is_iac_file(str(f.relative_to(self.root)))}

    def get_file(self, repo: str, path: str, ref: str) -> str:
        return self._safe(path).read_text()

    def create_change_request(self, *, repo: str, base_branch: str, branch: str, path: str, new_text: str,
                              commit_message: str, title: str, body: str, draft: bool,
                              labels: list[str]) -> ChangeRequest:
        slug = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
        target = self.out / slug
        (target / Path(path).parent).mkdir(parents=True, exist_ok=True)
        (target / path).write_text(new_text)
        (target / "PULL_REQUEST.md").write_text(
            f"# {title}\n\n{'**BORRADOR**' if draft else ''}\n\nRama: `{branch}` → `{base_branch}`\n"
            f"Etiquetas: {', '.join(labels)}\n\nCommit: {commit_message}\n\n---\n\n{body}\n")
        number = sum(1 for d in self.out.iterdir() if d.is_dir())
        return ChangeRequest(number=number, url=f"file://{target}", branch=branch, draft=draft)

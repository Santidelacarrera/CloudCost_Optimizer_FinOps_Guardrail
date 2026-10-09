"""Integrador GitLab (REST v4, gitlab.com o autoalojado). Solo crea ramas, commits y Merge Requests: jamás fusiona.

Equivale a `github.py`: el parche por offsets lo calcula `iac/patcher.py` (independiente del proveedor) y aquí solo se
publica el archivo ya modificado. La operación es idempotente: reintentar no duplica la rama, el commit ni el MR.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

import requests

from .base import ChangeRequest, GitProviderError

log = logging.getLogger(__name__)
_RETRY_STATUS = {429, 502, 503, 504}
MAX_FILES = 500
MAX_FILE_BYTES = 1_000_000
MAX_TREE_PAGES = 50                      # 50 × 100 entradas
DRAFT_PREFIX = "Draft: "                 # GitLab marca los borradores por el prefijo del título


class GitLabProvider:
    def __init__(self, token: str, api_url: str = "https://gitlab.com/api/v4", timeout: float = 15.0,
                 session: requests.Session | None = None):
        if not token:
            raise GitProviderError("Falta el token de GitLab (GITLAB_TOKEN o token_ref del repositorio)")
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({"PRIVATE-TOKEN": token, "Accept": "application/json", "User-Agent": "cloudcost-optimizer"})

    # ------------------------------------------------------------------ HTTP
    @staticmethod
    def _project(repo: str) -> str:
        """`grupo/subgrupo/proyecto` → ID de proyecto URL-encoded (las barras van como %2F)."""
        return quote(repo, safe="")

    def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kwargs) -> requests.Response:
        url = f"{self.api_url}{path}"
        for attempt in range(3):
            try:
                resp = self.http.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise GitProviderError(f"GitLab {method} {path}: error de red ({type(exc).__name__})") from exc
                time.sleep(min(2 ** attempt, 5))
                continue
            if resp.status_code in _RETRY_STATUS and attempt < 2:
                time.sleep(min(float(resp.headers.get("Retry-After") or 2 ** attempt), 5))
                continue
            if resp.status_code not in ok:
                try:
                    body = resp.json() or {}
                    detail = body.get("message") or body.get("error") or ""
                except (ValueError, AttributeError):
                    detail = resp.text[:120]
                raise GitProviderError(f"GitLab {method} {path} → {resp.status_code}: {str(detail)[:200]}", resp.status_code)
            return resp
        raise GitProviderError(f"GitLab {method} {path}: reintentos agotados")      # pragma: no cover

    # ------------------------------------------------------------------ lectura
    def get_file(self, repo: str, path: str, ref: str) -> str:
        resp = self._request("GET", f"/projects/{self._project(repo)}/repository/files/{quote(path, safe='')}/raw",
                             params={"ref": ref})
        try:
            return resp.content.decode("utf-8")      # `.text` supondría latin-1 si el servidor no declara charset
        except UnicodeDecodeError as exc:
            raise GitProviderError(f"{path}: el archivo no es UTF-8") from exc

    def _tree(self, repo: str, ref: str, prefix: str) -> list[dict]:
        entries: list[dict] = []
        page = "1"
        for _ in range(MAX_TREE_PAGES):
            params = {"ref": ref, "recursive": "true", "per_page": 100, "page": page}
            if prefix not in ("", "."):
                params["path"] = prefix
            resp = self._request("GET", f"/projects/{self._project(repo)}/repository/tree", params=params)
            entries += resp.json()
            page = resp.headers.get("X-Next-Page") or ""
            if not page:
                return entries
        raise GitProviderError("El árbol del repositorio es demasiado grande; acota iac_paths")

    def list_files(self, repo: str, ref: str, paths: list[str]) -> dict[str, str]:
        wanted: list[str] = []
        for prefix in dict.fromkeys(p.strip("/") for p in (paths or ["."])):
            for e in self._tree(repo, ref, prefix):
                if e.get("type") == "blob" and e["path"].endswith(".tf") and e["path"] not in wanted:
                    wanted.append(e["path"])
        if len(wanted) > MAX_FILES:
            raise GitProviderError(f"Demasiados archivos .tf ({len(wanted)} > {MAX_FILES}); acota iac_paths")
        out: dict[str, str] = {}
        for p in wanted:
            text = self.get_file(repo, p, ref)
            if len(text.encode("utf-8")) <= MAX_FILE_BYTES:
                out[p] = text
        return out

    # ------------------------------------------------------------------ escritura
    def _ensure_branch(self, repo: str, base: str, branch: str) -> None:
        try:
            self._request("POST", f"/projects/{self._project(repo)}/repository/branches", json={"branch": branch, "ref": base})
        except GitProviderError as exc:
            if exc.status == 400 and "already exists" in exc.message.lower():
                log.info("rama %s ya existe; se reutiliza", branch)
                return
            raise

    def _commit_file(self, repo: str, branch: str, path: str, new_text: str, message: str) -> None:
        if self.get_file(repo, path, branch) == new_text:
            return                                       # reintento: el commit ya está en la rama
        self._request("PUT", f"/projects/{self._project(repo)}/repository/files/{quote(path, safe='')}",
                      json={"branch": branch, "content": new_text, "commit_message": message})

    def create_change_request(self, *, repo: str, base_branch: str, branch: str, path: str, new_text: str,
                              commit_message: str, title: str, body: str, draft: bool,
                              labels: list[str]) -> ChangeRequest:
        project = self._project(repo)
        self._ensure_branch(repo, base_branch, branch)
        self._commit_file(repo, branch, path, new_text, commit_message)

        existing = self._request("GET", f"/projects/{project}/merge_requests",
                                 params={"source_branch": branch, "target_branch": base_branch, "state": "opened"}).json()
        if existing:
            mr = existing[0]
            if labels:
                self._add_labels(project, mr["iid"], labels)
        else:
            mr = self._request("POST", f"/projects/{project}/merge_requests", json={
                "source_branch": branch, "target_branch": base_branch, "description": body,
                "title": (DRAFT_PREFIX + title) if draft and not title.lower().startswith(("draft:", "[draft]")) else title,
                "labels": ",".join(labels), "remove_source_branch": False, "squash": False}).json()
        is_draft = bool(mr.get("draft") or mr.get("work_in_progress") or draft)
        return ChangeRequest(number=mr["iid"], url=mr["web_url"], branch=branch, draft=is_draft)

    def _add_labels(self, project: str, iid: int, labels: list[str]) -> None:
        try:
            self._request("PUT", f"/projects/{project}/merge_requests/{iid}", json={"add_labels": ",".join(labels)})
        except GitProviderError as exc:                 # las etiquetas son accesorias: no abortan el flujo
            log.warning("no se pudieron aplicar etiquetas: %s", exc.message)

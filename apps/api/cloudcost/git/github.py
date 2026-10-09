"""Integrador GitHub (REST). Solo crea ramas, commits y Pull Requests: jamás hace merge ni despliega."""
from __future__ import annotations

import base64
import logging
import time
from urllib.parse import quote

import requests

from .base import ChangeRequest, GitProviderError, is_iac_file

log = logging.getLogger(__name__)
_RETRY_STATUS = {429, 502, 503, 504}
MAX_FILES = 500
MAX_FILE_BYTES = 1_000_000


class GitHubProvider:
    def __init__(self, token: str, api_url: str = "https://api.github.com", timeout: float = 15.0,
                 session: requests.Session | None = None):
        if not token:
            raise GitProviderError("Falta el token de GitHub (GITHUB_TOKEN o token_ref del repositorio)")
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.http = session or requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cloudcost-optimizer",
        })

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), headers: dict | None = None,
                 **kwargs) -> requests.Response:
        url = f"{self.api_url}{path}"
        for attempt in range(3):
            try:
                resp = self.http.request(method, url, timeout=self.timeout, headers=headers, **kwargs)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise GitProviderError(f"GitHub {method} {path}: error de red ({type(exc).__name__})") from exc
                time.sleep(min(2 ** attempt, 5))
                continue
            if resp.status_code in _RETRY_STATUS and attempt < 2:
                time.sleep(min(2 ** attempt, 5))
                continue
            if resp.status_code not in ok:
                try:
                    detail = (resp.json() or {}).get("message", "")
                except ValueError:
                    detail = resp.text[:120]
                raise GitProviderError(f"GitHub {method} {path} → {resp.status_code}: {detail[:200]}", resp.status_code)
            return resp
        raise GitProviderError(f"GitHub {method} {path}: reintentos agotados")      # pragma: no cover

    # ------------------------------------------------------------------ lectura
    def get_file(self, repo: str, path: str, ref: str) -> str:
        resp = self._request("GET", f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref},
                             headers={"Accept": "application/vnd.github.raw+json"})
        return resp.text

    def list_files(self, repo: str, ref: str, paths: list[str]) -> dict[str, str]:
        tree = self._request("GET", f"/repos/{repo}/git/trees/{quote(ref, safe='')}", params={"recursive": "1"}).json()
        if tree.get("truncated"):
            raise GitProviderError("El árbol del repositorio es demasiado grande (truncado); acota iac_paths")
        prefixes = [p.strip("/") for p in (paths or ["."])]
        wanted = [
            e["path"] for e in tree.get("tree", [])
            if e.get("type") == "blob" and is_iac_file(e["path"]) and (e.get("size") or 0) <= MAX_FILE_BYTES
            and any(p in ("", ".") or e["path"] == p or e["path"].startswith(p + "/") for p in prefixes)
        ]
        if len(wanted) > MAX_FILES:
            raise GitProviderError(f"Demasiados archivos de IaC ({len(wanted)} > {MAX_FILES}); acota iac_paths")
        return {p: self.get_file(repo, p, ref) for p in wanted}

    # ------------------------------------------------------------------ escritura
    def _ensure_branch(self, repo: str, base: str, branch: str) -> None:
        base_sha = self._request("GET", f"/repos/{repo}/git/ref/heads/{quote(base, safe='')}").json()["object"]["sha"]
        try:
            self._request("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        except GitProviderError as exc:
            if exc.status == 422 and "already exists" in exc.message.lower():
                log.info("rama %s ya existe; se reutiliza", branch)
                return
            raise

    def _commit_file(self, repo: str, branch: str, path: str, new_text: str, message: str) -> None:
        current = self._request("GET", f"/repos/{repo}/contents/{quote(path)}", params={"ref": branch}).json()
        existing = base64.b64decode(current["content"]).decode("utf-8") if current.get("content") else None
        if existing == new_text:
            return                                       # reintento: el commit ya está en la rama
        self._request("PUT", f"/repos/{repo}/contents/{quote(path)}", json={
            "message": message,
            "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
            "sha": current["sha"],
            "branch": branch,
        })

    def create_change_request(self, *, repo: str, base_branch: str, branch: str, path: str, new_text: str,
                              commit_message: str, title: str, body: str, draft: bool,
                              labels: list[str]) -> ChangeRequest:
        self._ensure_branch(repo, base_branch, branch)
        self._commit_file(repo, branch, path, new_text, commit_message)

        owner = repo.split("/", 1)[0]
        existing = self._request("GET", f"/repos/{repo}/pulls",
                                 params={"head": f"{owner}:{branch}", "state": "open"}).json()
        if existing:
            pr = existing[0]
        else:
            pr = self._request("POST", f"/repos/{repo}/pulls", json={
                "title": title, "head": branch, "base": base_branch, "body": body, "draft": draft}).json()
        if labels:
            try:
                self._request("POST", f"/repos/{repo}/issues/{pr['number']}/labels", json={"labels": labels})
            except GitProviderError as exc:             # las etiquetas son accesorias: no abortan el flujo
                log.warning("no se pudieron aplicar etiquetas: %s", exc.message)
        return ChangeRequest(number=pr["number"], url=pr["html_url"], branch=branch, draft=bool(pr.get("draft", draft)))

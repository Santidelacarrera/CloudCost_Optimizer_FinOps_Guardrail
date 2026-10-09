"""La generación de Pull Requests NO ejecuta cambios: solo crea rama, commit y PR/MR (borrador si procede). Nunca fusiona, despliega ni aplica.

Se comprueba de cuatro maneras independientes: (1) la interfaz del proveedor no tiene operaciones de fusión; (2) el código fuente de los proveedores
y del flujo no contiene llamadas a endpoints de fusión ni de ejecución; (3) las peticiones HTTP REALES que emite cada proveedor, grabadas
de punta a punta; (4) nada del servicio lanza procesos salvo el motor de políticas.
"""
from __future__ import annotations

import base64
import inspect
import re
from pathlib import Path

import pytest
from cloudcost.git import base as git_base
from cloudcost.git.github import GitHubProvider
from cloudcost.git.gitlab import GitLabProvider
from cloudcost.git.local import LocalDemoProvider

API = Path(__file__).resolve().parents[2] / "apps" / "api" / "cloudcost"
FORBIDDEN_NAMES = re.compile(r"merge|apply|deploy|approve|push_to|force|delete_branch|rebase|terraform|helm_upgrade|kubectl", re.IGNORECASE)


def test_provider_interface_has_no_way_to_merge_or_apply():
    public = {n for n, _ in inspect.getmembers(git_base.GitProvider, inspect.isfunction) if not n.startswith("_")}
    assert public == {"list_files", "get_file", "create_change_request"}
    for cls in (GitHubProvider, GitLabProvider, LocalDemoProvider):
        names = {n for n, _ in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}
        assert names == public, (cls.__name__, names ^ public)
        assert not [n for n in names if FORBIDDEN_NAMES.search(n)]


def _string_literals(path: Path) -> list[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_provider_and_workflow_code_never_names_a_merge_or_execution_endpoint():
    """Se miran los literales de cadena del código (rutas de API, claves de cuerpo), no los comentarios que explican la prohibición."""
    forbidden = re.compile(r"/merge$|/merge[/?]|merge_pull_request|auto_?merge|merge_when_pipeline|should_remove_source_branch|/rebase|workflow_dispatch|"
                           r"/dispatches|/pipelines?\b|/jobs\b|/play\b|/deployments|/check-runs|/approve|enable_auto", re.IGNORECASE)
    files = [*(API / "git").glob("*.py"), API / "services" / "workflow.py"]
    offenders = {f.name: [s for s in _string_literals(f) if forbidden.search(s)] for f in files}
    offenders = {k: v for k, v in offenders.items() if v}
    # «/approve» aparece en workflow.py solo como NOMBRE DE ESTADO interno, nunca como llamada a un proveedor
    assert offenders in ({}, ) or set(offenders) == {"workflow.py"} and all("approve" in x.lower() for x in offenders["workflow.py"]), offenders


def test_no_process_is_launched_except_the_policy_engine():
    offenders = {}
    for path in API.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bsubprocess\b|os\.system|os\.popen|\bPopen\b|pty\.spawn|\bexec\(|\beval\(", text):
            offenders[str(path.relative_to(API))] = True
    assert set(offenders) == {"guardrail.py"}, offenders


# --------------------------------------------------------------------------- tráfico HTTP real de cada proveedor
class _Resp:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}
        self.text = text if text is not None else ""
        self.content = self.text.encode()

    def json(self):
        return self._body


class _Recorder:
    """Sesión `requests` falsa: graba cada petición y responde según una función (método, ruta) → respuesta."""

    def __init__(self, route):
        self.headers, self.route, self.sent = {}, route, []

    def request(self, method, url, **kw):
        path = url.split("/api/v4", 1)[-1] if "/api/v4" in url else url.split("api.github.com", 1)[-1]
        self.sent.append((method, path, kw.get("json")))
        return self.route(method, path, kw)


def _github_route(prs_open=False):
    def route(method, path, kw):
        if method == "GET" and "/git/ref/heads/main" in path:
            return _Resp(200, {"object": {"sha": "abc123"}})
        if method == "POST" and path.endswith("/git/refs"):
            return _Resp(201, {})
        if method == "GET" and "/contents/" in path:
            return _Resp(200, {"content": base64.b64encode(b"old").decode(), "sha": "blob1"})
        if method == "PUT" and "/contents/" in path:
            return _Resp(200, {})
        if method == "GET" and path.endswith("/pulls"):
            return _Resp(200, [{"number": 7, "html_url": "https://github.com/a/b/pull/7", "draft": True}] if prs_open else [])
        if method == "POST" and path.endswith("/pulls"):
            return _Resp(201, {"number": 7, "html_url": "https://github.com/a/b/pull/7", "draft": kw["json"]["draft"]})
        if method == "POST" and "/labels" in path:
            return _Resp(200, {})
        raise AssertionError(f"petición inesperada a GitHub: {method} {path}")
    return route


def _gitlab_route():
    def route(method, path, kw):
        if method == "POST" and path.endswith("/repository/branches"):
            return _Resp(201, {})
        if method == "GET" and "/repository/files/" in path:
            return _Resp(200, text="old")
        if method == "PUT" and "/repository/files/" in path:
            return _Resp(200, {})
        if method == "GET" and path.endswith("/merge_requests") or (method == "GET" and "/merge_requests?" in path):
            return _Resp(200, [])
        if method == "POST" and path.endswith("/merge_requests"):
            return _Resp(201, {"iid": 3, "web_url": "https://gitlab.com/a/b/-/merge_requests/3", "title": kw["json"]["title"]})
        raise AssertionError(f"petición inesperada a GitLab: {method} {path}")
    return route


ARGS = dict(repo="acme/infra", base_branch="main", branch="finops/rec-1", path="main.tf", new_text="new", commit_message="msg", title="Reducir X",
            body="cuerpo", labels=["finops", "automated", "do-not-auto-merge"])


@pytest.mark.parametrize("draft", [True, False])
def test_github_creates_branch_commit_and_pull_request_and_nothing_else(draft):
    s = _Recorder(_github_route())
    cr = GitHubProvider("tok", session=s).create_change_request(draft=draft, **ARGS)
    assert cr.number == 7 and cr.draft is draft
    verbs = [(m, re.sub(r"/repos/[^/]+/[^/]+", "", p.split("?")[0])) for m, p, _ in s.sent]
    assert set(verbs) == {("GET", "/git/ref/heads/main"), ("POST", "/git/refs"), ("GET", "/contents/main.tf"), ("PUT", "/contents/main.tf"),
                          ("GET", "/pulls"), ("POST", "/pulls"), ("POST", "/issues/7/labels")}
    assert not [p for _, p, _ in s.sent if re.search(r"merge|/actions|/dispatches|/deployments|/check-runs|/statuses|/reviews|/rebase", p)]
    assert not [m for m, _, _ in s.sent if m in ("DELETE", "PATCH")]
    pr = next(b for m, p, b in s.sent if m == "POST" and p.endswith("/pulls"))
    assert pr["draft"] is draft and pr["base"] == "main" and pr["head"] == "finops/rec-1" and "merge" not in " ".join(pr)
    assert next(b for m, p, b in s.sent if m == "POST" and "/labels" in p)["labels"] == ARGS["labels"]


def test_github_retry_is_idempotent_and_still_never_merges():
    s = _Recorder(_github_route(prs_open=True))
    GitHubProvider("tok", session=s).create_change_request(draft=True, **ARGS)
    assert not [1 for m, p, _ in s.sent if m == "POST" and p.endswith("/pulls")]                  # el PR abierto se reutiliza
    assert not [1 for _, p, _ in s.sent if "merge" in p]


@pytest.mark.parametrize("draft", [True, False])
def test_gitlab_creates_branch_commit_and_merge_request_and_nothing_else(draft):
    s = _Recorder(_gitlab_route())
    cr = GitLabProvider("tok", session=s).create_change_request(draft=draft, **ARGS)
    assert cr.number == 3 and cr.draft is draft
    paths = [(m, re.sub(r"/projects/[^/]+", "", p.split("?")[0])) for m, p, _ in s.sent]
    assert set(paths) == {("POST", "/repository/branches"), ("GET", "/repository/files/main.tf/raw"), ("PUT", "/repository/files/main.tf"),
                          ("GET", "/merge_requests"), ("POST", "/merge_requests")}
    assert not [p for _, p, _ in s.sent if re.search(r"/merge\b|/pipelines|/jobs|/play|/rebase|/approve|/cancel_merge", p)]
    mr = next(b for m, p, b in s.sent if m == "POST" and p.endswith("/merge_requests"))
    assert mr["title"].startswith("Draft: ") is draft
    assert not {"merge_when_pipeline_succeeds", "auto_merge", "should_remove_source_branch", "merge_commit_message"} & set(mr)
    assert mr["remove_source_branch"] is False                                                     # tampoco borra la rama


def test_local_demo_provider_only_writes_inside_its_output_directory(tmp_path):
    iac = tmp_path / "iac"
    iac.mkdir()
    (iac / "main.tf").write_text("original")
    out = tmp_path / "prs"
    out.mkdir()
    LocalDemoProvider(str(iac), str(out)).create_change_request(draft=True, **ARGS)
    assert (iac / "main.tf").read_text() == "original"                                              # nunca toca los archivos originales
    assert (out / "finops_rec-1" / "main.tf").read_text() == "new" and "BORRADOR" in (out / "finops_rec-1" / "PULL_REQUEST.md").read_text()
    with pytest.raises(git_base.GitProviderError):
        LocalDemoProvider(str(iac), str(out)).get_file("x", "../../etc/passwd", "main")             # y no sale de su directorio

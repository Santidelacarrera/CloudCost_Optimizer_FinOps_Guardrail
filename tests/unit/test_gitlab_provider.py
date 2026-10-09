"""GitLabProvider contra un servidor HTTP local que simula la API v4 de GitLab (sin red externa).

Comprueba lo esencial: subgrupos (%2F), paginación, idempotencia, borradores, etiquetas, que jamás se fusiona,
que el token no se filtra y que el parche por offsets llega intacto al commit.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from cloudcost.domain.rules import ACTION_RESIZE
from cloudcost.git.base import GitProviderError
from cloudcost.git.gitlab import GitLabProvider
from cloudcost.iac.patcher import build_patch
from cloudcost.iac.terraform import IacIndex

EXAMPLE = Path(__file__).resolve().parents[2] / "infrastructure/terraform/example-iac/main.tf"
FILE = EXAMPLE.read_text()
PROJECT = "grupo%2Fsub%2Fproyecto"                      # grupo/sub/proyecto, URL-encoded


class FakeGitLab(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, status, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if isinstance(payload, bytes) else "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _record(self, method):
        s = self.state
        s["calls"].append((method, self.path))
        s["tokens"].add(self.headers.get("PRIVATE-TOKEN"))

    def do_GET(self):
        self._record("GET")
        s, url = self.state, urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        base = f"/api/v4/projects/{PROJECT}"
        if url.path == f"{base}/repository/tree":
            page = int(q.get("page", "1"))
            s["tree_queries"].append(q)
            entries = {1: [{"path": "infra/main.tf", "type": "blob"}, {"path": "infra/readme.md", "type": "blob"},
                           {"path": "infra", "type": "tree"}],
                       2: [{"path": "infra/net/vpc.tf", "type": "blob"}, {"path": "infra/charts/app/values.yaml", "type": "blob"},
                           {"path": "infra/k8s/deployment.yaml", "type": "blob"}]}[page]
            return self._send(200, entries, {"X-Next-Page": "2" if page == 1 else ""})
        m = re.fullmatch(rf"{re.escape(base)}/repository/files/(.+)/raw", url.path)
        if m:
            path, ref = unquote(m.group(1)), q["ref"]
            if path == "nope.tf":
                return self._send(404, {"message": "404 File Not Found"})
            if path == "binary.tf":
                return self._send(200, b"\xff\xfe\x00")
            if path == "infra/net/vpc.tf":
                return self._send(200, "# vpc ñ\n".encode())
            if path == "infra/charts/app/values.yaml":
                return self._send(200, b"replicaCount: 3\n")
            return self._send(200, s["branches"].get(ref, FILE).encode())
        if url.path == f"{base}/merge_requests":
            return self._send(200, [mr for mr in s["mrs"] if mr["source_branch"] == q["source_branch"]])
        self._send(404, {"message": "404 Not Found"})

    def do_POST(self):
        self._record("POST")
        s, body, url = self.state, self._body(), urlparse(self.path)
        base = f"/api/v4/projects/{PROJECT}"
        if url.path == f"{base}/repository/branches":
            if body["branch"] in s["branches"]:
                return self._send(400, {"message": "Branch already exists"})
            s["branches"][body["branch"]] = FILE
            return self._send(201, {"name": body["branch"]})
        if url.path == f"{base}/merge_requests":
            s["mr_payloads"].append(body)
            mr = {"iid": 12, "web_url": "https://gitlab.example.com/grupo/sub/proyecto/-/merge_requests/12",
                  "title": body["title"], "source_branch": body["source_branch"], "draft": body["title"].startswith("Draft:")}
            s["mrs"].append(mr)
            return self._send(201, mr)
        self._send(404, {"message": "nf"})

    def do_PUT(self):
        self._record("PUT")
        s, body, url = self.state, self._body(), urlparse(self.path)
        base = f"/api/v4/projects/{PROJECT}"
        if url.path.startswith(f"{base}/repository/files/"):
            s["branches"][body["branch"]] = body["content"]
            s["commit_messages"].append(body["commit_message"])
            return self._send(200, {"file_path": unquote(url.path.rsplit("/", 1)[1]), "branch": body["branch"]})
        if re.fullmatch(rf"{re.escape(base)}/merge_requests/\d+", url.path):
            s["label_updates"].append(body)
            return self._send(200, {})
        self._send(404, {"message": "nf"})


@pytest.fixture
def gitlab():
    FakeGitLab.state = {"branches": {}, "mrs": [], "calls": [], "tokens": set(), "tree_queries": [], "mr_payloads": [],
                        "commit_messages": [], "label_updates": []}
    srv = HTTPServer(("127.0.0.1", 0), FakeGitLab)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield GitLabProvider("glpat-secret", api_url=f"http://127.0.0.1:{srv.server_port}/api/v4"), FakeGitLab.state
    srv.shutdown()


REPO = "grupo/sub/proyecto"


def test_list_files_follows_pagination_filters_and_uses_path_filter(gitlab):
    gl, st = gitlab
    files = gl.list_files(REPO, "main", ["infra"])
    assert list(files) == ["infra/main.tf", "infra/net/vpc.tf", "infra/charts/app/values.yaml"]   # incluye values de Helm; excluye readme.md, el directorio y otros YAML
    assert files["infra/net/vpc.tf"] == "# vpc ñ\n"                           # UTF-8 correcto aunque no haya charset
    assert [q["page"] for q in st["tree_queries"]] == ["1", "2"] and st["tree_queries"][0]["path"] == "infra"
    assert st["tree_queries"][0]["recursive"] == "true"


def test_root_path_does_not_send_a_path_filter(gitlab):
    gl, st = gitlab
    gl.list_files(REPO, "main", ["."])
    assert "path" not in st["tree_queries"][0]


def test_create_change_request_is_idempotent_draft_and_never_merges(gitlab):
    gl, st = gitlab
    kw = dict(repo=REPO, base_branch="main", branch="finops/abc-x", path="infra/main.tf",
              new_text=FILE.replace("m5.2xlarge", "m5.xlarge"), commit_message="finops: reducir", title="[FinOps] Reducir web",
              body="cuerpo", draft=True, labels=["finops", "automated"])
    first = gl.create_change_request(**kw)
    second = gl.create_change_request(**kw)                                    # reintento
    assert first.number == second.number == 12 and first.draft and first.url.endswith("/merge_requests/12")
    assert len(st["mrs"]) == 1 and len(st["commit_messages"]) == 1               # ni MR ni commit duplicados
    assert st["mr_payloads"][0]["title"] == "Draft: [FinOps] Reducir web"
    assert st["mr_payloads"][0]["labels"] == "finops,automated" and st["mr_payloads"][0]["squash"] is False
    assert st["label_updates"] == [{"add_labels": "finops,automated"}]           # en el reintento solo se añaden etiquetas
    # Garantía de seguridad: solo ramas, commits de archivo, MR y etiquetas. Nada de /merge, aprobar ni borrar.
    for method, path in st["calls"]:
        route = re.sub(r"\?.*", "", unquote(path))
        assert method in ("GET", "POST", "PUT") and "/merge" not in route.replace("/merge_requests", "")
        assert not route.endswith(("/approve", "/rebase", "/cancel_merge_when_pipeline_succeeds"))
    assert st["tokens"] == {"glpat-secret"}


def test_non_draft_title_is_untouched(gitlab):
    gl, st = gitlab
    gl.create_change_request(repo=REPO, base_branch="main", branch="finops/x", path="infra/main.tf", new_text=FILE + "\n",
                             commit_message="m", title="[FinOps] T", body="b", draft=False, labels=[])
    assert st["mr_payloads"][0]["title"] == "[FinOps] T"


def test_offset_patch_reaches_the_commit_untouched(gitlab):
    gl, st = gitlab
    idx = IacIndex.build({"infra/main.tf": FILE})
    patch = build_patch(action=ACTION_RESIZE, block=idx.block_by_address("aws_instance.web"), index=idx,
                        params={"current_instance_type": "m5.2xlarge", "target_instance_type": "m5.xlarge"})
    gl.create_change_request(repo=REPO, base_branch="main", branch="finops/p", path="infra/main.tf", new_text=patch.new_text,
                             commit_message="m", title="t", body="b", draft=False, labels=[])
    committed = st["branches"]["finops/p"]
    assert committed == patch.new_text
    changed = [ln for ln in committed.splitlines(keepends=True) if ln not in FILE.splitlines(keepends=True)]
    assert changed == ['  instance_type = "m5.xlarge"\n']                       # una sola línea distinta: el resto, byte a byte


def test_errors_never_leak_token_and_report_status(gitlab):
    gl, _ = gitlab
    with pytest.raises(GitProviderError) as err:
        gl.get_file(REPO, "nope.tf", "main")
    assert "glpat-secret" not in str(err.value) and err.value.status == 404
    with pytest.raises(GitProviderError, match="UTF-8"):
        gl.get_file(REPO, "binary.tf", "main")
    with pytest.raises(GitProviderError):
        GitLabProvider("")


def test_network_failure_is_a_provider_error():
    gl = GitLabProvider("t", api_url="http://127.0.0.1:1/api/v4", timeout=0.2)
    with pytest.raises(GitProviderError, match="error de red"):
        gl.get_file(REPO, "a.tf", "main")


# --------------------------------------------------------------------------- fábrica, esquema y webhook
def test_factory_builds_gitlab_provider_with_settings_token():
    from cloudcost.config import Settings
    from cloudcost.secrets import SecretResolver
    from cloudcost.services.git_factory import get_git_provider

    settings = Settings(gitlab_token="glpat-x", gitlab_api_url="https://gitlab.example.com/api/v4")
    provider = get_git_provider({"provider": "gitlab", "token_ref": None}, settings, SecretResolver())
    assert isinstance(provider, GitLabProvider) and provider.api_url == "https://gitlab.example.com/api/v4"
    with pytest.raises(GitProviderError):
        get_git_provider({"provider": "gitlab", "token_ref": None}, Settings(), SecretResolver())      # sin token


def test_repository_schema_allows_gitlab_subgroups_only_for_gitlab():
    from cloudcost.schemas import RepositoryIn
    from pydantic import ValidationError

    assert RepositoryIn(provider="gitlab", full_name="grupo/sub/proyecto").full_name == "grupo/sub/proyecto"
    assert RepositoryIn(provider="github", full_name="org/repo").full_name == "org/repo"
    for bad in (dict(provider="github", full_name="org/sub/repo"), dict(provider="gitlab", full_name="grupo/../x"),
                dict(provider="gitlab", full_name="solo"), dict(provider="gitlab", full_name="a/b c")):
        with pytest.raises(ValidationError):
            RepositoryIn(**bad)


def test_gitlab_webhook_token_and_event_normalization():
    from cloudcost.services.webhook_events import normalize_gitlab_mr_event, verify_gitlab_token

    assert verify_gitlab_token("s3cret", "s3cret") and not verify_gitlab_token("s3cret", "otro")
    assert not verify_gitlab_token("s3cret", None) and not verify_gitlab_token("s3cret", "")
    base = {"object_kind": "merge_request", "user": {"username": "ana"}, "project": {"path_with_namespace": "grupo/sub/proyecto"}}
    merged = normalize_gitlab_mr_event({**base, "object_attributes": {"iid": 12, "action": "merge", "state": "merged"}})
    assert merged == {"action": "closed", "pull_request": {"number": 12, "merged": True, "merged_by": {"login": "ana"}},
                      "repository": {"full_name": "grupo/sub/proyecto"}}
    closed = normalize_gitlab_mr_event({**base, "object_attributes": {"iid": 12, "action": "close", "state": "closed"}})
    assert closed["pull_request"]["merged"] is False
    for ignored in ({"iid": 12, "action": "open", "state": "opened"}, {"iid": 12, "action": "update", "state": "opened"},
                    {"iid": 12, "action": "approved", "state": "opened"}):
        assert normalize_gitlab_mr_event({**base, "object_attributes": ignored}) is None
    assert normalize_gitlab_mr_event({"object_kind": "push"}) is None

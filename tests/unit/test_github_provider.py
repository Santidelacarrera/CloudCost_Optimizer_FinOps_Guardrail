"""Prueba GitHubProvider contra un servidor HTTP local que simula la API de GitHub (sin red externa)."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from cloudcost.git.base import GitProviderError
from cloudcost.git.github import GitHubProvider
from cloudcost.git.local import LocalDemoProvider
from cloudcost.git.pr_body import branch_name, pr_body, pr_title

FILE = 'resource "aws_instance" "web" {\n  instance_type = "m5.2xlarge"\n}\n'


class FakeGitHub(BaseHTTPRequestHandler):
    state = {"refs": {"main": "abc123"}, "branches": {}, "prs": [], "labels": [], "calls": [], "auth": set()}

    def log_message(self, *a):  # silencio
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        s = self.state
        s["calls"].append(("GET", self.path))
        s["auth"].add(self.headers.get("Authorization"))
        if self.path.startswith("/repos/o/r/git/ref/heads/main"):
            return self._send(200, {"object": {"sha": s["refs"]["main"]}})
        if self.path.startswith("/repos/o/r/git/trees/main"):
            return self._send(200, {"truncated": False, "tree": [
                {"path": "infra/main.tf", "type": "blob", "size": 50}, {"path": "infra/readme.md", "type": "blob", "size": 5},
                {"path": "other/x.tf", "type": "blob", "size": 5}]})
        if self.path.startswith("/repos/o/r/contents/infra/main.tf"):
            ref = unquote(self.path.split("ref=")[1])
            text = s["branches"].get(ref, FILE)
            if "raw" in (self.headers.get("Accept") or ""):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._send(200, {"sha": "blobsha", "content": base64.b64encode(text.encode()).decode()})
        if self.path.startswith("/repos/o/r/pulls"):
            return self._send(200, list(s["prs"]))
        self._send(404, {"message": "not found"})

    def do_POST(self):
        s, body = self.state, self._body()
        s["calls"].append(("POST", self.path))
        if self.path == "/repos/o/r/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in s["branches"]:
                return self._send(422, {"message": "Reference already exists"})
            s["branches"][branch] = FILE
            return self._send(201, {})
        if self.path == "/repos/o/r/pulls":
            pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "draft": body["draft"], "title": body["title"]}
            s["prs"].append(pr)
            return self._send(201, pr)
        if self.path.endswith("/labels"):
            s["labels"].extend(body["labels"])
            return self._send(200, [])
        self._send(404, {"message": "nf"})

    def do_PUT(self):
        s, body = self.state, self._body()
        s["calls"].append(("PUT", self.path))
        s["branches"][body["branch"]] = base64.b64decode(body["content"]).decode()
        self._send(200, {})


def _server():
    FakeGitHub.state = {"refs": {"main": "abc123"}, "branches": {}, "prs": [], "labels": [], "calls": [], "auth": set()}
    srv = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_list_files_filters_by_path_and_extension():
    srv, url = _server()
    try:
        gh = GitHubProvider("tok", api_url=url)
        files = gh.list_files("o/r", "main", ["infra"])
        assert list(files) == ["infra/main.tf"] and files["infra/main.tf"] == FILE      # excluye readme.md y other/
    finally:
        srv.shutdown()


def test_create_change_request_is_idempotent_and_never_merges():
    srv, url = _server()
    try:
        gh = GitHubProvider("tok", api_url=url)
        kw = dict(repo="o/r", base_branch="main", branch="finops/abc-x", path="infra/main.tf",
                  new_text=FILE.replace("m5.2xlarge", "m5.xlarge"), commit_message="m", title="t", body="b",
                  draft=True, labels=["finops"])
        first = gh.create_change_request(**kw)
        second = gh.create_change_request(**kw)             # reintento: no duplica
        assert first.number == second.number == 7 and first.draft
        st = FakeGitHub.state
        assert len(st["prs"]) == 1
        assert sum(1 for m, p in st["calls"] if m == "PUT") == 1        # el commit no se repite
        assert "m5.xlarge" in st["branches"]["finops/abc-x"]
        assert not any("merge" in p for _, p in st["calls"])
        assert st["auth"] == {"Bearer tok"}
    finally:
        srv.shutdown()


def test_errors_do_not_leak_the_token():
    srv, url = _server()
    try:
        gh = GitHubProvider("super-secret-token", api_url=url)
        try:
            gh.get_file("o/r", "nope.tf", "main")
            raise AssertionError
        except GitProviderError as e:
            assert "super-secret-token" not in str(e) and e.status == 404
    finally:
        srv.shutdown()
    try:
        GitHubProvider("")
        raise AssertionError
    except GitProviderError:
        pass


def test_local_demo_provider_never_touches_originals():
    import pathlib
    import tempfile
    base = pathlib.Path(tempfile.mkdtemp())
    (base / "iac").mkdir()
    (base / "iac/main.tf").write_text(FILE)
    prov = LocalDemoProvider(str(base / "iac"), str(base / "prs"))
    cr = prov.create_change_request(repo="x", base_branch="main", branch="finops/a-b", path="main.tf",
                                    new_text="changed", commit_message="m", title="T", body="B", draft=False, labels=["finops"])
    assert (base / "iac/main.tf").read_text() == FILE
    assert (base / "prs/finops_a-b/main.tf").read_text() == "changed" and cr.url.startswith("file://")


def test_pr_body_contains_required_sections():
    from datetime import datetime, timezone
    rec = {"id": "12345678-aaaa", "rule_id": "ec2_downsize", "title": "Reducir web", "summary": "CPU baja",
           "estimated_monthly_savings": 390, "current_monthly_cost": 800, "projected_monthly_cost": 410, "risk": "LOW",
           "confidence": 0.94, "priority": "P1", "approvals_required": 2, "destructive": True, "automation_blocked": True,
           "policy": {"reinforced": True}, "evidence": {"cpu_avg": 12.4, "nested": {"x": 1}}}
    body = pr_body(rec, patch_summary="aws_instance.web: a → b", validations=[{"check": "hcl_syntax", "passed": True, "detail": "ok"}],
                   approvals=[{"approver_role": "SRE", "email": "a@b.c", "user_id": "u", "reason": "ok", "created_at": datetime(2026, 10, 5, tzinfo=timezone.utc)}])
    for needle in ("USD 390.00/mes", "USD 4,680.00/año", "LOW", "94%", "borrador", "destructivo", "cpu_avg: 12.4", "hcl_syntax", "SRE"):
        assert needle in body, needle
    assert "nested" not in body
    assert branch_name(rec) == "finops/12345678-ec2-downsize" and "[FinOps]" in pr_title(rec)

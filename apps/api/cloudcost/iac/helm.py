"""Parcheo de `values.yaml` de Helm para bajar requests (y límites iguales a ellos) de un contenedor.

Edita el TEXTO por posiciones (PyYAML conserva el índice de cada escalar), así que comentarios, orden, sangría y estilo de comillas
quedan intactos. Solo toca `resources.requests.{cpu,memory}` y, cuando el límite valía lo mismo que el request, el límite.

Reglas de seguridad:
  * El bloque se identifica porque sus requests actuales COINCIDEN numéricamente con los del clúster; si no coinciden (otra
    plantilla, `--set`, deriva) o hay varios candidatos, no se parchea.
  * Nunca se sube un valor: el nuevo debe ser menor que el actual.
  * Alias/anclas (`*default`) y llaves de mezcla (`<<`) se rechazan: un cambio afectaría a otros bloques.
  * Tras editar, el archivo se vuelve a leer y solo pueden haber cambiado exactamente las rutas esperadas.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from ..domain.k8s import QuantityError, parse_cpu, parse_memory
from ..domain.models import NormalizedResource

VALUES_FILE = re.compile(r"(^|/)(values[^/]*|Chart)\.ya?ml$")
_CHART_VERSION = re.compile(r"^(?P<name>.+?)-v?\d+\.\d+\.\d+.*$")
MAX_DEPTH = 40


class HelmError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class HelmTarget:
    """Bloque `resources:` de un values.yaml (equivale al TfBlock de Terraform)."""
    path: str
    key_path: str                                  # p. ej. `api.resources` o `containers[0].resources`
    scalars: dict[str, ScalarNode] = field(repr=False, default_factory=dict)   # "requests.cpu" -> nodo
    name_hint: str | None = None                   # `name:` hermano, si el bloque está dentro de una lista de contenedores
    shared: bool = False                           # algún escalar se alcanza por alias: no es seguro editarlo

    @property
    def address(self) -> str:
        return f"{self.path}#{self.key_path}"

    def value(self, which: str) -> str | None:
        node = self.scalars.get(which)
        return node.value if node is not None else None


def _walk(node: Node, path: str, out: list[tuple[str, MappingNode, str | None]], depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise HelmError("too_deep", "values.yaml demasiado anidado")
    if isinstance(node, MappingNode):
        for key, value in node.value:
            if not isinstance(key, ScalarNode):
                continue
            sub = f"{path}.{key.value}" if path else key.value
            if key.value == "resources" and isinstance(value, MappingNode) and _child(value, "requests") is not None:
                name = _child(node, "name")
                out.append((sub, value, name.value if isinstance(name, ScalarNode) else None))
            else:
                _walk(value, sub, out, depth + 1)
    elif isinstance(node, SequenceNode):
        for i, item in enumerate(node.value):
            _walk(item, f"{path}[{i}]", out, depth + 1)


def _child(node: MappingNode, key: str) -> Node | None:
    for k, v in node.value:
        if isinstance(k, ScalarNode) and k.value == key:
            return v
    return None


MAX_VISITS = 200_000                           # tope contra «bombas» de alias (billion laughs)


def _count_nodes(node: Node, counter: Counter, depth: int = 0) -> None:
    counter[id(node)] += 1
    counter["__visits__"] += 1
    if depth > MAX_DEPTH or counter["__visits__"] > MAX_VISITS:
        raise HelmError("too_big", "values.yaml demasiado grande o con demasiados alias")
    if isinstance(node, MappingNode):
        for k, v in node.value:
            _count_nodes(k, counter, depth + 1)
            _count_nodes(v, counter, depth + 1)
    elif isinstance(node, SequenceNode):
        for item in node.value:
            _count_nodes(item, counter, depth + 1)


def parse_values(text: str, path: str) -> list[HelmTarget]:
    try:
        docs = [d for d in yaml.compose_all(text) if d is not None]
    except yaml.YAMLError as exc:
        raise HelmError("yaml_syntax", f"{path}: {str(exc).splitlines()[0]}") from exc
    targets: list[HelmTarget] = []
    for doc in docs:
        counter: Counter = Counter()
        _count_nodes(doc, counter)
        found: list[tuple[str, MappingNode, str | None]] = []
        _walk(doc, "", found)
        for key_path, block, hint in found:
            scalars: dict[str, ScalarNode] = {}
            merge = any(isinstance(k, ScalarNode) and k.tag == "tag:yaml.org,2002:merge" for k, _ in block.value)
            for section in ("requests", "limits"):
                sec = _child(block, section)
                if isinstance(sec, MappingNode):
                    merge = merge or any(isinstance(k, ScalarNode) and k.tag == "tag:yaml.org,2002:merge" for k, _ in sec.value)
                    for res_name in ("cpu", "memory"):
                        node = _child(sec, res_name)
                        if isinstance(node, ScalarNode):
                            scalars[f"{section}.{res_name}"] = node
            shared = bool(merge or counter[id(block)] > 1 or any(counter[id(n)] > 1 for n in scalars.values()))
            targets.append(HelmTarget(path, key_path, scalars, hint, shared))
    return targets


def _qty(value: str | None, kind: str) -> float | int | None:
    if value is None:
        return None
    try:
        return parse_cpu(value) if kind == "cpu" else parse_memory(value)
    except QuantityError:
        return None


def _chart_name(label: str | None) -> str | None:
    if not label:
        return None
    m = _CHART_VERSION.match(label)
    return m.group("name") if m else label


@dataclass
class HelmIndex:
    files: dict[str, str]
    targets: list[HelmTarget] = field(default_factory=list)
    charts: dict[str, str] = field(default_factory=dict)         # nombre del chart -> carpeta de su Chart.yaml
    parse_errors: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def build(cls, files: dict[str, str]) -> "HelmIndex":
        idx = cls(files={p: t for p, t in files.items() if VALUES_FILE.search(p)})
        for path, text in sorted(idx.files.items()):
            if path.rsplit("/", 1)[-1].lower().startswith("chart."):
                try:
                    meta = yaml.safe_load(text) or {}
                    if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                        idx.charts[meta["name"]] = path.rsplit("/", 1)[0] if "/" in path else ""
                except yaml.YAMLError:
                    pass
                continue
            try:
                idx.targets += parse_values(text, path)
            except HelmError as exc:
                idx.parse_errors[path] = exc.message
        return idx

    def by_address(self, address: str) -> HelmTarget | None:
        return next((t for t in self.targets if t.address == address), None)

    def _related(self, res: NormalizedResource) -> tuple[str | None, str | None]:
        helm = res.attributes.get("helm") or {}
        return _chart_name(helm.get("chart")) or helm.get("name"), helm.get("release")

    def match(self, res: NormalizedResource) -> HelmTarget | None:
        """Bloque cuyos requests actuales coinciden con los del clúster; ver reglas de desambiguación abajo."""
        a = res.attributes
        cpu, mem = a.get("cpu_request"), a.get("mem_request")
        if not cpu or not mem:
            return self._no_match(res, "sin_requests", "El contenedor no declara requests: no hay nada que ajustar.")
        exact = [t for t in self.targets
                 if _qty(t.value("requests.cpu"), "cpu") == cpu and _qty(t.value("requests.memory"), "memory") == mem]
        if not exact:
            return self._no_match(res, "helm_no_match", "Ningún values.yaml declara estos requests (otra plantilla, --set o deriva entre Git y el clúster).")
        usable = [t for t in exact if not t.shared]
        if not usable:
            return self._no_match(res, "helm_alias", "El bloque usa alias/anclas YAML: un cambio afectaría a otros bloques.")
        chart, release = self._related(res)
        candidates = usable
        for narrow in (lambda t: self._in_chart(t, chart), lambda t: bool(release) and release.lower() in t.path.lower(),
                       lambda t: self._names_container(t, a), ):
            subset = [t for t in candidates if narrow(t)]
            if subset and len(candidates) > 1:
                candidates = subset
        if len(candidates) == 1:
            return candidates[0]
        return self._no_match(res, "helm_ambiguous", "Varios values.yaml coinciden: " + ", ".join(sorted(t.address for t in candidates)[:6]))

    def _in_chart(self, t: HelmTarget, chart: str | None) -> bool:
        folder = self.charts.get(chart or "")
        return folder is not None and (folder == "" or t.path.startswith(folder + "/"))

    @staticmethod
    def _names_container(t: HelmTarget, a: dict[str, Any]) -> bool:
        names = {str(a.get("container", "")).lower(), str(a.get("workload", "")).lower()} - {""}
        hay = f"{t.key_path} {t.name_hint or ''}".lower()
        return any(n in hay for n in names)

    def _no_match(self, res: NormalizedResource, code: str, message: str) -> None:
        self.reasons[res.resource_id] = {"code": code, "message": message}
        return None


# --------------------------------------------------------------------------------------------------- parche
def _diff_leaves(a: Any, b: Any, path: str = "") -> set[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: set[str] = set()
        for k in a.keys() | b.keys():
            out |= _diff_leaves(a.get(k, _MISSING), b.get(k, _MISSING), f"{path}.{k}" if path else str(k))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = set()
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out |= _diff_leaves(x, y, f"{path}[{i}]")
        return out
    return set() if a == b else {path}


_MISSING = object()


def _render(node: ScalarNode, new: str) -> str:
    quote = node.style if node.style in ("'", '"') else ""
    return f"{quote}{new}{quote}"


def build_values_patch(text: str, target: HelmTarget, params: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Devuelve (texto nuevo, resumen, validaciones). Lanza HelmError si no es seguro."""
    wanted, limits, current = params.get("target") or {}, params.get("limits") or {}, params.get("current") or {}
    if not wanted or not set(wanted) <= {"cpu", "memory"}:
        raise HelmError("invalid_params", "El cambio debe indicar cpu y/o memory.")
    if target.shared:
        raise HelmError("helm_alias", "El bloque usa alias/anclas YAML; edítalo a mano.")
    # el archivo que se parchea es el que se leyó ahora: se vuelve a localizar el bloque para tener posiciones frescas
    fresh = next((t for t in parse_values(text, target.path) if t.key_path == target.key_path), None)
    if fresh is None:
        raise HelmError("drift", f"{target.path}: ya no existe `{target.key_path}`.")
    edits: list[tuple[int, int, str]] = []
    expected: set[str] = set()
    changes: list[str] = []
    for res_name, new in wanted.items():
        kind = "cpu" if res_name == "cpu" else "memory"
        new_q, cur_q = _qty(new, kind), _qty(current.get(res_name), kind)
        req_node = fresh.scalars.get(f"requests.{res_name}")
        if new_q is None or cur_q is None or req_node is None:
            raise HelmError("invalid_params", f"Valor inválido o ausente para {res_name}.")
        if _qty(req_node.value, kind) != cur_q:
            raise HelmError("drift", f"Deriva: {target.path} declara {res_name}={req_node.value} pero la recomendación se calculó para {current.get(res_name)}.")
        if not new_q < cur_q:
            raise HelmError("not_a_reduction", f"{res_name}: {new} no es menor que {current.get(res_name)}; solo se permiten reducciones.")
        edits.append((req_node.start_mark.index, req_node.end_mark.index, _render(req_node, new)))
        expected.add(f"{fresh.key_path}.requests.{res_name}")
        changes.append(f"requests.{res_name} {req_node.value} → {new}")
        if res_name in limits:
            lim_node = fresh.scalars.get(f"limits.{res_name}")
            if lim_node is None or _qty(lim_node.value, kind) != cur_q:
                raise HelmError("limits_mismatch", f"El límite de {res_name} en {target.path} no coincide con el del clúster.")
            if limits[res_name] != new:
                raise HelmError("invalid_params", "El límite nuevo debe ser igual al request nuevo.")
            edits.append((lim_node.start_mark.index, lim_node.end_mark.index, _render(lim_node, new)))
            expected.add(f"{fresh.key_path}.limits.{res_name}")
            changes.append(f"limits.{res_name} {lim_node.value} → {new}")
    new_text = text
    for start, end, repl in sorted(edits, reverse=True):
        new_text = new_text[:start] + repl + new_text[end:]
    try:
        before = list(yaml.safe_load_all(text))
        after = list(yaml.safe_load_all(new_text))
    except yaml.YAMLError as exc:
        raise HelmError("invalid_result", f"El archivo resultante no es YAML válido: {exc}") from exc
    changed = set().union(*(_diff_leaves(x, y) for x, y in zip(before, after, strict=True))) if len(before) == len(after) else {"*"}
    if changed != expected:
        raise HelmError("unexpected_changes", f"El parche cambiaría más de lo previsto: {sorted(changed ^ expected)[:5]}")
    validations = [
        {"check": "yaml_syntax", "passed": True, "detail": "PyYAML relee el archivo resultante"},
        {"check": "only_expected_keys_changed", "passed": True, "detail": ", ".join(sorted(expected))},
        {"check": "reductions_only", "passed": True, "detail": "ningún valor sube"},
        {"check": "matches_cluster", "passed": True, "detail": "los valores actuales coinciden con los observados en el clúster"},
    ]
    return new_text, f"{target.address}: " + "; ".join(changes), validations

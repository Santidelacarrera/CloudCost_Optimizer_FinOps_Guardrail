"""Análisis ligero de Terraform/HCL: localiza bloques `resource`, extrae atributos literales y los
relaciona con los recursos reales descubiertos en la nube.

No es un parser HCL completo: un analizador léxico (cadenas, interpolaciones, comentarios, heredocs) localiza
los bloques y trabaja con offsets de texto, de modo que se puede parchear sin reformatear el archivo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.models import NormalizedResource, tf_types_for

_RESOURCE_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
_HEREDOC_RE = re.compile(r'<<-?\s*([A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?\n')
_NAME_TAG_RE = re.compile(r'\bName\s*=\s*"([^"\n]+)"')
_META_RE = re.compile(r'^[ \t]*(count|for_each)[ \t]*=', re.MULTILINE)

_NAME_ATTR = {"azure", "gcp"}        # Azure y GCP nombran el recurso con `name = "..."`; AWS con la etiqueta Name


class HclSyntaxError(ValueError):
    pass


def _skip_heredoc(text: str, i: int) -> int | None:
    """Si en `i` empieza un heredoc, devuelve el índice posterior a su marcador final."""
    m = _HEREDOC_RE.match(text, i)
    if not m:
        return None
    marker, n, j = m.group(1), len(text), m.end()
    while j < n:
        nl = text.find("\n", j)
        line = text[j: n if nl == -1 else nl]
        j = n if nl == -1 else nl + 1
        if line.strip() == marker:
            break
    return j


def _walk_block(text: str, open_idx: int) -> tuple[int, bytearray]:
    """Recorre desde la llave de apertura hasta su cierre.

    Devuelve (índice exclusivo del fin, máscara). mask[i]==1 si el carácter i pertenece al nivel superior del
    bloque (fuera de bloques anidados, comentarios, heredocs e interpolaciones).
    """
    n = len(text)
    mask = bytearray(n)
    stack: list[str] = ["brace"]          # 'brace' | 'str' | 'interp'
    i = open_idx + 1
    while i < n:
        top, ch = stack[-1], text[i]
        if top == "str":
            top_level_string = stack == ["brace", "str"]
            if top_level_string:
                mask[i] = 1
            if ch == "\\":
                if top_level_string and i + 1 < n:
                    mask[i + 1] = 1
                i += 2
                continue
            if ch == '"':
                stack.pop()
            elif text.startswith(("${", "%{"), i):
                stack.append("interp")
                i += 2
                continue
            i += 1
            continue

        # ---- código (dentro de brace o interp)
        if ch == "#" or text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise HclSyntaxError("Comentario /* sin cerrar")
            i = end + 2
            continue
        if text.startswith("<<", i):
            after = _skip_heredoc(text, i)
            if after is not None:
                i = after
                continue
        if ch == '"':
            if stack == ["brace"]:
                mask[i] = 1
            stack.append("str")
            i += 1
            continue
        if ch == "{":
            if stack == ["brace"]:
                mask[i] = 1
            stack.append("brace")
            i += 1
            continue
        if ch == "}":
            stack.pop()
            if not stack:
                return i + 1, mask
            if stack == ["brace"]:
                mask[i] = 1
            i += 1
            continue
        if stack == ["brace"]:
            mask[i] = 1
        i += 1
    raise HclSyntaxError("Bloque sin cerrar")


@dataclass
class TfBlock:
    type: str
    name: str
    path: str
    start: int            # offset del archivo donde empieza `resource "..." "..." {`
    end: int              # offset exclusivo tras la llave de cierre
    text: str
    line: int             # línea (1-based) de inicio
    mask: bytearray = field(repr=False, default_factory=bytearray)

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"

    def top_level_text(self) -> str:
        """Texto del bloque con lo anidado/comentado reemplazado por espacios (mismos offsets)."""
        return "".join(c if (self.mask[self.start + k] or c == "\n") else " " for k, c in enumerate(self.text))

    def attr_literal(self, attr: str) -> tuple[str, int, int] | None:
        """(valor, inicio, fin) de `attr = "valor"` a nivel superior; offsets relativos al archivo.

        Devuelve None si el valor no es un literal simple (referencia, interpolación, expresión).
        """
        masked = self.top_level_text()
        m = re.search(rf'^[ \t]*{re.escape(attr)}[ \t]*=[ \t]*"([^"\n$%]*)"[ \t]*$', masked, re.MULTILINE)
        if not m:
            return None
        return m.group(1), self.start + m.start(1), self.start + m.end(1)

    def has_attr(self, attr: str) -> bool:
        return re.search(rf'^[ \t]*{re.escape(attr)}[ \t]*=', self.top_level_text(), re.MULTILINE) is not None

    @property
    def name_tag(self) -> str | None:
        m = _NAME_TAG_RE.search(self.text)
        return m.group(1) if m else None

    @property
    def name_literal(self) -> str | None:
        """Valor literal de `name = "..."` a nivel superior (Azure y GCP)."""
        lit = self.attr_literal("name")
        return lit[0] if lit else None

    @property
    def multi_instance(self) -> bool:
        """True si usa count/for_each (un cambio afectaría a varias instancias)."""
        return _META_RE.search(self.top_level_text()) is not None


def parse_resources(text: str, path: str = "main.tf") -> list[TfBlock]:
    blocks: list[TfBlock] = []
    n, i = len(text), 0
    while i < n:
        ch = text[i]
        if ch == "#" or text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise HclSyntaxError("Comentario /* sin cerrar")
            i = end + 2
            continue
        if ch == '"':                              # cadena suelta de nivel superior: saltarla
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "r" and (i == 0 or text[i - 1] in " \t\r\n"):
            m = _RESOURCE_RE.match(text, i)
            if m:
                end, mask = _walk_block(text, m.end() - 1)
                blocks.append(TfBlock(m.group(1), m.group(2), path, i, end, text[i:end],
                                      text.count("\n", 0, i) + 1, mask))
                i = end
                continue
        if ch == "{":                              # otro bloque (locals { ... }): saltarlo entero
            i, _ = _walk_block(text, i)
            continue
        i += 1
    return blocks


if TYPE_CHECKING:
    from .helm import HelmIndex, HelmTarget


@dataclass
class IacIndex:
    files: dict[str, str]
    blocks: list[TfBlock] = field(default_factory=list)
    state_ids: dict[str, str] = field(default_factory=dict)      # resource_id -> address (terraform.tfstate)
    parse_errors: dict[str, str] = field(default_factory=dict)
    helm: "HelmIndex | None" = None                              # values.yaml de Helm (workloads de Kubernetes)

    @classmethod
    def build(cls, files: dict[str, str], state_ids: dict[str, str] | None = None) -> "IacIndex":
        idx = cls(files=dict(files), state_ids=dict(state_ids or {}))
        for path, text in sorted(files.items()):
            if not path.endswith(".tf"):
                continue
            try:
                idx.blocks.extend(parse_resources(text, path))
            except HclSyntaxError as exc:
                idx.parse_errors[path] = str(exc)
        from .helm import HelmIndex

        idx.helm = HelmIndex.build(files)
        idx.parse_errors.update(idx.helm.parse_errors)
        return idx

    def block_by_address(self, address: str) -> "TfBlock | HelmTarget | None":
        if "#" in address:                                       # `ruta/values.yaml#ruta.yaml.del.bloque`
            return self.helm.by_address(address) if self.helm else None
        return next((b for b in self.blocks if b.address == address), None)

    def match(self, res: NormalizedResource) -> "TfBlock | HelmTarget | None":
        """Relaciona un recurso real con su bloque IaC: 1) terraform.tfstate, 2) etiqueta Name. Ambigüedad => None.

        Los workloads de Kubernetes se relacionan con el bloque `resources:` de un values.yaml cuyos requests coinciden."""
        if res.service == "k8s_workload":
            return self.helm.match(res) if self.helm else None
        tf_types = tf_types_for(res)
        if not tf_types:
            return None
        address = self.state_ids.get(res.resource_id)
        if address:
            return self.block_by_address(address)
        wanted = (res.tags or {}).get("Name") or res.name
        if not wanted:
            return None
        candidates = [b for b in self.blocks if b.type in tf_types
                      and (b.name_tag == wanted or (res.provider in _NAME_ATTR and b.name_literal == wanted))]
        return candidates[0] if len(candidates) == 1 else None

    def why_no_match(self, res: NormalizedResource) -> dict[str, str]:
        """Motivo por el que `match` no encontró nada (para mostrarlo en la recomendación)."""
        if res.service == "k8s_workload" and self.helm and res.resource_id in self.helm.reasons:
            return self.helm.reasons[res.resource_id]
        return {"code": "iac_not_found", "message": "No se encontró el recurso en el IaC del repositorio"}

    def references_to(self, block: TfBlock) -> list[str]:
        """Archivos que referencian `type.name` desde fuera del propio bloque."""
        pattern = re.compile(rf'(?<![\w.]){re.escape(block.type)}\.{re.escape(block.name)}\b')
        hits: list[str] = []
        for path, text in self.files.items():
            if not path.endswith(".tf"):
                continue
            hay = text[: block.start] + text[block.end:] if path == block.path else text
            if pattern.search(hay):
                hits.append(path)
        return hits


def state_ids_from_tfstate(state: dict) -> dict[str, str]:
    """Extrae {id: 'tipo.nombre'} de un terraform.tfstate (v4), ignorando recursos con count/for_each."""
    out: dict[str, str] = {}
    for r in state.get("resources", []):
        if r.get("mode") != "managed":
            continue
        instances = r.get("instances", [])
        if len(instances) != 1 or "index_key" in instances[0]:
            continue
        rid = (instances[0].get("attributes") or {}).get("id")
        if rid:
            out[str(rid)] = f"{r['type']}.{r['name']}"
    return out

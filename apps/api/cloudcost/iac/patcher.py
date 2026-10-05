"""Generación de parches IaC (Terraform) a partir de una recomendación aprobada, con validaciones previas."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..domain.rules import ACTION_DELETE_SNAPSHOT, ACTION_DELETE_VOLUME, ACTION_REMOVE, ACTION_RESIZE
from .terraform import HclSyntaxError, IacIndex, TfBlock, parse_resources


class PatchError(Exception):
    """El cambio no se puede aplicar de forma segura; `code` permite a la UI explicar el motivo."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class PatchResult:
    path: str
    new_text: str
    diff: str
    summary: str
    validations: list[dict] = field(default_factory=list)


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))


def _check_resulting_hcl(old: str, new: str, path: str) -> dict:
    """Verifica que el archivo resultante siga siendo HCL coherente (balance de llaves + python-hcl2 si está disponible)."""
    try:
        parse_resources(new, path)
    except HclSyntaxError as exc:
        raise PatchError("invalid_result", f"El archivo resultante no es HCL válido: {exc}") from exc
    detail = "estructura de bloques válida"
    try:
        import hcl2  # type: ignore
    except ImportError:
        return {"check": "hcl_syntax", "passed": True, "detail": detail}
    try:
        hcl2.loads(old)
    except Exception:                       # el original ya no lo parsea hcl2: no atribuimos el fallo al parche
        return {"check": "hcl_syntax", "passed": True, "detail": detail + " (hcl2 omitido: el original no parsea)"}
    try:
        hcl2.loads(new)
    except Exception as exc:
        raise PatchError("invalid_result", f"python-hcl2 rechaza el archivo resultante: {exc}") from exc
    return {"check": "hcl_syntax", "passed": True, "detail": detail + " + python-hcl2"}


def _resize(text: str, block: TfBlock, params: dict) -> tuple[str, str]:
    if block.multi_instance:
        raise PatchError("multi_instance", f"{block.address} usa count/for_each: el cambio afectaría a varias instancias; aplícalo manualmente.")
    lit = block.attr_literal("instance_type")
    if not lit:
        raise PatchError("not_literal", f"instance_type de {block.address} no es un literal (variable o expresión); no se puede parchear automáticamente.")
    current, a, b = lit
    expected = params.get("current_instance_type")
    if expected and current != expected:
        raise PatchError("drift", f"Deriva detectada: el IaC declara {current} pero la recomendación se calculó para {expected}.")
    target = params.get("target_instance_type")
    if not target or not re.fullmatch(r"[a-z0-9]+\.[a-z0-9]+", target):
        raise PatchError("invalid_params", "Tipo de instancia destino inválido.")
    return text[:a] + target + text[b:], f"{block.address}: instance_type {current} → {target}"


def _remove(text: str, block: TfBlock, index: IacIndex) -> tuple[str, str]:
    if block.multi_instance:
        raise PatchError("multi_instance", f"{block.address} usa count/for_each: no se elimina automáticamente.")
    refs = index.references_to(block)
    if refs:
        raise PatchError("referenced", f"{block.address} es referenciado desde {', '.join(sorted(refs))}; elimina primero esas referencias.")
    start, end = block.start, block.end
    # eliminar también el salto de línea final y una línea en blanco sobrante
    while end < len(text) and text[end] in " \t":
        end += 1
    if end < len(text) and text[end] == "\n":
        end += 1
    if end < len(text) and text[end] == "\n" and (start == 0 or text[start - 1] == "\n"):
        end += 1
    return text[:start] + text[end:], f"{block.address}: bloque eliminado"


def build_patch(*, action: str, params: dict, block: TfBlock, index: IacIndex) -> PatchResult:
    text = index.files[block.path]
    if action == ACTION_RESIZE:
        new_text, summary = _resize(text, block, params)
    elif action in (ACTION_REMOVE, ACTION_DELETE_VOLUME, ACTION_DELETE_SNAPSHOT):
        new_text, summary = _remove(text, block, index)
    else:
        raise PatchError("unsupported_action", f"Acción '{action}' no soportada para generar parches.")
    if new_text == text:
        raise PatchError("no_change", "El parche no produce cambios.")
    validations = [_check_resulting_hcl(text, new_text, block.path)]
    return PatchResult(block.path, new_text, unified_diff(block.path, text, new_text), summary, validations)

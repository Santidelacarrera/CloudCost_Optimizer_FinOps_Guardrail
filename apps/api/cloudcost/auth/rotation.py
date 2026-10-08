"""Herramienta de rotación del pepper (ver docs/pepper-rotation.md).

    python -m cloudcost.auth.rotation status                 # cuántos datos dependen de cada pepper
    python -m cloudcost.auth.rotation reencrypt-totp [--dry-run]   # re-cifra los secretos TOTP con el pepper actual
    python -m cloudcost.auth.rotation retire-check ID        # ¿se puede quitar el pepper ID? (código de salida 0 = sí)

Nunca imprime hashes, secretos ni correos: solo contadores e ids de cuenta. Las contraseñas no se pueden re-hashear aquí (no hay texto en
claro): se re-hashean solas cuando cada persona inicia sesión. Los códigos de recuperación tampoco; se regeneran desde la cuenta.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

from . import crypto, passwords
from .pepper import PepperError, PepperRing


def census(rows_pw: list[str], rows_totp: list[str], rows_codes: list[str]) -> dict[str, dict[str, int]]:
    """Cuenta datos por id de pepper. Entradas: hashes de contraseña, blobs TOTP y hashes de códigos de recuperación SIN usar."""
    pw = Counter((passwords.pepper_id_of(h) or "invalid") for h in rows_pw)
    totp = Counter(crypto.secret_pepper_id(b) for b in rows_totp)
    codes = Counter(crypto.recovery_pepper_id(c) for c in rows_codes)
    return {"passwords": dict(pw), "totp": dict(totp), "recovery_codes": dict(codes)}


def blockers(counts: dict[str, dict[str, int]], pepper_id: str) -> dict[str, int]:
    return {kind: counts[kind].get(pepper_id, 0) for kind in ("passwords", "totp", "recovery_codes") if counts.get(kind, {}).get(pepper_id, 0)}


def reencrypt_plan(rows: list[dict[str, Any]], ring: PepperRing) -> tuple[list[dict[str, str]], list[Any], int]:
    """(cambios, cuentas_que_fallan, ya_actuales). Cada cambio: id, old, new (el blob nuevo, nunca el secreto)."""
    changes, failed, current = [], [], 0
    for row in rows:
        blob, account_id = row["mfa_secret_enc"], str(row["id"])
        if crypto.secret_pepper_id(blob) == ring.current_id:
            current += 1
            continue
        try:
            changes.append({"id": account_id, "old": blob, "new": crypto.reencrypt_secret(blob, ring, account_id)})
        except Exception:               # pepper retirado o dato corrupto: se informa la cuenta, no el motivo criptográfico
            failed.append(account_id)
    return changes, failed, current


# ---------------------------------------------------------------- base de datos
def _status(ring: PepperRing) -> dict[str, Any]:
    from ..services.accounts import auth_tx

    with auth_tx() as conn:
        pw = [r["password_hash"] for r in conn.execute("select password_hash from accounts")]
        totp = [r["mfa_secret_enc"] for r in conn.execute("select mfa_secret_enc from accounts where mfa_secret_enc is not null")]
        codes = [r["code_hash"] for r in conn.execute("select code_hash from auth_recovery_codes where used_at is null")]
    return {"current": ring.current_id, "known": list(ring.ids), "accounts": len(pw), **census(pw, totp, codes)}


def _reencrypt(ring: PepperRing, *, dry_run: bool) -> dict[str, Any]:
    from ..services.accounts import auth_tx

    with auth_tx() as conn:
        rows = conn.execute("select id, mfa_secret_enc from accounts where mfa_secret_enc is not null").fetchall()
        changes, failed, current = reencrypt_plan(rows, ring)
        updated = 0
        if not dry_run:
            for ch in changes:       # comparar-y-cambiar: si el secreto cambió entre medias (alta/baja de 2FA) no se pisa
                updated += conn.execute("update accounts set mfa_secret_enc = %s where id = %s and mfa_secret_enc = %s",
                                        (ch["new"], ch["id"], ch["old"])).rowcount
    return {"dry_run": dry_run, "already_current": current, "to_update": len(changes), "updated": updated, "failed_accounts": failed}


def main(argv: list[str] | None = None) -> int:
    from ..config import get_settings

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "status"
    try:
        ring = get_settings().pepper_ring
        if cmd == "status":
            print(json.dumps(_status(ring), indent=2))
            return 0
        if cmd == "reencrypt-totp":
            out = _reencrypt(ring, dry_run="--dry-run" in args)
            print(json.dumps(out, indent=2))
            return 1 if out["failed_accounts"] else 0
        if cmd == "retire-check" and len(args) == 1:
            pending = blockers(_status(ring), args[0])
            print(json.dumps({"pepper": args[0], "still_in_use": pending, "safe_to_retire": not pending}, indent=2))
            return 0 if not pending else 2
    except PepperError as exc:
        print(f"Configuración de pepper inválida: {exc}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())

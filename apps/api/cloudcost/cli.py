"""Operaciones de mantenimiento que no pertenecen a la API pública. Se ejecutan dentro del contenedor de la API:

    docker compose ... exec api python -m cloudcost.cli reset-mfa persona@empresa.cl --reason "Verificada por videollamada, ticket 123"
"""
from __future__ import annotations

import argparse
import sys

from . import db
from .auth import mailer
from .config import get_settings
from .services import accounts


def reset_mfa(args: argparse.Namespace) -> int:
    settings = get_settings()
    outbox: list[mailer.Mail] = []
    db.init_pool()
    try:
        found = accounts.operator_reset_mfa(settings, args.email, args.reason, outbox)
    finally:
        db.close_pool()
    if not found:
        print(f"No existe una cuenta con el correo {args.email}.", file=sys.stderr)
        return 1
    for mail in outbox:
        mailer.send(settings, mail)
    print(f"Verificación en dos pasos quitada para {args.email}; sus sesiones quedaron cerradas y la acción consta en la auditoría.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudcost.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("reset-mfa", help="Quita el 2FA de una cuenta que perdió el teléfono y los códigos de recuperación")
    p.add_argument("email")
    p.add_argument("--reason", required=True, help="Cómo verificaste la identidad (queda en la auditoría)")
    p.set_defaults(func=reset_mfa)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

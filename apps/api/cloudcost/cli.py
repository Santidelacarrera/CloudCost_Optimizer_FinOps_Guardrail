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


def aws_lab(args: argparse.Namespace) -> int:
    """Valida el colector de AWS contra una cuenta de laboratorio (solo lectura) y genera un informe reproducible. No usa la base de datos."""
    from . import lab

    if args.from_snapshot:
        snap = lab.load_snapshot(args.from_snapshot)
    else:
        if not args.account_ref or not args.account_ref.isdigit() or len(args.account_ref) != 12:
            print("--account-ref debe ser el ID de 12 dígitos de la cuenta de laboratorio.", file=sys.stderr)
            return 2
        cfg = lab.LabConfig(account_ref=args.account_ref, regions=[r.strip() for r in args.regions.split(",") if r.strip()], role_arn=args.role_arn,
                            external_id_env=args.external_id_env, months=args.months, ce_request_budget=args.ce_budget,
                            use_cost_explorer=not args.no_cost_explorer, cost_tag_key=args.cost_tag_key, anonymize=args.anonymize,
                            salt=args.salt, scale=args.scale)
        snap = lab.collect_snapshot(cfg)
    paths = lab.write_outputs(snap, args.out_dir)
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    print(f"huella de la instantánea: {lab.snapshot_digest(snap)}")
    return 0 if not snap["meta"]["aborted"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudcost.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("reset-mfa", help="Quita el 2FA de una cuenta que perdió el teléfono y los códigos de recuperación")
    p.add_argument("email")
    p.add_argument("--reason", required=True, help="Cómo verificaste la identidad (queda en la auditoría)")
    p.set_defaults(func=reset_mfa)
    lab_p = sub.add_parser("aws-lab", help="Valida el colector de AWS contra una cuenta de laboratorio (solo lectura) y genera un informe reproducible")
    lab_p.add_argument("--account-ref", help="ID de 12 dígitos de la cuenta de laboratorio")
    lab_p.add_argument("--regions", default="us-east-1", help="Regiones separadas por comas")
    lab_p.add_argument("--role-arn", help="Rol de solo lectura a asumir (si se omite, se usan las credenciales por defecto de boto3)")
    lab_p.add_argument("--external-id-env", help="NOMBRE de la variable CC_SECRET_* que contiene el ExternalId del rol (nunca el valor)")
    lab_p.add_argument("--months", type=int, default=6, help="Meses de historial de coste (1-12)")
    lab_p.add_argument("--ce-budget", type=int, default=40, help="Máximo de solicitudes a Cost Explorer (cada una cuesta USD 0,01)")
    lab_p.add_argument("--no-cost-explorer", action="store_true", help="No llamar a Cost Explorer (solo inventario y métricas)")
    lab_p.add_argument("--cost-tag-key", help="Etiqueta de asignación de costos para el historial por etiqueta")
    lab_p.add_argument("--anonymize", action="store_true", help="Seudonimiza identificadores y descarta valores de etiquetas")
    lab_p.add_argument("--salt", help="Sal de la seudonimización (por defecto, aleatoria y no se guarda)")
    lab_p.add_argument("--scale", type=float, default=1.0, help="Multiplica los importes (con --anonymize) para ocultar el gasto real")
    lab_p.add_argument("--out-dir", default="lab-out")
    lab_p.add_argument("--from-snapshot", help="Regenera el informe a partir de una instantánea (sin acceso a AWS)")
    lab_p.set_defaults(func=aws_lab)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""Operaciones de mantenimiento que no pertenecen a la API pública. Se ejecutan dentro del contenedor de la API:

    docker compose ... exec api python -m cloudcost.cli reset-mfa persona@empresa.cl --reason "Verificada por videollamada, ticket 123"
"""
from __future__ import annotations

import argparse
import sys


def reset_mfa(args: argparse.Namespace) -> int:
    from . import db
    from .auth import mailer
    from .config import get_settings
    from .services import accounts

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


def k8s_lab(args: argparse.Namespace) -> int:
    """Valida el colector de Kubernetes contra un clúster de laboratorio (minikube) y genera un informe reproducible. No usa la base de datos."""
    from . import k8s_lab as lab

    if args.from_snapshot:
        snap = lab.load_snapshot(args.from_snapshot)
    else:
        snap = lab.collect_snapshot(cluster_ref=args.cluster_ref, prometheus_url=args.prometheus_url,
                                    namespaces=[n.strip() for n in args.namespaces.split(",") if n.strip()],
                                    min_observation_days=args.min_observation_days, cpu_hour=args.cpu_hour, mem_gib_hour=args.mem_gib_hour)
    paths = lab.write(snap, args.out_dir)
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    print(f"huella de la instantánea: {lab.snapshot_digest(snap)}")
    failed = [c for c in snap.get("checks", []) if c["result"] != "OK"]
    for c in failed:
        print(f"comprobación fallida: {c['workload']} — {c['expected']} (ocurrió: {c['observed']})", file=sys.stderr)
    return 0 if not snap["meta"]["aborted"] and not failed else 1


def expenses(args: argparse.Namespace) -> int:
    """Analiza archivos de gastos (CSV o Excel .xlsx; exportaciones de AWS/Azure/GCP incluidas) y escribe un informe. No usa la base de datos."""
    import json
    import os

    from .expenses import ExpenseFormatError, analyze_documents
    from .expenses.report import render_markdown
    from .expenses.xlsx import xlsx_to_csv_texts

    docs: list[tuple[str, str]] = []
    try:
        for path in args.files:
            name = os.path.basename(path)
            with open(path, "rb") as fh:
                raw = fh.read()
            if name.lower().endswith((".xlsx", ".xlsm")) or raw.startswith(b"PK\x03\x04"):
                docs.extend(xlsx_to_csv_texts(raw, name))
            else:
                try:
                    docs.append((name, raw.decode("utf-8")))
                except UnicodeDecodeError:
                    docs.append((name, raw.decode("windows-1252")))             # CSV de Excel en Windows
    except (OSError, ExpenseFormatError) as exc:
        print(f"No se pudo leer el archivo: {exc}", file=sys.stderr)
        return 2
    result, err = analyze_documents(docs)
    if err:
        for line in err["errors"]:
            print(line, file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(render_markdown(result))
    with open(os.path.join(args.out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"report: {os.path.join(args.out_dir, 'report.md')}")
    print(f"hallazgos: {result['total_findings']} · nube: {len(result['cloud'])} · estados: {len(result['statements'])}")
    return 0


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
    k_p = sub.add_parser("k8s-lab", help="Valida el colector de Kubernetes contra un clúster de laboratorio (minikube) y genera un informe reproducible")
    k_p.add_argument("--cluster-ref", default="minikube", help="Nombre del clúster en los informes")
    k_p.add_argument("--prometheus-url", default="http://localhost:9090", help="Prometheus del clúster (con kubectl port-forward)")
    k_p.add_argument("--namespaces", default="lab-dev", help="Namespaces a evaluar, separados por comas")
    k_p.add_argument("--min-observation-days", type=int, help="Reduce el umbral de días de datos (solo laboratorio; el producto usa 7)")
    k_p.add_argument("--cpu-hour", type=float, help="USD por núcleo-hora para valorar lo reservado")
    k_p.add_argument("--mem-gib-hour", type=float, help="USD por GiB-hora para valorar lo reservado")
    k_p.add_argument("--out-dir", default="k8s-lab-out")
    k_p.add_argument("--from-snapshot", help="Regenera el informe a partir de una instantánea (sin acceso al clúster)")
    k_p.set_defaults(func=k8s_lab)
    e_p = sub.add_parser("expenses", help="Analiza gastos desde CSV o Excel (.xlsx), incluidas exportaciones de facturación de AWS/Azure/GCP")
    e_p.add_argument("files", nargs="+", help="Uno o varios archivos (varios meses se comparan entre sí)")
    e_p.add_argument("--out-dir", default="gastos-out")
    e_p.set_defaults(func=expenses)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

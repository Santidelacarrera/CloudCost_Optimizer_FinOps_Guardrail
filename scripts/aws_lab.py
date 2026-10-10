#!/usr/bin/env python3
"""Lanzador de `aws-lab` que funciona desde la raíz del repo sin instalar nada (Windows/PowerShell, macOS, Linux):

    python scripts/aws_lab.py --account-ref 123456789012 --regions us-east-1 --out-dir lab-out

Equivale a `cd apps/api && python -m cloudcost.cli aws-lab ...`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "api"))
from cloudcost.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["aws-lab", *sys.argv[1:]]))

#!/usr/bin/env python3
"""Lanzador de `k8s-lab` que funciona desde la raíz del repo sin instalar nada (Windows/PowerShell, macOS, Linux):

    python scripts/k8s_lab.py --namespaces lab-dev --out-dir k8s-lab-out

Equivale a `cd apps/api && python -m cloudcost.cli k8s-lab ...`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "api"))
from cloudcost.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["k8s-lab", *sys.argv[1:]]))

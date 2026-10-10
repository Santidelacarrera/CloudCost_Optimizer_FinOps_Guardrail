#!/usr/bin/env python3
"""Lanzador de `expenses` (análisis de gastos desde CSV/Excel) que funciona desde la raíz del repo sin instalar nada (Windows/PowerShell, macOS, Linux):

    python scripts/expenses_analyze.py gastos.xlsx cur-agosto.csv --out-dir gastos-out

Equivale a `cd apps/api && python -m cloudcost.cli expenses ...`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "api"))
from cloudcost.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["expenses", *sys.argv[1:]]))

#!/usr/bin/env python3
"""Emite un token de desarrollo:  python scripts/dev_token.py --role ADMIN --email ana@example.com  (solo AUTH_MODE=dev)."""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))
from cloudcost.config import get_settings  # noqa: E402
from cloudcost.security import mint_dev_token  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--role", default="ADMIN")
p.add_argument("--email", default="demo@example.com")
a = p.parse_args()
print(mint_dev_token(get_settings(), email=a.email, role=a.role))

"""Señales de calidad del coste usado como referencia de un ahorro (compartidas por colectores y reglas)."""
from __future__ import annotations

# Bloquean tratar el coste como «verificado» (con require_verified_cost no se propone nada): los créditos o reembolsos hacen que la
# serie diaria no represente lo que cuesta el recurso.
BLOCKING_FLAGS = frozenset({"negative_amount"})
# Restan confianza (-0,10 una sola vez) y quedan en la evidencia: el promedio puede no ser representativo.
CONFIDENCE_FLAGS = frozenset({"spike", "trend_break", "partial_window"})
CONFIDENCE_PENALTY = 0.10

# Un coste real muy distinto del de la tabla de precios para el mismo tipo suele deberse a licencias, transferencia, créditos o a
# un pico: no se bloquea, pero se avisa y se resta confianza.
PRICE_TABLE_RATIO_LOW = 0.30
PRICE_TABLE_RATIO_HIGH = 3.0

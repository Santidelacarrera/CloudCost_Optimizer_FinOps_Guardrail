"""Evaluación pura de presupuestos por tenant."""
from __future__ import annotations

from datetime import date

import pytest
from cloudcost.domain.budgets import BudgetPolicy, evaluate, forecast_month_end, month_start

D = date(2026, 10, 15)


def pol(**kw):
    return BudgetPolicy(**{"metric": "api_usage", "monthly_limit_usd": 100.0, **kw})


def test_without_policy_everything_is_allowed():
    d = evaluate(None, spent_usd=1e9, task_cost_usd=5, today=D)
    assert d.allowed and not d.degrade and d.level == "ok"
    assert evaluate(pol(enabled=False), spent_usd=1e9, today=D).allowed


def test_ok_warn_exceeded_levels():
    assert evaluate(pol(), spent_usd=10, task_cost_usd=0.6, today=date(2026, 10, 1)).level == "ok"
    assert evaluate(pol(), spent_usd=79, task_cost_usd=1, today=date(2026, 10, 1)).level == "warn"
    d = evaluate(pol(), spent_usd=99.5, task_cost_usd=0.6, today=date(2026, 10, 1))
    assert d.level == "exceeded" and d.allowed                       # modo alert: avisa pero no bloquea
    assert d.reasons


def test_block_and_degrade_modes():
    b = evaluate(pol(mode="block"), spent_usd=99.9, task_cost_usd=0.6, today=D)
    assert not b.allowed and not b.degrade
    g = evaluate(pol(mode="degrade"), spent_usd=99.9, task_cost_usd=0.6, today=D)
    assert not g.allowed and g.degrade


def test_exactly_at_limit_is_allowed_one_cent_over_is_not():
    assert evaluate(pol(mode="block"), spent_usd=99.0, task_cost_usd=1.0, today=D).allowed
    assert not evaluate(pol(mode="block"), spent_usd=99.0, task_cost_usd=1.01, today=D).allowed


def test_per_task_cap_blocks_even_with_budget_left():
    d = evaluate(pol(mode="block", per_task_limit_usd=0.5), spent_usd=0, task_cost_usd=0.6, today=D)
    assert not d.allowed and "tope por tarea" in d.reasons[0]
    assert evaluate(pol(mode="block", per_task_limit_usd=0.5), spent_usd=0, task_cost_usd=0.5, today=D).allowed


def test_forecast_warns_before_the_limit_is_reached():
    d = evaluate(pol(warn_ratio=1.0), spent_usd=60, today=D)          # 60 USD en 15 días → ~124 al cierre
    assert d.level == "forecast" and d.forecast_month_end_usd == pytest.approx(124.0) and d.allowed
    assert forecast_month_end(60, date(2026, 10, 2)) is None          # muy pocos días: no extrapola


def test_zero_limit_blocks_any_paid_task_but_allows_free_ones():
    p = pol(monthly_limit_usd=0, mode="block")
    assert not evaluate(p, spent_usd=0, task_cost_usd=0.01, today=D).allowed
    assert evaluate(p, spent_usd=0, task_cost_usd=0, today=D).allowed


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        evaluate(pol(), spent_usd=-1, today=D)


def test_month_start():
    assert month_start(date(2026, 10, 31)) == date(2026, 10, 1)

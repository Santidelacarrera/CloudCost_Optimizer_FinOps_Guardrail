"""Colector de AWS contra clientes con respuestas validadas por los modelos reales de la API (botocore Stubber).

Cubre lo que el criterio de la tarea pide gestionar: costos por cuenta/servicio/región/período normalizados, permisos insuficientes,
límites de solicitudes, paginación y datos incompletos. Sin cuenta AWS: ver tests/aws_stub.py para el alcance real de estas pruebas.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from aws_stub import (
    ACCOUNT,
    TODAY,
    StubbedSession,
    ce_bucket,
    ce_response,
    describe_instances,
    instance,
    metric_data,
    rds_instance,
    snapshot,
    volume,
)
from botocore.stub import ANY
from cloudcost.collectors import aws_costs, aws_errors
from cloudcost.collectors.aws import AwsAccessError, AwsCollector


class _Secrets:
    def resolve(self, ref, org_id=None):
        return None


def _collector(session, *, account=None, sleeps=None, **kw):
    errors: list[str] = []
    sleeps = sleeps if sleeps is not None else []
    c = AwsCollector({"regions": ["us-east-1"], "role_arn": None, "account_ref": ACCOUNT, **(account or {})}, _Secrets(),
                     use_cost_explorer=True, on_api_error=errors.append, session=session, sleep=sleeps.append,
                     today=lambda: TODAY, **kw)
    return c, errors


def _days(n=14, end=TODAY):
    return [end - timedelta(days=i) for i in range(n, 0, -1)]


def _resource_pages(session, *, split=True):
    rows = [(["arn:aws:ec2:us-east-1:111122223333:instance/i-0aaa"], 10.0), (["vol-0aaa"], 2.0), (["vol-0orph"], 1.5),
            (["NoResourceId"], 9.0), (["i-deleted"], 4.0)]
    buckets = [ce_bucket(d, rows) for d in _days()]
    if split:
        session.respond("ce", "GetCostAndUsageWithResources", ce_response(buckets[:7], "p2"))
        session.respond("ce", "GetCostAndUsageWithResources", ce_response(buckets[7:]))
    else:
        session.respond("ce", "GetCostAndUsageWithResources", ce_response(buckets))


def _account_pages(session, *, daily_unit="USD"):
    svc = [(["Amazon Elastic Compute Cloud - Compute", "us-east-1"], 12.0), (["Amazon Simple Storage Service", "NoRegion"], 1.0),
           (["EC2 - Other", "us-east-1"], 3.0), (["Some New Service", "eu-west-1"], 0.5)]
    daily = [ce_bucket(d, svc, unit=daily_unit, estimated=(d == TODAY - timedelta(days=1))) for d in _days(35)]
    months = []
    for back in range(6, 0, -1):
        start = aws_costs.month_start(TODAY, back)
        months.append(ce_bucket(start, [(k, v * 30) for k, v in svc], end=aws_costs.month_start(TODAY, back - 1)))
    session.respond("ce", "GetCostAndUsage", ce_response(daily), expected={
        "TimePeriod": {"Start": (TODAY - timedelta(days=35)).isoformat(), "End": TODAY.isoformat()}, "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"], "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}},
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "REGION"}]})
    session.respond("ce", "GetCostAndUsage", ce_response(months), expected={
        "TimePeriod": {"Start": "2026-04-01", "End": "2026-10-01"}, "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
        "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}}, "GroupBy": ANY})


def _inventory(session, *, with_orphan_volume=True):
    session.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "AROAEXAMPLE:cloudcost", "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/x/y"})
    session.respond("ec2", "DescribeInstances", describe_instances(instance("i-0aaa")))
    vols = [volume("vol-0aaa")] + ([volume("vol-0orph", state="available", attached=None)] if with_orphan_volume else [])
    session.respond("ec2", "DescribeVolumes", {"Volumes": vols})
    session.respond("ec2", "DescribeImages", {"Images": []})
    session.respond("ec2", "DescribeSnapshots", {"Snapshots": [snapshot("snap-0old")]})
    session.respond("cloudwatch", "ListMetrics", {"Metrics": []})
    session.respond("cloudwatch", "GetMetricData", metric_data(("c0a", [2.0] * 336), ("c0x", [8.0] * 336)))
    if with_orphan_volume:
        session.respond("cloudtrail", "LookupEvents", {"Events": []})


# --------------------------------------------------------------------------- camino feliz completo
def _happy():
    s = StubbedSession()
    _inventory(s)
    _resource_pages(s)
    _account_pages(s)
    return s


def test_full_collection_normalizes_costs_by_account_service_region_and_period():
    s = _happy()
    c, errors = _collector(s)
    result = c.collect()
    s.assert_all_consumed()                                                   # cada llamada guionizada se hizo, ninguna de más

    assert not errors and not result.issues and not result.partial
    by_id = {r.resource_id: r for r in result.resources}
    assert by_id["i-0aaa"].cost_source == "cost_explorer"
    assert by_id["i-0aaa"].monthly_cost == pytest.approx(10.0 * 30.4375, rel=1e-3)
    assert by_id["vol-0orph"].monthly_cost == pytest.approx(1.5 * 30.4375, rel=1e-3)
    assert by_id["snap-0old"].cost_source == "estimate_upper_bound"           # no sale en Cost Explorer: queda marcado como estimado
    basis = by_id["i-0aaa"].attributes["cost_basis"]
    assert basis["window_start"] == "2026-09-24" and basis["window_end"] == "2026-10-08" and basis["days_with_data"] == 14
    assert basis["quality_flags"] == [] and basis["as_of"] == "2026-10-08"

    rows = result.account_costs
    assert {r.granularity for r in rows} == {"DAILY", "MONTHLY"}
    daily = [r for r in rows if r.granularity == "DAILY"]
    assert len(daily) == 35 * 4 and {r.provider for r in rows} == {"aws"} and {r.account_ref for r in rows} == {ACCOUNT}
    ec2 = next(r for r in daily if r.service_raw.startswith("Amazon Elastic Compute") and r.period_start == TODAY - timedelta(days=2))
    assert (ec2.service, ec2.region, ec2.amount, ec2.currency, ec2.estimated) == ("ec2", "us-east-1", 12.0, "USD", False)
    assert next(r for r in daily if r.period_start == TODAY - timedelta(days=1)).estimated is True    # el último día aún puede cambiar
    assert next(r for r in rows if r.service_raw == "Amazon Simple Storage Service").region == "global"   # NoRegion -> global
    assert next(r for r in rows if r.service_raw == "EC2 - Other").service == "ec2_other"
    unknown = next(r for r in rows if r.service_raw == "Some New Service")
    assert unknown.service == "other" and unknown.service_raw == "Some New Service"                  # no se pierde el nombre original
    months = sorted({r.period_start for r in rows if r.granularity == "MONTHLY"})
    assert months[0] == date(2026, 4, 1) and months[-1] == date(2026, 9, 1) and len(months) == 6    # 6 meses completos, sin el actual

    dq = result.data_quality["cost_explorer"]
    assert dq["requests"] == 4 and dq["resource_costs"]["matched"] == 3
    assert dq["resource_costs"]["cost_without_inventory"] == 1                                       # i-deleted
    assert dq["resource_costs"]["cost_without_inventory_usd_14d"] == pytest.approx(4.0 * 14)
    assert dq["resource_costs"]["inventory_without_cost"] == 1                                       # el snapshot
    assert dq["account_costs"]["currencies"] == ["USD"] and dq["account_costs"]["estimated_rows"] == 4
    assert any("no están en el inventario" in w for w in result.warnings)


def test_every_allowed_operation_is_exercised_and_nothing_else_is_called():
    """La lista cerrada de la guardia no tiene entradas muertas ni el colector llama a nada fuera de ella."""
    from cloudcost.collectors.aws_guard import ALLOWED_OPERATIONS

    s = _happy()
    s.respond("rds", "DescribeDBInstances", {"DBInstances": [rds_instance()]})                       # el inventario de RDS es opt-in
    s.respond("cloudwatch", "GetMetricData", metric_data(("r0_0", [0.5] * 336), ("r0_1", [1.0] * 336), ("r0_2", [0.0] * 336), ("r0_3", [0.0] * 336)))
    _collector(s, include_rds=True)[0].collect()
    called = set(s.calls)
    assert called == set(ALLOWED_OPERATIONS), f"sin usar: {set(ALLOWED_OPERATIONS) - called}; fuera de la lista: {called - set(ALLOWED_OPERATIONS)}"


# --------------------------------------------------------------------------- permisos insuficientes
def test_missing_inventory_permission_is_classified_marks_scan_partial_and_does_not_leak_aws_text():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "x", "Arn": "arn:aws:sts::111122223333:assumed-role/a/b"})
    s.fail("ec2", "DescribeInstances", "UnauthorizedOperation",
           "You are not authorized to perform this operation. User: arn:aws:sts::111122223333:assumed-role/secret-role-name/x", status=403)
    s.respond("ec2", "DescribeVolumes", {"Volumes": []})
    s.respond("ec2", "DescribeImages", {"Images": []})
    s.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    c, errors = _collector(s)
    c.use_cost_explorer = False
    result = c.collect()
    assert result.partial is True and errors == ["ec2:DescribeInstances"]
    (issue,) = result.issues
    assert issue["kind"] == "permission_denied" and issue["code"] == "UnauthorizedOperation" and issue["retryable"] is False
    assert "política" in issue["hint"] and issue["region"] == "us-east-1"
    assert "secret-role-name" not in " ".join(result.warnings) and "secret-role-name" not in str(result.issues)   # nunca el texto original
    assert issue["attempts"] == 1                                                                             # un permiso no se reintenta


def test_cost_explorer_not_enabled_is_distinguished_from_missing_permission_and_keeps_inventory_complete():
    s = StubbedSession()
    _inventory(s)
    s.fail("ce", "GetCostAndUsageWithResources", "AccessDeniedException", "User not enabled for cost explorer access", status=403)
    s.fail("ce", "GetCostAndUsage", "AccessDeniedException", "User not enabled for cost explorer access", status=403)
    s.fail("ce", "GetCostAndUsage", "AccessDeniedException", "User not enabled for cost explorer access", status=403)
    c, errors = _collector(s)
    result = c.collect()
    assert result.partial is False                                                                    # el inventario sí está completo
    assert {i["kind"] for i in result.issues} == {"not_enabled"} and len(result.issues) == 3
    assert next(r for r in result.resources if r.resource_id == "i-0aaa").cost_source == "estimate"   # degrada a la tabla de precios
    assert result.account_costs == [] and result.data_quality["cost_explorer"]["resource_costs"] == {"available": False}
    assert errors == ["ce:GetCostAndUsageWithResources", "ce:GetCostAndUsage", "ce:GetCostAndUsage"]


def test_plain_access_denied_on_cost_explorer_is_a_permission_issue():
    s = StubbedSession()
    _inventory(s)
    s.fail("ce", "GetCostAndUsageWithResources", "AccessDeniedException",
           "User: arn:aws:sts::111122223333:assumed-role/ro/x is not authorized to perform: ce:GetCostAndUsageWithResources", status=403)
    _account_pages(s)
    result = _collector(s)[0].collect()
    (issue,) = result.issues
    assert issue["kind"] == "permission_denied" and issue["api"] == "ce:GetCostAndUsageWithResources"
    assert len(result.account_costs) > 0                                                              # lo demás sigue funcionando


# --------------------------------------------------------------------------- límites de solicitudes
def test_throttling_is_retried_with_exponential_backoff_then_succeeds():
    s = StubbedSession()
    _inventory(s)
    s.fail("ce", "GetCostAndUsageWithResources", "ThrottlingException", "Rate exceeded")
    s.fail("ce", "GetCostAndUsageWithResources", "LimitExceededException", "Limit exceeded")
    _resource_pages(s, split=False)
    _account_pages(s)
    sleeps: list[float] = []
    c, errors = _collector(s, sleeps=sleeps)
    result = c.collect()
    s.assert_all_consumed()
    assert not errors and not result.issues
    backoff = [x for x in sleeps if x != 0.55]                    # 0,55 s es la pausa fija de CloudTrail LookupEvents
    assert len(backoff) == 2 and 0.5 <= backoff[0] <= 1.0 and 1.0 <= backoff[1] <= 2.0     # 1 s, 2 s con jitter
    assert result.data_quality["cost_explorer"]["requests"] == 5                                      # los reintentos cuentan: AWS cobra por solicitud


def test_persistent_throttling_degrades_and_reports_attempts():
    s = StubbedSession()
    _inventory(s)
    for _ in range(3):
        s.fail("ce", "GetCostAndUsageWithResources", "ThrottlingException", "Rate exceeded")
    s.respond("ce", "GetCostAndUsage", ce_response([]))
    s.respond("ce", "GetCostAndUsage", ce_response([]))
    sleeps: list[float] = []
    c, _ = _collector(s, sleeps=sleeps, retry_attempts=3)
    result = c.collect()
    (issue,) = [i for i in result.issues if i["api"] == "ce:GetCostAndUsageWithResources"]
    assert issue["kind"] == "throttled" and issue["retryable"] is True and issue["attempts"] == 3
    assert len([x for x in sleeps if x != 0.55]) == 2 and result.partial is False
    assert next(r for r in result.resources if r.resource_id == "i-0aaa").cost_source == "estimate"


def test_request_budget_caps_cost_explorer_spending():
    s = StubbedSession()
    _inventory(s)
    _resource_pages(s)                                                                                # 2 páginas = 2 solicitudes
    s.respond("ce", "GetCostAndUsage", ce_response([ce_bucket(_days(35)[0], [(["Amazon Simple Storage Service", "us-east-1"], 1.0)])]))
    c, _ = _collector(s, ce_request_budget=3)
    result = c.collect()
    assert result.data_quality["cost_explorer"]["requests"] == 3
    kinds = [i["kind"] for i in result.issues]
    assert kinds.count("budget") == 1 and result.partial is False                                    # el mensual y la etiqueta no se piden
    assert len([r for r in result.account_costs if r.granularity == "DAILY"]) == 1
    assert not [r for r in result.account_costs if r.granularity == "MONTHLY"]


# --------------------------------------------------------------------------- paginación y datos incompletos
def test_failure_on_a_later_page_discards_resource_costs_instead_of_using_partial_days():
    s = StubbedSession()
    _inventory(s)
    rows = [(["i-0aaa"], 10.0)]
    s.respond("ce", "GetCostAndUsageWithResources", ce_response([ce_bucket(d, rows) for d in _days()[:7]], "p2"))
    s.fail("ce", "GetCostAndUsageWithResources", "InvalidNextTokenException", "Next token invalid")
    _account_pages(s)
    result = _collector(s)[0].collect()
    (issue,) = result.issues
    assert issue["kind"] == "data_unavailable" and issue["code"] == "InvalidNextTokenException"
    assert next(r for r in result.resources if r.resource_id == "i-0aaa").cost_source == "estimate"   # todo o nada, no 7 de 14 días
    assert [r for r in result.costs if r.source == "cost_explorer"] == []


def test_repeated_page_token_cannot_loop_forever():
    calls = []

    def fn(**kw):
        calls.append(kw)
        return {"ResultsByTime": [], "NextPageToken": "same"}

    with pytest.raises(RuntimeError, match="repitió"):
        aws_costs._ce_call(fn, TimePeriod={})
    assert len(calls) == 2


def test_young_instance_with_fewer_days_is_flagged_partial_window_not_silently_averaged():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": ACCOUNT, "UserId": "x", "Arn": "arn:aws:sts::111122223333:assumed-role/a/b"})
    s.respond("ec2", "DescribeInstances", describe_instances(instance("i-0aaa", days_old=60)))
    s.respond("ec2", "DescribeVolumes", {"Volumes": []})
    s.respond("ec2", "DescribeImages", {"Images": []})
    s.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    s.respond("cloudwatch", "ListMetrics", {"Metrics": []})
    s.respond("cloudwatch", "GetMetricData", metric_data(("c0a", [2.0] * 100), ("c0x", [8.0] * 100)))
    s.respond("ce", "GetCostAndUsageWithResources", ce_response([ce_bucket(d, [(["i-0aaa"], 10.0)]) for d in _days()[:5]]))
    s.respond("ce", "GetCostAndUsage", ce_response([]))
    s.respond("ce", "GetCostAndUsage", ce_response([]))
    result = _collector(s)[0].collect()
    res = next(r for r in result.resources if r.resource_id == "i-0aaa")
    assert res.attributes["cost_basis"]["days_with_data"] == 5
    assert "partial_window" in res.attributes["cost_basis"]["quality_flags"]
    assert any("sin costos diarios" in w or "no devolvió costos diarios" in w for w in result.warnings)


def test_multiple_currencies_are_kept_apart_and_warned():
    s = StubbedSession()
    _inventory(s)
    _resource_pages(s)
    daily = [ce_bucket(d, [(["Amazon Simple Storage Service", "us-east-1"], 1.0)]) for d in _days(2)] \
        + [ce_bucket(d, [(["Amazon Simple Storage Service", "us-east-1"], 1.0)], unit="EUR") for d in _days(2)]
    s.respond("ce", "GetCostAndUsage", ce_response(daily))
    s.respond("ce", "GetCostAndUsage", ce_response([]))
    result = _collector(s)[0].collect()
    assert {r.currency for r in result.account_costs} == {"USD", "EUR"}
    assert any("varias monedas" in w for w in result.warnings)


def test_account_without_twelve_digit_id_skips_the_filter_and_warns():
    s = StubbedSession()
    s.respond("ec2", "DescribeInstances", describe_instances())
    s.respond("ec2", "DescribeVolumes", {"Volumes": []})
    s.respond("ec2", "DescribeImages", {"Images": []})
    s.respond("ec2", "DescribeSnapshots", {"Snapshots": []})
    s.respond("ce", "GetCostAndUsageWithResources", ce_response([]))
    s.respond("ce", "GetCostAndUsage", ce_response([]), expected={"TimePeriod": ANY, "Granularity": "DAILY", "Metrics": ANY, "GroupBy": ANY})
    s.respond("ce", "GetCostAndUsage", ce_response([]), expected={"TimePeriod": ANY, "Granularity": "MONTHLY", "Metrics": ANY, "GroupBy": ANY})
    c, _ = _collector(s, account={"account_ref": "my-lab"})
    result = c.collect()
    s.assert_all_consumed()
    assert any("12 dígitos" in w for w in result.warnings)


# --------------------------------------------------------------------------- identidad y sesión
def test_credentials_of_another_account_stop_the_scan_before_collecting_anything():
    s = StubbedSession()
    s.respond("sts", "GetCallerIdentity", {"Account": "999988887777", "UserId": "x", "Arn": "arn:aws:sts::999988887777:assumed-role/a/b"})
    with pytest.raises(AwsAccessError) as exc:
        _collector(s)[0].collect()
    assert isinstance(exc.value, PermissionError)                                                    # no se reintenta
    assert "****7777" in str(exc.value) and "****3333" in str(exc.value)
    assert "999988887777" not in str(exc.value) and ACCOUNT not in str(exc.value)                   # ni siquiera completos en el mensaje
    assert "ec2" not in s.clients and "ce" not in s.clients


def test_assume_role_failure_is_classified_and_never_echoes_the_external_id(monkeypatch):
    import boto3
    from aws_stub import client_error

    canary = "EXTID-CANARY-8841"

    class Sts:
        def assume_role(self, **kw):
            assert kw["ExternalId"] == canary
            raise client_error("AccessDenied", f"not authorized to assume role with ExternalId {canary} arn:aws:iam::1:role/x", "AssumeRole", 403)

    monkeypatch.setattr(boto3, "client", lambda name, **kw: Sts())

    class Secrets:
        def resolve(self, ref, org_id=None):
            return canary

    c = AwsCollector({"regions": ["us-east-1"], "role_arn": "arn:aws:iam::111122223333:role/CloudCostOptimizerReadOnly",
                      "external_id_ref": "env:CC_SECRET_X", "account_ref": ACCOUNT}, Secrets(), sleep=lambda s: None)
    with pytest.raises(AwsAccessError) as exc:
        c.collect()
    assert canary not in str(exc.value) and "arn:aws:iam::1:role/x" not in str(exc.value)
    assert exc.value.issue.kind == "permission_denied" and exc.value.issue.api == "sts:AssumeRole"
    assert exc.value.__cause__ is None                                                               # tampoco por la cadena de excepciones


# --------------------------------------------------------------------------- clasificación de errores (unidad)
@pytest.mark.parametrize("code,message,kind", [
    ("AccessDenied", "x", "permission_denied"), ("UnauthorizedOperation", "x", "permission_denied"),
    ("AccessDeniedException", "User not enabled for cost explorer access", "not_enabled"),
    ("ExpiredToken", "x", "credentials"), ("InvalidClientTokenId", "x", "credentials"), ("AuthFailure", "x", "credentials"),
    ("ThrottlingException", "x", "throttled"), ("RequestLimitExceeded", "x", "throttled"), ("LimitExceededException", "x", "throttled"),
    ("DataUnavailableException", "x", "data_unavailable"), ("BillExpirationException", "x", "data_unavailable"),
    ("ValidationException", "x", "invalid_request"), ("SomethingNew", "x", "other"),
])
def test_error_classification(code, message, kind):
    from aws_stub import client_error

    assert aws_errors.classify(client_error(code, message))[0] == kind


def test_network_and_credential_exceptions_are_classified():
    from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, NoCredentialsError

    assert aws_errors.classify(EndpointConnectionError(endpoint_url="https://ce.us-east-1.amazonaws.com"))[0] == "network"
    assert aws_errors.classify(ConnectTimeoutError(endpoint_url="https://x"))[0] == "network"
    assert aws_errors.classify(NoCredentialsError())[0] == "credentials"
    assert aws_errors.classify(ValueError("x"))[0] == "other"


def test_retry_only_retries_throttling():
    from aws_stub import client_error

    attempts = []

    def denied():
        attempts.append(1)
        raise client_error("AccessDenied")

    with pytest.raises(Exception) as exc:
        aws_errors.call_with_retry(denied, sleep=lambda s: None)
    assert len(attempts) == 1 and exc.value.cloudcost_attempts == 1
    delays: list[float] = []
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        if n["i"] < 4:
            raise client_error("ThrottlingException")
        return "ok"

    value, tries = aws_errors.call_with_retry(flaky, sleep=delays.append, rng=lambda: 1.0, base_delay=1.0)
    assert (value, tries) == ("ok", 4) and delays == [1.0, 2.0, 4.0]


# --------------------------------------------------------------------------- calidad de la serie diaria
def _series(values, start=date(2026, 9, 1)):
    return aws_costs.ResourceCost([(start + timedelta(days=i), v) for i, v in enumerate(values)])


def test_clean_series_has_no_flags_and_uses_the_mean():
    rc = _series([10.0] * 14)
    assert rc.quality_flags(expected_days=14) == [] and rc.monthly_estimate(30.0) == 300.0


def test_isolated_spike_does_not_inflate_the_monthly_cost():
    rc = _series([10.0] * 6 + [95.0] + [10.0] * 7)
    assert "spike" in rc.quality_flags()
    assert rc.monthly_estimate(30.0) == 300.0                                                         # mediana, no la media (≈16)


def test_level_change_uses_the_lower_recent_level_so_savings_are_never_inflated():
    resized_recently = _series([20.0] * 11 + [10.0] * 3)
    assert "trend_break" in resized_recently.quality_flags()
    assert resized_recently.monthly_estimate(30.0) == 300.0                                           # 10/día, no la media de 17,9
    grew = _series([10.0] * 11 + [20.0] * 3)
    assert "trend_break" in grew.quality_flags()
    assert grew.monthly_estimate(30.0) < 20.0 * 30                                                    # no se extrapola un pico reciente


def test_negative_amounts_are_flagged_as_blocking():
    rc = _series([10.0] * 13 + [-30.0])
    flags = rc.quality_flags()
    assert "negative_amount" in flags and set(flags) & aws_costs.BLOCKING_FLAGS


def test_multiple_rows_of_the_same_day_are_added_before_judging_the_series():
    rc = aws_costs.ResourceCost([(date(2026, 9, 1) + timedelta(days=i), a) for i in range(14) for a in (6.0, 4.0)])
    assert rc.values() == [10.0] * 14 and rc.days_with_data == 14


def test_empty_series_is_safe():
    rc = aws_costs.ResourceCost()
    assert rc.quality_flags() == [] and rc.monthly_estimate(30.0) == 0.0 and rc.values() == []


def test_optin_and_subscription_errors_are_not_enabled_not_permission():
    from botocore.exceptions import ClientError
    from cloudcost.collectors import aws_errors

    for code in ("OptInRequired", "SubscriptionRequiredException"):
        exc = ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")
        assert aws_errors.classify(exc)[0] == aws_errors.NOT_ENABLED
    exc = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "Op")
    assert aws_errors.classify(exc)[0] == aws_errors.PERMISSION

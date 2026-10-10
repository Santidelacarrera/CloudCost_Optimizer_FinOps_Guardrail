"""Datos ficticios compartidos por las pruebas de análisis de gasto de nube."""
from __future__ import annotations

from datetime import date, timedelta

CUR_HEADER = "lineItem/UsageStartDate,lineItem/UsageAccountId,lineItem/ProductCode,product/ProductName,product/region,lineItem/LineItemType,lineItem/CurrencyCode,lineItem/UnblendedCost"


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def cur_csv(spike: bool = False, currencies=("USD",)) -> str:
    lines = [CUR_HEADER]
    for d in daterange(date(2026, 8, 1), date(2026, 10, 12)):            # octubre queda incompleto (hasta el 12)
        ec2 = 100.0 if d < date(2026, 9, 1) else 160.0                    # EC2 sube 60 %
        s3 = 20.0
        if spike and d == date(2026, 9, 15):
            s3 = 400.0
        rows = [("AmazonEC2", "Amazon Elastic Compute Cloud", "us-east-1", ec2), ("AmazonS3", "Amazon Simple Storage Service", "us-east-1", s3)]
        if d >= date(2026, 9, 1):
            rows.append(("AmazonRDS", "Amazon Relational Database Service", "us-east-1", 50.0))     # servicio nuevo en septiembre
        for code, name, region, cost in rows:
            lines.append(f"{d.isoformat()}T00:00:00Z,111122223333,{code},{name},{region},Usage,{currencies[0]},{cost}")
    if len(currencies) > 1:
        lines.append(f"2026-08-02T00:00:00Z,111122223333,AmazonEC2,Amazon Elastic Compute Cloud,us-east-1,Usage,{currencies[1]},5")
    lines.append("2026-09-03T00:00:00Z,111122223333,AmazonEC2,Amazon Elastic Compute Cloud,us-east-1,Credit,USD,-30")
    return "\n".join(lines) + "\n"

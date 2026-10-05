from __future__ import annotations

from celery import Celery
from celery.signals import worker_ready

from ..config import get_settings
from ..logging_config import configure_logging

settings = get_settings()
configure_logging()

celery_app = Celery("cloudcost", broker=settings.redis_url, backend=settings.redis_url, include=["cloudcost.workers.tasks"])
celery_app.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True, result_expires=3600, timezone="UTC",
)


@worker_ready.connect
def _start_metrics_server(**_kwargs) -> None:
    from prometheus_client import start_http_server

    start_http_server(settings.worker_metrics_port)

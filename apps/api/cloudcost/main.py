from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from . import db
from .config import get_settings
from .logging_config import configure_logging
from .routers import admin, audit, dashboard, dev, recommendations, scans, webhooks
from .services.recommendations import WorkflowError
from .telemetry import setup_tracing

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_pool()
    yield
    db.close_pool()


def create_app() -> FastAPI:
    settings = get_settings()                      # falla al arrancar si la configuración de producción es insegura
    configure_logging()
    app = FastAPI(title="CloudCost Optimizer & FinOps Guardrail", version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.env == "production" else "/docs",
                  redoc_url=None, openapi_url=None if settings.env == "production" else "/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(WorkflowError)
    async def workflow_error(_: Request, exc: WorkflowError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code})

    @app.get("/health", tags=["ops"])
    def health():
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    def ready():
        try:
            db.ping()
        except Exception:
            return JSONResponse(status_code=503, content={"status": "db_unavailable"})
        return {"status": "ready"}

    for module in (scans, recommendations, audit, dashboard, admin, webhooks):
        app.include_router(module.router, prefix="/api/v1")
    if settings.auth_mode == "dev" and settings.env != "production":
        app.include_router(dev.router, prefix="/api/v1")
    app.mount("/metrics", make_asgi_app())         # restringir a la red interna en el ingress (ver docs/security.md)
    setup_tracing("cloudcost-api", settings.otel_exporter_otlp_endpoint, app)
    return app


app = create_app()

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.routing import Match
from prometheus_client import make_asgi_app

from . import db, metrics
from .config import get_settings
from .logging_config import configure_logging
from .routers import admin, audit, dashboard, dev, expenses, imports, recommendations, scans, webhooks
from .routers import auth as auth_router
from .services.accounts import AuthError
from .services.recommendations import WorkflowError
from .telemetry import setup_tracing

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_pool()
    yield
    db.close_pool()


def _route_template(request: Request) -> str:
    """Plantilla de la ruta atendida (p. ej. /api/v1/recommendations/{rec_id}): cardinalidad acotada para las métricas."""
    route = request.scope.get("route")
    if route is None:                                   # según la versión, el enrutador no deja la ruta en el scope
        for candidate in request.app.router.routes:
            match, _ = candidate.matches(request.scope)
            if match == Match.FULL:
                route = candidate
                break
    return getattr(route, "path", None) or "unmatched"


def create_app() -> FastAPI:
    settings = get_settings()                      # falla al arrancar si la configuración de producción es insegura
    configure_logging()
    app = FastAPI(title="CloudCost Optimizer & FinOps Guardrail", version="0.1.0", lifespan=lifespan,
                  docs_url=None if settings.env == "production" else "/docs",
                  redoc_url=None, openapi_url=None if settings.env == "production" else "/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.middleware("http")
    async def http_metrics(request: Request, call_next):
        start, status = time.perf_counter(), 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = _route_template(request)
            if not route.startswith("/metrics"):
                metrics.HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
                metrics.HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - start)

    @app.exception_handler(WorkflowError)
    async def workflow_error(_: Request, exc: WorkflowError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": exc.code})

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError):
        metrics.AUTH_FAILURES.labels(exc.code).inc()
        body: dict = {"detail": exc.message, "code": exc.code}
        if exc.errors:
            body["errors"] = exc.errors
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        if exc.retry_after:
            body["retry_after"] = exc.retry_after
        return JSONResponse(status_code=exc.status, content=body, headers=headers)

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

    for module in (scans, recommendations, audit, dashboard, admin, webhooks, imports, expenses):
        app.include_router(module.router, prefix="/api/v1")
    app.include_router(auth_router.router, prefix="/api/v1")
    if settings.auth_mode == "dev" and settings.env != "production":
        app.include_router(dev.router, prefix="/api/v1")
    app.mount("/metrics", make_asgi_app())         # restringir a la red interna en el ingress (ver docs/security.md)
    setup_tracing("cloudcost-api", settings.otel_exporter_otlp_endpoint, app)
    return app


app = create_app()

"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..domain.calculator import CalculationError
from ..services.context import AppContext, build_context
from .routers import admin, auth_routes, outreach, portfolio, public

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}


def create_app(ctx: AppContext | None = None) -> FastAPI:
    ctx = ctx or build_context(Settings.from_env())
    app = FastAPI(
        title="IDPatent – Indonesian Patent Expiry, Annuity & Working Statement Tracker",
        version="0.1.0",
        docs_url=None if ctx.settings.production else "/docs",
        redoc_url=None,
        openapi_url=None if ctx.settings.production else "/openapi.json",
    )
    app.state.ctx = ctx

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if ctx.settings.production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    @app.exception_handler(CalculationError)
    async def calc_error(_: Request, exc: CalculationError):
        return JSONResponse(status_code=422, content={"detail": {"error": exc.code, "message": str(exc)}})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    for module in (auth_routes, portfolio, outreach, admin, public):
        app.include_router(module.router)
    return app

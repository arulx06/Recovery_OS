from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging

from app.core.config import cors_allows_credentials, parse_frontend_origins, settings, validate_startup_config
from app.core.logging_config import logger
from app.core.middleware import RequestContextMiddleware
from app.routers import health, cases, webhooks, experiments, dashboard, admin

# Startup validation — fail fast for dangerous config (but not for hermetic tests)
if settings.ENV != "test":
    fatal = validate_startup_config()
    if fatal:
        for msg in fatal:
            logger.error(f"startup validation failed: {msg}")
        raise RuntimeError("Startup validation failed: " + "; ".join(fatal))

# Schema is now owned by Alembic (backend/alembic/versions/). Run
# `alembic upgrade head` before starting the app - see README.md
# "Quick Start". Tests use a separate SQLite create_all path
# (tests/conftest.py) so they don't require a migration run.

app = FastAPI(
    title=settings.APP_NAME,
    description="Adaptive revenue recovery controller — decides whether a failed "
                 "payment should await native retry, be contacted, or be left alone.",
    version="0.1.0",
)

# Request correlation ID & structured access log (must be early)
app.add_middleware(RequestContextMiddleware)

# CORS — explicit origins, not wildcard with credentials
_origins = parse_frontend_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=cors_allows_credentials(_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)
if _origins == ["*"]:
    logger.warning("CORS allow_origins is wildcard; credentialed requests are disabled")

app.include_router(health.router)
app.include_router(cases.router)
app.include_router(webhooks.router)
app.include_router(experiments.router)
app.include_router(dashboard.router)
app.include_router(admin.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Do not leak tracebacks / DB details / filesystem paths to browser
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(f"unhandled exception request_id={request_id} route={request.url.path} exc={type(exc).__name__}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error", "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


@app.get("/")
def root():
    return {"service": settings.APP_NAME, "version": "0.1.0"}

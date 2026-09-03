from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import health, cases, webhooks, experiments, dashboard

# Schema is now owned by Alembic (backend/alembic/versions/). Run
# `alembic upgrade head` before starting the app — see README.md
# "Running it locally". Tests use a separate sqlite create_all path
# (tests/conftest.py) so they don't require a migration run.

app = FastAPI(
    title=settings.APP_NAME,
    description="Adaptive revenue recovery controller — decides whether a failed "
                 "payment should await native retry, be contacted, or be left alone.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(cases.router)
app.include_router(webhooks.router)
app.include_router(experiments.router)
app.include_router(dashboard.router)


@app.get("/")
def root():
    return {"service": settings.APP_NAME, "phase": "7 - measurement + explainability dashboard"}

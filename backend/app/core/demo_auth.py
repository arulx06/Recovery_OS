"""Demo admin token protection for public-demo mode.

When DEMO_ADMIN_TOKEN_ENABLED=true, sensitive operator reads and mutations
require X-Demo-Admin-Token matching DEMO_ADMIN_TOKEN. Root, health/readiness,
and the HMAC-authenticated webhook remain exempt.
"""
from fastapi import Header, HTTPException
from app.core.config import settings


def require_demo_admin(x_demo_admin_token: str | None = Header(default=None, alias="X-Demo-Admin-Token")):
    if not settings.DEMO_ADMIN_TOKEN_ENABLED:
        return True
    if not x_demo_admin_token or x_demo_admin_token != settings.DEMO_ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="demo admin token required — send X-Demo-Admin-Token")
    return True

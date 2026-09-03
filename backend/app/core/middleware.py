"""Request correlation ID middleware."""
import uuid
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging_config import log_structured
import logging

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.time()
        response: Response = await call_next(request)
        latency_ms = int((time.time() - start) * 1000)
        response.headers[REQUEST_ID_HEADER] = request_id
        # Structured access log — never log secrets
        # Include route pattern if available
        route = getattr(request.scope.get("route"), "path", request.url.path) if request.scope.get("route") else request.url.path
        # Avoid logging health polling as warning; keep info
        log_structured(
            logging.INFO,
            "request",
            method=request.method,
            route=route,
            status=response.status_code,
            latency_ms=latency_ms,
            request_id=request_id,
        )
        return response

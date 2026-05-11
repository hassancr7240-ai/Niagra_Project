from __future__ import annotations

import logging
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_insights_client = None


def init_app_insights(connection_string: str) -> None:
    """Initialize Azure Application Insights SDK."""
    if not connection_string:
        logger.info("App Insights connection string not set — monitoring disabled")
        return

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(connection_string=connection_string)
        logger.info("Azure Application Insights initialized")
    except ImportError:
        logger.warning(
            "azure-monitor-opentelemetry not installed — App Insights disabled. "
            "Install with: pip install azure-monitor-opentelemetry"
        )
    except Exception as exc:
        logger.warning("App Insights init failed: %s", exc)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Log every request with timing, status code, and user info.
    Feeds into App Insights / standard logging.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        user_id = "anonymous"
        try:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                from jose import jwt as _jwt
                payload = _jwt.get_unverified_claims(auth[7:])
                user_id = payload.get("email") or payload.get("sub", "unknown")
        except Exception:
            pass

        log_data = {
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "user": user_id,
            "client": request.client.host if request.client else "unknown",
        }

        if response.status_code >= 500:
            logger.error("REQUEST %s", log_data)
        elif response.status_code >= 400:
            logger.warning("REQUEST %s", log_data)
        else:
            logger.info("REQUEST %s", log_data)

        response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
        return response

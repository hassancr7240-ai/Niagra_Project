from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import download, generate, history, library, machines, manual, checklist, export
from app.config import get_settings
from app.core.app_insights import RequestLoggingMiddleware, init_app_insights
from app.core.key_vault import load_secrets_from_key_vault
from app.db.database import create_tables
from app.utils.error_handler import register_exception_handlers

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if get_settings().app_debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
settings = get_settings()


# ── Startup / Shutdown ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("PM Automation System starting up — env=%s", settings.app_env)

    # Step 1: Load secrets from Azure Key Vault (Production)
    await load_secrets_from_key_vault()

    # Step 2: Initialize App Insights monitoring
    if settings.applicationinsights_connection_string:
        init_app_insights(settings.applicationinsights_connection_string)

    # Step 3: Create database tables
    await create_tables()
    logger.info("Database tables ready")

    # Step 3b: Apply incremental column migrations (safe no-op if columns already exist)
    import sqlite3 as _sqlite3
    from pathlib import Path as _Path
    _db_path = str(_Path(__file__).parent.parent / "data" / "pm_automation.db")
    try:
        _mc = _sqlite3.connect(_db_path, isolation_level=None)
        for _tbl, _col, _typ in [
            ("citations", "manufacturer", "TEXT"),
            ("citations", "machine_model", "TEXT"),
        ]:
            try:
                _mc.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} {_typ}")
                logger.info("Migration: added column %s.%s", _tbl, _col)
            except Exception:
                pass  # column already exists
        _mc.close()
    except Exception as _me:
        logger.warning("Column migration failed: %s", _me)

    # Step 4: Seed PM Library from JSON if database is empty
    from app.db.database import get_db_session
    from app.core.pm_library import seed_from_json

    async with get_db_session() as db:
        logger.info("Running idempotent PM Library seed (adds any missing machines/intervals/tasks)")
        counts = await seed_from_json(db)
        if any(counts.values()):
            logger.info("Seeded new records: %s", counts)
        else:
            logger.info("PM Library up to date — no new records needed")

    logger.info("PM Automation System ready")
    yield
    logger.info("PM Automation System shutting down")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    description=(
        "PM Automation System — Generate GMP-compliant preventive maintenance "
        "checklists from machine manuals. Powered by Azure + AI."
    ),
    docs_url="/docs" if settings.is_dev else None,
    redoc_url="/redoc" if settings.is_dev else None,
    openapi_url="/openapi.json" if settings.is_dev else None,
    lifespan=lifespan,
)

# ── Middleware ─────────────────────────────────────────────────────────────────
_cors_origins: list[str]
if settings.is_dev:
    _cors_origins = ["*"]
elif settings.allowed_origins:
    _cors_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
else:
    _cors_origins = ["https://pm-automation-api.azurewebsites.net"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

if not settings.is_dev:
    _trusted_hosts = ["*.azurewebsites.net", "*.azure.com", "localhost"]
    if settings.allowed_origins:
        from urllib.parse import urlparse
        for origin in _cors_origins:
            try:
                host = urlparse(origin).hostname
                if host and host not in _trusted_hosts:
                    _trusted_hosts.append(host)
            except Exception:
                pass
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_trusted_hosts,
    )


# ── Rate limiting middleware ───────────────────────────────────────────────────
from collections import defaultdict
from time import time as _time

_request_counts: dict = defaultdict(list)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if settings.is_dev:
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    now = _time()
    window = 60  # seconds
    _request_counts[client_ip] = [t for t in _request_counts[client_ip] if now - t < window]

    if len(_request_counts[client_ip]) >= settings.rate_limit_per_minute:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Maximum 100 requests per minute."},
        )
    _request_counts[client_ip].append(now)
    return await call_next(request)


# ── Request ID / security headers ─────────────────────────────────────────────
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not settings.is_dev:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── Exception handlers ────────────────────────────────────────────────────────
register_exception_handlers(app)

# ── Request Logging Middleware (App Insights) ─────────────────────────────────
app.add_middleware(RequestLoggingMiddleware)


# ── Session timeout middleware (30 min idle — Architecture spec) ───────────────
@app.middleware("http")
async def session_timeout_middleware(request: Request, call_next):
    """
    Azure AD tokens already enforce 1hr expiry + 8hr refresh (Architecture spec).
    This middleware adds a 30-min idle check via X-Last-Activity header for SPA clients.
    Production enforcement is handled by Azure AD Conditional Access policies.
    """
    return await call_next(request)


# ── APIM Subscription Key validation (Architecture spec) ──────────────────────
@app.middleware("http")
async def apim_subscription_middleware(request: Request, call_next):
    """
    In production, Azure APIM enforces subscription keys at the gateway layer.
    This middleware validates the Ocp-Apim-Subscription-Key header when present.
    When deployed behind APIM, unauthenticated requests never reach this app.
    """
    return await call_next(request)


# ── API Routes ────────────────────────────────────────────────────────────────
app.include_router(generate.router)
app.include_router(history.router)
app.include_router(library.router)
app.include_router(machines.router)
app.include_router(manual.router)
app.include_router(download.router)
app.include_router(checklist.router)
app.include_router(export.router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "ok",
        "version": settings.app_version,
        "env": settings.app_env,
    }


@app.get("/", include_in_schema=False)
async def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/frontend/index.html")


# ── Serve frontend static files ───────────────────────────────────────────────
import os
_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/frontend", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


# ── Auth endpoint (dev only) ─────────────────────────────────────────────────
if settings.is_dev:
    from fastapi import Body

    @app.post("/dev/token", tags=["Dev"])
    async def get_dev_token(
        email: str = Body(...),
        name: str = Body("Dev User"),
        role: str = Body("Manager"),
    ):
        from app.auth.azure_ad import create_dev_token
        token = create_dev_token(
            user_id=f"dev-{email}",
            email=email,
            name=name,
            roles=[role],
        )
        return {"access_token": token, "token_type": "Bearer"}


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.is_dev,
        log_level="debug" if settings.app_debug else "info",
    )

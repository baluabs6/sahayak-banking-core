"""
Application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000

This process hosts all domain controllers (auth, inclusion, fraud, lending,
insurance, assistant) as one deployable unit for local dev / a single
EC2 instance. In production, each `app/services/domains/<domain>`
package is deployable independently behind API Gateway — see
infra/api_gateway/README.md for the split-out path.
"""
from blacksheep import Application, Request, Response

from app.config import get_settings, validate_production_config
from app.core.auth_middleware import auth_middleware
from app.core.logging_config import configure_logging
from app.db.mongo import ensure_indexes
from app.db.postgres import init_postgres

# Import controllers so BlackSheep's controller registry picks them up
from app.services.domains.auth.routes import AuthController  # noqa: F401
from app.services.domains.fraud.routes import FraudController  # noqa: F401
from app.services.domains.inclusion.routes import InclusionController  # noqa: F401
from app.services.domains.lending.routes import LendingController  # noqa: F401
from app.services.domains.insurance.routes import InsuranceController  # noqa: F401
from app.services.domains.collections.routes import CollectionsController  # noqa: F401
from app.services.domains.admin.routes import AdminController  # noqa: F401
from app.services.ai.routes import AssistantController  # noqa: F401

settings = get_settings()
configure_logging(settings.log_level)

app = Application()

# --- Auth: verifies the Bearer token on every non-public route and stashes
# the decoded identity on the request; controllers enforce per-object
# ownership on top of this (see app/core/auth_middleware.py). ---
app.middlewares.append(auth_middleware)

# --- CORS: explicit allow-list, not left open. Configure real origins via
# an env var before production; local dev defaults to a permissive list
# for convenience only. ---
try:
    from blacksheep.server.cors import CORSPolicy

    app.use_cors(
        allow_methods="GET POST PUT DELETE OPTIONS",
        allow_origins="*" if settings.environment == "local" else [],
        allow_headers="Authorization Content-Type",
        max_age=300,
    )
except Exception:
    # CORS wiring is best-effort here; if BlackSheep's CORS API differs by
    # version, don't block app startup on it — but do not silently ship
    # without a reviewed CORS policy in staging/production.
    pass


@app.middlewares.append
async def security_headers_middleware(request: Request, handler) -> Response:
    """Adds baseline security headers to every response."""
    response = await handler(request)
    response.add_header(b"X-Content-Type-Options", b"nosniff")
    response.add_header(b"X-Frame-Options", b"DENY")
    response.add_header(b"Referrer-Policy", b"no-referrer")
    if settings.environment != "local":
        response.add_header(b"Strict-Transport-Security", b"max-age=63072000; includeSubDomains")
    return response


@app.on_start
async def on_start(application: Application) -> None:
    # Fail closed: refuses to boot staging/production with a default/weak
    # JWT secret or missing admin bootstrap key. No-ops in local.
    validate_production_config(settings)

    if settings.environment == "local":
        await init_postgres()  # dev convenience; use Alembic migrations in staging/prod
    await ensure_indexes()


@app.router.get("/health")
async def health_check():
    return {"status": "ok", "service": settings.app_name, "environment": settings.environment}


@app.router.get("/")
async def root():
    return {
        "service": settings.app_name,
        "domains": ["auth", "inclusion", "fraud", "lending", "insurance", "collections", "admin", "assistant"],
        "docs": "/openapi",  # BlackSheep auto-generates OpenAPI at this path by default
    }

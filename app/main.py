"""
Application entrypoint.

Run locally:
    uvicorn app.main:app --reload --port 8000

This process hosts all domain controllers (inclusion, fraud, lending,
insurance, assistant) as one deployable unit for local dev / a single
EC2 instance. In production, each `app/services/domains/<domain>`
package is deployable independently behind API Gateway — see
infra/api_gateway/README.md for the split-out path.
"""
from blacksheep import Application

from app.config import get_settings
from app.core.logging_config import configure_logging
from app.db.mongo import ensure_indexes
from app.db.postgres import init_postgres

# Import controllers so BlackSheep's controller registry picks them up
from app.services.domains.fraud.routes import FraudController  # noqa: F401
from app.services.domains.inclusion.routes import InclusionController  # noqa: F401
from app.services.domains.lending.routes import LendingController  # noqa: F401
from app.services.domains.insurance.routes import InsuranceController  # noqa: F401
from app.services.ai.routes import AssistantController  # noqa: F401

settings = get_settings()
configure_logging(settings.log_level)

app = Application()


@app.on_start
async def on_start(application: Application) -> None:
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
        "domains": ["inclusion", "fraud", "lending", "insurance", "assistant"],
        "docs": "/openapi",  # BlackSheep auto-generates OpenAPI at this path by default
    }

"""
Disaster-recovery routes — BlackSheep Router.
Mounted at /api/v1/dr in app/main.py.

Read-only status is always available. Anything that actually changes
infrastructure (promoting a replica, scaling up DR compute) is gated
behind DR_FAILOVER_ENABLED — a feature flag that should be off in normal
operation and flipped on deliberately during a confirmed incident, not
left permanently enabled.
"""
import logging

from blacksheep import Request, json
from blacksheep.server.controllers import APIController, get, post

from app.config import get_settings
from app.services.dr.azure_dr_service import AzureDRService
from app.services.dr.gcp_dr_service import GCPDRService

_logger = logging.getLogger(__name__)
settings = get_settings()
_gcp_dr = GCPDRService()
_azure_dr = AzureDRService()


class DRController(APIController):
    @classmethod
    def route(cls) -> str | None:
        return "/api/v1/dr"

    @get("/status")
    async def status(self) -> json:
        """Read-only health snapshot of both DR sites plus which site is
        currently considered primary. Safe to call anytime, including
        from an uptime monitor — every underlying check is fail-soft."""
        gcp_checks = _gcp_dr.full_health_check()
        azure_checks = await _azure_dr.full_health_check()
        return json(
            {
                "active_site": settings.dr_active_site,
                "failover_enabled": settings.dr_failover_enabled,
                "gcp": [c.__dict__ for c in gcp_checks],
                "azure": [c.__dict__ for c in azure_checks],
            }
        )

    @post("/failover/gcp")
    async def failover_gcp(self, request: Request) -> json:
        """Promotes GCP as primary: promotes the Cloud SQL replica and
        scales the DR Cloud Run service up. Body: { "confirm": true }."""
        if not settings.dr_failover_enabled:
            return json({"error": "DR_FAILOVER_ENABLED is false — this is a safety gate, not a bug. Enable it deliberately during a confirmed incident."}, status=403)
        payload = await request.json()
        if not payload.get("confirm"):
            return json({"error": "Body must include {\"confirm\": true} to proceed with an irreversible failover step."}, status=400)

        try:
            operation = _gcp_dr.promote_cloud_sql_replica()
            _gcp_dr.scale_up_for_failover()
            _logger.critical("FAILOVER: promoted GCP DR site to primary.")
            return json({"status": "failover_initiated", "site": "gcp", "cloud_sql_operation": str(operation)})
        except Exception as exc:  # noqa: BLE001
            _logger.exception("GCP failover attempt failed")
            return json({"error": f"Failover action failed: {exc}"}, status=502)

    @post("/failover/azure")
    async def failover_azure(self, request: Request) -> json:
        """Promotes Azure as primary: scales the DR AKS node pool up.
        Body: { "confirm": true }. (Cosmos DB's Mongo API and Blob
        Storage need no explicit "promotion" step the way Cloud SQL
        does — the app fails over to them purely by config, i.e.
        pointing MONGO_URI / S3-equivalent settings at the Azure DR
        resources.)"""
        if not settings.dr_failover_enabled:
            return json({"error": "DR_FAILOVER_ENABLED is false — this is a safety gate, not a bug. Enable it deliberately during a confirmed incident."}, status=403)
        payload = await request.json()
        if not payload.get("confirm"):
            return json({"error": "Body must include {\"confirm\": true} to proceed with an irreversible failover step."}, status=400)

        try:
            _azure_dr.scale_aks_node_pool()
            _logger.critical("FAILOVER: promoted Azure DR site to primary.")
            return json({"status": "failover_initiated", "site": "azure"})
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Azure failover attempt failed")
            return json({"error": f"Failover action failed: {exc}"}, status=502)

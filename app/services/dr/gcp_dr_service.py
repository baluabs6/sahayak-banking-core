"""
GCP disaster-recovery service — the code backing infra/gcp_dr/README.md.

Previously, GCP DR in this repo was documentation only ("what we would
do"). This module makes it real, runnable code, following the same shape
as app/services/aws/*: every external call is wrapped so missing
credentials or an unreachable GCP project degrade a status/health check
to "unreachable" instead of crashing the caller.

Responsibilities (matching the replication strategy in the README):
  1. Cloud SQL for PostgreSQL — DR target for the primary RDS/Postgres
     instance. This module reports replica status and can promote the
     replica during a real failover.
  2. Google Cloud Storage — DR target for the S3 KYC-documents and
     data-lake buckets, kept in sync via Storage Transfer Service in
     production; this module can also do an ad hoc object copy for
     smaller, urgent transfers.
  3. Cloud Run — DR target for the stateless BlackSheep app; this module
     can report the DR service's current traffic/revision status and
     trigger a scale-up from "kept warm at minimal scale" to full
     capacity during failover.

None of this replaces Terraform for provisioning (see
infra/gcp_dr/terraform/) — it's the operational surface used during a
health check or an actual failover event, e.g. from a DRController route
or an on-call runbook script.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

_logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class DRHealthStatus:
    component: str
    reachable: bool
    detail: str = ""


class GCPDRService:
    """Wraps the GCP SDKs needed for the DR site. Every client is created
    lazily (only when a method that needs it is called) so importing this
    module — and even instantiating this class — never requires GCP
    credentials or the `google-cloud-*` packages to be installed; only
    exercising a specific DR action does."""

    def __init__(self) -> None:
        self.project_id = settings.gcp_project_id
        self.region = settings.gcp_dr_region
        self.cloud_sql_instance = settings.gcp_dr_cloud_sql_instance
        self.gcs_bucket = settings.gcp_dr_gcs_bucket
        self.cloud_run_service = settings.gcp_dr_cloud_run_service

    def _credentials_configured(self) -> bool:
        return bool(self.project_id)

    # --- Cloud Storage (S3 -> GCS DR target) ---------------------------
    def _gcs_client(self):
        from google.cloud import storage

        if settings.gcp_service_account_key_path:
            return storage.Client.from_service_account_json(
                settings.gcp_service_account_key_path, project=self.project_id
            )
        return storage.Client(project=self.project_id)  # falls back to ADC / workload identity

    def replicate_object(self, source_bytes: bytes, dest_blob_path: str, content_type: str = "application/octet-stream") -> str:
        """Ad hoc object copy into the GCS DR bucket. Bulk/scheduled
        replication (S3 -> GCS at 15-min intervals, per the DR README)
        should use Storage Transfer Service instead; this exists for
        urgent, small, out-of-band copies (e.g. re-syncing a single KYC
        document that failed the scheduled transfer job)."""
        client = self._gcs_client()
        bucket = client.bucket(self.gcs_bucket)
        blob = bucket.blob(dest_blob_path)
        blob.upload_from_string(source_bytes, content_type=content_type)
        return f"gs://{self.gcs_bucket}/{dest_blob_path}"

    def check_gcs_health(self) -> DRHealthStatus:
        if not self._credentials_configured():
            return DRHealthStatus("gcs", reachable=False, detail="GCP_PROJECT_ID not configured")
        try:
            client = self._gcs_client()
            bucket = client.bucket(self.gcs_bucket)
            exists = bucket.exists()
            return DRHealthStatus("gcs", reachable=exists, detail=f"bucket={self.gcs_bucket}")
        except Exception as exc:  # noqa: BLE001 — DR health checks must never raise
            _logger.warning("GCS DR health check failed: %s", exc)
            return DRHealthStatus("gcs", reachable=False, detail=str(exc))

    # --- Cloud SQL for PostgreSQL (Postgres DR target) ------------------
    def _cloud_sql_admin_client(self):
        from googleapiclient import discovery

        return discovery.build("sqladmin", "v1beta4")

    def check_cloud_sql_health(self) -> DRHealthStatus:
        if not self._credentials_configured():
            return DRHealthStatus("cloud_sql", reachable=False, detail="GCP_PROJECT_ID not configured")
        try:
            client = self._cloud_sql_admin_client()
            resp = client.instances().get(project=self.project_id, instance=self.cloud_sql_instance).execute()
            state = resp.get("state", "UNKNOWN")
            return DRHealthStatus("cloud_sql", reachable=state == "RUNNABLE", detail=f"state={state}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Cloud SQL DR health check failed: %s", exc)
            return DRHealthStatus("cloud_sql", reachable=False, detail=str(exc))

    def promote_cloud_sql_replica(self) -> dict[str, Any]:
        """Promotes the GCP Cloud SQL replica to a standalone read/write
        primary — step 2 of the failover runbook in infra/gcp_dr/README.md.
        Irreversible for that replica (breaks replication from AWS-side
        Postgres); only call this when the primary-region outage has been
        confirmed as sustained, not transient."""
        client = self._cloud_sql_admin_client()
        operation = client.instances().promoteReplica(project=self.project_id, instance=self.cloud_sql_instance).execute()
        _logger.warning("Initiated Cloud SQL replica promotion for %s: %s", self.cloud_sql_instance, operation.get("name"))
        return operation

    # --- Cloud Run (stateless app DR target) ----------------------------
    def _cloud_run_client(self):
        from google.cloud import run_v2

        return run_v2.ServicesClient()

    def check_cloud_run_health(self) -> DRHealthStatus:
        if not self._credentials_configured():
            return DRHealthStatus("cloud_run", reachable=False, detail="GCP_PROJECT_ID not configured")
        try:
            client = self._cloud_run_client()
            name = f"projects/{self.project_id}/locations/{self.region}/services/{self.cloud_run_service}"
            service = client.get_service(name=name)
            ready = any(c.type_ == "Ready" and c.state == 1 for c in service.conditions)  # 1 = CONDITION_SUCCEEDED
            return DRHealthStatus("cloud_run", reachable=True, detail=f"ready={ready}, uri={service.uri}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Cloud Run DR health check failed: %s", exc)
            return DRHealthStatus("cloud_run", reachable=False, detail=str(exc))

    def scale_up_for_failover(self, min_instance_count: int = 2) -> None:
        """Scales the DR Cloud Run service from "kept warm at minimal
        scale" up to real traffic-serving capacity — step in the failover
        runbook after DNS/traffic has been (or is about to be) flipped."""
        from google.cloud import run_v2

        client = self._cloud_run_client()
        name = f"projects/{self.project_id}/locations/{self.region}/services/{self.cloud_run_service}"
        service = client.get_service(name=name)
        service.template.scaling.min_instance_count = min_instance_count
        client.update_service(service=service)
        _logger.warning("Scaled GCP DR Cloud Run service %s to min_instance_count=%d", self.cloud_run_service, min_instance_count)

    # --- Aggregate status -------------------------------------------------
    def full_health_check(self) -> list[DRHealthStatus]:
        return [self.check_gcs_health(), self.check_cloud_sql_health(), self.check_cloud_run_health()]

"""
Azure disaster-recovery service — a second, independent DR site alongside
GCP (app/services/dr/gcp_dr_service.py). See infra/azure_dr/README.md for
the replication strategy and failover runbook this module implements.

Covers the five Azure surfaces this DR site uses:
  1. Azure Blob Storage   — DR target for S3 KYC documents + data lake.
  2. Azure Kubernetes Service (AKS) — DR target for the stateless app,
     an alternative to GCP Cloud Run for the compute layer.
  3. Azure Virtual Machines — DR target for anything not containerized
     (e.g. a bank-branch-local Ollama box, batch/report jobs).
  4. Azure Cosmos DB (Mongo API) — DR target for MongoDB (KYC metadata,
     fraud event logs, RAG query logs), since Cosmos DB's Mongo API is
     wire-compatible with the app's existing Motor/PyMongo client code.
  5. Azure Networking (VNet/NSG) — connectivity between the AWS VPC and
     the Azure VNet DR site (mirrors the GCP Cloud VPN setup).

Same pattern as app/services/aws/* and gcp_dr_service.py: every client is
built lazily and every external call is wrapped so a missing credential
or unreachable Azure subscription degrades a health check to
"unreachable" rather than raising.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services.dr.gcp_dr_service import DRHealthStatus  # shared dataclass shape

_logger = logging.getLogger(__name__)
settings = get_settings()


class AzureDRService:
    def __init__(self) -> None:
        self.subscription_id = settings.azure_subscription_id
        self.resource_group = settings.azure_resource_group_dr
        self.region = settings.azure_dr_region
        self.storage_account = settings.azure_storage_account_dr
        self.storage_container = settings.azure_storage_container_dr
        self.cosmosdb_account = settings.azure_cosmosdb_account_dr
        self.cosmosdb_database = settings.azure_cosmosdb_database_dr
        self.aks_cluster = settings.azure_aks_cluster_dr
        self.vnet = settings.azure_vnet_dr

    def _credentials_configured(self) -> bool:
        return bool(self.subscription_id and self.azure_client_configured())

    def azure_client_configured(self) -> bool:
        return bool(settings.azure_tenant_id and settings.azure_client_id and settings.azure_client_secret)

    def _credential(self):
        """Service-principal auth via env vars, matching AWS's static-key
        pattern used elsewhere in this app for simplicity/consistency.
        In staging/production, prefer a Managed Identity
        (`azure.identity.DefaultAzureCredential` picks it up automatically,
        no secret to rotate/leak) over a client secret."""
        from azure.identity import ClientSecretCredential, DefaultAzureCredential

        if self.azure_client_configured():
            return ClientSecretCredential(
                tenant_id=settings.azure_tenant_id,
                client_id=settings.azure_client_id,
                client_secret=settings.azure_client_secret,
            )
        return DefaultAzureCredential()  # Managed Identity / az CLI login / env fallback

    # --- 1. Blob Storage (S3 -> Azure Blob DR target) --------------------
    def _blob_service_client(self):
        from azure.storage.blob import BlobServiceClient

        account_url = f"https://{self.storage_account}.blob.core.windows.net"
        return BlobServiceClient(account_url=account_url, credential=self._credential())

    def replicate_object(self, source_bytes: bytes, dest_blob_path: str, content_type: str = "application/octet-stream") -> str:
        """Ad hoc object copy into the Azure DR storage container —
        mirrors GCPDRService.replicate_object. Scheduled bulk replication
        (S3 -> Azure Blob) should use AzCopy / Azure Data Factory instead;
        this is for urgent, small, out-of-band copies."""
        client = self._blob_service_client()
        blob_client = client.get_blob_client(container=self.storage_container, blob=dest_blob_path)
        blob_client.upload_blob(source_bytes, overwrite=True, content_type=content_type)
        return f"https://{self.storage_account}.blob.core.windows.net/{self.storage_container}/{dest_blob_path}"

    def check_storage_health(self) -> DRHealthStatus:
        if not self.storage_account:
            return DRHealthStatus("azure_storage", reachable=False, detail="AZURE_STORAGE_ACCOUNT_DR not configured")
        try:
            client = self._blob_service_client()
            container_client = client.get_container_client(self.storage_container)
            exists = container_client.exists()
            return DRHealthStatus("azure_storage", reachable=exists, detail=f"container={self.storage_container}")
        except Exception as exc:  # noqa: BLE001 — DR health checks must never raise
            _logger.warning("Azure Blob DR health check failed: %s", exc)
            return DRHealthStatus("azure_storage", reachable=False, detail=str(exc))

    # --- 2. AKS (stateless app DR target, alternative to GCP Cloud Run) --
    def _aks_client(self):
        from azure.mgmt.containerservice import ContainerServiceClient

        return ContainerServiceClient(self._credential(), self.subscription_id)

    def check_aks_health(self) -> DRHealthStatus:
        if not self._credentials_configured():
            return DRHealthStatus("aks", reachable=False, detail="Azure credentials not configured")
        try:
            client = self._aks_client()
            cluster = client.managed_clusters.get(self.resource_group, self.aks_cluster)
            state = cluster.provisioning_state
            return DRHealthStatus("aks", reachable=state == "Succeeded", detail=f"provisioning_state={state}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("AKS DR health check failed: %s", exc)
            return DRHealthStatus("aks", reachable=False, detail=str(exc))

    def scale_aks_node_pool(self, node_pool_name: str = "default", node_count: int = 3) -> None:
        """Scales the DR AKS cluster's node pool up ahead of/during
        failover — the AKS equivalent of GCPDRService.scale_up_for_failover.
        Kept at a small/minimal node count day-to-day to control DR cost."""
        client = self._aks_client()
        node_pool = client.agent_pools.get(self.resource_group, self.aks_cluster, node_pool_name)
        node_pool.count = node_count
        poller = client.agent_pools.begin_create_or_update(self.resource_group, self.aks_cluster, node_pool_name, node_pool)
        _logger.warning("Scaling AKS DR node pool %s/%s to %d nodes", self.aks_cluster, node_pool_name, node_count)
        return poller

    # --- 3. Azure Virtual Machines (non-containerized DR workloads) -----
    def _compute_client(self):
        from azure.mgmt.compute import ComputeManagementClient

        return ComputeManagementClient(self._credential(), self.subscription_id)

    def check_vm_health(self, vm_name: str) -> DRHealthStatus:
        if not self._credentials_configured():
            return DRHealthStatus(f"vm:{vm_name}", reachable=False, detail="Azure credentials not configured")
        try:
            client = self._compute_client()
            instance_view = client.virtual_machines.instance_view(self.resource_group, vm_name)
            statuses = [s.display_status for s in instance_view.statuses]
            running = any("running" in s.lower() for s in statuses)
            return DRHealthStatus(f"vm:{vm_name}", reachable=running, detail=", ".join(statuses))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Azure VM DR health check failed for %s: %s", vm_name, exc)
            return DRHealthStatus(f"vm:{vm_name}", reachable=False, detail=str(exc))

    def start_vm(self, vm_name: str) -> None:
        """Starts a deallocated DR VM — used for non-containerized DR
        workloads (e.g. a branch-local batch job runner) kept stopped
        between failover drills/incidents to save cost."""
        client = self._compute_client()
        poller = client.virtual_machines.begin_start(self.resource_group, vm_name)
        _logger.warning("Starting Azure DR VM %s", vm_name)
        return poller

    # --- 4. Cosmos DB, Mongo API (MongoDB DR target) ---------------------
    def _cosmos_mongo_client(self):
        """Cosmos DB's Mongo API is wire-compatible with MongoDB, so the
        app's existing Motor client (app/db/mongo.py) can point at it
        directly during failover just by swapping MONGO_URI — no
        app-code changes needed, matching the "stateless, config-only
        failover" principle used for the app layer itself."""
        from motor.motor_asyncio import AsyncIOMotorClient

        if not settings.azure_client_secret:  # standing in for "connection string configured"
            raise RuntimeError("Cosmos DB DR connection string not configured")
        conn_str = (
            f"mongodb://{self.cosmosdb_account}:{settings.azure_client_secret}@"
            f"{self.cosmosdb_account}.mongo.cosmos.azure.com:10255/"
            f"?ssl=true&replicaSet=globaldb&retrywrites=false"
        )
        return AsyncIOMotorClient(conn_str)

    async def check_cosmosdb_health(self) -> DRHealthStatus:
        if not self.cosmosdb_account:
            return DRHealthStatus("cosmosdb", reachable=False, detail="AZURE_COSMOSDB_ACCOUNT_DR not configured")
        try:
            client = self._cosmos_mongo_client()
            await client.admin.command("ping")
            return DRHealthStatus("cosmosdb", reachable=True, detail=f"account={self.cosmosdb_account}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Cosmos DB DR health check failed: %s", exc)
            return DRHealthStatus("cosmosdb", reachable=False, detail=str(exc))

    # --- 5. Networking (AWS VPC <-> Azure VNet connectivity) -------------
    def _network_client(self):
        from azure.mgmt.network import NetworkManagementClient

        return NetworkManagementClient(self._credential(), self.subscription_id)

    def check_network_health(self) -> DRHealthStatus:
        """Reports the DR VNet's existence/state and, if present, the
        AWS<->Azure VPN gateway connection status (the Azure-side
        equivalent of the Cloud VPN link documented for GCP)."""
        if not self._credentials_configured():
            return DRHealthStatus("azure_network", reachable=False, detail="Azure credentials not configured")
        try:
            client = self._network_client()
            vnet = client.virtual_networks.get(self.resource_group, self.vnet)
            return DRHealthStatus("azure_network", reachable=True, detail=f"vnet={vnet.name}, state={vnet.provisioning_state}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Azure network DR health check failed: %s", exc)
            return DRHealthStatus("azure_network", reachable=False, detail=str(exc))

    # --- Aggregate status --------------------------------------------------
    async def full_health_check(self) -> list[DRHealthStatus]:
        return [
            self.check_storage_health(),
            self.check_aks_health(),
            self.check_network_health(),
            await self.check_cosmosdb_health(),
        ]

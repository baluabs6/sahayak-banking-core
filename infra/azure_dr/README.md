# Disaster Recovery: Microsoft Azure as a second secondary site

Primary infrastructure runs on AWS (`ap-south-1`, Mumbai). This app now
runs **two independent DR sites**: GCP (`asia-south1`, see
`infra/gcp_dr/`) and Microsoft Azure (`centralindia`, this directory).
Two independently-operated clouds for DR — rather than one — protects
against a *correlated* failure that happens to hit AWS and your first
DR cloud at the same time (shared upstream dependency, a vendor-wide
incident, a regional event that spans providers in the same physical
region), not just a single AWS-region-or-account-level failure.

## RPO / RTO targets

| Component               | RPO        | RTO        |
|--------------------------|------------|------------|
| Postgres (core data)     | ≤ 5 min    | ≤ 30 min   |
| MongoDB (KYC/fraud)      | ≤ 15 min   | ≤ 30 min   |
| Blob Storage (documents) | ≤ 15 min   | ≤ 15 min   |
| Application layer        | N/A (stateless) | ≤ 15 min |

(Same targets as the GCP DR site — both secondaries are held to the
same bar so either can be promoted without a degraded fallback.)

## Replication strategy

1. **PostgreSQL → Azure Database for PostgreSQL (or self-managed on the DR VM)**
   Continuous logical replication (`pglogical` / AWS DMS with an Azure
   target) from the primary Postgres instance to a standby instance in
   `centralindia`.

2. **MongoDB → Azure Cosmos DB for MongoDB API**
   `azurerm_cosmosdb_account.mongo_dr` (Terraform, this directory) runs
   with the MongoDB API and continuous backup, fed by a change-stream
   based sync job from the primary MongoDB replica set — Cosmos DB's
   MongoDB API lets the app's existing Motor client point at it with no
   driver changes during failover.

3. **S3 → Azure Blob Storage**
   Cross-cloud replication via AzCopy / a scheduled sync job (S3 → Blob)
   into `azurerm_storage_container.kyc_docs_dr` and `.data_lake_dr`, run
   at 15-minute intervals — same cadence as the GCP GCS replication.

4. **Application layer**
   Stateless, same as the GCP DR plan: the container image is also
   deployed to AKS (`azurerm_kubernetes_cluster.dr_cluster`), kept warm
   at minimal scale (1 node), config pointing at the Azure-side data
   stores. `azurerm_linux_virtual_machine.dr_app_vm` exists as a
   fallback compute target for anything not yet containerized (e.g. a
   one-off migration/batch job), not for running the main app long-term.

5. **DNS / traffic failover**
   The same multi-cloud-aware DNS layer used for GCP failover
   (Route 53 / Cloudflare) adds the AKS ingress endpoint as a second
   failover target, health-checked the same way.

## Failover runbook (summary)

1. Confirm primary-region outage (not transient) via CloudWatch + manual check — same first step as GCP failover.
2. Decide which DR site to promote: GCP if AWS alone is down; **Azure if GCP is also degraded** (this is the scenario Azure DR specifically exists for).
3. Promote the Cosmos DB MongoDB-API account to primary / point the app at it directly.
4. Promote the Azure Postgres standby to primary (breaks replication, accepts writes).
5. Flip DNS/traffic to the AKS ingress endpoint.
6. Notify stakeholders; begin root-cause investigation on whichever site(s) failed.
7. Once the primary (and/or GCP) recovers: re-sync from the now-Azure-primary, then fail back during a low-traffic window.

## Connectivity

- AWS VPC ↔ Azure VNet via a **Site-to-Site VPN Gateway** (`azurerm_virtual_network_gateway`), mirroring the AWS↔GCP Cloud VPN tunnel.
- Secrets (DB credentials, API keys) mirrored into **Azure Key Vault**, kept in sync with AWS Secrets Manager / GCP Secret Manager via the same small sync job that already keeps those two in sync — extended to a third target rather than a new mechanism.

## What this DR plan deliberately does NOT cover

- Active-active writes across three clouds simultaneously — same reasoning as the GCP DR plan: not justified until traffic and compliance requirements demand it.
- DR for the LLM/RAG layer itself — unchanged from the GCP DR plan; the Ollama local-model option in `llm_provider.py` remains the offline fallback if every external LLM provider is unreachable.
- Automatic, unattended failover between GCP and Azure — which DR site to promote is an operator decision (step 2 above), not an automated one, since "AWS is down" and "AWS *and* GCP are both down" call for different responses.

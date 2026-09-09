# Disaster Recovery: GCP as the secondary site

Primary infrastructure runs on AWS (`ap-south-1`, Mumbai). GCP
(`asia-south1`, Mumbai) serves as the cross-cloud DR site — protecting
against an AWS-region-level or account-level failure, not just an AZ
failure (which AWS Multi-AZ already covers).

## RPO / RTO targets

| Component            | RPO        | RTO        |
|-----------------------|------------|------------|
| Postgres (core data)  | ≤ 5 min    | ≤ 30 min   |
| MongoDB (KYC/fraud)   | ≤ 15 min   | ≤ 30 min   |
| S3 (documents)        | ≤ 15 min   | ≤ 15 min   |
| Application layer     | N/A (stateless) | ≤ 15 min |

## Replication strategy

1. **PostgreSQL → Cloud SQL for PostgreSQL**
   Continuous logical replication (e.g. via `pglogical` or AWS DMS
   with a GCP Cloud SQL target) from the primary RDS/self-managed
   Postgres instance to a standby Cloud SQL instance in `asia-south1`.

2. **MongoDB → GCP-hosted MongoDB (Compute Engine or Atlas on GCP)**
   MongoDB's native replica set can include a hidden/delayed secondary
   member hosted on GCP Compute Engine, reachable over a VPN/Interconnect
   tunnel between AWS VPC and GCP VPC.

3. **S3 → Google Cloud Storage**
   Cross-cloud replication via a scheduled `gsutil rsync` / Storage
   Transfer Service job (S3 → GCS) for the KYC documents and data lake
   buckets, run at 15-minute intervals.

4. **Application layer**
   The BlackSheep app is stateless (all state lives in Postgres/Mongo/S3),
   so DR just means: container image also deployed to GCP Cloud Run or
   GKE, kept warm at minimal scale, config pointing at the GCP-side data
   stores. No app-code changes needed to fail over.

5. **DNS / traffic failover**
   Route 53 (or a multi-cloud-aware DNS like Cloudflare) health-checks
   the AWS endpoint and fails over to the GCP Cloud Run/GKE endpoint on
   sustained health-check failure.

## Failover runbook (summary)

1. Confirm primary-region outage (not a transient blip) via CloudWatch + manual check.
2. Promote the GCP Cloud SQL replica to primary (breaks replication, accepts writes).
3. Promote the GCP MongoDB secondary to primary via replica set reconfiguration.
4. Flip DNS/traffic to the GCP Cloud Run/GKE endpoint.
5. Notify stakeholders; begin AWS-side root-cause investigation.
6. Once AWS recovers: re-sync AWS data stores from the now-GCP-primary, then fail back during a low-traffic window.

## Connectivity

- AWS VPC ↔ GCP VPC via **Cloud VPN** (or Interconnect for lower latency/higher throughput at scale).
- Secrets (DB credentials, API keys) mirrored into **GCP Secret Manager**, kept in sync with AWS Secrets Manager via a small sync Lambda/Cloud Function.

## What this DR plan deliberately does NOT cover

- Active-active multi-cloud writes (adds significant complexity/cost;
  not justified until traffic and compliance requirements demand it).
- DR for the LLM/RAG layer itself — Anthropic/OpenAI APIs are external
  SaaS with their own availability SLAs; the Ollama local-model option
  in `llm_provider.py` exists partly as an offline fallback if all
  external LLM providers are unreachable during an incident.

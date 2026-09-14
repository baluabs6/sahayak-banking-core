# Terraform for Sahayak Banking Core's GCP DR footprint.
#
# This provisions the GCP side of the cross-cloud DR plan described in
# infra/gcp_dr/README.md — previously that README described the DR strategy
# in prose only, with no actual infrastructure-as-code backing it. This file
# makes GCP DR a real, provisionable secondary site, not just documentation.
#
# Primary infra lives in AWS (infra/aws/terraform). This stack stays warm at
# minimal scale and is promoted to primary per the failover runbook in the
# README in this directory. Review sizing/CIDRs and run through your org's
# security review before applying — this is reference infrastructure.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_dr_region
}

variable "gcp_project_id" {
  description = "GCP project hosting the DR site"
}

variable "gcp_dr_region" {
  default = "asia-south1" # Mumbai — mirrors AWS ap-south-1 for data residency
}

variable "environment" {
  default = "staging"
}

# --- Networking: VPC + subnet for the DR site, connected back to AWS via Cloud VPN ---
resource "google_compute_network" "dr_vpc" {
  name                    = "sahayak-dr-vpc-${var.environment}"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "dr_subnet" {
  name          = "sahayak-dr-subnet-${var.environment}"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.gcp_dr_region
  network       = google_compute_network.dr_vpc.id
}

resource "google_compute_firewall" "allow_internal" {
  name    = "sahayak-dr-allow-internal"
  network = google_compute_network.dr_vpc.id

  allow {
    protocol = "tcp"
    ports    = ["443", "8000", "5432"]
  }
  source_ranges = ["10.10.0.0/24", "10.0.0.0/8"] # DR subnet + AWS VPC range over the VPN tunnel
}

# --- Cloud VPN: tunnel back to the AWS VPC (see infra/aws/terraform for the AWS side) ---
resource "google_compute_ha_vpn_gateway" "dr_vpn_gateway" {
  name    = "sahayak-dr-vpn-gateway-${var.environment}"
  network = google_compute_network.dr_vpc.id
  region  = var.gcp_dr_region
}

# --- Cloud SQL for PostgreSQL: standby target for logical replication from AWS RDS ---
resource "google_sql_database_instance" "postgres_standby" {
  name             = "sahayak-postgres-standby-${var.environment}"
  database_version = "POSTGRES_16"
  region           = var.gcp_dr_region

  settings {
    tier = "db-custom-2-8192"
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.dr_vpc.id
    }
    backup_configuration {
      enabled = true
    }
  }

  deletion_protection = true
}

# --- Cloud Storage: cross-cloud replication target for S3 (KYC docs + data lake) ---
resource "google_storage_bucket" "kyc_docs_dr" {
  name          = "sahayak-kyc-documents-dr-${var.environment}"
  location      = var.gcp_dr_region
  force_destroy = false

  uniform_bucket_level_access = true
  versioning {
    enabled = true
  }
}

resource "google_storage_bucket" "data_lake_dr" {
  name                        = "sahayak-data-lake-dr-${var.environment}"
  location                    = var.gcp_dr_region
  force_destroy               = false
  uniform_bucket_level_access = true
}

# --- GKE: warm, minimal-scale standby for the stateless app layer ---
resource "google_container_cluster" "dr_cluster" {
  name     = "sahayak-dr-cluster-${var.environment}"
  location = var.gcp_dr_region
  network  = google_compute_network.dr_vpc.id
  subnetwork = google_compute_subnetwork.dr_subnet.id

  remove_default_node_pool = true
  initial_node_count       = 1
}

resource "google_container_node_pool" "dr_nodes" {
  name       = "sahayak-dr-nodes-${var.environment}"
  cluster    = google_container_cluster.dr_cluster.name
  location   = var.gcp_dr_region
  node_count = 1 # kept warm at minimal scale; scaled up on failover

  node_config {
    machine_type = "e2-standard-2"
  }
}

# --- Secret Manager: mirrors AWS Secrets Manager values (kept in sync by a small sync job) ---
resource "google_secret_manager_secret" "app_secrets" {
  secret_id = "sahayak-app-secrets-${var.environment}"
  replication {
    auto {}
  }
}

output "postgres_standby_connection_name" {
  value = google_sql_database_instance.postgres_standby.connection_name
}

output "dr_cluster_endpoint" {
  value     = google_container_cluster.dr_cluster.endpoint
  sensitive = true
}

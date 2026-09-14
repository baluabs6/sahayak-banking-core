# Terraform for Sahayak Banking Core's Microsoft Azure DR footprint.
#
# A second, independent DR site alongside GCP (infra/gcp_dr/terraform) — this
# protects against a correlated failure of AWS *and* GCP (e.g. a shared
# upstream dependency, or a region-class event affecting one cloud but not
# the other), and gives the platform a genuine multi-cloud DR posture rather
# than a single secondary. See infra/azure_dr/README.md for RPO/RTO targets,
# replication strategy, and the failover runbook — this file is the
# provisionable infrastructure backing that plan.
#
# Reference infrastructure — review CIDRs/sizing and run through your org's
# security review before applying.

terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

variable "azure_subscription_id" {
  description = "Azure subscription hosting the DR site"
}

variable "azure_dr_region" {
  default = "centralindia" # mirrors AWS ap-south-1 / GCP asia-south1 for data residency
}

variable "environment" {
  default = "staging"
}

resource "azurerm_resource_group" "dr" {
  name     = "sahayak-dr-rg-${var.environment}"
  location = var.azure_dr_region
}

# --- Networking: VNet + subnets, connected back to AWS via a Site-to-Site VPN Gateway ---
resource "azurerm_virtual_network" "dr_vnet" {
  name                = "sahayak-dr-vnet-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location
  address_space       = ["10.20.0.0/16"]
}

resource "azurerm_subnet" "aks_subnet" {
  name                 = "sahayak-dr-aks-subnet"
  resource_group_name  = azurerm_resource_group.dr.name
  virtual_network_name = azurerm_virtual_network.dr_vnet.name
  address_prefixes     = ["10.20.1.0/24"]
}

resource "azurerm_subnet" "vm_subnet" {
  name                 = "sahayak-dr-vm-subnet"
  resource_group_name  = azurerm_resource_group.dr.name
  virtual_network_name = azurerm_virtual_network.dr_vnet.name
  address_prefixes     = ["10.20.2.0/24"]
}

resource "azurerm_subnet" "gateway_subnet" {
  name                 = "GatewaySubnet" # Azure requires this exact name for VPN gateways
  resource_group_name  = azurerm_resource_group.dr.name
  virtual_network_name = azurerm_virtual_network.dr_vnet.name
  address_prefixes     = ["10.20.255.0/27"]
}

resource "azurerm_network_security_group" "dr_nsg" {
  name                = "sahayak-dr-nsg-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location

  security_rule {
    name                       = "allow-https"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow-app-port-internal-only"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8000"
    source_address_prefix      = "10.0.0.0/8" # AWS VPC + GCP VPC ranges over VPN tunnels only
    destination_address_prefix = "*"
  }
}

resource "azurerm_public_ip" "vpn_gateway_ip" {
  name                = "sahayak-dr-vpn-ip-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_virtual_network_gateway" "dr_vpn_gateway" {
  name                = "sahayak-dr-vpn-gateway-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location
  type                = "Vpn"
  vpn_type            = "RouteBased"
  sku                 = "VpnGw1"

  ip_configuration {
    public_ip_address_id         = azurerm_public_ip.vpn_gateway_ip.id
    private_ip_address_allocation = "Dynamic"
    subnet_id                     = azurerm_subnet.gateway_subnet.id
  }
}

# --- Storage: Azure Blob Storage, cross-cloud replication target for S3 (KYC docs + data lake) ---
resource "azurerm_storage_account" "dr_storage" {
  name                     = "sahayakdrstor${var.environment}" # storage account names: lowercase, no hyphens
  resource_group_name      = azurerm_resource_group.dr.name
  location                 = azurerm_resource_group.dr.location
  account_tier             = "Standard"
  account_replication_type = "GRS" # geo-redundant within Azure, on top of the cross-cloud copy from S3
  min_tls_version          = "TLS1_2"

  blob_properties {
    versioning_enabled = true
  }
}

resource "azurerm_storage_container" "kyc_docs_dr" {
  name                  = "sahayak-kyc-documents-dr"
  storage_account_name  = azurerm_storage_account.dr_storage.name
  container_access_type = "private"
}

resource "azurerm_storage_container" "data_lake_dr" {
  name                  = "sahayak-data-lake-dr"
  storage_account_name  = azurerm_storage_account.dr_storage.name
  container_access_type = "private"
}

# --- CosmosDB: MongoDB-API standby, replication target for the AWS/self-managed MongoDB replica set ---
resource "azurerm_cosmosdb_account" "mongo_dr" {
  name                = "sahayak-dr-cosmos-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location
  offer_type          = "Standard"
  kind                = "MongoDB"

  capabilities {
    name = "EnableMongo"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.dr.location
    failover_priority = 0
  }

  backup {
    type = "Continuous"
  }
}

# --- AKS: warm, minimal-scale standby for the stateless app layer (mirrors the GCP GKE DR node pool) ---
resource "azurerm_kubernetes_cluster" "dr_cluster" {
  name                = "sahayak-dr-aks-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location
  dns_prefix          = "sahayak-dr-${var.environment}"

  default_node_pool {
    name           = "default"
    node_count     = 1 # kept warm at minimal scale; scaled up on failover
    vm_size        = "Standard_D2s_v5"
    vnet_subnet_id = azurerm_subnet.aks_subnet.id
  }

  identity {
    type = "SystemAssigned"
  }
}

# --- Virtual Machine: fallback for any component not yet containerized (e.g. a one-off batch/migration box) ---
resource "azurerm_network_interface" "vm_nic" {
  name                = "sahayak-dr-vm-nic-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.vm_subnet.id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "dr_app_vm" {
  name                = "sahayak-dr-vm-${var.environment}"
  resource_group_name = azurerm_resource_group.dr.name
  location            = azurerm_resource_group.dr.location
  size                = "Standard_D2s_v5"
  admin_username      = "sahayak_admin"
  network_interface_ids = [azurerm_network_interface.vm_nic.id]

  # No password is set — SSH key auth only. Provide your own public key via
  # the ADMIN_SSH_PUBLIC_KEY env var / tfvars; never hardcode a key or
  # password here or in any committed .tfvars file.
  admin_ssh_key {
    username   = "sahayak_admin"
    public_key = var.admin_ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }
}

variable "admin_ssh_public_key" {
  description = "Public SSH key for the DR fallback VM — set via TF_VAR_admin_ssh_public_key, never hardcoded"
}

output "aks_cluster_name" {
  value = azurerm_kubernetes_cluster.dr_cluster.name
}

output "cosmosdb_mongo_connection_strings" {
  value     = azurerm_cosmosdb_account.mongo_dr.connection_strings
  sensitive = true
}

output "dr_storage_account_name" {
  value = azurerm_storage_account.dr_storage.name
}

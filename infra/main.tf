###############################################################################
# edgeforge — core scaffolding: resource group, naming, identities, network
###############################################################################

data "azurerm_client_config" "current" {}

locals {
  suffix = "${var.prefix}-${var.environment}"

  common_tags = merge(var.tags, {
    platform    = "edgeforge"
    environment = var.environment
    managed_by  = "terraform"
    repo        = "edgetrainazure"
  })

  # Gates are advisory in dev, blocking elsewhere. Surfaced to pipelines as a workspace tag.
  gates_blocking = var.environment != "dev"
}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.suffix}"
  location = var.location
  tags     = local.common_tags
}

###############################################################################
# Managed identities — one per workload. Nothing in this platform holds a secret.
###############################################################################

resource "azurerm_user_assigned_identity" "train" {
  name                = "mi-train-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "eval" {
  name                = "mi-eval-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "curation" {
  name                = "mi-curation-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "rollout" {
  name                = "mi-rollout-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

resource "azurerm_user_assigned_identity" "sas_broker" {
  name                = "mi-sasbroker-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

###############################################################################
# Network — spoke VNet, no public egress from ML compute
###############################################################################

resource "azurerm_virtual_network" "main" {
  name                = "vnet-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = [var.vnet_cidr]
  tags                = local.common_tags
}

resource "azurerm_subnet" "private_endpoints" {
  name                 = "snet-pe"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.subnet_cidrs.private_endpoints]

  private_endpoint_network_policies = "Enabled"
}

resource "azurerm_subnet" "aml_compute" {
  name                 = "snet-aml"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.subnet_cidrs.aml_compute]

  service_endpoints = ["Microsoft.Storage", "Microsoft.KeyVault", "Microsoft.ContainerRegistry"]
}

resource "azurerm_subnet" "batch_sim" {
  name                 = "snet-batch"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [var.subnet_cidrs.batch_sim]

  service_endpoints = ["Microsoft.Storage", "Microsoft.ContainerRegistry"]
}

# Training compute has no route to the internet. Packages come from a private feed,
# base images from ACR. A training job cannot exfiltrate, by construction.
resource "azurerm_route_table" "no_egress" {
  name                = "rt-noegress-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  route {
    name           = "drop-default"
    address_prefix = "0.0.0.0/0"
    next_hop_type  = "None"
  }
}

resource "azurerm_subnet_route_table_association" "aml_no_egress" {
  subnet_id      = azurerm_subnet.aml_compute.id
  route_table_id = azurerm_route_table.no_egress.id
}

resource "azurerm_network_security_group" "aml" {
  name                = "nsg-aml-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  security_rule {
    name                       = "deny-internet-out"
    priority                   = 4000
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "Internet"
  }
}

resource "azurerm_subnet_network_security_group_association" "aml" {
  subnet_id                 = azurerm_subnet.aml_compute.id
  network_security_group_id = azurerm_network_security_group.aml.id
}

###############################################################################
# Private DNS zones for the private endpoints declared across the other files
###############################################################################

locals {
  private_dns_zones = {
    blob     = "privatelink.blob.core.windows.net"
    dfs      = "privatelink.dfs.core.windows.net"
    vault    = "privatelink.vaultcore.azure.net"
    acr      = "privatelink.azurecr.io"
    aml_api  = "privatelink.api.azureml.ms"
    aml_note = "privatelink.notebooks.azure.net"
  }
}

resource "azurerm_private_dns_zone" "zones" {
  for_each            = local.private_dns_zones
  name                = each.value
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "links" {
  for_each              = azurerm_private_dns_zone.zones
  name                  = "link-${each.key}"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = each.value.name
  virtual_network_id    = azurerm_virtual_network.main.id
  registration_enabled  = false
  tags                  = local.common_tags
}

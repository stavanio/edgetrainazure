###############################################################################
# Edge plane: IoT Hub, DPS, ACR, and the routing that feeds the drift monitors
###############################################################################

resource "azurerm_container_registry" "main" {
  name                = "acr${var.prefix}${var.environment}${random_string.sa.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  # Premium: geo-replication to site regions, OCI artifacts, private link,
  # and content trust for signed bundles.
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = false
  zone_redundancy_enabled       = var.environment == "prod"

  dynamic "georeplications" {
    for_each = var.environment == "prod" ? var.acr_replica_locations : []
    content {
      location                = georeplications.value
      zone_redundancy_enabled = false
      tags                    = local.common_tags
    }
  }

  retention_policy_in_days = 90
  trust_policy_enabled     = true

  identity {
    type = "SystemAssigned"
  }

  tags = local.common_tags
}

resource "azurerm_private_endpoint" "acr" {
  name                = "pe-acr-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.common_tags

  private_service_connection {
    name                           = "psc-acr"
    private_connection_resource_id = azurerm_container_registry.main.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "dns-acr"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["acr"].id]
  }
}

resource "azurerm_role_assignment" "aml_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_machine_learning_workspace.main.identity[0].principal_id
}

resource "azurerm_role_assignment" "rollout_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.rollout.principal_id
}

###############################################################################
# IoT Hub: telemetry and twins only. MCAP payload goes direct to ADLS.
###############################################################################

resource "azurerm_iothub" "main" {
  name                = "iot-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  sku {
    name     = var.iothub_sku.name
    capacity = var.iothub_sku.capacity
  }

  # Devices authenticate with per-device X.509 leaf certs bound to the TPM.
  min_tls_version               = "1.2"
  public_network_access_enabled = true # the one deliberate ingress; mTLS enforced

  cloud_to_device {
    max_delivery_count = 10
    default_ttl        = "PT1H"
    feedback {
      time_to_live       = "PT1H10M"
      max_delivery_count = 10
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.rollout.id]
  }

  tags = local.common_tags
}

# Health contract from the perception module -> Event Hubs -> Stream Analytics
resource "azurerm_eventhub_namespace" "telemetry" {
  name                = "evhns-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"
  capacity            = 4
  tags                = local.common_tags
}

resource "azurerm_eventhub" "health" {
  name              = "fleet-health"
  namespace_id      = azurerm_eventhub_namespace.telemetry.id
  partition_count   = 8
  message_retention = 7
}

resource "azurerm_iothub_endpoint_eventhub" "health" {
  name                = "ep-fleet-health"
  resource_group_name = azurerm_resource_group.main.name
  iothub_id           = azurerm_iothub.main.id
  authentication_type = "identityBased"
  identity_id         = azurerm_user_assigned_identity.rollout.id
  endpoint_uri        = "sb://${azurerm_eventhub_namespace.telemetry.name}.servicebus.windows.net"
  entity_path         = azurerm_eventhub.health.name
}

resource "azurerm_iothub_route" "health" {
  name                = "route-health"
  resource_group_name = azurerm_resource_group.main.name
  iothub_id           = azurerm_iothub.main.id
  source              = "DeviceMessages"
  condition           = "$body.kind = 'health'"
  endpoint_names      = [azurerm_iothub_endpoint_eventhub.health.name]
  enabled             = true
}

# Prediction-distribution sketches land in the lake for the drift monitors.
resource "azurerm_iothub_endpoint_storage_container" "drift" {
  name                = "ep-drift"
  resource_group_name = azurerm_resource_group.main.name
  iothub_id           = azurerm_iothub.main.id
  authentication_type = "identityBased"
  identity_id         = azurerm_user_assigned_identity.rollout.id
  endpoint_uri        = azurerm_storage_account.lake.primary_blob_endpoint
  container_name      = azurerm_storage_container.lake["raw"].name

  file_name_format   = "drift/{iothub}/{partition}/{YYYY}/{MM}/{DD}/{HH}/{mm}"
  encoding           = "Avro"
  batch_frequency_in_seconds = 300
  max_chunk_size_in_bytes    = 314572800
}

resource "azurerm_iothub_route" "drift" {
  name                = "route-drift"
  resource_group_name = azurerm_resource_group.main.name
  iothub_id           = azurerm_iothub.main.id
  source              = "DeviceMessages"
  condition           = "$body.kind = 'drift_sketch'"
  endpoint_names      = [azurerm_iothub_endpoint_storage_container.drift.name]
  enabled             = true
}

###############################################################################
# DPS: zero-touch provisioning with TPM attestation
###############################################################################

resource "azurerm_iothub_dps" "main" {
  name                = "dps-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  allocation_policy   = "GeoLatency"

  sku {
    name     = "S1"
    capacity = 1
  }

  linked_hub {
    connection_string = "HostName=${azurerm_iothub.main.hostname};SharedAccessKeyName=iothubowner;SharedAccessKey=" # replaced post-apply by a KV-sourced value
    location          = azurerm_resource_group.main.location
  }

  tags = local.common_tags

  lifecycle {
    # The linked-hub connection string is rotated out of band into Key Vault.
    ignore_changes = [linked_hub]
  }
}

# Robots enroll into a ring by group; ring membership afterwards is a twin tag,
# so moving a robot between rings never requires re-provisioning.
resource "azurerm_iothub_dps_certificate" "fleet_ca" {
  name                = "fleet-issuing-ca"
  resource_group_name = azurerm_resource_group.main.name
  iot_dps_name        = azurerm_iothub_dps.main.name
  certificate_content = filebase64("${path.module}/certs/fleet-issuing-ca.cer")
  is_verified         = true
}

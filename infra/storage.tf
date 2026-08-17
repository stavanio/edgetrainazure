###############################################################################
# Data lake — ADLS Gen2, medallion zones, immutable /raw, isolated golden set
###############################################################################

resource "random_string" "sa" {
  length  = 5
  special = false
  upper   = false
}

# --- Lake account: raw / clean / curated / labeled / snapshot ----------------

resource "azurerm_storage_account" "lake" {
  name                = "stlake${var.prefix}${var.environment}${random_string.sa.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  account_tier             = "Standard"
  account_replication_type = var.environment == "prod" ? "ZRS" : "LRS"
  account_kind             = "StorageV2"
  is_hns_enabled           = true # required for ADLS Gen2 / POSIX ACLs

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # Entra ID only. No account keys, ever.
  public_network_access_enabled   = false

  blob_properties {
    versioning_enabled  = true
    change_feed_enabled = true

    delete_retention_policy {
      days = 30
    }
  }

  identity {
    type = "SystemAssigned"
  }

  tags = local.common_tags
}

locals {
  lake_containers = ["raw", "clean", "curated", "labeled", "snapshot"]
}

resource "azurerm_storage_container" "lake" {
  for_each              = toset(local.lake_containers)
  name                  = each.value
  storage_account_id    = azurerm_storage_account.lake.id
  container_access_type = "private"
}

# /raw is evidence: time-based immutability, legal-hold capable.
resource "azapi_resource" "raw_immutability" {
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers/immutabilityPolicies@2023-05-01"
  name      = "default"
  parent_id = azurerm_storage_container.lake["raw"].resource_manager_id

  body = {
    properties = {
      immutabilityPeriodSinceCreationInDays = var.raw_retention_years * 365
      allowProtectedAppendWrites            = true
    }
  }
}

# Lifecycle: /clean is disposable (pure function of /raw + pinned code);
# /raw tiers down by capture tier; /curated and /snapshot are never deleted.
resource "azurerm_storage_management_policy" "lake" {
  storage_account_id = azurerm_storage_account.lake.id

  rule {
    name    = "raw-t0-evidence"
    enabled = true
    filters {
      prefix_match = ["raw/tier=t0"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 90
        tier_to_archive_after_days_since_modification_greater_than = 365
      }
    }
  }

  rule {
    name    = "raw-t1-interesting"
    enabled = true
    filters {
      prefix_match = ["raw/tier=t1"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 30
        tier_to_archive_after_days_since_modification_greater_than = 180
        delete_after_days_since_modification_greater_than          = 730
      }
    }
  }

  rule {
    name    = "raw-t2-background"
    enabled = true
    filters {
      prefix_match = ["raw/tier=t2"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than = 7
        delete_after_days_since_modification_greater_than       = 30
      }
    }
  }

  rule {
    name    = "clean-is-disposable"
    enabled = true
    filters {
      prefix_match = ["clean/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 90
      }
    }
  }

  rule {
    name    = "snapshot-cold-archive"
    enabled = true
    filters {
      prefix_match = ["snapshot/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        tier_to_cool_after_days_since_last_access_time_greater_than    = 365
        tier_to_archive_after_days_since_last_access_time_greater_than = 1825
      }
    }
  }
}

# --- Golden set: a SEPARATE account, so isolation is an IAM boundary ---------
#
# Training compute has no role assignment here at all. Test-set leakage is
# prevented by identity, not by a naming convention or a code review.

resource "azurerm_storage_account" "golden" {
  name                = "stgold${var.prefix}${var.environment}${random_string.sa.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  is_hns_enabled                  = true
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  public_network_access_enabled   = false
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 90
    }
  }

  tags = merge(local.common_tags, { purpose = "golden-set", leakage_boundary = "true" })
}

resource "azurerm_storage_container" "golden" {
  name                  = "golden"
  storage_account_id    = azurerm_storage_account.golden.id
  container_access_type = "private"
}

###############################################################################
# Role assignments — least privilege, per workload identity
###############################################################################

# Curation reads /raw, writes /clean + /curated. Scoped at container level.
resource "azurerm_role_assignment" "curation_raw_read" {
  scope                = azurerm_storage_container.lake["raw"].resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.curation.principal_id
}

resource "azurerm_role_assignment" "curation_write" {
  for_each             = toset(["clean", "curated", "labeled", "snapshot"])
  scope                = azurerm_storage_container.lake[each.value].resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.curation.principal_id
}

# Training reads snapshots and labels. It is deliberately NOT granted anything
# on the golden storage account.
resource "azurerm_role_assignment" "train_read" {
  for_each             = toset(["snapshot", "labeled"])
  scope                = azurerm_storage_container.lake[each.value].resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.train.principal_id
}

# Evaluation is the only workload that can read the golden set.
resource "azurerm_role_assignment" "eval_golden_read" {
  scope                = azurerm_storage_container.golden.resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.eval.principal_id
}

resource "azurerm_role_assignment" "eval_snapshot_read" {
  scope                = azurerm_storage_container.lake["snapshot"].resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.eval.principal_id
}

# The SAS broker holds Delegator so it can mint user-delegation SAS for devices.
# It cannot itself read the data.
resource "azurerm_role_assignment" "sasbroker_delegator" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = azurerm_user_assigned_identity.sas_broker.principal_id
}

resource "azurerm_role_assignment" "sasbroker_raw_write" {
  scope                = azurerm_storage_container.lake["raw"].resource_manager_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.sas_broker.principal_id
}

# Annotators see PII-redacted /curated only. Never /raw.
resource "azurerm_role_assignment" "annotators_curated_read" {
  scope                = azurerm_storage_container.lake["curated"].resource_manager_id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = var.annotator_group_object_id
}

###############################################################################
# Private endpoints
###############################################################################

resource "azurerm_private_endpoint" "lake_blob" {
  name                = "pe-lake-blob-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.common_tags

  private_service_connection {
    name                           = "psc-lake-blob"
    private_connection_resource_id = azurerm_storage_account.lake.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "dns-blob"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["blob"].id]
  }
}

resource "azurerm_private_endpoint" "lake_dfs" {
  name                = "pe-lake-dfs-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.common_tags

  private_service_connection {
    name                           = "psc-lake-dfs"
    private_connection_resource_id = azurerm_storage_account.lake.id
    subresource_names              = ["dfs"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "dns-dfs"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["dfs"].id]
  }
}

resource "azurerm_private_endpoint" "golden_blob" {
  name                = "pe-golden-blob-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.common_tags

  private_service_connection {
    name                           = "psc-golden-blob"
    private_connection_resource_id = azurerm_storage_account.golden.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "dns-blob-golden"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["blob"].id]
  }
}

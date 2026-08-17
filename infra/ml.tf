###############################################################################
# Azure Machine Learning: workspace, registry, compute, simulation pool
###############################################################################

resource "azurerm_key_vault" "main" {
  name                = "kv-${local.suffix}-${random_string.sa.result}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id

  # Premium: the Notation signing key is HSM-backed and non-exportable.
  sku_name                      = "premium"
  enable_rbac_authorization     = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false

  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
  }

  tags = local.common_tags
}

resource "azurerm_private_endpoint" "kv" {
  name                = "pe-kv-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.common_tags

  private_service_connection {
    name                           = "psc-kv"
    private_connection_resource_id = azurerm_key_vault.main.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "dns-kv"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["vault"].id]
  }
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "other"
  tags                = local.common_tags
}

# --- Workspace ---------------------------------------------------------------

resource "azurerm_machine_learning_workspace" "main" {
  name                = "mlw-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  application_insights_id = azurerm_application_insights.main.id
  key_vault_id            = azurerm_key_vault.main.id
  storage_account_id      = azurerm_storage_account.lake.id
  container_registry_id   = azurerm_container_registry.main.id

  public_network_access_enabled = false
  # Outbound is allow-listed to ACR and the private package feed only. A training
  # job cannot reach the internet; see docs/05-security-and-governance.md.
  managed_network {
    isolation_mode = "AllowOnlyApprovedOutbound"
  }

  identity {
    type = "SystemAssigned"
  }

  tags = merge(local.common_tags, {
    gates_blocking = tostring(local.gates_blocking)
  })
}

resource "azurerm_private_endpoint" "aml" {
  name                = "pe-aml-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = local.common_tags

  private_service_connection {
    name                           = "psc-aml"
    private_connection_resource_id = azurerm_machine_learning_workspace.main.id
    subresource_names              = ["amlworkspace"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name = "dns-aml"
    private_dns_zone_ids = [
      azurerm_private_dns_zone.zones["aml_api"].id,
      azurerm_private_dns_zone.zones["aml_note"].id,
    ]
  }
}

# --- Registry: the promotion boundary between dev / stage / prod -------------
#
# Models are promoted by sharing an artifact across workspaces, never by
# re-training in the target environment.

resource "azapi_resource" "ml_registry" {
  type      = "Microsoft.MachineLearningServices/registries@2024-04-01"
  name      = "mlr-${var.prefix}"
  location  = var.location
  parent_id = azurerm_resource_group.main.id

  body = {
    identity = { type = "SystemAssigned" }
    properties = {
      publicNetworkAccess = "Disabled"
      regionDetails = [
        {
          location = var.location
          storageAccountDetails = [{
            systemCreatedStorageAccount = {
              storageAccountType = "Standard_ZRS"
            }
          }]
          acrDetails = [{
            systemCreatedAcrAccount = {
              acrAccountSku = "Premium"
            }
          }]
        }
      ]
    }
    tags = local.common_tags
  }
}

# --- Compute -----------------------------------------------------------------

resource "azurerm_machine_learning_compute_cluster" "train" {
  name                          = "gpu-train"
  location                      = azurerm_resource_group.main.location
  machine_learning_workspace_id = azurerm_machine_learning_workspace.main.id
  vm_priority                   = "Dedicated" # final runs are never preempted
  vm_size                       = var.gpu_train_sku
  subnet_resource_id            = azurerm_subnet.aml_compute.id

  scale_settings {
    min_node_count                       = 0
    max_node_count                       = var.gpu_train_max_nodes
    scale_down_nodes_after_idle_duration = "PT10M"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.train.id]
  }

  tags = local.common_tags
}

# Sweeps run low-priority: a preempted trial is cheap, and losing one is fine.
resource "azurerm_machine_learning_compute_cluster" "sweep" {
  name                          = "gpu-sweep"
  location                      = azurerm_resource_group.main.location
  machine_learning_workspace_id = azurerm_machine_learning_workspace.main.id
  vm_priority                   = "LowPriority"
  vm_size                       = var.gpu_train_sku
  subnet_resource_id            = azurerm_subnet.aml_compute.id

  scale_settings {
    min_node_count                       = 0
    max_node_count                       = var.gpu_train_max_nodes
    scale_down_nodes_after_idle_duration = "PT5M"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.train.id]
  }

  tags = local.common_tags
}

# Evaluation runs under a distinct identity: the only one with golden-set access.
resource "azurerm_machine_learning_compute_cluster" "eval" {
  name                          = "gpu-eval"
  location                      = azurerm_resource_group.main.location
  machine_learning_workspace_id = azurerm_machine_learning_workspace.main.id
  vm_priority                   = "Dedicated"
  vm_size                       = var.gpu_eval_sku
  subnet_resource_id            = azurerm_subnet.aml_compute.id

  scale_settings {
    min_node_count                       = 0
    max_node_count                       = 2
    scale_down_nodes_after_idle_duration = "PT10M"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.eval.id]
  }

  tags = local.common_tags
}

###############################################################################
# Simulation: Azure Batch spot pool running headless Isaac Sim
###############################################################################

resource "azurerm_batch_account" "sim" {
  name                = "bat${var.prefix}${var.environment}${random_string.sa.result}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  pool_allocation_mode = "BatchService"

  public_network_access_enabled = false

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.curation.id]
  }

  tags = local.common_tags
}

resource "azurerm_batch_pool" "sim" {
  name                = "isaac-sim"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_batch_account.sim.name
  display_name        = "Headless simulation renderers (spot)"
  vm_size             = var.sim_pool_sku
  node_agent_sku_id   = "batch.node.ubuntu 22.04"

  # Rendering is embarrassingly parallel and fully interruption tolerant.
  auto_scale {
    evaluation_interval = "PT5M"
    formula             = <<-EOT
      $pending = $PendingTasks.GetSample(1);
      $target  = min(${var.sim_pool_max_nodes}, $pending);
      $TargetLowPriorityNodes = $target;
      $TargetDedicatedNodes   = 0;
      $NodeDeallocationOption = "taskcompletion";
    EOT
  }

  storage_image_reference {
    publisher = "microsoft-azure-batch"
    offer     = "ubuntu-server-container"
    sku       = "22-04-lts"
    version   = "latest"
  }

  container_configuration {
    type = "DockerCompatible"
    container_registries {
      registry_server           = azurerm_container_registry.main.login_server
      user_assigned_identity_id = azurerm_user_assigned_identity.curation.id
    }
  }

  network_configuration {
    subnet_id                        = azurerm_subnet.batch_sim.id
    public_address_provisioning_type = "NoPublicIPAddresses"
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.curation.id]
  }
}

###############################################################################
# Key Vault access for signing and pipeline identities
###############################################################################

resource "azurerm_role_assignment" "aml_kv" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_machine_learning_workspace.main.identity[0].principal_id
}

# The HIL build runners sign bundles. They can use the key; they cannot read it.
resource "azurerm_role_assignment" "signing_key_user" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.rollout.principal_id
}

resource "azurerm_key_vault_key" "bundle_signing" {
  name         = "edge-bundle-signing"
  key_vault_id = azurerm_key_vault.main.id
  key_type     = "RSA-HSM"
  key_size     = 3072
  key_opts     = ["sign", "verify"]

  rotation_policy {
    expire_after         = "P2Y"
    notify_before_expiry = "P60D"
    automatic {
      time_before_expiry = "P90D"
    }
  }

  depends_on = [azurerm_role_assignment.aml_kv]
}

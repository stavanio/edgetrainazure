output "resource_group" {
  description = "Resource group holding the whole environment."
  value       = azurerm_resource_group.main.name
}

output "aml_workspace" {
  description = "Azure ML workspace name. Used by every `az ml` call in the Makefile."
  value       = azurerm_machine_learning_workspace.main.name
}

output "aml_registry" {
  description = "Cross-workspace ML registry. The promotion boundary between environments."
  value       = azapi_resource.ml_registry.name
}

output "lake_account" {
  description = "ADLS Gen2 account holding raw/clean/curated/labeled/snapshot."
  value       = azurerm_storage_account.lake.name
}

output "lake_dfs_endpoint" {
  description = "abfss:// endpoint for Databricks and AML datastores."
  value       = azurerm_storage_account.lake.primary_dfs_endpoint
}

output "golden_account" {
  description = "Golden-set account. Evaluation identity only; training has no role here."
  value       = azurerm_storage_account.golden.name
}

output "acr_login_server" {
  description = "ACR login server for edge bundles and training images."
  value       = azurerm_container_registry.main.login_server
}

output "iothub_name" {
  description = "IoT Hub carrying telemetry, twins, and rollout targeting."
  value       = azurerm_iothub.main.name
}

output "iothub_hostname" {
  description = "IoT Hub hostname devices connect to."
  value       = azurerm_iothub.main.hostname
}

output "dps_id_scope" {
  description = "DPS ID scope baked into robot provisioning config."
  value       = azurerm_iothub_dps.main.id_scope
}

output "key_vault_uri" {
  description = "Key Vault holding the HSM-backed bundle signing key."
  value       = azurerm_key_vault.main.vault_uri
}

output "signing_key_id" {
  description = "Versionless key ID used by Notation to sign edge bundles."
  value       = azurerm_key_vault_key.bundle_signing.versionless_id
}

output "log_analytics_workspace_id" {
  description = "Workspace ID for fleet health queries and rollback predicates."
  value       = azurerm_log_analytics_workspace.main.workspace_id
}

output "grafana_endpoint" {
  description = "Managed Grafana endpoint for the fleet and pipeline dashboards."
  value       = azurerm_dashboard_grafana.main.endpoint
}

output "batch_account" {
  description = "Batch account running the headless simulation pool."
  value       = azurerm_batch_account.sim.name
}

output "workload_identities" {
  description = "Client IDs of the per-workload managed identities."
  value = {
    train      = azurerm_user_assigned_identity.train.client_id
    eval       = azurerm_user_assigned_identity.eval.client_id
    curation   = azurerm_user_assigned_identity.curation.client_id
    rollout    = azurerm_user_assigned_identity.rollout.client_id
    sas_broker = azurerm_user_assigned_identity.sas_broker.client_id
  }
}

output "make_env" {
  description = "Paste into .env for the Makefile targets."
  value       = <<-EOT
    AZ_RESOURCE_GROUP=${azurerm_resource_group.main.name}
    AZ_WORKSPACE=${azurerm_machine_learning_workspace.main.name}
    AZ_REGISTRY=${azapi_resource.ml_registry.name}
    AZ_ACR=${azurerm_container_registry.main.login_server}
    AZ_IOTHUB=${azurerm_iothub.main.name}
    AZ_LAKE=${azurerm_storage_account.lake.name}
    AZ_KEYVAULT=${azurerm_key_vault.main.name}
    AZ_SIGNING_KEY=${azurerm_key_vault_key.bundle_signing.versionless_id}
  EOT
}

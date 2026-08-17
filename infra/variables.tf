variable "prefix" {
  description = "Short resource name prefix. Lowercase alphanumeric, <= 8 chars."
  type        = string
  default     = "edgefrg"

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.prefix))
    error_message = "prefix must be 3-8 lowercase alphanumeric characters."
  }
}

variable "environment" {
  description = "Deployment environment. Each environment is a separate subscription."
  type        = string

  validation {
    condition     = contains(["dev", "stage", "prod"], var.environment)
    error_message = "environment must be one of: dev, stage, prod."
  }
}

variable "location" {
  description = "Primary Azure region. Must have A100 quota and IoT Hub availability."
  type        = string
  default     = "eastus"
}

variable "acr_replica_locations" {
  description = "Regions to geo-replicate ACR into. Should track where robot sites are."
  type        = list(string)
  default     = ["westeurope", "australiaeast"]
}

variable "vnet_cidr" {
  description = "Address space for the spoke VNet."
  type        = string
  default     = "10.42.0.0/16"
}

variable "subnet_cidrs" {
  description = "Per-subnet CIDRs carved out of vnet_cidr."
  type        = map(string)
  default = {
    private_endpoints = "10.42.0.0/24"
    aml_compute       = "10.42.4.0/22"  # needs room; AML burns addresses per node
    databricks_public = "10.42.8.0/23"
    databricks_private= "10.42.10.0/23"
    batch_sim         = "10.42.12.0/22"
    functions         = "10.42.16.0/26"
  }
}

variable "gpu_train_sku" {
  description = "VM SKU for the training cluster."
  type        = string
  default     = "Standard_NC96ads_A100_v4"
}

variable "gpu_train_max_nodes" {
  description = "Max nodes in the training cluster. Guard against runaway sweeps."
  type        = number
  default     = 4
}

variable "gpu_eval_sku" {
  description = "VM SKU for the evaluation cluster. Smaller; eval is not distributed."
  type        = string
  default     = "Standard_NC24ads_A100_v4"
}

variable "sim_pool_sku" {
  description = "VM SKU for the Isaac Sim Batch pool. Rendering, not training."
  type        = string
  default     = "Standard_NV36ads_A10_v5"
}

variable "sim_pool_max_nodes" {
  description = "Max spot nodes in the simulation pool."
  type        = number
  default     = 20
}

variable "iothub_sku" {
  description = "IoT Hub SKU. S2 units carry telemetry only; payload goes direct to ADLS."
  type        = object({ name = string, capacity = number })
  default     = { name = "S2", capacity = 2 }
}

variable "raw_retention_years" {
  description = "Immutable retention on the /raw container. Evidence retention period."
  type        = number
  default     = 7
}

variable "log_retention_days" {
  description = "Log Analytics interactive retention. Long-term goes to immutable export."
  type        = number
  default     = 180
}

variable "admin_group_object_id" {
  description = "Entra ID group object ID for platform admins (PIM-eligible, no standing access)."
  type        = string
}

variable "annotator_group_object_id" {
  description = "Entra ID group object ID for the labeling workforce. Gets /curated read only."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

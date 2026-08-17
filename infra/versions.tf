terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.12"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.2"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state. Create once, out of band, with a customer-managed key.
  #   az group create -n rg-edgeforge-tfstate -l eastus
  #   az storage account create -n stedgeforgetfstate -g rg-edgeforge-tfstate --sku Standard_ZRS
  backend "azurerm" {
    resource_group_name  = "rg-edgeforge-tfstate"
    storage_account_name = "stedgeforgetfstate"
    container_name       = "tfstate"
    key                  = "edgeforge.tfstate"
    use_azuread_auth     = true
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy               = false
      purge_soft_deleted_keys_on_destroy         = false
      recover_soft_deleted_key_vaults            = true
    }
    resource_group {
      # Refuse to delete a resource group that still contains resources.
      prevent_deletion_if_contains_resources = true
    }
  }
  storage_use_azuread = true
}

provider "azapi" {}

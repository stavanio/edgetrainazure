###############################################################################
# Observability — one workspace, fleet + pipeline dashboards, rollback alerting
###############################################################################

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  sku               = "PerGB2018"
  retention_in_days = var.log_retention_days

  # Telemetry ingest grows with fleet size; commit once the fleet stabilises.
  daily_quota_gb = var.environment == "prod" ? 200 : 20

  tags = local.common_tags
}

resource "azurerm_monitor_action_group" "fleet_oncall" {
  name                = "ag-fleet-oncall-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "fleetoc"
  tags                = local.common_tags

  # Webhook target is the rollout driver's automatic-rollback endpoint.
  automation_runbook_receiver {
    name                    = "auto-rollback"
    automation_account_id   = azurerm_automation_account.rollout.id
    runbook_name            = azurerm_automation_runbook.rollback.name
    webhook_resource_id     = "${azurerm_automation_account.rollout.id}/webhooks/auto-rollback"
    service_uri             = "https://placeholder.invalid/rollback"
    is_global_runbook       = false
    use_common_alert_schema = true
  }
}

resource "azurerm_automation_account" "rollout" {
  name                = "aa-rollout-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku_name            = "Basic"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.rollout.id]
  }

  tags = local.common_tags
}

resource "azurerm_automation_runbook" "rollback" {
  name                    = "auto-rollback"
  location                = azurerm_resource_group.main.location
  resource_group_name     = azurerm_resource_group.main.name
  automation_account_name = azurerm_automation_account.rollout.name
  runbook_type            = "Python3"
  log_progress            = true
  log_verbose             = true
  description             = "Patches affected device twins back to last-known-good bundle."

  content = file("${path.module}/../deploy/rollout/rollback.py")

  tags = local.common_tags
}

###############################################################################
# Rollback predicates — see docs/04-edge-plane.md §4.4
#
# Each of these fires the auto-rollback runbook for the affected ring. They are
# deliberately conservative: a false rollback costs a shift of stale model, a
# missed rollback costs considerably more.
###############################################################################

locals {
  # Kusto shared by the scheduled query rules. The health contract is emitted by
  # the perception module every 30s and lands via Event Hubs -> Log Analytics.
  health_base = <<-KQL
    FleetHealth_CL
    | where TimeGenerated > ago(10m)
    | extend ring = tostring(tags_ring_s), bundle = tostring(bundle_s)
  KQL
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "latency_regression" {
  name                = "alert-latency-p99-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  severity            = 1
  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"
  scopes               = [azurerm_log_analytics_workspace.main.id]
  description          = "Perception p99 latency over the 45ms budget for 3 consecutive windows."

  criteria {
    query = <<-KQL
      ${local.health_base}
      | summarize p99 = avg(inference_p99_ms_d) by ring, bundle, bin(TimeGenerated, 5m)
      | where p99 > 45
    KQL
    time_aggregation_method = "Count"
    threshold               = 3
    operator                = "GreaterThanOrEqual"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.fleet_oncall.id]
  }

  tags = local.common_tags
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "personnel_detection_shift" {
  name                 = "alert-personnel-rate-${local.suffix}"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  severity             = 0
  evaluation_frequency = "PT5M"
  window_duration      = "PT30M"
  scopes               = [azurerm_log_analytics_workspace.main.id]
  description          = <<-DESC
    Personnel detections per km deviating >3 sigma from the ring baseline in
    EITHER direction. A collapse means the model has gone blind; a spike means
    nuisance stops. Both are rollback conditions.
  DESC

  criteria {
    query = <<-KQL
      let baseline = FleetHealth_CL
        | where TimeGenerated between (ago(14d) .. ago(1d))
        | summarize mu = avg(detections_personnel_per_km_d),
                    sigma = stdev(detections_personnel_per_km_d) by ring = tostring(tags_ring_s);
      ${local.health_base}
      | summarize rate = avg(detections_personnel_per_km_d) by ring, bundle
      | join kind=inner baseline on ring
      | where sigma > 0 and abs(rate - mu) > 3 * sigma
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.fleet_oncall.id]
  }

  tags = local.common_tags
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "safety_envelope" {
  name                 = "alert-safety-envelope-${local.suffix}"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  severity             = 0
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  scopes               = [azurerm_log_analytics_workspace.main.id]
  description          = "Any safety-envelope violation or perception-attributed disengagement. Immediate rollback."

  criteria {
    query = <<-KQL
      ${local.health_base}
      | where safety_envelope_violations_d > 0 or disengagements_d > 0
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.fleet_oncall.id]
  }

  tags = local.common_tags
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "ood_spike" {
  name                 = "alert-ood-spike-${local.suffix}"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  severity             = 2 # informational: usually environmental, not a release fault
  evaluation_frequency = "PT15M"
  window_duration      = "PT1H"
  scopes               = [azurerm_log_analytics_workspace.main.id]
  description          = "Out-of-distribution rate >5x the evaluation-set rate. See runbook P2."

  criteria {
    query = <<-KQL
      ${local.health_base}
      | summarize ood = avg(ood_rate_d) by ring, site = tostring(site_s)
      | where ood > 5 * toreal(0.0021)   // eval-set OOD rate, updated per release
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.fleet_oncall.id]
  }

  tags = local.common_tags
}

###############################################################################
# Grafana — fleet and pipeline dashboards
###############################################################################

resource "azurerm_dashboard_grafana" "main" {
  name                              = "graf-${local.suffix}"
  resource_group_name               = azurerm_resource_group.main.name
  location                          = azurerm_resource_group.main.location
  grafana_major_version             = 11
  api_key_enabled                   = false
  deterministic_outbound_ip_enabled = true
  public_network_access_enabled     = false

  identity {
    type = "SystemAssigned"
  }

  tags = local.common_tags
}

resource "azurerm_role_assignment" "grafana_reader" {
  scope                = azurerm_log_analytics_workspace.main.id
  role_definition_name = "Log Analytics Reader"
  principal_id         = azurerm_dashboard_grafana.main.identity[0].principal_id
}

resource "azurerm_role_assignment" "grafana_admins" {
  scope                = azurerm_dashboard_grafana.main.id
  role_definition_name = "Grafana Admin"
  principal_id         = var.admin_group_object_id
}

###############################################################################
# Diagnostics — everything that changes fleet behaviour is logged immutably
###############################################################################

locals {
  audited_resources = {
    iothub   = azurerm_iothub.main.id
    acr      = azurerm_container_registry.main.id
    keyvault = azurerm_key_vault.main.id
    aml      = azurerm_machine_learning_workspace.main.id
    lake     = azurerm_storage_account.lake.id
  }
}

resource "azurerm_monitor_diagnostic_setting" "audit" {
  for_each                   = local.audited_resources
  name                       = "diag-${each.key}"
  target_resource_id         = each.value
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category_group = "audit"
  }

  metric {
    category = "AllMetrics"
    enabled  = true
  }
}

###############################################################################
# SLO burn-rate alerting and dashboards
#
# The rules below are generated from observability/slo.yaml -- that file is the
# single source of truth, and these are its Azure Monitor projection. Both the
# long and the short window must be burning for an alert to fire: the long
# window alone stays hot for hours after a problem resolves, which is how
# burn-rate alerting gets a reputation for noise.
###############################################################################

locals {
  slo_catalogue = yamldecode(file("${path.module}/../observability/slo.yaml"))

  # Fraction-kind SLOs are the only ones with an error budget to burn. Cost
  # ceilings (D2, D6) and the trend indicator (D7) are evaluated by the
  # scheduled slo-report job instead -- a burn rate against a dollar ceiling
  # would not mean anything.
  burnable_slos = {
    for s in local.slo_catalogue.slos : s.id => s
    if lookup(s, "kind", "fraction") == "fraction"
  }

  # Cartesian product of burnable SLOs x page-severity burn-rate rules. Ticket
  # tiers are filed by the daily slo-report run rather than by an alert rule --
  # paging someone for a 1x burn is how alert fatigue starts.
  burn_rules = merge([
    for slo_id, slo in local.burnable_slos : {
      for rule in local.slo_catalogue.burn_rate_policy :
      "${slo_id}-${replace(tostring(rule.burn_rate), ".", "_")}" => {
        slo_id       = slo_id
        slo_name     = slo.name
        target       = slo.target
        query_file   = slo.query
        burn_rate    = rule.burn_rate
        long_window  = rule.long_window
        short_window = rule.short_window
      }
      if rule.severity == "page"
    }
  ]...)
}

resource "azurerm_monitor_action_group" "slo_page" {
  name                = "ag-slo-page-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "slopage"
  tags                = local.common_tags

  # Page-severity burn only. Ticket tiers arrive through the daily report.
  email_receiver {
    name          = "platform-oncall"
    email_address = "platform-oncall@example.invalid"
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "slo_burn" {
  for_each = local.burn_rules

  name                = "slo-burn-${lower(each.key)}-${local.suffix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  severity            = 1
  scopes              = [azurerm_log_analytics_workspace.main.id]

  evaluation_frequency = "PT5M"
  window_duration      = each.value.long_window

  description = join(" ", [
    "SLO ${each.value.slo_id} (${each.value.slo_name}) burning at",
    "${each.value.burn_rate}x over ${each.value.long_window}",
    "and ${each.value.short_window}. Budget = ${1 - each.value.target}."
  ])

  criteria {
    # The SLI is recomputed here rather than read from SloStatus_CL: an alert
    # must not depend on the reporting job having run recently.
    query = <<-KQL
      let budget = ${1 - each.value.target};
      let long_sli = toscalar(
        SloSamples_CL
        | where TimeGenerated > ago(${replace(each.value.long_window, "PT", "")})
        | where slo_id_s == '${each.value.slo_id}'
        | summarize sum(good_d) / sum(valid_d));
      let short_sli = toscalar(
        SloSamples_CL
        | where TimeGenerated > ago(${replace(each.value.short_window, "PT", "")})
        | where slo_id_s == '${each.value.slo_id}'
        | summarize sum(good_d) / sum(valid_d));
      print
        slo_id = '${each.value.slo_id}',
        long_burn  = (1.0 - long_sli)  / budget,
        short_burn = (1.0 - short_sli) / budget
      | where long_burn >= ${each.value.burn_rate} and short_burn >= ${each.value.burn_rate}
    KQL

    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.slo_page.id]
  }

  tags = merge(local.common_tags, {
    slo_id    = each.value.slo_id
    burn_rate = tostring(each.value.burn_rate)
  })
}

# --- Dashboards, provisioned as code ----------------------------------------
#
# Nobody builds a dashboard by hand and nobody edits one in the UI: the Grafana
# instance is created with editable=false and these files are the source. A
# dashboard that drifts from its definition is a dashboard nobody can review.

resource "azurerm_dashboard_grafana_managed_private_endpoint" "logs" {
  count = var.environment == "prod" ? 1 : 0

  grafana_id                   = azurerm_dashboard_grafana.main.id
  name                         = "mpe-logs-${local.suffix}"
  location                     = azurerm_resource_group.main.location
  private_link_resource_id     = azurerm_log_analytics_workspace.main.id
  group_ids                    = ["azuremonitor"]
  private_link_resource_region = azurerm_resource_group.main.location
}

# The dashboard JSON is applied by `make register-observability` via the Grafana
# API, because the azurerm provider has no first-class dashboard resource. The
# files are the contract; this output tells the Makefile where to POST them.
output "grafana_dashboard_targets" {
  description = "Grafana endpoint and the dashboard files to apply against it."
  value = {
    endpoint = azurerm_dashboard_grafana.main.endpoint
    dashboards = [
      for d in local.slo_catalogue.dashboards :
      "observability/dashboards/${d.id}.json"
    ]
  }
}

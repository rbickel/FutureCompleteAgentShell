# Deployment Plan: Application Insights Integration

Status: Implementing locally, not deployed

## Scope
- Add workspace-based Azure Application Insights to existing Bicep infrastructure.
- Configure the Azure App Service with `APPLICATIONINSIGHTS_CONNECTION_STRING` from the Application Insights resource.
- Instrument the Python M365/MAF agent so every inbound and outbound conversation exchange emits telemetry.
- Use the M365 conversation ID as the raw `correlation_id` / `conversation_id` custom dimension, and use a deterministic hash-derived trace ID for OpenTelemetry operation correlation.

## Architecture
- Existing host: Python aiohttp Microsoft 365 Agents Toolkit app deployed to Azure App Service.
- Telemetry backend: Azure Monitor Application Insights with Log Analytics workspace.
- Runtime instrumentation: `azure-monitor-opentelemetry` when `APPLICATIONINSIGHTS_CONNECTION_STRING` is configured; safe local fallback to standard logging when not configured.

## Files To Change
- `infra/azure.bicep`: add Log Analytics workspace, Application Insights resource, app settings, and outputs.
- `infra/azure.parameters.json`: add optional observability naming parameters if needed.
- `src/requirements.txt`: add Azure Monitor OpenTelemetry dependency.
- `src/telemetry.py`: new telemetry helper module.
- `src/agent.py`: emit conversation and proactive-notification telemetry.
- `m365agents.local.yml` and `m365agents.playground.yml`: pass optional local App Insights connection string into `.env`.
- Tests: add coverage for telemetry helper behavior and run existing suite.

## Validation
- Run unit tests with `python -m pytest`.
- Run offline evaluator with `python evals/futurecomplete_e2e_eval.py --json`.
- Run diagnostics on touched files.

## Deployment Notes
- Deployment is intentionally not executed here.
- Provisioning through the existing M365 Agents Toolkit/ARM flow will create the App Insights resource and populate App Service settings.

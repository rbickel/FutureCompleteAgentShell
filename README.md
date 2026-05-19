# Overview of the Basic Custom Engine Agent template

This app template is built on top of [Microsoft 365 Agents SDK](https://aka.ms/m365sdkdocs).
This template showcases a custom engine agent app that connects to your own LLM and responds to user questions like an AI assistant. This enables your users to talk with the AI assistant in Teams to find information.

## Get started with the template

> **Prerequisites**
>
> To run the template in your local dev machine, you will need:
>
> - [Python](https://www.python.org/), version 3.8 to 3.11.
> - [Python extension](https://code.visualstudio.com/docs/languages/python), version v2024.0.1 or higher.
> - [Microsoft 365 Agents Toolkit Visual Studio Code Extension](https://aka.ms/teams-toolkit) latest version or [Microsoft 365 Agents Toolkit CLI](https://aka.ms/teams-toolkit-cli).
> - An account with [Azure OpenAI](https://aka.ms/oai/access).
> - A [Microsoft 365 account for development](https://docs.microsoft.com/microsoftteams/platform/toolkit/accounts).

### Configurations

1. Open the command box and enter `Python: Create Environment` to create and activate your desired virtual environment. Remember to select `src/requirements.txt` as dependencies to install when creating the virtual environment.
1. In file *env/.env.local.user*, fill in your Azure OpenAI key `SECRET_AZURE_OPENAI_API_KEY`, deployment name `AZURE_OPENAI_DEPLOYMENT_NAME` and endpoint `AZURE_OPENAI_ENDPOINT`.

### Conversation with agent

1. Select the Microsoft 365 Agents Toolkit icon on the left in the VS Code toolbar.
1. In the Account section, sign in with your [Microsoft 365 account](https://docs.microsoft.com/microsoftteams/platform/toolkit/accounts) if you haven't already.
1. Press F5 to start debugging which launches your app in Teams using a web browser. Select `Debug in Teams (Edge)` or `Debug in Teams (Chrome)`.
1. When Teams launches in the browser, select the Add button in the dialog to install your app to Teams.
1. You will receive a welcome message from the agent, or send any message to get a response.

**Congratulations**! You are running an application that can now interact with users in Teams:

> For local debugging using Microsoft 365 Agents Toolkit CLI, you need to do some extra steps described in [Set up your Microsoft 365 Agents Toolkit CLI for local debugging](https://aka.ms/teamsfx-cli-debugging).

![ai chat agent](https://user-images.githubusercontent.com/7642967/258726187-8306610b-579e-4301-872b-1b5e85141eff.png)

### Tests and evaluations

Install development dependencies:

```powershell
pip install -r requirements-dev.txt
```

Run the unit tests:

```powershell
pytest
```

Run the offline end-to-end evaluation for the FutureComplete single-agent flow:

```powershell
python evals/futurecomplete_e2e_eval.py --json
```

Run the same evaluation with live public API checks:

```powershell
python evals/futurecomplete_e2e_eval.py --include-live-api --json
```

The tests and evaluations use `dataset_GKYZ_2016_AAPL_MSFT_trimmed.csv` as the sample input dataset. Backend calls are mocked by default, so the suite validates request shape, trial gating, job cancellation, and agent-flow behavior without calling production APIs. The optional live checks call `/health` and intentionally call `/v1/backtest` without a subscription key to verify the public server and authentication error contract without printing or requiring secrets. If `/health` is protected by API Management, `401` or `403` is treated as a successful reachability/auth validation.

During an interactive conversation, type `debug` at any point to enable debug mode for that session. When a FutureComplete API call runs in debug mode, the agent shows request and response method, URL, headers, and body values. Uploaded dataset contents are still omitted from debug output and replaced with column/count metadata. Trial subscription creation returns the generated subscription key so it can be reused outside Teams, such as in a notebook.

### Application Insights tracing

Azure deployments provision a workspace-based Application Insights resource and set `APPLICATIONINSIGHTS_CONNECTION_STRING` on the App Service. Local and playground runs can opt in by setting the same environment variable before starting the agent.

When Application Insights is configured, the agent also enables Microsoft Agent Framework OpenTelemetry instrumentation. This emits GenAI semantic-convention spans for the Application Insights Agents view, including `invoke_agent FutureCompleteAgent`, `chat <model>`, and `execute_tool <tool>` spans. The agent uses the stable OpenTelemetry agent ID `futurecomplete-agent` and name `FutureCompleteAgent`.

Relevant telemetry environment variables:

- `APPLICATIONINSIGHTS_CONNECTION_STRING`: exports telemetry to Application Insights.
- `APPLICATIONINSIGHTS_ROLE_NAME`: sets the App Insights cloud role name used by custom telemetry.
- `OTEL_SERVICE_NAME`: sets the OpenTelemetry service name; defaults to the role name.
- `ENABLE_INSTRUMENTATION`: enables Microsoft Agent Framework instrumentation; defaults to `true` when App Insights is configured.
- `ENABLE_SENSITIVE_DATA`: controls whether prompts, responses, tool arguments, and tool results are included in GenAI spans. Keep this `false` in production unless the data policy explicitly allows it.

The agent traces each conversation turn with these custom dimensions:

- `correlation_id`: the Microsoft 365 conversation ID.
- `conversation_id`: the Microsoft 365 conversation ID.
- `session_id`: the Microsoft Agent Framework session ID.
- `event_name`: for example `conversation.inbound`, `maf.request`, `conversation.outbound`, `futurecomplete.api.request`, or `futurecomplete.api.response`.
- `direction`: `inbound`, `outbound`, or `internal`.

Outbound FutureComplete HTTP calls and the MAF-backed Azure OpenAI call are emitted as OpenTelemetry client spans so they appear in the Application Insights `dependencies` table and Transaction details. FutureComplete dependencies use type `HTTP`; LLM dependencies use type `GenAI | azure_openai` when Azure Monitor GenAI tracing is enabled.

Use this KQL in Application Insights Logs to verify that agent-view telemetry is flowing:

```kusto
dependencies
| where timestamp > ago(30m)
| where name startswith "invoke_agent"
	or name startswith "chat"
	or name startswith "execute_tool"
	or customDimensions["gen_ai.operation.name"] in ("invoke_agent", "chat", "execute_tool")
| project timestamp, name, type, target, operation_Id, customDimensions
| order by timestamp desc
```

Use this KQL in Application Insights Logs to review one conversation:

```kusto
traces
| where customDimensions.correlation_id == "<conversation-id>"
| order by timestamp asc
| project timestamp, message, event_name = customDimensions.event_name, direction = customDimensions.direction, session_id = customDimensions.session_id
```

Use this KQL to review outbound dependencies for the same conversation:

```kusto
dependencies
| where customDimensions.correlation_id == "<conversation-id>"
| order by timestamp asc
| project timestamp, name, type, target, success, resultCode, session_id = customDimensions.session_id
```

OpenTelemetry spans use a deterministic trace ID derived from the conversation ID, while the raw conversation ID remains available as `customDimensions.correlation_id` for exact lookup.

## What's included in the template

| Folder        | Contents                                     |
|---------------|----------------------------------------------|
| `.vscode/`    | VS Code files for debugging                  |
| `appPackage/` | Templates for the Teams application manifest |
| `env/`        | Environment files                            |
| `infra/`      | Templates for provisioning Azure resources   |
| `src/`        | The source code for the application          |

The following files can be customized and demonstrate an example implementation to get you started.

| File            | Contents                                                              |
|-----------------|-----------------------------------------------------------------------|
| `src/agent.py`  | Handles the agent app logic, built with Microsoft 365 Agents SDK.     |
| `src/config.py` | Defines the environment variables.                                    |
| `src/app.py`    | Hosts the agent using aiohttp.                                        |

## Additional information and references

- [Microsoft 365 Agents Toolkit Documentations](https://docs.microsoft.com/microsoftteams/platform/toolkit/teams-toolkit-fundamentals)
- [Microsoft 365 Agents Toolkit CLI](https://aka.ms/teamsfx-toolkit-cli)
- [Microsoft 365 Agents Toolkit Samples](https://github.com/OfficeDev/TeamsFx-Samples)
- [Microsoft 365 Agents SDK](https://github.com/microsoft/Agents)
- [Microsoft 365 Agents for Python](https://github.com/microsoft/Agents-for-python)
- [Microsoft 365 Agents SDK QuickStart](https://github.com/microsoft/Agents/tree/main/samples/python/quickstart)

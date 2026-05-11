import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Annotated
from dotenv import load_dotenv
from pydantic import Field

from microsoft_agents.hosting.core import (
    AgentApplication,
    TurnState,
    TurnContext,
    MemoryStorage,
)
from microsoft_agents.activity import (
    load_configuration_from_env,
    ActivityTypes,
)
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.authentication.msal import MsalConnectionManager

from agent_framework import (
    AgentSession,
    FunctionInvocationContext,
    MCPStreamableHTTPTool,
    function_middleware,
    tool,
)
from agent_framework.openai import OpenAIChatClient

from config import Config

load_dotenv()

# Load configuration
config = Config(os.environ)
agents_sdk_config = load_configuration_from_env(os.environ)

system_prompt = (
    Path(__file__).parent / "agent.md"
).read_text(encoding="utf-8")

@tool(approval_mode="never_require")
def get_day_of_week() -> Annotated[str, Field(description="Today's day of the week (e.g. 'Monday').")]:
    """Return the current day of the week from the host OS clock."""
    return datetime.now().strftime("%A")

@tool(approval_mode="never_require")
def get_trial_key() -> Annotated[str, Field(description="Return a trial key.")]:
    """Return a trial key."""
    return "TRIAL_KEY"

mcp_server = MCPStreamableHTTPTool(
    name="Microsoft Learn MCP",
    url="https://learn.microsoft.com/api/mcp",
)


@function_middleware
async def inject_user_identity(context: FunctionInvocationContext, call_next):
    """Make the caller's identity available to every tool invocation.

    Tools can read it via `context.metadata["user_identity"]` (when invoked
    through the framework) or by importing `get_current_user(session_id)`.
    """
    session = context.session
    if session is not None:
        identity = _session_users.get(session.session_id)
        if identity is not None:
            # `metadata` is a Mapping on the dataclass; create a fresh dict
            # that merges any existing entries with our identity payload.
            merged = dict(context.metadata or {})
            merged["user_identity"] = identity
            context.metadata = merged
    await call_next()


def get_current_user(session_id: str) -> dict[str, str | None] | None:
    """Helper for tools that don't receive FunctionInvocationContext."""
    return _session_users.get(session_id)

# Build a Microsoft Agent Framework agent backed by Azure OpenAI's
# Responses API (required for gpt-5.x family). `model` is the Azure
# deployment name.
chat_client = OpenAIChatClient(
    model=config.azure_openai_deployment_name,
    api_key=config.azure_openai_api_key,
    azure_endpoint=config.azure_openai_endpoint,
    api_version="preview",
)

maf_agent = chat_client.as_agent(
    name="FutureCompleteAgent",
    instructions=system_prompt,
    tools=[get_day_of_week, get_trial_key, mcp_server],
    middleware=[inject_user_identity],
)

# Keep one MAF AgentSession per Bot Framework conversation so multi-turn
# context is preserved across messages.
_sessions: dict[str, AgentSession] = {}

# Per-session user identity captured from the channel activity. Keyed by
# session_id so tools/middleware can look up "who is calling" without
# touching TurnContext.
_session_users: dict[str, dict[str, str | None]] = {}


def _extract_user_identity(context: TurnContext) -> dict[str, str | None]:
    """Pull the current user's identity from the inbound activity.

    The values available depend on the channel:
      * Teams: aad_object_id + tenant_id are populated; email/UPN requires
        a Graph call or TeamsInfo.get_member().
      * Playground / emulator: aad_object_id is usually None.
    """
    activity = context.activity
    from_property = getattr(activity, "from_property", None)
    conversation = getattr(activity, "conversation", None)
    claims = context.identity  # bot/channel claims, not the user's

    return {
        "user_id": getattr(from_property, "id", None),
        "user_name": getattr(from_property, "name", None),
        "aad_object_id": getattr(from_property, "aad_object_id", None),
        "tenant_id": getattr(conversation, "tenant_id", None),
        "channel_id": getattr(activity, "channel_id", None),
        "caller_app_id": claims.get_app_id() if claims else None,
    }

# Define storage and application
storage = MemoryStorage()
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)

agent_app = AgentApplication[TurnState](
    storage=storage, 
    adapter=adapter, 
    **agents_sdk_config
)

# @agent_app.conversation_update("membersAdded")
# async def on_members_added(context: TurnContext, _state: TurnState):
#     await context.send_activity("Hi there! I'm an agent to chat with you.")

# Listen for ANY message to be received. MUST BE AFTER ANY OTHER MESSAGE HANDLERS
@agent_app.activity(ActivityTypes.message)
async def on_message(context: TurnContext, _state: TurnState):
    # Delegate the conversational turn to the Microsoft Agent Framework agent.
    conversation_id = context.activity.conversation.id
    session = _sessions.get(conversation_id)
    if session is None:
        session = AgentSession(session_id=conversation_id)
        _sessions[conversation_id] = session
        
    # Capture/refresh the caller's identity for this session and stash it
    # in the parallel _session_users store so tools/middleware can look up
    # "who is calling" by session_id.
    user_identity = _extract_user_identity(context)
    _session_users[session.session_id] = user_identity
    print(f"[session {session.session_id}] user identity: {user_identity}", file=sys.stderr)

    response = await maf_agent.run(context.activity.text or "", session=session)

    await context.send_activity(response.text)

@agent_app.error
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    # NOTE: In production environment, you should consider logging this to Azure
    #       application insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    # Send a message to the user
    await context.send_activity("The agent encountered an error or bug.")

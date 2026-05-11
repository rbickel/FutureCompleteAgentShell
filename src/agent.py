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

from agent_framework import AgentSession, tool, MCPStreamableHTTPTool
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
)

# Keep one MAF AgentSession per Bot Framework conversation so multi-turn
# context is preserved across messages.
_sessions: dict[str, AgentSession] = {}

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

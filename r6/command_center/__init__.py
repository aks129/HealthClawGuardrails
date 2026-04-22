"""
r6.command_center — The "My Health in Good Hands" command center.

Projects dashboard state from existing DB tables (AuditEventRecord, R6Resource,
FastenConnection, WearableConnection, etc.) plus live probes (OpenClaw gateway,
MCP server, Flask health). Defines a small agent registry so actions can be
attributed to named health personas (Health Advisor, Fitness & Dietician, etc.).
"""

from r6.command_center.models import ConversationMessage, AgentTask
from r6.command_center.agents import load_agents, get_agent, agent_for_tool
from r6.command_center.routes import command_center_blueprint

__all__ = [
    "ConversationMessage",
    "AgentTask",
    "load_agents",
    "get_agent",
    "agent_for_tool",
    "command_center_blueprint",
]

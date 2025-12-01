"""
MCP 桥接插件
将 MCP (Model Context Protocol) 服务器的工具桥接到 MaiBot
"""

from .plugin import MCPBridgePlugin, mcp_tool_registry, MCPStartupHandler, MCPStopHandler
from .mcp_client import mcp_manager, MCPClientManager, MCPServerConfig, TransportType

__all__ = [
    "MCPBridgePlugin",
    "mcp_tool_registry",
    "mcp_manager",
    "MCPClientManager",
    "MCPServerConfig",
    "TransportType",
    "MCPStartupHandler",
    "MCPStopHandler",
]

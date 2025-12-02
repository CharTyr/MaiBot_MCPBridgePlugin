"""
MCP 桥接插件
将 MCP (Model Context Protocol) 服务器的工具桥接到 MaiBot
"""

import asyncio
from typing import Any, Dict, List, Optional, Tuple, Type

from src.common.logger import get_logger
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseTool,
    BaseCommand,
    ComponentInfo,
    ConfigField,
    ToolParamType,
)
from src.plugin_system.base.component_types import ToolInfo, CommandInfo, ComponentType, EventHandlerInfo, EventType
from src.plugin_system.base.base_events_handler import BaseEventHandler

from .mcp_client import (
    MCPClientManager,
    MCPServerConfig,
    MCPToolInfo,
    TransportType,
    mcp_manager,
)

logger = get_logger("mcp_bridge_plugin")


def convert_json_type_to_tool_param_type(json_type: str) -> ToolParamType:
    """将 JSON Schema 类型转换为 MaiBot 的 ToolParamType
    
    MaiBot 支持的类型: STRING, INTEGER, FLOAT, BOOLEAN
    对于不支持的类型（array, object 等），转换为 STRING 并在描述中说明
    """
    type_mapping = {
        "string": ToolParamType.STRING,
        "integer": ToolParamType.INTEGER,
        "number": ToolParamType.FLOAT,  # JSON number 对应 FLOAT
        "boolean": ToolParamType.BOOLEAN,
        # array 和 object 不被 MaiBot 原生支持，转为 STRING（JSON 字符串形式）
        "array": ToolParamType.STRING,
        "object": ToolParamType.STRING,
    }
    return type_mapping.get(json_type, ToolParamType.STRING)


def parse_mcp_parameters(input_schema: Dict[str, Any]) -> List[Tuple[str, ToolParamType, str, bool, Optional[List[str]]]]:
    """解析 MCP 工具的参数 schema，转换为 MaiBot 的参数格式"""
    parameters = []
    
    if not input_schema:
        return parameters
    
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    
    for param_name, param_info in properties.items():
        json_type = param_info.get("type", "string")
        param_type = convert_json_type_to_tool_param_type(json_type)
        description = param_info.get("description", f"参数 {param_name}")
        
        # 对于复杂类型，在描述中添加说明
        if json_type == "array":
            description = f"{description} (JSON 数组格式)"
        elif json_type == "object":
            description = f"{description} (JSON 对象格式)"
        
        is_required = param_name in required
        enum_values = param_info.get("enum")
        
        # 确保 enum_values 是字符串列表
        if enum_values is not None:
            enum_values = [str(v) for v in enum_values]
        
        parameters.append((
            param_name,
            param_type,
            description,
            is_required,
            enum_values
        ))
    
    return parameters


class MCPToolProxy(BaseTool):
    """MCP 工具代理基类
    
    每个 MCP 工具都会动态创建一个继承此类的子类，
    子类会设置具体的 name、description、parameters 等属性
    """
    
    # 这些属性会被动态子类覆盖
    name: str = ""
    description: str = ""
    parameters: List[Tuple[str, ToolParamType, str, bool, Optional[List[str]]]] = []
    available_for_llm: bool = True
    
    # MCP 相关属性
    _mcp_tool_key: str = ""  # 在 mcp_manager 中的工具键
    _mcp_original_name: str = ""  # MCP 服务器中的原始工具名
    _mcp_server_name: str = ""  # MCP 服务器名称
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """执行 MCP 工具调用"""
        import json
        
        # 移除 MaiBot 内部添加的标记
        args = {k: v for k, v in function_args.items() if k != "llm_called"}
        
        # 尝试解析 JSON 字符串参数（用于 array/object 类型）
        parsed_args = {}
        for key, value in args.items():
            if isinstance(value, str):
                # 尝试解析为 JSON
                try:
                    if value.startswith(('[', '{')):
                        parsed_args[key] = json.loads(value)
                    else:
                        parsed_args[key] = value
                except json.JSONDecodeError:
                    parsed_args[key] = value
            else:
                parsed_args[key] = value
        
        logger.debug(f"调用 MCP 工具: {self._mcp_tool_key}, 参数: {parsed_args}")
        
        result = await mcp_manager.call_tool(self._mcp_tool_key, parsed_args)
        
        if result.success:
            return {
                "name": self.name,
                "content": result.content
            }
        else:
            # 友好的错误提示
            error_msg = self._format_error_message(result.error, result.duration_ms)
            logger.warning(f"MCP 工具 {self.name} 调用失败: {result.error}")
            return {
                "name": self.name,
                "content": error_msg
            }
    
    def _format_error_message(self, error: str, duration_ms: float) -> str:
        """格式化友好的错误消息"""
        if not error:
            return "工具调用失败（未知错误）"
        
        error_lower = error.lower()
        
        # 连接相关错误
        if "未连接" in error or "not connected" in error_lower:
            return f"⚠️ MCP 服务器 [{self._mcp_server_name}] 未连接，请检查服务器状态或等待自动重连"
        
        # 超时错误
        if "超时" in error or "timeout" in error_lower:
            return f"⏱️ 工具调用超时（耗时 {duration_ms:.0f}ms），服务器响应过慢，请稍后重试"
        
        # 连接断开
        if "connection" in error_lower and ("closed" in error_lower or "reset" in error_lower):
            return f"🔌 与 MCP 服务器 [{self._mcp_server_name}] 的连接已断开，正在尝试重连..."
        
        # 参数错误
        if "invalid" in error_lower and "argument" in error_lower:
            return f"❌ 参数错误: {error}"
        
        # 其他错误
        return f"❌ 工具调用失败: {error}"
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        """直接执行（供其他插件调用）"""
        return await self.execute(function_args)


def create_mcp_tool_class(
    tool_key: str,
    tool_info: MCPToolInfo,
    tool_prefix: str
) -> Type[MCPToolProxy]:
    """根据 MCP 工具信息动态创建 BaseTool 子类"""
    # 解析参数
    parameters = parse_mcp_parameters(tool_info.input_schema)
    
    # 生成类名（确保是有效的 Python 标识符）
    class_name = f"MCPTool_{tool_info.server_name}_{tool_info.name}".replace("-", "_").replace(".", "_")
    
    # 生成工具名称（用于 LLM 识别）
    tool_name = tool_key.replace("-", "_").replace(".", "_")
    
    # 生成描述
    description = tool_info.description
    if not description.endswith(f"[来自 MCP 服务器: {tool_info.server_name}]"):
        description = f"{description} [来自 MCP 服务器: {tool_info.server_name}]"
    
    # 动态创建类
    tool_class = type(
        class_name,
        (MCPToolProxy,),
        {
            "name": tool_name,
            "description": description,
            "parameters": parameters,
            "available_for_llm": True,
            "_mcp_tool_key": tool_key,
            "_mcp_original_name": tool_info.name,
            "_mcp_server_name": tool_info.server_name,
        }
    )
    
    return tool_class


class MCPToolRegistry:
    """MCP 工具注册表，管理动态创建的工具类"""
    
    def __init__(self):
        self._tool_classes: Dict[str, Type[MCPToolProxy]] = {}
        self._tool_infos: Dict[str, ToolInfo] = {}
    
    def register_tool(self, tool_key: str, tool_info: MCPToolInfo, tool_prefix: str) -> Tuple[ToolInfo, Type[MCPToolProxy]]:
        """注册 MCP 工具，返回组件信息和工具类"""
        tool_class = create_mcp_tool_class(tool_key, tool_info, tool_prefix)
        
        self._tool_classes[tool_key] = tool_class
        
        # 创建 ToolInfo
        info = ToolInfo(
            name=tool_class.name,
            tool_description=tool_class.description,
            enabled=True,
            tool_parameters=tool_class.parameters,
            component_type=ComponentType.TOOL,
        )
        self._tool_infos[tool_key] = info
        
        return info, tool_class
    
    def unregister_tool(self, tool_key: str) -> bool:
        """注销工具"""
        if tool_key in self._tool_classes:
            del self._tool_classes[tool_key]
            del self._tool_infos[tool_key]
            return True
        return False
    
    def get_all_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """获取所有工具组件"""
        return [
            (self._tool_infos[key], self._tool_classes[key])
            for key in self._tool_classes.keys()
        ]
    
    def clear(self) -> None:
        """清空所有注册"""
        self._tool_classes.clear()
        self._tool_infos.clear()


# 全局工具注册表
mcp_tool_registry = MCPToolRegistry()

# 全局插件实例引用（用于事件处理器访问）
_plugin_instance: Optional["MCPBridgePlugin"] = None


class MCPStatusTool(BaseTool):
    """MCP 状态查询工具 - 查看 MCP 服务器连接状态和调用统计"""
    
    name = "mcp_status"
    description = "查询 MCP 桥接插件的状态，包括服务器连接状态、可用工具列表、调用统计等信息"
    parameters = [
        ("query_type", ToolParamType.STRING, "查询类型", False, ["status", "tools", "stats", "all"]),
        ("server_name", ToolParamType.STRING, "指定服务器名称（可选，不指定则查询所有）", False, None),
    ]
    available_for_llm = True
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """执行状态查询"""
        query_type = function_args.get("query_type", "status")
        server_name = function_args.get("server_name")
        
        result_parts = []
        
        if query_type in ("status", "all"):
            result_parts.append(self._format_status(server_name))
        
        if query_type in ("tools", "all"):
            result_parts.append(self._format_tools(server_name))
        
        if query_type in ("stats", "all"):
            result_parts.append(self._format_stats(server_name))
        
        return {
            "name": self.name,
            "content": "\n\n".join(result_parts) if result_parts else "未知的查询类型"
        }
    
    def _format_status(self, server_name: Optional[str] = None) -> str:
        """格式化状态信息"""
        status = mcp_manager.get_status()
        lines = ["📊 MCP 桥接插件状态"]
        lines.append(f"  总服务器数: {status['total_servers']}")
        lines.append(f"  已连接: {status['connected_servers']}")
        lines.append(f"  已断开: {status['disconnected_servers']}")
        lines.append(f"  可用工具数: {status['total_tools']}")
        lines.append(f"  心跳检测: {'运行中' if status['heartbeat_running'] else '已停止'}")
        
        lines.append("\n🔌 服务器详情:")
        for name, info in status['servers'].items():
            if server_name and name != server_name:
                continue
            status_icon = "✅" if info['connected'] else "❌"
            enabled_text = "" if info['enabled'] else " (已禁用)"
            lines.append(f"  {status_icon} {name}{enabled_text}")
            lines.append(f"     传输: {info['transport']}, 工具数: {info['tools_count']}")
            if info['consecutive_failures'] > 0:
                lines.append(f"     ⚠️ 连续失败: {info['consecutive_failures']} 次")
        
        return "\n".join(lines)
    
    def _format_tools(self, server_name: Optional[str] = None) -> str:
        """格式化工具列表"""
        tools = mcp_manager.all_tools
        lines = ["🔧 可用 MCP 工具"]
        
        # 按服务器分组
        by_server: Dict[str, List[str]] = {}
        for tool_key, (tool_info, _) in tools.items():
            if server_name and tool_info.server_name != server_name:
                continue
            if tool_info.server_name not in by_server:
                by_server[tool_info.server_name] = []
            by_server[tool_info.server_name].append(f"  • {tool_key}: {tool_info.description[:50]}...")
        
        for srv_name, tool_list in by_server.items():
            lines.append(f"\n📦 {srv_name} ({len(tool_list)} 个工具):")
            lines.extend(tool_list)
        
        if not by_server:
            lines.append("  (无可用工具)")
        
        return "\n".join(lines)
    
    def _format_stats(self, server_name: Optional[str] = None) -> str:
        """格式化统计信息"""
        stats = mcp_manager.get_all_stats()
        lines = ["📈 调用统计"]
        
        # 全局统计
        g = stats['global']
        lines.append(f"  总调用次数: {g['total_tool_calls']}")
        lines.append(f"  成功: {g['successful_calls']}, 失败: {g['failed_calls']}")
        if g['total_tool_calls'] > 0:
            success_rate = (g['successful_calls'] / g['total_tool_calls']) * 100
            lines.append(f"  成功率: {success_rate:.1f}%")
        lines.append(f"  运行时间: {g['uptime_seconds']:.0f} 秒")
        lines.append(f"  调用频率: {g['calls_per_minute']:.2f} 次/分钟")
        
        # 工具统计
        tool_stats = stats.get('tools', {})
        if tool_stats:
            lines.append("\n🔧 工具调用详情:")
            for tool_key, ts in tool_stats.items():
                if server_name and not tool_key.startswith(f"mcp_{server_name}_"):
                    continue
                if ts['total_calls'] > 0:
                    lines.append(f"  • {tool_key}")
                    lines.append(f"    调用: {ts['total_calls']} 次, 成功率: {ts['success_rate']}%")
                    lines.append(f"    平均耗时: {ts['avg_duration_ms']:.0f}ms")
                    if ts['last_error']:
                        lines.append(f"    最近错误: {ts['last_error'][:50]}...")
        
        return "\n".join(lines)
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        return await self.execute(function_args)


class MCPStatusCommand(BaseCommand):
    """MCP 状态查询命令 - 通过 /mcp 命令查看服务器状态"""

    command_name = "mcp_status_command"
    command_description = "查看 MCP 服务器连接状态和统计信息"
    command_pattern = r"^[/／]mcp(?:\s+(?P<subcommand>status|tools|stats|reconnect))?(?:\s+(?P<server>\S+))?$"

    async def execute(self):
        """执行命令"""
        subcommand = self.matched_groups.get("subcommand", "status") or "status"
        server_name = self.matched_groups.get("server")

        if subcommand == "reconnect":
            # 重连指定服务器或所有服务器
            return await self._handle_reconnect(server_name)

        # 查询状态
        result = self._format_output(subcommand, server_name)
        await self.send_text(result)
        return (True, None, True)

    async def _handle_reconnect(self, server_name: str = None):
        """处理重连请求"""
        if server_name:
            # 重连指定服务器
            if server_name not in mcp_manager._clients:
                await self.send_text(f"❌ 服务器 {server_name} 不存在")
                return (True, None, True)

            await self.send_text(f"🔄 正在重连服务器 {server_name}...")
            success = await mcp_manager.reconnect_server(server_name)
            if success:
                await self.send_text(f"✅ 服务器 {server_name} 重连成功")
            else:
                await self.send_text(f"❌ 服务器 {server_name} 重连失败")
        else:
            # 重连所有断开的服务器
            disconnected = mcp_manager.disconnected_servers
            if not disconnected:
                await self.send_text("✅ 所有服务器都已连接")
                return (True, None, True)

            await self.send_text(f"🔄 正在重连 {len(disconnected)} 个断开的服务器...")
            for srv in disconnected:
                success = await mcp_manager.reconnect_server(srv)
                status = "✅" if success else "❌"
                await self.send_text(f"{status} {srv}")

        return (True, None, True)

    def _format_output(self, subcommand: str, server_name: str = None) -> str:
        """格式化输出"""
        status = mcp_manager.get_status()
        stats = mcp_manager.get_all_stats()
        lines = []

        if subcommand in ("status", "all"):
            lines.append("📊 MCP 桥接插件状态")
            lines.append(f"├ 服务器: {status['connected_servers']}/{status['total_servers']} 已连接")
            lines.append(f"├ 工具数: {status['total_tools']}")
            lines.append(f"└ 心跳: {'运行中' if status['heartbeat_running'] else '已停止'}")

            if status["servers"]:
                lines.append("\n🔌 服务器列表:")
                for name, info in status["servers"].items():
                    if server_name and name != server_name:
                        continue
                    icon = "✅" if info["connected"] else "❌"
                    enabled = "" if info["enabled"] else " (禁用)"
                    lines.append(f"  {icon} {name}{enabled}")
                    lines.append(f"     {info['transport']} | {info['tools_count']} 工具")
                    if info["consecutive_failures"] > 0:
                        lines.append(f"     ⚠️ 连续失败 {info['consecutive_failures']} 次")

        if subcommand in ("tools", "all"):
            tools = mcp_manager.all_tools
            if tools:
                lines.append("\n🔧 可用工具:")
                by_server = {}
                for key, (info, _) in tools.items():
                    if server_name and info.server_name != server_name:
                        continue
                    by_server.setdefault(info.server_name, []).append(info.name)

                for srv, tool_list in by_server.items():
                    lines.append(f"  📦 {srv} ({len(tool_list)})")
                    for t in tool_list[:5]:  # 最多显示5个
                        lines.append(f"     • {t}")
                    if len(tool_list) > 5:
                        lines.append(f"     ... 还有 {len(tool_list) - 5} 个")

        if subcommand in ("stats", "all"):
            g = stats["global"]
            lines.append("\n📈 调用统计:")
            lines.append(f"  总调用: {g['total_tool_calls']}")
            if g["total_tool_calls"] > 0:
                rate = (g["successful_calls"] / g["total_tool_calls"]) * 100
                lines.append(f"  成功率: {rate:.1f}%")
            lines.append(f"  运行: {g['uptime_seconds']:.0f}秒")

        if not lines:
            lines.append("使用方法: /mcp [status|tools|stats|reconnect] [服务器名]")

        return "\n".join(lines)


class MCPStartupHandler(BaseEventHandler):
    """MCP 启动事件处理器
    
    在 MaiBot 启动完成后（ON_START 事件）异步连接 MCP 服务器并启动心跳检测
    """
    
    event_type = EventType.ON_START
    handler_name = "mcp_startup_handler"
    handler_description = "MCP 桥接插件启动处理器"
    weight = 0
    intercept_message = False
    
    async def execute(self, message):
        """处理启动事件"""
        global _plugin_instance
        
        if _plugin_instance is None:
            logger.warning("MCP 桥接插件实例未初始化")
            return (False, True, None, None, None)
        
        logger.info("MCP 桥接插件收到 ON_START 事件，开始连接 MCP 服务器...")
        await _plugin_instance._async_connect_servers()
        
        # 启动心跳检测
        await mcp_manager.start_heartbeat()
        
        return (True, True, None, None, None)


class MCPStopHandler(BaseEventHandler):
    """MCP 停止事件处理器
    
    在 MaiBot 停止时（ON_STOP 事件）关闭所有 MCP 连接和心跳检测
    """
    
    event_type = EventType.ON_STOP
    handler_name = "mcp_stop_handler"
    handler_description = "MCP 桥接插件停止处理器"
    weight = 0
    intercept_message = False
    
    async def execute(self, message):
        """处理停止事件"""
        logger.info("MCP 桥接插件收到 ON_STOP 事件，正在关闭...")
        
        # shutdown 会自动停止心跳检测
        await mcp_manager.shutdown()
        mcp_tool_registry.clear()
        
        logger.info("MCP 桥接插件已关闭所有连接")
        return (True, True, None, None, None)


@register_plugin
class MCPBridgePlugin(BasePlugin):
    """MCP 桥接插件 - 将 MCP 服务器的工具桥接到 MaiBot"""
    
    # 插件基本信息
    plugin_name: str = "mcp_bridge_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["mcp"]
    config_file_name: str = "config.toml"
    
    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本信息",
        "settings": "全局设置",
        "servers": "MCP 服务器配置（支持多个服务器）",
        "status": "运行状态（只读）",
    }
    
    # 配置 Schema 定义
    # 注意: plugin section 中只保留 enabled，其他字段不在 schema 中定义
    # 这样 WebUI 就不会显示 name/version/config_version
    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="是否启用插件",
                label="启用插件",
            ),
        },
        "settings": {
            "tool_prefix": ConfigField(
                type=str,
                default="mcp",
                description="🏷️ 工具前缀 - 用于区分 MCP 工具和原生工具，生成的工具名格式: {前缀}_{服务器名}_{工具名}",
                label="🏷️ 工具前缀",
                placeholder="mcp",
                order=1,
            ),
            "connect_timeout": ConfigField(
                type=float,
                default=30.0,
                description="⏱️ 连接超时 - 连接 MCP 服务器的超时时间（秒）",
                label="⏱️ 连接超时（秒）",
                min=5.0,
                max=120.0,
                step=5.0,
                order=2,
            ),
            "call_timeout": ConfigField(
                type=float,
                default=60.0,
                description="⏱️ 调用超时 - 工具调用的超时时间（秒）",
                label="⏱️ 调用超时（秒）",
                min=10.0,
                max=300.0,
                step=10.0,
                order=3,
            ),
            "auto_connect": ConfigField(
                type=bool,
                default=True,
                description="🔄 自动连接 - 启动时自动连接所有已启用的服务器",
                label="🔄 自动连接",
                order=4,
            ),
            "retry_attempts": ConfigField(
                type=int,
                default=3,
                description="🔁 重试次数 - 连接失败时的重试次数",
                label="🔁 重试次数",
                min=0,
                max=10,
                order=5,
            ),
            "retry_interval": ConfigField(
                type=float,
                default=5.0,
                description="⏳ 重试间隔 - 重试之间的等待时间（秒）",
                label="⏳ 重试间隔（秒）",
                min=1.0,
                max=60.0,
                step=1.0,
                order=6,
            ),
            "heartbeat_enabled": ConfigField(
                type=bool,
                default=True,
                description="💓 心跳检测 - 定期检测服务器连接状态",
                label="💓 启用心跳检测",
                order=7,
            ),
            "heartbeat_interval": ConfigField(
                type=float,
                default=60.0,
                description="💓 心跳间隔 - 心跳检测的间隔时间（秒）",
                label="💓 心跳间隔（秒）",
                min=10.0,
                max=300.0,
                step=10.0,
                order=8,
            ),
            "auto_reconnect": ConfigField(
                type=bool,
                default=True,
                description="🔄 自动重连 - 检测到断开时自动尝试重连",
                label="🔄 自动重连",
                order=9,
            ),
            "max_reconnect_attempts": ConfigField(
                type=int,
                default=3,
                description="🔄 最大重连次数 - 连续重连失败后暂停重连",
                label="🔄 最大重连次数",
                min=1,
                max=10,
                order=10,
            ),
        },
        "servers": {
            "list": ConfigField(
                type=str,
                default="[]",
                description="MCP 服务器列表配置（JSON 格式）",
                label="🔌 服务器列表",
                input_type="textarea",
                placeholder='''[
  {
    "name": "howtocook",
    "enabled": true,
    "transport": "http",
    "url": "https://mcp.example.com/mcp"
  }
]''',
                hint="JSON 数组格式。字段: name(名称), enabled(启用), transport(stdio/sse/http), url(地址), command/args/env(stdio专用)",
                rows=12,
                order=1,
            ),
        },
        "status": {
            "connection_status": ConfigField(
                type=str,
                default="未初始化",
                description="当前 MCP 服务器连接状态",
                label="📊 连接状态",
                input_type="textarea",
                disabled=True,
                rows=8,
                hint="此状态仅在插件启动时更新。查询实时状态请发送 /mcp 命令",
                order=1,
            ),
        },
    }
    
    def __init__(self, *args, **kwargs):
        global _plugin_instance
        super().__init__(*args, **kwargs)
        self._initialized = False
        _plugin_instance = self
        
        # 配置 MCP 管理器
        settings = self.config.get("settings", {})
        mcp_manager.configure(settings)
    
    async def _async_connect_servers(self) -> None:
        """异步连接所有配置的 MCP 服务器"""
        import json
        
        settings = self.config.get("settings", {})
        
        # 支持多种配置格式:
        # 1. TOML 数组格式: [[servers]] (直接是列表)
        # 2. WebUI JSON 格式: [servers] list = [...] (嵌套在 servers.list 中)
        # 3. WebUI 字符串格式: [servers] list = "..." (JSON 字符串)
        servers_section = self.config.get("servers", [])
        
        if isinstance(servers_section, dict):
            # WebUI 格式
            servers_list = servers_section.get("list", [])
            if isinstance(servers_list, str):
                # JSON 字符串格式，需要解析
                try:
                    servers_config = json.loads(servers_list) if servers_list.strip() else []
                except json.JSONDecodeError as e:
                    logger.error(f"解析服务器配置 JSON 失败: {e}")
                    servers_config = []
            else:
                servers_config = servers_list
        else:
            # TOML 数组格式
            servers_config = servers_section
        
        if not servers_config:
            logger.warning("未配置任何 MCP 服务器")
            self._initialized = True
            return
        
        auto_connect = settings.get("auto_connect", True)
        if not auto_connect:
            logger.info("auto_connect 已禁用，跳过自动连接")
            self._initialized = True
            return
        
        tool_prefix = settings.get("tool_prefix", "mcp")
        registered_count = 0
        
        for server_conf in servers_config:
            if not server_conf.get("enabled", True):
                logger.info(f"服务器 {server_conf.get('name', 'unknown')} 已禁用，跳过")
                continue
            
            # 解析服务器配置
            try:
                config = self._parse_server_config(server_conf)
            except Exception as e:
                logger.error(f"解析服务器配置失败: {e}")
                continue
            
            # 添加服务器
            success = await mcp_manager.add_server(config)
            if not success:
                logger.warning(f"服务器 {config.name} 连接失败")
                continue
            
            # 动态注册工具到组件系统
            from src.plugin_system.core.component_registry import component_registry
            
            for tool_key, (tool_info, _) in mcp_manager.all_tools.items():
                if tool_info.server_name == config.name:
                    info, tool_class = mcp_tool_registry.register_tool(
                        tool_key, tool_info, tool_prefix
                    )
                    # 设置插件名称
                    info.plugin_name = self.plugin_name
                    
                    # 动态注册到组件系统
                    if component_registry.register_component(info, tool_class):
                        registered_count += 1
                        logger.info(f"✅ 注册 MCP 工具: {tool_class.name}")
                    else:
                        logger.warning(f"❌ 注册 MCP 工具失败: {tool_class.name}")
        
        self._initialized = True
        logger.info(f"MCP 桥接插件初始化完成，已注册 {registered_count} 个工具")
        
        # 更新状态显示
        self._update_status_display()
    
    def _parse_server_config(self, conf: Dict) -> MCPServerConfig:
        """解析服务器配置字典"""
        transport_str = conf.get("transport", "stdio").lower()
        
        # 支持所有传输类型
        transport_map = {
            "stdio": TransportType.STDIO,
            "sse": TransportType.SSE,
            "http": TransportType.HTTP,
            "streamable_http": TransportType.STREAMABLE_HTTP,
        }
        transport = transport_map.get(transport_str, TransportType.STDIO)
        
        return MCPServerConfig(
            name=conf.get("name", "unnamed"),
            enabled=conf.get("enabled", True),
            transport=transport,
            command=conf.get("command", ""),
            args=conf.get("args", []),
            env=conf.get("env", {}),
            url=conf.get("url", ""),
        )
    
    def _update_status_display(self) -> None:
        """更新配置中的状态显示字段"""
        status = mcp_manager.get_status()
        lines = []
        
        # 概览
        lines.append(f"服务器: {status['connected_servers']}/{status['total_servers']} 已连接")
        lines.append(f"工具数: {status['total_tools']}")
        lines.append(f"心跳: {'运行中' if status['heartbeat_running'] else '已停止'}")
        lines.append("")
        
        # 服务器详情
        for name, info in status.get("servers", {}).items():
            icon = "✅" if info["connected"] else "❌"
            lines.append(f"{icon} {name} ({info['transport']}) - {info['tools_count']} 工具")
        
        if not status.get("servers"):
            lines.append("(无服务器)")
        
        # 更新配置
        if "status" not in self.config:
            self.config["status"] = {}
        self.config["status"]["connection_status"] = "\n".join(lines)
    
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件的所有组件
        
        返回事件处理器、命令和内置工具，MCP 工具会在 ON_START 事件后动态注册
        """
        components: List[Tuple[ComponentInfo, Type]] = []
        
        # 添加启动事件处理器
        startup_handler_info = MCPStartupHandler.get_handler_info()
        components.append((startup_handler_info, MCPStartupHandler))
        
        # 添加停止事件处理器
        stop_handler_info = MCPStopHandler.get_handler_info()
        components.append((stop_handler_info, MCPStopHandler))
        
        # 添加 /mcp 状态查询命令
        mcp_command_info = MCPStatusCommand.get_command_info()
        components.append((mcp_command_info, MCPStatusCommand))
        
        # 添加内置状态查询工具（供 LLM 调用）
        status_tool_info = ToolInfo(
            name=MCPStatusTool.name,
            tool_description=MCPStatusTool.description,
            enabled=True,
            tool_parameters=MCPStatusTool.parameters,
            component_type=ComponentType.TOOL,
        )
        components.append((status_tool_info, MCPStatusTool))
        
        return components
    
    def get_status(self) -> Dict[str, Any]:
        """获取插件状态"""
        return {
            "initialized": self._initialized,
            "mcp_manager": mcp_manager.get_status(),
            "registered_tools": len(mcp_tool_registry._tool_classes),
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取详细统计信息"""
        return mcp_manager.get_all_stats()

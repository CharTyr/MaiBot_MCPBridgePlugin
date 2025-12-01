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
    ComponentInfo,
    ConfigField,
    ToolParamType,
)
from src.plugin_system.base.component_types import ToolInfo, ComponentType, EventHandlerInfo, EventType
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
            error_msg = f"MCP 工具调用失败: {result.error}"
            logger.warning(error_msg)
            return {
                "name": self.name,
                "content": error_msg
            }
    
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


class MCPStartupHandler(BaseEventHandler):
    """MCP 启动事件处理器
    
    在 MaiBot 启动完成后（ON_START 事件）异步连接 MCP 服务器
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
        
        return (True, True, None, None, None)


class MCPStopHandler(BaseEventHandler):
    """MCP 停止事件处理器
    
    在 MaiBot 停止时（ON_STOP 事件）关闭所有 MCP 连接
    """
    
    event_type = EventType.ON_STOP
    handler_name = "mcp_stop_handler"
    handler_description = "MCP 桥接插件停止处理器"
    weight = 0
    intercept_message = False
    
    async def execute(self, message):
        """处理停止事件"""
        logger.info("MCP 桥接插件收到 ON_STOP 事件，正在关闭 MCP 连接...")
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
        },
        "servers": {
            "list": ConfigField(
                type=list,
                default=[
                    {
                        "name": "example",
                        "enabled": False,
                        "transport": "http",
                        "url": "https://example.com/mcp",
                    }
                ],
                description="MCP 服务器列表配置（JSON 数组格式）",
                label="🔌 服务器列表",
                input_type="json",
                hint="""每个服务器配置字段说明:
• name: 服务器名称（唯一标识）
• enabled: 是否启用 (true/false)
• transport: 传输方式 (stdio/sse/http)
• url: 服务器地址 (sse/http 模式)
• command: 启动命令 (stdio 模式，如 npx/uvx)
• args: 命令参数数组 (stdio 模式)
• env: 环境变量对象 (stdio 模式，可选)""",
                rows=20,
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
    
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件的所有组件
        
        返回事件处理器，MCP 工具会在 ON_START 事件后动态注册
        """
        components: List[Tuple[ComponentInfo, Type]] = []
        
        # 添加启动事件处理器
        startup_handler_info = MCPStartupHandler.get_handler_info()
        components.append((startup_handler_info, MCPStartupHandler))
        
        # 添加停止事件处理器
        stop_handler_info = MCPStopHandler.get_handler_info()
        components.append((stop_handler_info, MCPStopHandler))
        
        return components
    
    def get_status(self) -> Dict[str, Any]:
        """获取插件状态"""
        return {
            "initialized": self._initialized,
            "mcp_manager": mcp_manager.get_status(),
            "registered_tools": len(mcp_tool_registry._tool_classes),
        }

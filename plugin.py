"""
MCP 桥接插件
将 MCP (Model Context Protocol) 服务器的工具桥接到 MaiBot
"""

import asyncio
import json
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
    MCPResourceInfo,
    MCPPromptInfo,
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
        # 移除 MaiBot 内部添加的标记
        args = {k: v for k, v in function_args.items() if k != "llm_called"}
        
        # 尝试解析 JSON 字符串参数（用于 array/object 类型）
        parsed_args = {}
        for key, value in args.items():
            if isinstance(value, str):
                # 尝试解析为 JSON
                try:
                    if value.startswith(("[", "{")):
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
            content = result.content
            
            # v1.3.0: 后处理逻辑
            content = await self._post_process_result(content)
            
            return {
                "name": self.name,
                "content": content
            }
        else:
            # 友好的错误提示
            error_msg = self._format_error_message(result.error, result.duration_ms)
            logger.warning(f"MCP 工具 {self.name} 调用失败: {result.error}")
            return {
                "name": self.name,
                "content": error_msg
            }
    
    async def _post_process_result(self, content: str) -> str:
        """v1.3.0: 对工具返回结果进行后处理（摘要提炼）
        
        Args:
            content: 原始工具返回内容
            
        Returns:
            处理后的内容（如果未启用后处理或不满足条件，返回原内容）
        """
        global _plugin_instance
        
        # 检查插件实例是否存在
        if _plugin_instance is None:
            return content
        
        settings = _plugin_instance.config.get("settings", {})
        
        # 检查全局后处理开关
        if not settings.get("post_process_enabled", False):
            return content
        
        # 获取服务器级别配置（如果有）
        server_post_config = self._get_server_post_process_config()
        
        # 确定是否启用（服务器配置优先）
        if server_post_config is not None:
            if not server_post_config.get("enabled", True):
                return content
        
        # 获取阈值（服务器配置 > 全局配置）
        threshold = settings.get("post_process_threshold", 500)
        if server_post_config and "threshold" in server_post_config:
            threshold = server_post_config["threshold"]
        
        # 检查内容长度是否超过阈值
        content_length = len(content) if content else 0
        if content_length <= threshold:
            logger.debug(f"MCP 工具 {self.name} 结果长度 {content_length} 未超过阈值 {threshold}，跳过后处理")
            return content
        
        # 获取用户原始问题
        user_query = self._get_user_query()
        if not user_query:
            logger.debug(f"MCP 工具 {self.name} 无法获取用户问题，跳过后处理")
            return content
        
        # 获取后处理配置
        max_tokens = settings.get("post_process_max_tokens", 500)
        if server_post_config and "max_tokens" in server_post_config:
            max_tokens = server_post_config["max_tokens"]
        
        prompt_template = settings.get("post_process_prompt", "")
        if server_post_config and "prompt" in server_post_config:
            prompt_template = server_post_config["prompt"]
        
        if not prompt_template:
            prompt_template = """用户问题：{query}

工具返回内容：
{result}

请从上述内容中提取与用户问题最相关的关键信息，简洁准确地输出："""
        
        # 构建后处理 prompt
        try:
            prompt = prompt_template.format(query=user_query, result=content)
        except KeyError as e:
            logger.warning(f"后处理 prompt 模板格式错误，缺少变量: {e}")
            return content
        
        # 调用 LLM 进行后处理
        try:
            processed_content = await self._call_post_process_llm(prompt, max_tokens, settings, server_post_config)
            if processed_content:
                logger.info(f"MCP 工具 {self.name} 后处理完成: {content_length} -> {len(processed_content)} 字符")
                return processed_content
            else:
                logger.warning(f"MCP 工具 {self.name} 后处理返回空内容，使用原始结果")
                return content
        except Exception as e:
            logger.error(f"MCP 工具 {self.name} 后处理失败: {e}")
            return content
    
    def _get_server_post_process_config(self) -> Optional[Dict[str, Any]]:
        """获取当前服务器的后处理配置（如果有）"""
        global _plugin_instance
        
        if _plugin_instance is None:
            return None
        
        # 从服务器配置中查找 post_process 配置
        servers_section = _plugin_instance.config.get("servers", {})
        if isinstance(servers_section, dict):
            servers_list = servers_section.get("list", "[]")
            if isinstance(servers_list, str):
                try:
                    servers = json.loads(servers_list) if servers_list.strip() else []
                except json.JSONDecodeError:
                    return None
            elif isinstance(servers_list, list):
                servers = servers_list
            else:
                return None
        else:
            servers = servers_section if isinstance(servers_section, list) else []
        
        # 查找当前服务器的配置
        for server_conf in servers:
            if server_conf.get("name") == self._mcp_server_name:
                return server_conf.get("post_process")
        
        return None
    
    def _get_user_query(self) -> Optional[str]:
        """获取用户原始问题"""
        # 尝试从 chat_stream 获取
        if self.chat_stream and hasattr(self.chat_stream, "context") and self.chat_stream.context:
            try:
                last_message = self.chat_stream.context.get_last_message()
                if last_message and hasattr(last_message, "processed_plain_text"):
                    return last_message.processed_plain_text
            except Exception as e:
                logger.debug(f"从 chat_stream 获取用户问题失败: {e}")
        
        return None
    
    async def _call_post_process_llm(
        self,
        prompt: str,
        max_tokens: int,
        settings: Dict[str, Any],
        server_config: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """调用 LLM 进行后处理
        
        Args:
            prompt: 后处理 prompt
            max_tokens: 最大输出 token
            settings: 全局设置
            server_config: 服务器级别配置
            
        Returns:
            处理后的内容，失败返回 None
        """
        from src.config.config import model_config
        from src.config.api_ada_configs import TaskConfig
        from src.llm_models.utils_model import LLMRequest
        
        # 确定使用的模型
        model_name = settings.get("post_process_model", "")
        if server_config and "model" in server_config:
            model_name = server_config["model"]
        
        if model_name:
            # 用户指定了模型，创建自定义 TaskConfig
            task_config = TaskConfig(
                model_list=[model_name],
                max_tokens=max_tokens,
                temperature=0.3,  # 使用较低温度确保输出稳定
                slow_threshold=30.0,
            )
            logger.debug(f"后处理使用指定模型: {model_name}")
        else:
            # 使用 Utils 模型组
            task_config = model_config.model_task_config.utils
            logger.debug(f"后处理使用 Utils 模型组")
        
        # 创建 LLM 请求
        llm_request = LLMRequest(model_set=task_config, request_type="mcp_post_process")
        
        # 调用 LLM
        response, (reasoning, model_used, _) = await llm_request.generate_response_async(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        
        logger.debug(f"后处理使用模型: {model_used}")
        
        return response.strip() if response else None
    
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


class MCPReadResourceTool(BaseTool):
    """v1.2.0: MCP 资源读取工具 - 读取 MCP 服务器提供的资源内容"""
    
    name = "mcp_read_resource"
    description = "读取 MCP 服务器提供的资源内容（如文件、数据库记录等）。使用前请先用 mcp_list_resources 查看可用资源。"
    parameters = [
        ("uri", ToolParamType.STRING, "资源 URI（如 file:///path/to/file 或自定义 URI）", True, None),
        ("server_name", ToolParamType.STRING, "指定服务器名称（可选，不指定则自动查找）", False, None),
    ]
    available_for_llm = True
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """执行资源读取"""
        uri = function_args.get("uri", "")
        server_name = function_args.get("server_name")
        
        if not uri:
            return {
                "name": self.name,
                "content": "❌ 请提供资源 URI"
            }
        
        result = await mcp_manager.read_resource(uri, server_name)
        
        if result.success:
            return {
                "name": self.name,
                "content": result.content
            }
        else:
            return {
                "name": self.name,
                "content": f"❌ 读取资源失败: {result.error}"
            }
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        return await self.execute(function_args)


class MCPGetPromptTool(BaseTool):
    """v1.2.0: MCP 提示模板工具 - 获取 MCP 服务器提供的提示模板内容"""
    
    name = "mcp_get_prompt"
    description = "获取 MCP 服务器提供的提示模板内容。使用前请先用 mcp_list_prompts 查看可用模板。"
    parameters = [
        ("name", ToolParamType.STRING, "提示模板名称", True, None),
        ("arguments", ToolParamType.STRING, "模板参数（JSON 对象格式，如 {\"key\": \"value\"}）", False, None),
        ("server_name", ToolParamType.STRING, "指定服务器名称（可选）", False, None),
    ]
    available_for_llm = True
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """获取提示模板"""
        import json
        
        prompt_name = function_args.get("name", "")
        arguments_str = function_args.get("arguments", "")
        server_name = function_args.get("server_name")
        
        if not prompt_name:
            return {
                "name": self.name,
                "content": "❌ 请提供提示模板名称"
            }
        
        # 解析参数
        arguments = None
        if arguments_str:
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                return {
                    "name": self.name,
                    "content": f"❌ 参数格式错误，请使用 JSON 对象格式"
                }
        
        result = await mcp_manager.get_prompt(prompt_name, arguments, server_name)
        
        if result.success:
            return {
                "name": self.name,
                "content": result.content
            }
        else:
            return {
                "name": self.name,
                "content": f"❌ 获取提示模板失败: {result.error}"
            }
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        return await self.execute(function_args)


class MCPStatusTool(BaseTool):
    """MCP 状态查询工具 - 查看 MCP 服务器连接状态、工具、资源、模板和调用统计"""
    
    name = "mcp_status"
    description = "查询 MCP 桥接插件的状态，包括服务器连接状态、可用工具列表、资源列表、提示模板列表、调用统计等信息"
    parameters = [
        ("query_type", ToolParamType.STRING, "查询类型", False, ["status", "tools", "resources", "prompts", "stats", "all"]),
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
        
        # v1.2.0: 资源列表
        if query_type in ("resources", "all"):
            result_parts.append(self._format_resources(server_name))
        
        # v1.2.0: 提示模板列表
        if query_type in ("prompts", "all"):
            result_parts.append(self._format_prompts(server_name))
        
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
    
    def _format_resources(self, server_name: Optional[str] = None) -> str:
        """v1.2.0: 格式化资源列表"""
        resources = mcp_manager.all_resources
        if not resources:
            return "📦 当前没有可用的 MCP 资源\n  提示: 确保已启用 enable_resources 配置"
        
        lines = ["📦 可用 MCP 资源"]
        
        # 按服务器分组
        by_server: Dict[str, List[MCPResourceInfo]] = {}
        for key, (resource_info, _) in resources.items():
            if server_name and resource_info.server_name != server_name:
                continue
            if resource_info.server_name not in by_server:
                by_server[resource_info.server_name] = []
            by_server[resource_info.server_name].append(resource_info)
        
        for srv_name, resource_list in by_server.items():
            lines.append(f"\n🔌 {srv_name} ({len(resource_list)} 个资源):")
            for res in resource_list:
                lines.append(f"  • {res.name}")
                lines.append(f"    URI: {res.uri}")
                if res.description:
                    desc = res.description[:50] + "..." if len(res.description) > 50 else res.description
                    lines.append(f"    描述: {desc}")
                if res.mime_type:
                    lines.append(f"    类型: {res.mime_type}")
        
        if not by_server:
            lines.append("  (无匹配的资源)")
        
        return "\n".join(lines)
    
    def _format_prompts(self, server_name: Optional[str] = None) -> str:
        """v1.2.0: 格式化提示模板列表"""
        prompts = mcp_manager.all_prompts
        if not prompts:
            return "📝 当前没有可用的 MCP 提示模板\n  提示: 确保已启用 enable_prompts 配置"
        
        lines = ["📝 可用 MCP 提示模板"]
        
        # 按服务器分组
        by_server: Dict[str, List[MCPPromptInfo]] = {}
        for key, (prompt_info, _) in prompts.items():
            if server_name and prompt_info.server_name != server_name:
                continue
            if prompt_info.server_name not in by_server:
                by_server[prompt_info.server_name] = []
            by_server[prompt_info.server_name].append(prompt_info)
        
        for srv_name, prompt_list in by_server.items():
            lines.append(f"\n🔌 {srv_name} ({len(prompt_list)} 个模板):")
            for prompt in prompt_list:
                lines.append(f"  • {prompt.name}")
                if prompt.description:
                    desc = prompt.description[:60] + "..." if len(prompt.description) > 60 else prompt.description
                    lines.append(f"    描述: {desc}")
                if prompt.arguments:
                    args_str = ", ".join([
                        f"{a['name']}{'*' if a.get('required') else ''}"
                        for a in prompt.arguments
                    ])
                    lines.append(f"    参数: {args_str}")
        
        if not by_server:
            lines.append("  (无匹配的模板)")
        
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
            "enable_resources": ConfigField(
                type=bool,
                default=False,
                description="📦 启用 Resources - 允许读取 MCP 服务器提供的资源（文件、数据等）",
                label="📦 启用 Resources（实验性）",
                hint="启用后会自动发现并注册服务器提供的资源，可通过 mcp_read_resource 工具读取",
                order=11,
            ),
            "enable_prompts": ConfigField(
                type=bool,
                default=False,
                description="📝 启用 Prompts - 允许使用 MCP 服务器提供的提示模板",
                label="📝 启用 Prompts（实验性）",
                hint="启用后会自动发现并注册服务器提供的提示模板，可通过 mcp_get_prompt 工具获取",
                order=12,
            ),
            # ============ v1.3.0 后处理配置 ============
            "post_process_enabled": ConfigField(
                type=bool,
                default=False,
                description="🔄 结果后处理 - 使用 LLM 对 MCP 工具返回的长结果进行摘要提炼",
                label="🔄 启用结果后处理",
                hint="当工具返回内容过长时，使用 LLM 提取关键信息，提高回复质量",
                order=20,
            ),
            "post_process_threshold": ConfigField(
                type=int,
                default=500,
                description="📏 后处理阈值 - 结果长度（字符数）超过此值才触发后处理",
                label="📏 后处理阈值（字符）",
                min=100,
                max=5000,
                step=100,
                hint="建议设置为 300-1000，太小会增加不必要的 LLM 调用",
                order=21,
            ),
            "post_process_max_tokens": ConfigField(
                type=int,
                default=500,
                description="📝 后处理输出限制 - LLM 摘要输出的最大 token 数",
                label="📝 后处理最大输出 token",
                min=100,
                max=2000,
                step=50,
                order=22,
            ),
            "post_process_model": ConfigField(
                type=str,
                default="",
                description="🤖 后处理模型 - 指定用于后处理的模型名称（需与 model_config.toml 中一致）",
                label="🤖 后处理模型（可选）",
                placeholder="留空则使用 Utils 模型组",
                hint="留空将使用主程序 model_config.toml 中的 utils 模型组；填写模型名称可指定特定模型",
                order=23,
            ),
            "post_process_prompt": ConfigField(
                type=str,
                default="""用户问题：{query}

工具返回内容：
{result}

请从上述内容中提取与用户问题最相关的关键信息，简洁准确地输出：""",
                description="📋 后处理提示词模板 - {query} 为用户问题，{result} 为工具返回内容",
                label="📋 后处理提示词模板",
                input_type="textarea",
                rows=8,
                hint="可用变量：{query}=用户问题，{result}=工具返回内容",
                order=24,
            ),
        },
        "servers": {
            "list": ConfigField(
                type=str,
                default='''[
  {
    "name": "time-mcp-server",
    "enabled": false,
    "transport": "streamable_http",
    "url": "https://mcp.api-inference.modelscope.cn/server/mcp-server-time"
  },
  {
    "name": "fetch-local",
    "enabled": false,
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-fetch"]
  }
]''',
                description="MCP 服务器列表配置（JSON 数组格式，必须以 [ 开头，以 ] 结尾）",
                label="🔌 服务器列表",
                input_type="textarea",
                placeholder='''[
  {
    "name": "remote-example",
    "enabled": true,
    "transport": "streamable_http",
    "url": "https://mcp.example.com/mcp"
  },
  {
    "name": "local-example",
    "enabled": true,
    "transport": "stdio",
    "command": "uvx",
    "args": ["mcp-server-xxx"]
  }
]''',
                hint="""⚠️ 格式要求：必须是 JSON 数组！
• 整个配置必须用 [ ] 包裹
• 多个服务器之间用逗号分隔
• 每个服务器是一个 { } 对象
• transport 可选: stdio / sse / http / streamable_http
• stdio 类型需要 command/args/env 字段，其他类型需要 url 字段
❌ 错误示例: { "name": "a" }, { "name": "b" }  ← 缺少外层 [ ]
✅ 正确示例: [{ "name": "a" }, { "name": "b" }]
💡 默认示例已禁用(enabled=false)，修改后启用即可使用""",
                rows=18,
                order=1,
            ),
        },
        "status": {
            "connection_status": ConfigField(
                type=str,
                default="未初始化",
                description="当前 MCP 服务器连接状态和工具列表",
                label="📊 连接状态",
                input_type="textarea",
                disabled=True,
                rows=15,
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
        
        # 注册状态变化回调，实时更新 WebUI 显示
        mcp_manager.set_status_change_callback(self._update_status_display)
    
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
            logger.debug(f"servers.list 类型: {type(servers_list).__name__}")
            if isinstance(servers_list, str):
                # JSON 字符串格式，需要解析
                logger.debug(f"servers.list 原始内容长度: {len(servers_list)}")
                servers_config = self._parse_servers_json(servers_list)
            elif isinstance(servers_list, list):
                servers_config = servers_list
                logger.info(f"从 list 类型获取到 {len(servers_config)} 个服务器配置")
            else:
                logger.warning(f"servers.list 类型不支持: {type(servers_list).__name__}")
                servers_config = []
        else:
            # TOML 数组格式
            servers_config = servers_section
            logger.info(f"从 TOML 数组获取到 {len(servers_config)} 个服务器配置")
        
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
        
        logger.info(f"准备连接 {len(servers_config)} 个 MCP 服务器")
        
        for idx, server_conf in enumerate(servers_config):
            server_name = server_conf.get("name", f"unknown_{idx}")
            logger.info(f"[{idx+1}/{len(servers_config)}] 处理服务器: {server_name}")
            
            if not server_conf.get("enabled", True):
                logger.info(f"服务器 {server_name} 已禁用，跳过")
                continue
            
            # 解析服务器配置
            try:
                config = self._parse_server_config(server_conf)
            except Exception as e:
                logger.error(f"解析服务器 {server_name} 配置失败: {e}")
                continue
            
            # 添加服务器
            logger.info(f"正在连接服务器: {config.name} ({config.transport.value})")
            success = await mcp_manager.add_server(config)
            if not success:
                logger.warning(f"服务器 {config.name} 连接失败，继续处理下一个")
                continue
            
            logger.info(f"服务器 {config.name} 连接成功")
            
            # v1.2.0: 如果启用了 Resources，获取资源列表
            if settings.get("enable_resources", False):
                try:
                    await mcp_manager.fetch_resources_for_server(config.name)
                except Exception as e:
                    logger.warning(f"服务器 {config.name} 获取资源列表失败: {e}")
            
            # v1.2.0: 如果启用了 Prompts，获取提示模板列表
            if settings.get("enable_prompts", False):
                try:
                    await mcp_manager.fetch_prompts_for_server(config.name)
                except Exception as e:
                    logger.warning(f"服务器 {config.name} 获取提示模板列表失败: {e}")
            
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
    
    def _parse_servers_json(self, servers_list: str) -> List[Dict]:
        """解析服务器列表 JSON 字符串，包含防呆逻辑
        
        常见错误格式及修复:
        1. 缺少外层数组括号: { "name": "a" }, { "name": "b" } -> 自动包裹为数组
        2. 单个对象未包裹: { "name": "a" } -> 自动包裹为数组
        3. JSON 语法错误: 给出详细错误提示
        """
        if not servers_list.strip():
            return []
        
        content = servers_list.strip()
        
        try:
            parsed = json.loads(content)
            # 解析成功，检查是否为数组
            if isinstance(parsed, list):
                logger.info(f"从 JSON 字符串解析到 {len(parsed)} 个服务器配置")
                return parsed
            elif isinstance(parsed, dict):
                # 单个对象，自动包裹为数组
                logger.warning("服务器配置是单个对象而非数组，已自动转换为数组格式")
                logger.warning("建议: 请将配置改为 JSON 数组格式，用 [ ] 包裹")
                return [parsed]
            else:
                logger.error(f"服务器配置格式错误: 期望数组或对象，得到 {type(parsed).__name__}")
                return []
        except json.JSONDecodeError as e:
            # JSON 解析失败，尝试智能修复
            logger.warning(f"JSON 解析失败: {e}")
            
            # 检测常见错误: 多个对象未包裹在数组中
            # 例如: { "name": "a" }, { "name": "b" }
            if content.startswith("{") and not content.startswith("["):
                logger.warning("检测到可能缺少外层数组括号 [ ]，尝试自动修复...")
                try:
                    fixed_content = f"[{content}]"
                    parsed = json.loads(fixed_content)
                    if isinstance(parsed, list):
                        logger.warning(f"✅ 自动修复成功！解析到 {len(parsed)} 个服务器配置")
                        logger.warning("⚠️ 请修正配置: 服务器列表必须用 [ ] 包裹成 JSON 数组")
                        logger.warning("   错误格式: {{ \"name\": \"a\" }}, {{ \"name\": \"b\" }}")
                        logger.warning("   正确格式: [{{ \"name\": \"a\" }}, {{ \"name\": \"b\" }}]")
                        return parsed
                except json.JSONDecodeError:
                    pass  # 修复失败，继续报错
            
            # 无法修复，输出详细错误信息
            logger.error("❌ 服务器配置 JSON 格式错误，无法解析")
            logger.error(f"   错误位置: 第 {e.lineno} 行，第 {e.colno} 列")
            logger.error(f"   错误原因: {e.msg}")
            logger.error("   配置内容预览:")
            # 显示前几行帮助定位问题
            lines = content.split("\n")[:5]
            for i, line in enumerate(lines, 1):
                logger.error(f"   {i}: {line[:80]}{'...' if len(line) > 80 else ''}")
            if len(content.split("\n")) > 5:
                logger.error("   ...")
            logger.error("")
            logger.error("💡 正确格式示例:")
            logger.error('   [')
            logger.error('     { "name": "server1", "enabled": true, "transport": "http", "url": "https://..." },')
            logger.error('     { "name": "server2", "enabled": true, "transport": "streamable_http", "url": "https://..." }')
            logger.error('   ]')
            return []
    
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
        """更新配置文件中的状态显示字段"""
        import tomlkit
        from pathlib import Path
        
        status = mcp_manager.get_status()
        settings = self.config.get("settings", {})
        lines = []
        
        # 概览
        lines.append(f"服务器: {status['connected_servers']}/{status['total_servers']} 已连接")
        lines.append(f"工具数: {status['total_tools']}")
        # v1.2.0: 显示资源和提示模板数量
        if settings.get("enable_resources", False):
            lines.append(f"资源数: {status.get('total_resources', 0)}")
        if settings.get("enable_prompts", False):
            lines.append(f"模板数: {status.get('total_prompts', 0)}")
        lines.append(f"心跳: {'运行中' if status['heartbeat_running'] else '已停止'}")
        lines.append("")
        
        # 服务器详情和工具列表
        tools = mcp_manager.all_tools
        resources = mcp_manager.all_resources
        prompts = mcp_manager.all_prompts
        
        for name, info in status.get("servers", {}).items():
            icon = "✅" if info["connected"] else "❌"
            lines.append(f"{icon} {name} ({info['transport']})")
            
            # 列出该服务器的工具
            server_tools = [t.name for key, (t, _) in tools.items() if t.server_name == name]
            if server_tools:
                for tool_name in server_tools:
                    lines.append(f"   • {tool_name}")
            else:
                lines.append("   (无工具)")
            
            # v1.2.0: 显示资源数量
            if settings.get("enable_resources", False) and info.get("supports_resources"):
                res_count = info.get("resources_count", 0)
                lines.append(f"   📦 {res_count} 个资源")
            
            # v1.2.0: 显示提示模板数量
            if settings.get("enable_prompts", False) and info.get("supports_prompts"):
                prompt_count = info.get("prompts_count", 0)
                lines.append(f"   📝 {prompt_count} 个模板")
        
        if not status.get("servers"):
            lines.append("(无服务器)")
        
        status_text = "\n".join(lines)
        
        # 更新内存中的配置
        if "status" not in self.config:
            self.config["status"] = {}
        self.config["status"]["connection_status"] = status_text
        
        # 写入配置文件
        try:
            config_path = Path(__file__).parent / "config.toml"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
                
                if "status" not in doc:
                    doc["status"] = tomlkit.table()
                doc["status"]["connection_status"] = status_text
                
                with open(config_path, "w", encoding="utf-8") as f:
                    tomlkit.dump(doc, f)
                
                logger.debug("已更新配置文件中的状态显示")
        except Exception as e:
            logger.warning(f"更新配置文件状态失败: {e}")
    
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
        
        # v1.2.0: 添加 Resources/Prompts 操作工具（列表功能已合并到 mcp_status）
        settings = self.config.get("settings", {})
        
        if settings.get("enable_resources", False):
            # 资源读取工具
            read_resource_info = ToolInfo(
                name=MCPReadResourceTool.name,
                tool_description=MCPReadResourceTool.description,
                enabled=True,
                tool_parameters=MCPReadResourceTool.parameters,
                component_type=ComponentType.TOOL,
            )
            components.append((read_resource_info, MCPReadResourceTool))
        
        if settings.get("enable_prompts", False):
            # 提示模板获取工具
            get_prompt_info = ToolInfo(
                name=MCPGetPromptTool.name,
                tool_description=MCPGetPromptTool.description,
                enabled=True,
                tool_parameters=MCPGetPromptTool.parameters,
                component_type=ComponentType.TOOL,
            )
            components.append((get_prompt_info, MCPGetPromptTool))
        
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

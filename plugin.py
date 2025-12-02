"""
MCP 桥接插件 v1.4.0
将 MCP (Model Context Protocol) 服务器的工具桥接到 MaiBot

v1.4.0 新增功能:
- 工具禁用管理
- 调用链路追踪
- 工具调用缓存
- 工具权限控制
"""

import asyncio
import fnmatch
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
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


# ============================================================================
# v1.4.0: 调用链路追踪
# ============================================================================

@dataclass
class ToolCallRecord:
    """工具调用记录"""
    call_id: str
    timestamp: float
    tool_name: str
    server_name: str
    chat_id: str = ""
    user_id: str = ""
    user_query: str = ""
    arguments: Dict = field(default_factory=dict)
    raw_result: str = ""
    processed_result: str = ""
    duration_ms: float = 0.0
    success: bool = True
    error: str = ""
    post_processed: bool = False
    cache_hit: bool = False


class ToolCallTracer:
    """工具调用追踪器"""
    
    def __init__(self, max_records: int = 100):
        self._records: deque[ToolCallRecord] = deque(maxlen=max_records)
        self._enabled: bool = True
        self._log_enabled: bool = False
        self._log_path: Optional[Path] = None
    
    def configure(self, enabled: bool, max_records: int, log_enabled: bool, log_path: Optional[Path] = None):
        """配置追踪器"""
        self._enabled = enabled
        self._records = deque(self._records, maxlen=max_records)
        self._log_enabled = log_enabled
        self._log_path = log_path
    
    def record(self, record: ToolCallRecord) -> None:
        """添加调用记录"""
        if not self._enabled:
            return
        
        self._records.append(record)
        
        if self._log_enabled and self._log_path:
            self._write_to_log(record)
    
    def get_recent(self, n: int = 10) -> List[ToolCallRecord]:
        """获取最近 N 条记录"""
        return list(self._records)[-n:]
    
    def get_by_tool(self, tool_name: str) -> List[ToolCallRecord]:
        """按工具名筛选记录"""
        return [r for r in self._records if r.tool_name == tool_name]
    
    def get_by_server(self, server_name: str) -> List[ToolCallRecord]:
        """按服务器名筛选记录"""
        return [r for r in self._records if r.server_name == server_name]
    
    def clear(self) -> None:
        """清空记录"""
        self._records.clear()
    
    def _write_to_log(self, record: ToolCallRecord) -> None:
        """写入 JSONL 日志文件"""
        try:
            if self._log_path:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写入追踪日志失败: {e}")
    
    @property
    def total_records(self) -> int:
        return len(self._records)


# 全局追踪器实例
tool_call_tracer = ToolCallTracer()


# ============================================================================
# v1.4.0: 工具调用缓存
# ============================================================================

@dataclass
class CacheEntry:
    """缓存条目"""
    tool_name: str
    args_hash: str
    result: str
    created_at: float
    expires_at: float
    hit_count: int = 0


class ToolCallCache:
    """工具调用缓存（LRU）"""
    
    def __init__(self, max_entries: int = 200, ttl: int = 300):
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_entries = max_entries
        self._ttl = ttl
        self._enabled = False
        self._exclude_patterns: List[str] = []
        self._stats = {"hits": 0, "misses": 0}
    
    def configure(self, enabled: bool, ttl: int, max_entries: int, exclude_tools: str):
        """配置缓存"""
        self._enabled = enabled
        self._ttl = ttl
        self._max_entries = max_entries
        self._exclude_patterns = [p.strip() for p in exclude_tools.strip().split("\n") if p.strip()]
    
    def get(self, tool_name: str, args: Dict) -> Optional[str]:
        """获取缓存"""
        if not self._enabled:
            return None
        
        if self._is_excluded(tool_name):
            return None
        
        key = self._generate_key(tool_name, args)
        
        if key not in self._cache:
            self._stats["misses"] += 1
            return None
        
        entry = self._cache[key]
        
        # 检查是否过期
        if time.time() > entry.expires_at:
            del self._cache[key]
            self._stats["misses"] += 1
            return None
        
        # LRU: 移到末尾
        self._cache.move_to_end(key)
        entry.hit_count += 1
        self._stats["hits"] += 1
        
        return entry.result
    
    def set(self, tool_name: str, args: Dict, result: str) -> None:
        """设置缓存"""
        if not self._enabled:
            return
        
        if self._is_excluded(tool_name):
            return
        
        key = self._generate_key(tool_name, args)
        now = time.time()
        
        entry = CacheEntry(
            tool_name=tool_name,
            args_hash=key,
            result=result,
            created_at=now,
            expires_at=now + self._ttl,
        )
        
        # 如果已存在，更新
        if key in self._cache:
            self._cache[key] = entry
            self._cache.move_to_end(key)
        else:
            # 检查容量
            self._evict_if_needed()
            self._cache[key] = entry
    
    def clear(self) -> None:
        """清空缓存"""
        self._cache.clear()
        self._stats = {"hits": 0, "misses": 0}
    
    def _generate_key(self, tool_name: str, args: Dict) -> str:
        """生成缓存键"""
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        content = f"{tool_name}:{args_str}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def _is_excluded(self, tool_name: str) -> bool:
        """检查是否在排除列表中"""
        for pattern in self._exclude_patterns:
            if fnmatch.fnmatch(tool_name, pattern):
                return True
        return False
    
    def _evict_if_needed(self) -> None:
        """必要时淘汰条目"""
        # 先清理过期的
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if now > v.expires_at]
        for k in expired_keys:
            del self._cache[k]
        
        # LRU 淘汰
        while len(self._cache) >= self._max_entries:
            self._cache.popitem(last=False)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0
        return {
            "enabled": self._enabled,
            "entries": len(self._cache),
            "max_entries": self._max_entries,
            "ttl": self._ttl,
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "hit_rate": f"{hit_rate:.1f}%",
        }


# 全局缓存实例
tool_call_cache = ToolCallCache()


# ============================================================================
# v1.4.0: 工具权限控制
# ============================================================================

class PermissionChecker:
    """工具权限检查器"""
    
    def __init__(self):
        self._enabled = False
        self._default_mode = "allow_all"  # allow_all 或 deny_all
        self._rules: List[Dict] = []
    
    def configure(self, enabled: bool, default_mode: str, rules_json: str):
        """配置权限检查器"""
        self._enabled = enabled
        self._default_mode = default_mode if default_mode in ("allow_all", "deny_all") else "allow_all"
        
        try:
            self._rules = json.loads(rules_json) if rules_json.strip() else []
        except json.JSONDecodeError as e:
            logger.warning(f"权限规则 JSON 解析失败: {e}")
            self._rules = []
    
    def check(self, tool_name: str, chat_id: str, user_id: str, is_group: bool) -> bool:
        """检查权限
        
        Args:
            tool_name: 工具名称
            chat_id: 聊天 ID（群号或私聊 ID）
            user_id: 用户 ID
            is_group: 是否为群聊
            
        Returns:
            True 表示允许，False 表示拒绝
        """
        if not self._enabled:
            return True
        
        # 查找匹配的规则
        for rule in self._rules:
            tool_pattern = rule.get("tool", "")
            if not self._match_tool(tool_pattern, tool_name):
                continue
            
            # 找到匹配的规则
            mode = rule.get("mode", "")
            allowed = rule.get("allowed", [])
            denied = rule.get("denied", [])
            
            # 构建当前上下文的 ID 列表
            context_ids = self._build_context_ids(chat_id, user_id, is_group)
            
            # 检查 denied 列表（优先级最高）
            if denied:
                for ctx_id in context_ids:
                    if self._match_id_list(denied, ctx_id):
                        return False
            
            # 检查 allowed 列表
            if allowed:
                for ctx_id in context_ids:
                    if self._match_id_list(allowed, ctx_id):
                        return True
                # 如果是 whitelist 模式且不在 allowed 中，拒绝
                if mode == "whitelist":
                    return False
            
            # 规则匹配但没有明确允许/拒绝，继续检查下一条规则
        
        # 没有匹配的规则，使用默认模式
        return self._default_mode == "allow_all"
    
    def _match_tool(self, pattern: str, tool_name: str) -> bool:
        """工具名通配符匹配"""
        if not pattern:
            return False
        return fnmatch.fnmatch(tool_name, pattern)
    
    def _build_context_ids(self, chat_id: str, user_id: str, is_group: bool) -> List[str]:
        """构建上下文 ID 列表"""
        ids = []
        
        # 用户级别（任何场景生效）
        if user_id:
            ids.append(f"qq:{user_id}:user")
        
        # 场景级别
        if is_group and chat_id:
            ids.append(f"qq:{chat_id}:group")
        elif chat_id:
            ids.append(f"qq:{chat_id}:private")
        
        return ids
    
    def _match_id_list(self, id_list: List[str], context_id: str) -> bool:
        """检查 ID 是否在列表中"""
        for rule_id in id_list:
            if fnmatch.fnmatch(context_id, rule_id):
                return True
        return False
    
    def get_rules_for_tool(self, tool_name: str) -> List[Dict]:
        """获取特定工具的权限规则"""
        return [r for r in self._rules if self._match_tool(r.get("tool", ""), tool_name)]


# 全局权限检查器实例
permission_checker = PermissionChecker()


# ============================================================================
# 工具类型转换
# ============================================================================

def convert_json_type_to_tool_param_type(json_type: str) -> ToolParamType:
    """将 JSON Schema 类型转换为 MaiBot 的 ToolParamType"""
    type_mapping = {
        "string": ToolParamType.STRING,
        "integer": ToolParamType.INTEGER,
        "number": ToolParamType.FLOAT,
        "boolean": ToolParamType.BOOLEAN,
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
        
        if json_type == "array":
            description = f"{description} (JSON 数组格式)"
        elif json_type == "object":
            description = f"{description} (JSON 对象格式)"
        
        is_required = param_name in required
        enum_values = param_info.get("enum")
        
        if enum_values is not None:
            enum_values = [str(v) for v in enum_values]
        
        parameters.append((param_name, param_type, description, is_required, enum_values))
    
    return parameters


# ============================================================================
# MCP 工具代理
# ============================================================================

class MCPToolProxy(BaseTool):
    """MCP 工具代理基类"""
    
    name: str = ""
    description: str = ""
    parameters: List[Tuple[str, ToolParamType, str, bool, Optional[List[str]]]] = []
    available_for_llm: bool = True
    
    _mcp_tool_key: str = ""
    _mcp_original_name: str = ""
    _mcp_server_name: str = ""
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """执行 MCP 工具调用"""
        global _plugin_instance
        
        call_id = str(uuid.uuid4())[:8]
        start_time = time.time()
        
        # 移除 MaiBot 内部标记
        args = {k: v for k, v in function_args.items() if k != "llm_called"}
        
        # 解析 JSON 字符串参数
        parsed_args = {}
        for key, value in args.items():
            if isinstance(value, str):
                try:
                    if value.startswith(("[", "{")):
                        parsed_args[key] = json.loads(value)
                    else:
                        parsed_args[key] = value
                except json.JSONDecodeError:
                    parsed_args[key] = value
            else:
                parsed_args[key] = value
        
        # 获取上下文信息
        chat_id, user_id, is_group, user_query = self._get_context_info()
        
        # v1.4.0: 权限检查
        if not permission_checker.check(self.name, chat_id, user_id, is_group):
            logger.warning(f"权限拒绝: 工具 {self.name}, chat={chat_id}, user={user_id}")
            return {
                "name": self.name,
                "content": f"⛔ 权限不足：工具 {self.name} 在当前场景下不可用"
            }
        
        logger.debug(f"调用 MCP 工具: {self._mcp_tool_key}, 参数: {parsed_args}")
        
        # v1.4.0: 检查缓存
        cache_hit = False
        cached_result = tool_call_cache.get(self.name, parsed_args)
        
        if cached_result is not None:
            cache_hit = True
            content = cached_result
            raw_result = cached_result
            success = True
            error = ""
            logger.debug(f"MCP 工具 {self.name} 命中缓存")
        else:
            # 调用 MCP
            result = await mcp_manager.call_tool(self._mcp_tool_key, parsed_args)
            
            if result.success:
                content = result.content
                raw_result = content
                success = True
                error = ""
                
                # 存入缓存
                tool_call_cache.set(self.name, parsed_args, content)
            else:
                content = self._format_error_message(result.error, result.duration_ms)
                raw_result = result.error
                success = False
                error = result.error
                logger.warning(f"MCP 工具 {self.name} 调用失败: {result.error}")
        
        # v1.3.0: 后处理
        post_processed = False
        processed_result = content
        if success:
            processed_content = await self._post_process_result(content)
            if processed_content != content:
                post_processed = True
                processed_result = processed_content
                content = processed_content
        
        duration_ms = (time.time() - start_time) * 1000
        
        # v1.4.0: 记录调用追踪
        record = ToolCallRecord(
            call_id=call_id,
            timestamp=start_time,
            tool_name=self.name,
            server_name=self._mcp_server_name,
            chat_id=chat_id,
            user_id=user_id,
            user_query=user_query,
            arguments=parsed_args,
            raw_result=raw_result[:1000] if raw_result else "",
            processed_result=processed_result[:1000] if processed_result else "",
            duration_ms=duration_ms,
            success=success,
            error=error,
            post_processed=post_processed,
            cache_hit=cache_hit,
        )
        tool_call_tracer.record(record)
        
        return {"name": self.name, "content": content}
    
    def _get_context_info(self) -> Tuple[str, str, bool, str]:
        """获取上下文信息"""
        chat_id = ""
        user_id = ""
        is_group = False
        user_query = ""
        
        if self.chat_stream and hasattr(self.chat_stream, "context") and self.chat_stream.context:
            try:
                ctx = self.chat_stream.context
                if hasattr(ctx, "chat_id"):
                    chat_id = str(ctx.chat_id) if ctx.chat_id else ""
                if hasattr(ctx, "user_id"):
                    user_id = str(ctx.user_id) if ctx.user_id else ""
                if hasattr(ctx, "is_group"):
                    is_group = bool(ctx.is_group)
                
                last_message = ctx.get_last_message()
                if last_message and hasattr(last_message, "processed_plain_text"):
                    user_query = last_message.processed_plain_text or ""
            except Exception as e:
                logger.debug(f"获取上下文信息失败: {e}")
        
        return chat_id, user_id, is_group, user_query

    async def _post_process_result(self, content: str) -> str:
        """v1.3.0: 对工具返回结果进行后处理（摘要提炼）"""
        global _plugin_instance
        
        if _plugin_instance is None:
            return content
        
        settings = _plugin_instance.config.get("settings", {})
        
        if not settings.get("post_process_enabled", False):
            return content
        
        server_post_config = self._get_server_post_process_config()
        
        if server_post_config is not None:
            if not server_post_config.get("enabled", True):
                return content
        
        threshold = settings.get("post_process_threshold", 500)
        if server_post_config and "threshold" in server_post_config:
            threshold = server_post_config["threshold"]
        
        content_length = len(content) if content else 0
        if content_length <= threshold:
            return content
        
        user_query = self._get_context_info()[3]
        if not user_query:
            return content
        
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
        
        try:
            prompt = prompt_template.format(query=user_query, result=content)
        except KeyError as e:
            logger.warning(f"后处理 prompt 模板格式错误: {e}")
            return content
        
        try:
            processed_content = await self._call_post_process_llm(prompt, max_tokens, settings, server_post_config)
            if processed_content:
                logger.info(f"MCP 工具 {self.name} 后处理完成: {content_length} -> {len(processed_content)} 字符")
                return processed_content
            return content
        except Exception as e:
            logger.error(f"MCP 工具 {self.name} 后处理失败: {e}")
            return content
    
    def _get_server_post_process_config(self) -> Optional[Dict[str, Any]]:
        """获取当前服务器的后处理配置"""
        global _plugin_instance
        
        if _plugin_instance is None:
            return None
        
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
        
        for server_conf in servers:
            if server_conf.get("name") == self._mcp_server_name:
                return server_conf.get("post_process")
        
        return None
    
    async def _call_post_process_llm(
        self,
        prompt: str,
        max_tokens: int,
        settings: Dict[str, Any],
        server_config: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """调用 LLM 进行后处理"""
        from src.config.config import model_config
        from src.config.api_ada_configs import TaskConfig
        from src.llm_models.utils_model import LLMRequest
        
        model_name = settings.get("post_process_model", "")
        if server_config and "model" in server_config:
            model_name = server_config["model"]
        
        if model_name:
            task_config = TaskConfig(
                model_list=[model_name],
                max_tokens=max_tokens,
                temperature=0.3,
                slow_threshold=30.0,
            )
        else:
            task_config = model_config.model_task_config.utils
        
        llm_request = LLMRequest(model_set=task_config, request_type="mcp_post_process")
        
        response, (reasoning, model_used, _) = await llm_request.generate_response_async(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        
        return response.strip() if response else None
    
    def _format_error_message(self, error: str, duration_ms: float) -> str:
        """格式化友好的错误消息"""
        if not error:
            return "工具调用失败（未知错误）"
        
        error_lower = error.lower()
        
        if "未连接" in error or "not connected" in error_lower:
            return f"⚠️ MCP 服务器 [{self._mcp_server_name}] 未连接，请检查服务器状态或等待自动重连"
        
        if "超时" in error or "timeout" in error_lower:
            return f"⏱️ 工具调用超时（耗时 {duration_ms:.0f}ms），服务器响应过慢，请稍后重试"
        
        if "connection" in error_lower and ("closed" in error_lower or "reset" in error_lower):
            return f"🔌 与 MCP 服务器 [{self._mcp_server_name}] 的连接已断开，正在尝试重连..."
        
        if "invalid" in error_lower and "argument" in error_lower:
            return f"❌ 参数错误: {error}"
        
        return f"❌ 工具调用失败: {error}"
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        """直接执行（供其他插件调用）"""
        return await self.execute(function_args)


def create_mcp_tool_class(
    tool_key: str,
    tool_info: MCPToolInfo,
    tool_prefix: str,
    disabled: bool = False
) -> Type[MCPToolProxy]:
    """根据 MCP 工具信息动态创建 BaseTool 子类"""
    parameters = parse_mcp_parameters(tool_info.input_schema)
    
    class_name = f"MCPTool_{tool_info.server_name}_{tool_info.name}".replace("-", "_").replace(".", "_")
    tool_name = tool_key.replace("-", "_").replace(".", "_")
    
    description = tool_info.description
    if not description.endswith(f"[来自 MCP 服务器: {tool_info.server_name}]"):
        description = f"{description} [来自 MCP 服务器: {tool_info.server_name}]"
    
    tool_class = type(
        class_name,
        (MCPToolProxy,),
        {
            "name": tool_name,
            "description": description,
            "parameters": parameters,
            "available_for_llm": not disabled,  # v1.4.0: 禁用的工具不可被 LLM 调用
            "_mcp_tool_key": tool_key,
            "_mcp_original_name": tool_info.name,
            "_mcp_server_name": tool_info.server_name,
        }
    )
    
    return tool_class


class MCPToolRegistry:
    """MCP 工具注册表"""
    
    def __init__(self):
        self._tool_classes: Dict[str, Type[MCPToolProxy]] = {}
        self._tool_infos: Dict[str, ToolInfo] = {}
    
    def register_tool(
        self,
        tool_key: str,
        tool_info: MCPToolInfo,
        tool_prefix: str,
        disabled: bool = False
    ) -> Tuple[ToolInfo, Type[MCPToolProxy]]:
        """注册 MCP 工具"""
        tool_class = create_mcp_tool_class(tool_key, tool_info, tool_prefix, disabled)
        
        self._tool_classes[tool_key] = tool_class
        
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
        return [(self._tool_infos[key], self._tool_classes[key]) for key in self._tool_classes.keys()]
    
    def clear(self) -> None:
        """清空所有注册"""
        self._tool_classes.clear()
        self._tool_infos.clear()


# 全局工具注册表
mcp_tool_registry = MCPToolRegistry()

# 全局插件实例引用
_plugin_instance: Optional["MCPBridgePlugin"] = None


# ============================================================================
# 内置工具
# ============================================================================

class MCPReadResourceTool(BaseTool):
    """v1.2.0: MCP 资源读取工具"""
    
    name = "mcp_read_resource"
    description = "读取 MCP 服务器提供的资源内容（如文件、数据库记录等）。使用前请先用 mcp_status 查看可用资源。"
    parameters = [
        ("uri", ToolParamType.STRING, "资源 URI（如 file:///path/to/file 或自定义 URI）", True, None),
        ("server_name", ToolParamType.STRING, "指定服务器名称（可选，不指定则自动查找）", False, None),
    ]
    available_for_llm = True
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        uri = function_args.get("uri", "")
        server_name = function_args.get("server_name")
        
        if not uri:
            return {"name": self.name, "content": "❌ 请提供资源 URI"}
        
        result = await mcp_manager.read_resource(uri, server_name)
        
        if result.success:
            return {"name": self.name, "content": result.content}
        else:
            return {"name": self.name, "content": f"❌ 读取资源失败: {result.error}"}
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        return await self.execute(function_args)


class MCPGetPromptTool(BaseTool):
    """v1.2.0: MCP 提示模板工具"""
    
    name = "mcp_get_prompt"
    description = "获取 MCP 服务器提供的提示模板内容。使用前请先用 mcp_status 查看可用模板。"
    parameters = [
        ("name", ToolParamType.STRING, "提示模板名称", True, None),
        ("arguments", ToolParamType.STRING, "模板参数（JSON 对象格式）", False, None),
        ("server_name", ToolParamType.STRING, "指定服务器名称（可选）", False, None),
    ]
    available_for_llm = True
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        prompt_name = function_args.get("name", "")
        arguments_str = function_args.get("arguments", "")
        server_name = function_args.get("server_name")
        
        if not prompt_name:
            return {"name": self.name, "content": "❌ 请提供提示模板名称"}
        
        arguments = None
        if arguments_str:
            try:
                arguments = json.loads(arguments_str)
            except json.JSONDecodeError:
                return {"name": self.name, "content": "❌ 参数格式错误，请使用 JSON 对象格式"}
        
        result = await mcp_manager.get_prompt(prompt_name, arguments, server_name)
        
        if result.success:
            return {"name": self.name, "content": result.content}
        else:
            return {"name": self.name, "content": f"❌ 获取提示模板失败: {result.error}"}
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        return await self.execute(function_args)


class MCPStatusTool(BaseTool):
    """MCP 状态查询工具"""
    
    name = "mcp_status"
    description = "查询 MCP 桥接插件的状态，包括服务器连接状态、可用工具列表、资源列表、提示模板列表、调用统计、追踪记录等信息"
    parameters = [
        ("query_type", ToolParamType.STRING, "查询类型", False, ["status", "tools", "resources", "prompts", "stats", "trace", "cache", "all"]),
        ("server_name", ToolParamType.STRING, "指定服务器名称（可选）", False, None),
    ]
    available_for_llm = True
    
    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        query_type = function_args.get("query_type", "status")
        server_name = function_args.get("server_name")
        
        result_parts = []
        
        if query_type in ("status", "all"):
            result_parts.append(self._format_status(server_name))
        
        if query_type in ("tools", "all"):
            result_parts.append(self._format_tools(server_name))
        
        if query_type in ("resources", "all"):
            result_parts.append(self._format_resources(server_name))
        
        if query_type in ("prompts", "all"):
            result_parts.append(self._format_prompts(server_name))
        
        if query_type in ("stats", "all"):
            result_parts.append(self._format_stats(server_name))
        
        # v1.4.0: 追踪记录
        if query_type in ("trace",):
            result_parts.append(self._format_trace())
        
        # v1.4.0: 缓存状态
        if query_type in ("cache",):
            result_parts.append(self._format_cache())
        
        return {
            "name": self.name,
            "content": "\n\n".join(result_parts) if result_parts else "未知的查询类型"
        }
    
    def _format_status(self, server_name: Optional[str] = None) -> str:
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
        tools = mcp_manager.all_tools
        lines = ["🔧 可用 MCP 工具"]
        
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
        stats = mcp_manager.get_all_stats()
        lines = ["📈 调用统计"]
        
        g = stats['global']
        lines.append(f"  总调用次数: {g['total_tool_calls']}")
        lines.append(f"  成功: {g['successful_calls']}, 失败: {g['failed_calls']}")
        if g['total_tool_calls'] > 0:
            success_rate = (g['successful_calls'] / g['total_tool_calls']) * 100
            lines.append(f"  成功率: {success_rate:.1f}%")
        lines.append(f"  运行时间: {g['uptime_seconds']:.0f} 秒")
        
        return "\n".join(lines)
    
    def _format_resources(self, server_name: Optional[str] = None) -> str:
        resources = mcp_manager.all_resources
        if not resources:
            return "📦 当前没有可用的 MCP 资源"
        
        lines = ["📦 可用 MCP 资源"]
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
                lines.append(f"  • {res.name}: {res.uri}")
        
        return "\n".join(lines)
    
    def _format_prompts(self, server_name: Optional[str] = None) -> str:
        prompts = mcp_manager.all_prompts
        if not prompts:
            return "📝 当前没有可用的 MCP 提示模板"
        
        lines = ["📝 可用 MCP 提示模板"]
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
        
        return "\n".join(lines)
    
    def _format_trace(self) -> str:
        """v1.4.0: 格式化追踪记录"""
        records = tool_call_tracer.get_recent(10)
        if not records:
            return "🔍 暂无调用追踪记录"
        
        lines = ["🔍 最近调用追踪记录"]
        for r in reversed(records):
            status = "✅" if r.success else "❌"
            cache = "📦" if r.cache_hit else ""
            post = "🔄" if r.post_processed else ""
            lines.append(f"  {status}{cache}{post} {r.tool_name} ({r.duration_ms:.0f}ms)")
            if r.error:
                lines.append(f"     错误: {r.error[:50]}")
        
        return "\n".join(lines)
    
    def _format_cache(self) -> str:
        """v1.4.0: 格式化缓存状态"""
        stats = tool_call_cache.get_stats()
        lines = ["🗄️ 缓存状态"]
        lines.append(f"  启用: {'是' if stats['enabled'] else '否'}")
        lines.append(f"  条目数: {stats['entries']}/{stats['max_entries']}")
        lines.append(f"  TTL: {stats['ttl']}秒")
        lines.append(f"  命中: {stats['hits']}, 未命中: {stats['misses']}")
        lines.append(f"  命中率: {stats['hit_rate']}")
        return "\n".join(lines)
    
    async def direct_execute(self, **function_args) -> Dict[str, Any]:
        return await self.execute(function_args)


# ============================================================================
# 命令处理
# ============================================================================

class MCPStatusCommand(BaseCommand):
    """MCP 状态查询命令 - 通过 /mcp 命令查看服务器状态"""

    command_name = "mcp_status_command"
    command_description = "查看 MCP 服务器连接状态和统计信息"
    command_pattern = r"^[/／]mcp(?:\s+(?P<subcommand>status|tools|stats|reconnect|trace|cache|perm))?(?:\s+(?P<arg>\S+))?$"

    async def execute(self):
        """执行命令"""
        subcommand = self.matched_groups.get("subcommand", "status") or "status"
        arg = self.matched_groups.get("arg")

        if subcommand == "reconnect":
            return await self._handle_reconnect(arg)
        
        # v1.4.0: 追踪命令
        if subcommand == "trace":
            return await self._handle_trace(arg)
        
        # v1.4.0: 缓存命令
        if subcommand == "cache":
            return await self._handle_cache(arg)
        
        # v1.4.0: 权限命令
        if subcommand == "perm":
            return await self._handle_perm(arg)

        result = self._format_output(subcommand, arg)
        await self.send_text(result)
        return (True, None, True)

    async def _handle_reconnect(self, server_name: str = None):
        """处理重连请求"""
        if server_name:
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
    
    async def _handle_trace(self, arg: str = None):
        """v1.4.0: 处理追踪命令"""
        if arg and arg.isdigit():
            # /mcp trace 20 - 最近 N 条
            n = int(arg)
            records = tool_call_tracer.get_recent(n)
        elif arg:
            # /mcp trace <tool_name> - 特定工具
            records = tool_call_tracer.get_by_tool(arg)
        else:
            # /mcp trace - 最近 10 条
            records = tool_call_tracer.get_recent(10)
        
        if not records:
            await self.send_text("🔍 暂无调用追踪记录")
            return (True, None, True)
        
        lines = [f"🔍 调用追踪记录 ({len(records)} 条)"]
        for r in reversed(records):
            status = "✅" if r.success else "❌"
            cache = "📦" if r.cache_hit else ""
            post = "🔄" if r.post_processed else ""
            ts = time.strftime("%H:%M:%S", time.localtime(r.timestamp))
            lines.append(f"{status}{cache}{post} [{ts}] {r.tool_name}")
            lines.append(f"   耗时: {r.duration_ms:.0f}ms | 服务器: {r.server_name}")
            if r.error:
                lines.append(f"   错误: {r.error[:60]}")
        
        await self.send_text("\n".join(lines))
        return (True, None, True)
    
    async def _handle_cache(self, arg: str = None):
        """v1.4.0: 处理缓存命令"""
        if arg == "clear":
            tool_call_cache.clear()
            await self.send_text("✅ 缓存已清空")
            return (True, None, True)
        
        stats = tool_call_cache.get_stats()
        lines = ["🗄️ 缓存状态"]
        lines.append(f"├ 启用: {'是' if stats['enabled'] else '否'}")
        lines.append(f"├ 条目: {stats['entries']}/{stats['max_entries']}")
        lines.append(f"├ TTL: {stats['ttl']}秒")
        lines.append(f"├ 命中: {stats['hits']}")
        lines.append(f"├ 未命中: {stats['misses']}")
        lines.append(f"└ 命中率: {stats['hit_rate']}")
        
        await self.send_text("\n".join(lines))
        return (True, None, True)
    
    async def _handle_perm(self, arg: str = None):
        """v1.4.0: 处理权限命令"""
        global _plugin_instance
        
        if _plugin_instance is None:
            await self.send_text("❌ 插件未初始化")
            return (True, None, True)
        
        perm_config = _plugin_instance.config.get("permissions", {})
        enabled = perm_config.get("perm_enabled", False)
        default_mode = perm_config.get("perm_default_mode", "allow_all")
        
        if arg:
            # 查看特定工具的权限
            rules = permission_checker.get_rules_for_tool(arg)
            if not rules:
                await self.send_text(f"🔐 工具 {arg} 无特定权限规则\n默认模式: {default_mode}")
            else:
                lines = [f"🔐 工具 {arg} 的权限规则:"]
                for r in rules:
                    lines.append(f"  • 模式: {r.get('mode', 'default')}")
                    if r.get("allowed"):
                        lines.append(f"    允许: {', '.join(r['allowed'][:3])}...")
                    if r.get("denied"):
                        lines.append(f"    拒绝: {', '.join(r['denied'][:3])}...")
                await self.send_text("\n".join(lines))
        else:
            # 查看权限配置概览
            lines = ["🔐 权限控制配置"]
            lines.append(f"├ 启用: {'是' if enabled else '否'}")
            lines.append(f"├ 默认模式: {default_mode}")
            lines.append(f"└ 规则数: {len(permission_checker._rules)}")
            await self.send_text("\n".join(lines))
        
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
                    for t in tool_list[:5]:
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
            lines.append("使用方法: /mcp [status|tools|stats|reconnect|trace|cache|perm] [参数]")

        return "\n".join(lines)


# ============================================================================
# 事件处理器
# ============================================================================

class MCPStartupHandler(BaseEventHandler):
    """MCP 启动事件处理器"""
    
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
        
        await mcp_manager.start_heartbeat()
        
        return (True, True, None, None, None)


class MCPStopHandler(BaseEventHandler):
    """MCP 停止事件处理器"""
    
    event_type = EventType.ON_STOP
    handler_name = "mcp_stop_handler"
    handler_description = "MCP 桥接插件停止处理器"
    weight = 0
    intercept_message = False
    
    async def execute(self, message):
        """处理停止事件"""
        logger.info("MCP 桥接插件收到 ON_STOP 事件，正在关闭...")
        
        await mcp_manager.shutdown()
        mcp_tool_registry.clear()
        
        logger.info("MCP 桥接插件已关闭所有连接")
        return (True, True, None, None, None)


# ============================================================================
# 主插件类
# ============================================================================

@register_plugin
class MCPBridgePlugin(BasePlugin):
    """MCP 桥接插件 v1.4.0 - 将 MCP 服务器的工具桥接到 MaiBot"""
    
    plugin_name: str = "mcp_bridge_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["mcp"]
    config_file_name: str = "config.toml"
    
    config_section_descriptions = {
        "plugin": "插件基本信息",
        "settings": "全局设置",
        "servers": "MCP 服务器配置",
        "tools": "工具管理",
        "permissions": "权限控制",
        "status": "运行状态（只读）",
    }
    
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
                description="🏷️ 工具前缀 - 生成的工具名格式: {前缀}_{服务器名}_{工具名}",
                label="🏷️ 工具前缀",
                placeholder="mcp",
                order=1,
            ),
            "connect_timeout": ConfigField(
                type=float,
                default=30.0,
                description="⏱️ 连接超时（秒）",
                label="⏱️ 连接超时（秒）",
                min=5.0,
                max=120.0,
                step=5.0,
                order=2,
            ),
            "call_timeout": ConfigField(
                type=float,
                default=60.0,
                description="⏱️ 调用超时（秒）",
                label="⏱️ 调用超时（秒）",
                min=10.0,
                max=300.0,
                step=10.0,
                order=3,
            ),
            "auto_connect": ConfigField(
                type=bool,
                default=True,
                description="🔄 启动时自动连接所有已启用的服务器",
                label="🔄 自动连接",
                order=4,
            ),
            "retry_attempts": ConfigField(
                type=int,
                default=3,
                description="🔁 连接失败时的重试次数",
                label="🔁 重试次数",
                min=0,
                max=10,
                order=5,
            ),
            "retry_interval": ConfigField(
                type=float,
                default=5.0,
                description="⏳ 重试间隔（秒）",
                label="⏳ 重试间隔（秒）",
                min=1.0,
                max=60.0,
                step=1.0,
                order=6,
            ),
            "heartbeat_enabled": ConfigField(
                type=bool,
                default=True,
                description="💓 定期检测服务器连接状态",
                label="💓 启用心跳检测",
                order=7,
            ),
            "heartbeat_interval": ConfigField(
                type=float,
                default=60.0,
                description="💓 心跳间隔（秒）",
                label="💓 心跳间隔（秒）",
                min=10.0,
                max=300.0,
                step=10.0,
                order=8,
            ),
            "auto_reconnect": ConfigField(
                type=bool,
                default=True,
                description="🔄 检测到断开时自动尝试重连",
                label="🔄 自动重连",
                order=9,
            ),
            "max_reconnect_attempts": ConfigField(
                type=int,
                default=3,
                description="🔄 连续重连失败后暂停重连",
                label="🔄 最大重连次数",
                min=1,
                max=10,
                order=10,
            ),
            "enable_resources": ConfigField(
                type=bool,
                default=False,
                description="📦 允许读取 MCP 服务器提供的资源",
                label="📦 启用 Resources（实验性）",
                order=11,
            ),
            "enable_prompts": ConfigField(
                type=bool,
                default=False,
                description="📝 允许使用 MCP 服务器提供的提示模板",
                label="📝 启用 Prompts（实验性）",
                order=12,
            ),
            # v1.3.0 后处理配置
            "post_process_enabled": ConfigField(
                type=bool,
                default=False,
                description="🔄 使用 LLM 对长结果进行摘要提炼",
                label="🔄 启用结果后处理",
                order=20,
            ),
            "post_process_threshold": ConfigField(
                type=int,
                default=500,
                description="📏 结果长度超过此值才触发后处理",
                label="📏 后处理阈值（字符）",
                min=100,
                max=5000,
                step=100,
                order=21,
            ),
            "post_process_max_tokens": ConfigField(
                type=int,
                default=500,
                description="📝 LLM 摘要输出的最大 token 数",
                label="📝 后处理最大输出 token",
                min=100,
                max=2000,
                step=50,
                order=22,
            ),
            "post_process_model": ConfigField(
                type=str,
                default="",
                description="🤖 指定用于后处理的模型名称",
                label="🤖 后处理模型（可选）",
                placeholder="留空则使用 Utils 模型组",
                order=23,
            ),
            "post_process_prompt": ConfigField(
                type=str,
                default="""用户问题：{query}

工具返回内容：
{result}

请从上述内容中提取与用户问题最相关的关键信息，简洁准确地输出：""",
                description="📋 后处理提示词模板",
                label="📋 后处理提示词模板",
                input_type="textarea",
                rows=8,
                order=24,
            ),
            # v1.4.0 追踪配置
            "trace_enabled": ConfigField(
                type=bool,
                default=True,
                description="🔍 记录工具调用详情",
                label="🔍 启用调用追踪",
                order=30,
            ),
            "trace_max_records": ConfigField(
                type=int,
                default=100,
                description="内存中保留的最大记录数",
                label="📊 追踪记录上限",
                min=10,
                max=1000,
                order=31,
            ),
            "trace_log_enabled": ConfigField(
                type=bool,
                default=False,
                description="是否将追踪记录写入日志文件",
                label="📝 追踪日志文件",
                hint="启用后记录写入 plugins/MaiBot_MCPBridgePlugin/logs/trace.jsonl",
                order=32,
            ),
            # v1.4.0 缓存配置
            "cache_enabled": ConfigField(
                type=bool,
                default=False,
                description="🗄️ 缓存相同参数的调用结果",
                label="🗄️ 启用调用缓存",
                hint="相同参数的调用会返回缓存结果，减少重复请求",
                order=40,
            ),
            "cache_ttl": ConfigField(
                type=int,
                default=300,
                description="缓存有效期（秒）",
                label="⏱️ 缓存有效期（秒）",
                min=60,
                max=3600,
                order=41,
            ),
            "cache_max_entries": ConfigField(
                type=int,
                default=200,
                description="最大缓存条目数（超出后 LRU 淘汰）",
                label="📦 最大缓存条目",
                min=50,
                max=1000,
                order=42,
            ),
            "cache_exclude_tools": ConfigField(
                type=str,
                default="",
                description="不缓存的工具（每行一个，支持通配符 *）",
                label="🚫 缓存排除列表",
                input_type="textarea",
                rows=4,
                hint="时间类、随机类工具建议排除，如 mcp_time_*",
                order=43,
            ),
        },
        # v1.4.0 工具管理
        "tools": {
            "tool_list": ConfigField(
                type=str,
                default="(启动后自动生成)",
                description="当前已注册的 MCP 工具列表（只读）",
                label="📋 工具清单",
                input_type="textarea",
                disabled=True,
                rows=12,
                hint="从此处复制工具名到下方禁用列表",
                order=1,
            ),
            "disabled_tools": ConfigField(
                type=str,
                default="",
                description="要禁用的工具名（每行一个）",
                label="🚫 禁用工具列表",
                input_type="textarea",
                rows=6,
                hint="从上方工具清单复制工具名，每行一个。禁用后该工具不会被 LLM 调用",
                order=2,
            ),
        },
        # v1.4.0 权限控制
        "permissions": {
            "perm_enabled": ConfigField(
                type=bool,
                default=False,
                description="🔐 按群/用户限制工具使用",
                label="🔐 启用权限控制",
                order=1,
            ),
            "perm_default_mode": ConfigField(
                type=str,
                default="allow_all",
                description="默认模式：allow_all（默认允许）或 deny_all（默认禁止）",
                label="📋 默认模式",
                placeholder="allow_all",
                hint="allow_all: 未配置规则的工具默认允许；deny_all: 未配置规则的工具默认禁止",
                order=2,
            ),
            "perm_rules": ConfigField(
                type=str,
                default="[]",
                description="权限规则（JSON 数组格式）",
                label="📜 权限规则",
                input_type="textarea",
                rows=12,
                placeholder='''[
  {
    "tool": "mcp_filesystem_*",
    "mode": "whitelist",
    "allowed": ["qq:123456789:group", "qq:111111:user"]
  },
  {
    "tool": "mcp_bing_*",
    "denied": ["qq:987654321:group"]
  }
]''',
                hint="""ID 格式：qq:ID:type
• qq:123456:group - QQ群
• qq:123456:private - 私聊
• qq:123456:user - 特定用户（任何场景生效）
工具名支持通配符 *""",
                order=3,
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
                description="MCP 服务器列表配置（JSON 数组格式）",
                label="🔌 服务器列表",
                input_type="textarea",
                rows=18,
                hint="""⚠️ 格式要求：必须是 JSON 数组！
• transport 可选: stdio / sse / http / streamable_http
• stdio 类型需要 command/args/env 字段，其他类型需要 url 字段""",
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
        
        # v1.4.0: 配置追踪器
        trace_log_path = Path(__file__).parent / "logs" / "trace.jsonl"
        tool_call_tracer.configure(
            enabled=settings.get("trace_enabled", True),
            max_records=settings.get("trace_max_records", 100),
            log_enabled=settings.get("trace_log_enabled", False),
            log_path=trace_log_path,
        )
        
        # v1.4.0: 配置缓存
        tool_call_cache.configure(
            enabled=settings.get("cache_enabled", False),
            ttl=settings.get("cache_ttl", 300),
            max_entries=settings.get("cache_max_entries", 200),
            exclude_tools=settings.get("cache_exclude_tools", ""),
        )
        
        # v1.4.0: 配置权限检查器
        perm_config = self.config.get("permissions", {})
        permission_checker.configure(
            enabled=perm_config.get("perm_enabled", False),
            default_mode=perm_config.get("perm_default_mode", "allow_all"),
            rules_json=perm_config.get("perm_rules", "[]"),
        )
        
        # 注册状态变化回调
        mcp_manager.set_status_change_callback(self._update_status_display)
    
    def _get_disabled_tools(self) -> set:
        """v1.4.0: 获取禁用的工具列表"""
        tools_config = self.config.get("tools", {})
        disabled_str = tools_config.get("disabled_tools", "")
        return {t.strip() for t in disabled_str.strip().split("\n") if t.strip()}
    
    async def _async_connect_servers(self) -> None:
        """异步连接所有配置的 MCP 服务器"""
        settings = self.config.get("settings", {})
        
        servers_section = self.config.get("servers", [])
        
        if isinstance(servers_section, dict):
            servers_list = servers_section.get("list", [])
            if isinstance(servers_list, str):
                servers_config = self._parse_servers_json(servers_list)
            elif isinstance(servers_list, list):
                servers_config = servers_list
            else:
                servers_config = []
        else:
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
        disabled_tools = self._get_disabled_tools()
        registered_count = 0
        
        logger.info(f"准备连接 {len(servers_config)} 个 MCP 服务器")
        
        for idx, server_conf in enumerate(servers_config):
            server_name = server_conf.get("name", f"unknown_{idx}")
            logger.info(f"[{idx+1}/{len(servers_config)}] 处理服务器: {server_name}")
            
            if not server_conf.get("enabled", True):
                logger.info(f"服务器 {server_name} 已禁用，跳过")
                continue
            
            try:
                config = self._parse_server_config(server_conf)
            except Exception as e:
                logger.error(f"解析服务器 {server_name} 配置失败: {e}")
                continue
            
            logger.info(f"正在连接服务器: {config.name} ({config.transport.value})")
            success = await mcp_manager.add_server(config)
            if not success:
                logger.warning(f"服务器 {config.name} 连接失败")
                continue
            
            logger.info(f"服务器 {config.name} 连接成功")
            
            if settings.get("enable_resources", False):
                try:
                    await mcp_manager.fetch_resources_for_server(config.name)
                except Exception as e:
                    logger.warning(f"服务器 {config.name} 获取资源列表失败: {e}")
            
            if settings.get("enable_prompts", False):
                try:
                    await mcp_manager.fetch_prompts_for_server(config.name)
                except Exception as e:
                    logger.warning(f"服务器 {config.name} 获取提示模板列表失败: {e}")
            
            # 动态注册工具
            from src.plugin_system.core.component_registry import component_registry
            
            for tool_key, (tool_info, _) in mcp_manager.all_tools.items():
                if tool_info.server_name == config.name:
                    # v1.4.0: 检查是否禁用
                    tool_name = tool_key.replace("-", "_").replace(".", "_")
                    is_disabled = tool_name in disabled_tools
                    
                    info, tool_class = mcp_tool_registry.register_tool(
                        tool_key, tool_info, tool_prefix, disabled=is_disabled
                    )
                    info.plugin_name = self.plugin_name
                    
                    if component_registry.register_component(info, tool_class):
                        registered_count += 1
                        status = "🚫" if is_disabled else "✅"
                        logger.info(f"{status} 注册 MCP 工具: {tool_class.name}")
                    else:
                        logger.warning(f"❌ 注册 MCP 工具失败: {tool_class.name}")
        
        self._initialized = True
        logger.info(f"MCP 桥接插件初始化完成，已注册 {registered_count} 个工具")
        
        # 更新状态显示
        self._update_status_display()
        self._update_tool_list_display()
    
    def _parse_servers_json(self, servers_list: str) -> List[Dict]:
        """解析服务器列表 JSON 字符串"""
        if not servers_list.strip():
            return []
        
        content = servers_list.strip()
        
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                return parsed
            elif isinstance(parsed, dict):
                logger.warning("服务器配置是单个对象，已自动转换为数组")
                return [parsed]
            else:
                logger.error(f"服务器配置格式错误: 期望数组或对象")
                return []
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}")
            
            if content.startswith("{") and not content.startswith("["):
                try:
                    fixed_content = f"[{content}]"
                    parsed = json.loads(fixed_content)
                    if isinstance(parsed, list):
                        logger.warning("✅ 自动修复成功！请修正配置格式")
                        return parsed
                except json.JSONDecodeError:
                    pass
            
            logger.error("❌ 服务器配置 JSON 格式错误")
            return []
    
    def _parse_server_config(self, conf: Dict) -> MCPServerConfig:
        """解析服务器配置字典"""
        transport_str = conf.get("transport", "stdio").lower()
        
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
    
    def _update_tool_list_display(self) -> None:
        """v1.4.0: 更新工具列表显示"""
        import tomlkit
        
        tools = mcp_manager.all_tools
        disabled_tools = self._get_disabled_tools()
        
        lines = []
        by_server: Dict[str, List[str]] = {}
        
        for tool_key, (tool_info, _) in tools.items():
            tool_name = tool_key.replace("-", "_").replace(".", "_")
            if tool_info.server_name not in by_server:
                by_server[tool_info.server_name] = []
            
            is_disabled = tool_name in disabled_tools
            status = " ❌" if is_disabled else ""
            by_server[tool_info.server_name].append(f"  • {tool_name}{status}")
        
        for srv_name, tool_list in by_server.items():
            lines.append(f"📦 {srv_name} ({len(tool_list)}个工具):")
            lines.extend(tool_list)
            lines.append("")
        
        if not by_server:
            lines.append("(无已注册工具)")
        
        tool_list_text = "\n".join(lines)
        
        # 更新内存配置
        if "tools" not in self.config:
            self.config["tools"] = {}
        self.config["tools"]["tool_list"] = tool_list_text
        
        # 写入配置文件
        try:
            config_path = Path(__file__).parent / "config.toml"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
                
                if "tools" not in doc:
                    doc["tools"] = tomlkit.table()
                doc["tools"]["tool_list"] = tool_list_text
                
                with open(config_path, "w", encoding="utf-8") as f:
                    tomlkit.dump(doc, f)
        except Exception as e:
            logger.warning(f"更新工具列表显示失败: {e}")
    
    def _update_status_display(self) -> None:
        """更新配置文件中的状态显示字段"""
        import tomlkit
        
        status = mcp_manager.get_status()
        settings = self.config.get("settings", {})
        lines = []
        
        lines.append(f"服务器: {status['connected_servers']}/{status['total_servers']} 已连接")
        lines.append(f"工具数: {status['total_tools']}")
        if settings.get("enable_resources", False):
            lines.append(f"资源数: {status.get('total_resources', 0)}")
        if settings.get("enable_prompts", False):
            lines.append(f"模板数: {status.get('total_prompts', 0)}")
        lines.append(f"心跳: {'运行中' if status['heartbeat_running'] else '已停止'}")
        lines.append("")
        
        tools = mcp_manager.all_tools
        
        for name, info in status.get("servers", {}).items():
            icon = "✅" if info["connected"] else "❌"
            lines.append(f"{icon} {name} ({info['transport']})")
            
            server_tools = [t.name for key, (t, _) in tools.items() if t.server_name == name]
            if server_tools:
                for tool_name in server_tools:
                    lines.append(f"   • {tool_name}")
            else:
                lines.append("   (无工具)")
        
        if not status.get("servers"):
            lines.append("(无服务器)")
        
        status_text = "\n".join(lines)
        
        if "status" not in self.config:
            self.config["status"] = {}
        self.config["status"]["connection_status"] = status_text
        
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
        except Exception as e:
            logger.warning(f"更新配置文件状态失败: {e}")
    
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件的所有组件"""
        components: List[Tuple[ComponentInfo, Type]] = []
        
        # 事件处理器
        components.append((MCPStartupHandler.get_handler_info(), MCPStartupHandler))
        components.append((MCPStopHandler.get_handler_info(), MCPStopHandler))
        
        # 命令
        components.append((MCPStatusCommand.get_command_info(), MCPStatusCommand))
        
        # 内置工具
        status_tool_info = ToolInfo(
            name=MCPStatusTool.name,
            tool_description=MCPStatusTool.description,
            enabled=True,
            tool_parameters=MCPStatusTool.parameters,
            component_type=ComponentType.TOOL,
        )
        components.append((status_tool_info, MCPStatusTool))
        
        settings = self.config.get("settings", {})
        
        if settings.get("enable_resources", False):
            read_resource_info = ToolInfo(
                name=MCPReadResourceTool.name,
                tool_description=MCPReadResourceTool.description,
                enabled=True,
                tool_parameters=MCPReadResourceTool.parameters,
                component_type=ComponentType.TOOL,
            )
            components.append((read_resource_info, MCPReadResourceTool))
        
        if settings.get("enable_prompts", False):
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
            "trace_records": tool_call_tracer.total_records,
            "cache_stats": tool_call_cache.get_stats(),
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """获取详细统计信息"""
        return mcp_manager.get_all_stats()

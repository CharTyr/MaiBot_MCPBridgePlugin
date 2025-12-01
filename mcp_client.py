"""
MCP 客户端封装模块
负责与 MCP 服务器建立连接、获取工具列表、执行工具调用
"""

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager

from src.common.logger import get_logger

logger = get_logger("mcp_client")


class TransportType(Enum):
    """MCP 传输类型"""
    STDIO = "stdio"              # 本地进程通信
    SSE = "sse"                  # Server-Sent Events (旧版 HTTP)
    HTTP = "http"                # HTTP Streamable (新版，推荐)
    STREAMABLE_HTTP = "streamable_http"  # HTTP Streamable 的别名


@dataclass
class MCPToolInfo:
    """MCP 工具信息"""
    name: str
    description: str
    input_schema: Dict[str, Any]
    server_name: str


@dataclass
class MCPServerConfig:
    """MCP 服务器配置"""
    name: str
    enabled: bool = True
    transport: TransportType = TransportType.STDIO
    # stdio 配置
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    # http 配置
    url: str = ""


@dataclass
class MCPCallResult:
    """MCP 工具调用结果"""
    success: bool
    content: Any
    error: Optional[str] = None


class MCPClientSession:
    """MCP 客户端会话，管理与单个 MCP 服务器的连接"""
    
    def __init__(self, config: MCPServerConfig, call_timeout: float = 60.0):
        self.config = config
        self.call_timeout = call_timeout
        self._session = None
        self._read_stream = None
        self._write_stream = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._tools: List[MCPToolInfo] = []
        self._connected = False
        self._lock = asyncio.Lock()
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    @property
    def tools(self) -> List[MCPToolInfo]:
        return self._tools.copy()
    
    @property
    def server_name(self) -> str:
        return self.config.name

    async def connect(self) -> bool:
        """连接到 MCP 服务器"""
        async with self._lock:
            if self._connected:
                return True
            
            try:
                if self.config.transport == TransportType.STDIO:
                    return await self._connect_stdio()
                elif self.config.transport == TransportType.SSE:
                    return await self._connect_sse()
                elif self.config.transport in (TransportType.HTTP, TransportType.STREAMABLE_HTTP):
                    return await self._connect_http()
                else:
                    logger.error(f"[{self.server_name}] 不支持的传输类型: {self.config.transport}")
                    return False
            except Exception as e:
                logger.error(f"[{self.server_name}] 连接失败: {e}")
                self._connected = False
                return False
    
    async def _connect_stdio(self) -> bool:
        """通过 stdio 连接 MCP 服务器"""
        try:
            # 尝试导入 mcp 库
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
            except ImportError:
                logger.error(f"[{self.server_name}] 未安装 mcp 库，请运行: pip install mcp")
                return False
            
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env if self.config.env else None
            )
            
            # 创建 stdio 客户端连接
            self._stdio_context = stdio_client(server_params)
            self._read_stream, self._write_stream = await self._stdio_context.__aenter__()
            
            # 创建会话
            self._session_context = ClientSession(self._read_stream, self._write_stream)
            self._session = await self._session_context.__aenter__()
            
            # 初始化连接
            await self._session.initialize()
            
            # 获取工具列表
            await self._fetch_tools()
            
            self._connected = True
            logger.info(f"[{self.server_name}] stdio 连接成功，发现 {len(self._tools)} 个工具")
            return True
            
        except Exception as e:
            logger.error(f"[{self.server_name}] stdio 连接失败: {e}")
            await self._cleanup()
            return False
    
    async def _connect_sse(self) -> bool:
        """通过 SSE (Server-Sent Events) 连接 MCP 服务器"""
        try:
            try:
                from mcp import ClientSession
                from mcp.client.sse import sse_client
            except ImportError:
                logger.error(f"[{self.server_name}] 未安装 mcp 库，请运行: pip install mcp")
                return False
            
            if not self.config.url:
                logger.error(f"[{self.server_name}] SSE 传输需要配置 url")
                return False
            
            logger.debug(f"[{self.server_name}] 正在连接 SSE MCP 服务器: {self.config.url}")
            
            # 创建 SSE 客户端连接
            self._sse_context = sse_client(
                url=self.config.url,
                timeout=60.0,
                sse_read_timeout=300.0,
            )
            self._read_stream, self._write_stream = await self._sse_context.__aenter__()
            
            logger.debug(f"[{self.server_name}] SSE 传输层已建立，正在创建会话...")
            
            # 创建会话
            self._session_context = ClientSession(self._read_stream, self._write_stream)
            self._session = await self._session_context.__aenter__()
            
            logger.debug(f"[{self.server_name}] 会话已创建，正在初始化...")
            
            # 初始化连接
            await self._session.initialize()
            
            logger.debug(f"[{self.server_name}] 初始化完成，正在获取工具列表...")
            
            # 获取工具列表
            await self._fetch_tools()
            
            self._connected = True
            logger.info(f"[{self.server_name}] SSE 连接成功，发现 {len(self._tools)} 个工具")
            return True
            
        except Exception as e:
            logger.error(f"[{self.server_name}] SSE 连接失败: {e}")
            import traceback
            logger.debug(f"[{self.server_name}] 详细错误: {traceback.format_exc()}")
            await self._cleanup()
            return False
    
    async def _connect_http(self) -> bool:
        """通过 HTTP Streamable 连接 MCP 服务器（推荐）"""
        try:
            try:
                from mcp import ClientSession
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError:
                logger.error(f"[{self.server_name}] 未安装 mcp 库，请运行: pip install mcp")
                return False
            
            if not self.config.url:
                logger.error(f"[{self.server_name}] HTTP 传输需要配置 url")
                return False
            
            logger.debug(f"[{self.server_name}] 正在连接 HTTP MCP 服务器: {self.config.url}")
            
            # 创建 HTTP 客户端连接（使用更长的超时时间）
            self._http_context = streamablehttp_client(
                url=self.config.url,
                timeout=60.0,  # HTTP 请求超时
                sse_read_timeout=300.0,  # SSE 读取超时
            )
            self._read_stream, self._write_stream, self._get_session_id = await self._http_context.__aenter__()
            
            logger.debug(f"[{self.server_name}] HTTP 传输层已建立，正在创建会话...")
            
            # 创建会话
            self._session_context = ClientSession(self._read_stream, self._write_stream)
            self._session = await self._session_context.__aenter__()
            
            logger.debug(f"[{self.server_name}] 会话已创建，正在初始化...")
            
            # 初始化连接
            await self._session.initialize()
            
            logger.debug(f"[{self.server_name}] 初始化完成，正在获取工具列表...")
            
            # 获取工具列表
            await self._fetch_tools()
            
            self._connected = True
            logger.info(f"[{self.server_name}] HTTP 连接成功，发现 {len(self._tools)} 个工具")
            return True
            
        except Exception as e:
            logger.error(f"[{self.server_name}] HTTP 连接失败: {e}")
            import traceback
            logger.debug(f"[{self.server_name}] 详细错误: {traceback.format_exc()}")
            await self._cleanup()
            return False

    async def _fetch_tools(self) -> None:
        """获取 MCP 服务器的工具列表"""
        if not self._session:
            return
        
        try:
            result = await self._session.list_tools()
            self._tools = []
            
            for tool in result.tools:
                tool_info = MCPToolInfo(
                    name=tool.name,
                    description=tool.description or f"MCP tool: {tool.name}",
                    input_schema=tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                    server_name=self.server_name
                )
                self._tools.append(tool_info)
                logger.debug(f"[{self.server_name}] 发现工具: {tool.name}")
                
        except Exception as e:
            logger.error(f"[{self.server_name}] 获取工具列表失败: {e}")
            self._tools = []
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPCallResult:
        """调用 MCP 工具"""
        if not self._connected or not self._session:
            return MCPCallResult(
                success=False,
                content=None,
                error=f"服务器 {self.server_name} 未连接"
            )
        
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool_name, arguments=arguments),
                timeout=self.call_timeout
            )
            
            # 处理返回内容
            content_parts = []
            for content in result.content:
                if hasattr(content, 'text'):
                    content_parts.append(content.text)
                elif hasattr(content, 'data'):
                    content_parts.append(f"[二进制数据: {len(content.data)} bytes]")
                else:
                    content_parts.append(str(content))
            
            return MCPCallResult(
                success=True,
                content="\n".join(content_parts) if content_parts else "执行成功（无返回内容）"
            )
            
        except asyncio.TimeoutError:
            return MCPCallResult(
                success=False,
                content=None,
                error=f"工具调用超时（{self.call_timeout}秒）"
            )
        except Exception as e:
            logger.error(f"[{self.server_name}] 调用工具 {tool_name} 失败: {e}")
            return MCPCallResult(
                success=False,
                content=None,
                error=str(e)
            )
    
    async def disconnect(self) -> None:
        """断开连接"""
        async with self._lock:
            await self._cleanup()
    
    async def _cleanup(self) -> None:
        """清理资源"""
        self._connected = False
        self._tools = []
        
        try:
            if hasattr(self, '_session_context') and self._session_context:
                await self._session_context.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"[{self.server_name}] 关闭会话时出错: {e}")
        
        try:
            if hasattr(self, '_stdio_context') and self._stdio_context:
                await self._stdio_context.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"[{self.server_name}] 关闭 stdio 连接时出错: {e}")
        
        try:
            if hasattr(self, '_http_context') and self._http_context:
                await self._http_context.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"[{self.server_name}] 关闭 HTTP 连接时出错: {e}")
        
        try:
            if hasattr(self, '_sse_context') and self._sse_context:
                await self._sse_context.__aexit__(None, None, None)
        except Exception as e:
            logger.debug(f"[{self.server_name}] 关闭 SSE 连接时出错: {e}")
        
        self._session = None
        self._read_stream = None
        self._write_stream = None
        
        logger.debug(f"[{self.server_name}] 连接已关闭")


class MCPClientManager:
    """MCP 客户端管理器，管理多个 MCP 服务器连接"""
    
    _instance: Optional["MCPClientManager"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._clients: Dict[str, MCPClientSession] = {}
        self._all_tools: Dict[str, Tuple[MCPToolInfo, MCPClientSession]] = {}
        self._settings: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
    
    def configure(self, settings: Dict[str, Any]) -> None:
        """配置管理器"""
        self._settings = settings
    
    @property
    def all_tools(self) -> Dict[str, Tuple[MCPToolInfo, MCPClientSession]]:
        """获取所有已注册的工具"""
        return self._all_tools.copy()
    
    @property
    def connected_servers(self) -> List[str]:
        """获取已连接的服务器列表"""
        return [name for name, client in self._clients.items() if client.is_connected]
    
    async def add_server(self, config: MCPServerConfig) -> bool:
        """添加并连接 MCP 服务器"""
        async with self._lock:
            if config.name in self._clients:
                logger.warning(f"服务器 {config.name} 已存在")
                return False
            
            call_timeout = self._settings.get("call_timeout", 60.0)
            client = MCPClientSession(config, call_timeout)
            self._clients[config.name] = client
            
            if not config.enabled:
                logger.info(f"服务器 {config.name} 已添加但未启用")
                return True
            
            # 尝试连接
            retry_attempts = self._settings.get("retry_attempts", 3)
            retry_interval = self._settings.get("retry_interval", 5.0)
            
            for attempt in range(1, retry_attempts + 1):
                if await client.connect():
                    # 注册工具
                    self._register_tools(client)
                    return True
                
                if attempt < retry_attempts:
                    logger.warning(f"服务器 {config.name} 连接失败，{retry_interval}秒后重试 ({attempt}/{retry_attempts})")
                    await asyncio.sleep(retry_interval)
            
            logger.error(f"服务器 {config.name} 连接失败，已达最大重试次数")
            return False
    
    def _register_tools(self, client: MCPClientSession) -> None:
        """注册客户端的工具"""
        tool_prefix = self._settings.get("tool_prefix", "mcp")
        
        for tool in client.tools:
            # 生成唯一的工具名称
            # 如果工具名已经包含服务器名前缀，则不再添加
            if tool.name.startswith(f"{tool_prefix}_{client.server_name}_"):
                tool_key = tool.name
            else:
                tool_key = f"{tool_prefix}_{client.server_name}_{tool.name}"
            self._all_tools[tool_key] = (tool, client)
            logger.debug(f"注册 MCP 工具: {tool_key}")
    
    def _unregister_tools(self, server_name: str) -> None:
        """注销服务器的工具"""
        tool_prefix = self._settings.get("tool_prefix", "mcp")
        prefix = f"{tool_prefix}_{server_name}_"
        
        keys_to_remove = [k for k in self._all_tools.keys() if k.startswith(prefix)]
        for key in keys_to_remove:
            del self._all_tools[key]
            logger.debug(f"注销 MCP 工具: {key}")
    
    async def remove_server(self, server_name: str) -> bool:
        """移除 MCP 服务器"""
        async with self._lock:
            if server_name not in self._clients:
                return False
            
            client = self._clients[server_name]
            await client.disconnect()
            self._unregister_tools(server_name)
            del self._clients[server_name]
            
            logger.info(f"服务器 {server_name} 已移除")
            return True
    
    async def reconnect_server(self, server_name: str) -> bool:
        """重新连接服务器"""
        async with self._lock:
            if server_name not in self._clients:
                return False
            
            client = self._clients[server_name]
            self._unregister_tools(server_name)
            await client.disconnect()
            
            if await client.connect():
                self._register_tools(client)
                return True
            return False
    
    async def call_tool(self, tool_key: str, arguments: Dict[str, Any]) -> MCPCallResult:
        """调用 MCP 工具"""
        if tool_key not in self._all_tools:
            return MCPCallResult(
                success=False,
                content=None,
                error=f"工具 {tool_key} 不存在"
            )
        
        tool_info, client = self._all_tools[tool_key]
        return await client.call_tool(tool_info.name, arguments)
    
    async def shutdown(self) -> None:
        """关闭所有连接"""
        async with self._lock:
            for client in self._clients.values():
                await client.disconnect()
            self._clients.clear()
            self._all_tools.clear()
            logger.info("MCP 客户端管理器已关闭")
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态信息"""
        return {
            "total_servers": len(self._clients),
            "connected_servers": len(self.connected_servers),
            "total_tools": len(self._all_tools),
            "servers": {
                name: {
                    "connected": client.is_connected,
                    "tools_count": len(client.tools)
                }
                for name, client in self._clients.items()
            }
        }


# 全局单例
mcp_manager = MCPClientManager()

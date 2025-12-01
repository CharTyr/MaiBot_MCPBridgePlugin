# MCP 桥接插件 - 开发文档

本文档面向希望参与插件开发或进行二次开发的开发者。

## 项目结构

```
MCPBridgePlugin/
├── __init__.py           # 模块导出
├── _manifest.json        # 插件清单（MaiBot 插件系统要求）
├── .gitignore            # Git 忽略规则
├── config.example.toml   # 配置文件示例
├── mcp_client.py         # MCP 客户端封装（核心）
├── plugin.py             # MaiBot 插件主逻辑
├── README.md             # 用户文档
└── DEVELOPMENT.md        # 开发文档（本文件）
```

## 核心模块说明

### 1. mcp_client.py - MCP 客户端封装

这是与 MCP 服务器通信的核心模块。

#### 主要类

```python
class TransportType(Enum):
    """MCP 传输类型"""
    STDIO = "stdio"              # 本地进程通信
    SSE = "sse"                  # Server-Sent Events
    HTTP = "http"                # HTTP Streamable（推荐）
    STREAMABLE_HTTP = "streamable_http"
```

```python
@dataclass
class MCPServerConfig:
    """MCP 服务器配置"""
    name: str                    # 服务器名称（唯一标识）
    enabled: bool = True         # 是否启用
    transport: TransportType     # 传输方式
    command: str = ""            # stdio 模式的启动命令
    args: List[str] = []         # stdio 模式的命令参数
    env: Dict[str, str] = {}     # stdio 模式的环境变量
    url: str = ""                # HTTP/SSE 模式的服务器 URL
```

```python
@dataclass
class MCPToolInfo:
    """MCP 工具信息"""
    name: str                    # 工具名称
    description: str             # 工具描述
    input_schema: Dict[str, Any] # 参数 JSON Schema
    server_name: str             # 所属服务器名称
```

```python
@dataclass
class MCPCallResult:
    """MCP 工具调用结果"""
    success: bool                # 是否成功
    content: Any                 # 返回内容
    error: Optional[str] = None  # 错误信息
```

#### MCPClientSession

管理与单个 MCP 服务器的连接。

```python
class MCPClientSession:
    async def connect(self) -> bool:
        """连接到 MCP 服务器，返回是否成功"""
    
    async def disconnect(self) -> None:
        """断开连接"""
    
    async def call_tool(self, tool_name: str, arguments: Dict) -> MCPCallResult:
        """调用 MCP 工具"""
    
    @property
    def tools(self) -> List[MCPToolInfo]:
        """获取服务器提供的工具列表"""
    
    @property
    def is_connected(self) -> bool:
        """是否已连接"""
```

#### MCPClientManager

全局单例，管理多个 MCP 服务器连接。

```python
class MCPClientManager:
    async def add_server(self, config: MCPServerConfig) -> bool:
        """添加并连接服务器"""
    
    async def remove_server(self, server_name: str) -> bool:
        """移除服务器"""
    
    async def reconnect_server(self, server_name: str) -> bool:
        """重新连接服务器"""
    
    async def call_tool(self, tool_key: str, arguments: Dict) -> MCPCallResult:
        """调用工具（tool_key 格式: {prefix}_{server}_{tool}）"""
    
    async def shutdown(self) -> None:
        """关闭所有连接"""
    
    @property
    def all_tools(self) -> Dict[str, Tuple[MCPToolInfo, MCPClientSession]]:
        """获取所有已注册的工具"""
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态信息"""

# 全局单例
mcp_manager = MCPClientManager()
```

### 2. plugin.py - MaiBot 插件主逻辑

#### 核心函数

```python
def convert_json_type_to_tool_param_type(json_type: str) -> ToolParamType:
    """将 JSON Schema 类型转换为 MaiBot 的 ToolParamType"""
    # MaiBot 支持: STRING, INTEGER, FLOAT, BOOLEAN
    # array/object 转为 STRING（JSON 字符串形式）

def parse_mcp_parameters(input_schema: Dict) -> List[Tuple]:
    """解析 MCP 工具的参数 schema，转换为 MaiBot 参数格式"""

def create_mcp_tool_class(tool_key, tool_info, tool_prefix) -> Type[MCPToolProxy]:
    """根据 MCP 工具信息动态创建 BaseTool 子类"""
```

#### MCPToolProxy

MCP 工具的代理基类，所有 MCP 工具都继承自此类。

```python
class MCPToolProxy(BaseTool):
    """MCP 工具代理基类"""
    
    # 类属性（由动态子类覆盖）
    name: str = ""
    description: str = ""
    parameters: List[Tuple] = []
    available_for_llm: bool = True
    
    # MCP 相关属性
    _mcp_tool_key: str = ""      # 在 mcp_manager 中的工具键
    _mcp_original_name: str = "" # MCP 服务器中的原始工具名
    _mcp_server_name: str = ""   # MCP 服务器名称
    
    async def execute(self, function_args: Dict) -> Dict:
        """执行 MCP 工具调用"""
    
    async def direct_execute(self, **function_args) -> Dict:
        """直接执行（供其他插件调用）"""
```

#### MCPToolRegistry

管理动态创建的工具类。

```python
class MCPToolRegistry:
    def register_tool(self, tool_key, tool_info, tool_prefix) -> Tuple[ToolInfo, Type]:
        """注册 MCP 工具，返回组件信息和工具类"""
    
    def unregister_tool(self, tool_key: str) -> bool:
        """注销工具"""
    
    def clear(self) -> None:
        """清空所有注册"""

# 全局实例
mcp_tool_registry = MCPToolRegistry()
```

#### 事件处理器

```python
class MCPStartupHandler(BaseEventHandler):
    """ON_START 事件处理器 - 启动时连接 MCP 服务器"""
    event_type = EventType.ON_START

class MCPStopHandler(BaseEventHandler):
    """ON_STOP 事件处理器 - 停止时关闭连接"""
    event_type = EventType.ON_STOP
```

#### MCPBridgePlugin

主插件类。

```python
@register_plugin
class MCPBridgePlugin(BasePlugin):
    plugin_name = "mcp_bridge_plugin"
    python_dependencies = ["mcp"]
    
    async def _async_connect_servers(self) -> None:
        """异步连接所有配置的 MCP 服务器"""
    
    def _parse_server_config(self, conf: Dict) -> MCPServerConfig:
        """解析服务器配置字典"""
    
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件组件（事件处理器）"""
    
    def get_status(self) -> Dict[str, Any]:
        """获取插件状态"""
```

## 数据流

```
1. MaiBot 启动
   ↓
2. MCPBridgePlugin.__init__() 初始化
   ↓
3. ON_START 事件触发 MCPStartupHandler
   ↓
4. _async_connect_servers() 连接所有服务器
   ↓
5. 对每个服务器:
   - MCPClientManager.add_server() 连接
   - 获取工具列表
   - MCPToolRegistry.register_tool() 创建工具类
   - component_registry.register_component() 注册到 MaiBot
   ↓
6. LLM 可以看到并调用这些工具
   ↓
7. 工具调用时:
   - MCPToolProxy.execute() 被调用
   - mcp_manager.call_tool() 转发到 MCP 服务器
   - 返回结果给 LLM
   ↓
8. ON_STOP 事件触发 MCPStopHandler
   ↓
9. mcp_manager.shutdown() 关闭所有连接
```

## 配置格式

插件支持两种配置格式：

### 1. WebUI JSON 格式（推荐）

```toml
[servers]
list = '''
[
  {
    "name": "server-name",
    "enabled": true,
    "transport": "http",
    "url": "https://example.com/mcp"
  }
]
'''
```

### 2. TOML 数组格式（已废弃，但仍支持）

```toml
[[servers]]
name = "server-name"
enabled = true
transport = "http"
url = "https://example.com/mcp"
```

代码中通过 `_async_connect_servers()` 自动检测并处理两种格式。

## 扩展开发指南

### 添加新的传输类型

1. 在 `mcp_client.py` 的 `TransportType` 枚举中添加新类型
2. 在 `MCPClientSession` 中添加 `_connect_xxx()` 方法
3. 在 `connect()` 方法中添加分支处理
4. 在 `_cleanup()` 中添加资源清理逻辑

### 添加新的配置项

1. 在 `plugin.py` 的 `config_schema` 中添加 `ConfigField`
2. 在 `config.example.toml` 中添加示例
3. 在相应的方法中读取和使用配置

### 添加工具过滤/转换

在 `create_mcp_tool_class()` 或 `MCPToolRegistry.register_tool()` 中添加逻辑。

### 添加运行时管理 API

可以在 `MCPBridgePlugin` 中添加方法，通过 MaiBot 的插件 API 暴露：

```python
async def add_server_runtime(self, config: Dict) -> bool:
    """运行时添加服务器"""
    
async def remove_server_runtime(self, server_name: str) -> bool:
    """运行时移除服务器"""
```

## 调试技巧

### 启用详细日志

MCP 客户端使用 `src.common.logger` 记录日志，日志名为 `mcp_client` 和 `mcp_bridge_plugin`。

### 检查连接状态

```python
from plugins.MCPBridgePlugin import mcp_manager

status = mcp_manager.get_status()
print(status)
# {
#   "total_servers": 1,
#   "connected_servers": 1,
#   "total_tools": 5,
#   "servers": {
#     "howtocook": {"connected": True, "tools_count": 5}
#   }
# }
```

### 手动调用工具

```python
result = await mcp_manager.call_tool("mcp_howtocook_whatToEat", {})
print(result.content)
```

## 依赖说明

- `mcp` - MCP Python SDK，提供客户端实现
  - `mcp.ClientSession` - 会话管理
  - `mcp.client.stdio` - stdio 传输
  - `mcp.client.sse` - SSE 传输
  - `mcp.client.streamable_http` - HTTP Streamable 传输

## 已知限制

1. **参数类型限制**: MaiBot 的 `ToolParamType` 只支持 STRING, INTEGER, FLOAT, BOOLEAN，复杂类型（array, object）会转为 JSON 字符串
2. **动态注册**: 工具在 ON_START 后动态注册，无法在 `get_plugin_components()` 中返回
3. **WebUI 配置**: 服务器列表使用 JSON 编辑器，不支持可视化表单编辑

## 贡献指南

1. Fork 仓库
2. 创建功能分支: `git checkout -b feature/xxx`
3. 提交更改: `git commit -m "feat: xxx"`
4. 推送分支: `git push origin feature/xxx`
5. 创建 Pull Request

## 许可证

AGPL-3.0

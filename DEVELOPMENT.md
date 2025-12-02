# MCP 桥接插件 - 开发文档

本文档面向希望参与插件开发或进行二次开发的开发者。

## 版本历史

### v1.4.x (当前版本)
- ✅ 工具禁用管理 - WebUI 直接禁用特定工具
- ✅ 调用链路追踪 - 记录每次调用详情
- ✅ 工具调用缓存 - LRU 缓存相同参数调用
- ✅ 工具权限控制 - 按群/用户限制工具使用
- ✅ 快速入门引导 - WebUI 新手友好
- ✅ 权限快捷配置 - 无需写 JSON

### v1.3.0
- ✅ 结果后处理 - 使用 LLM 对长结果进行摘要提炼

### v1.2.0
- ✅ Resources 支持 - 读取 MCP 服务器提供的资源（实验性）
- ✅ Prompts 支持 - 使用 MCP 服务器提供的提示模板（实验性）

### v1.1.x
- ✅ 心跳检测、自动重连、调用统计
- ✅ `/mcp` 命令、WebUI 状态显示
- ✅ 内置状态查询工具 `mcp_status`

### v1.0.0
- 基础 MCP 桥接功能

---

## 项目结构

```
MCPBridgePlugin/
├── __init__.py           # 模块导出
├── _manifest.json        # 插件清单
├── config.example.toml   # 配置示例
├── config.toml           # 运行时配置
├── mcp_client.py         # MCP 客户端封装（核心）
├── plugin.py             # MaiBot 插件主逻辑
├── requirements.txt      # 依赖
├── README.md             # 用户文档
├── DEVELOPMENT.md        # 开发文档（本文件）
└── logs/                 # 追踪日志目录（自动创建）
```

---

## 核心模块

### mcp_client.py

与 MCP 服务器通信的核心模块。

```python
# 传输类型
class TransportType(Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    STREAMABLE_HTTP = "streamable_http"

# 服务器配置
@dataclass
class MCPServerConfig:
    name: str
    enabled: bool
    transport: TransportType
    command: str = ""      # stdio
    args: List[str] = []   # stdio
    url: str = ""          # http/sse

# 全局管理器
mcp_manager = MCPClientManager()
```

### plugin.py

MaiBot 插件主逻辑，v1.4.0 新增模块：

```python
# 调用追踪
@dataclass
class ToolCallRecord:
    call_id: str
    timestamp: float
    tool_name: str
    server_name: str
    arguments: Dict
    raw_result: str
    duration_ms: float
    success: bool
    cache_hit: bool
    post_processed: bool

tool_call_tracer = ToolCallTracer()

# 调用缓存
@dataclass
class CacheEntry:
    tool_name: str
    args_hash: str
    result: str
    expires_at: float
    hit_count: int

tool_call_cache = ToolCallCache()

# 权限检查
permission_checker = PermissionChecker()
```

---

## 数据流

```
MaiBot 启动
    ↓
MCPBridgePlugin.__init__()
    ├─ 配置 mcp_manager
    ├─ 配置 tool_call_tracer
    ├─ 配置 tool_call_cache
    └─ 配置 permission_checker
    ↓
ON_START → _async_connect_servers()
    ├─ 连接服务器
    ├─ 获取工具列表
    ├─ 检查禁用列表
    └─ 注册工具到 MaiBot
    ↓
LLM 调用工具 → MCPToolProxy.execute()
    ├─ 权限检查 (permission_checker)
    ├─ 缓存检查 (tool_call_cache)
    ├─ 调用 MCP 服务器
    ├─ 后处理 (可选)
    └─ 记录追踪 (tool_call_tracer)
    ↓
ON_STOP → mcp_manager.shutdown()
```

---

## v1.4.0 新增功能详解

### 工具禁用管理

```python
# 配置
[tools]
disabled_tools = "mcp_xxx_delete_file\nmcp_xxx_write_file"

# 实现：在注册工具时检查
disabled_tools = self._get_disabled_tools()
is_disabled = tool_name in disabled_tools
tool_class.available_for_llm = not is_disabled
```

### 调用链路追踪

```python
class ToolCallTracer:
    _records: deque[ToolCallRecord]  # 环形缓冲
    
    def record(self, record: ToolCallRecord) -> None
    def get_recent(self, n: int) -> List[ToolCallRecord]
    def get_by_tool(self, tool_name: str) -> List[ToolCallRecord]

# 配置
trace_enabled = True
trace_max_records = 100
trace_log_enabled = False  # 写入 logs/trace.jsonl
```

### 工具调用缓存

```python
class ToolCallCache:
    _cache: OrderedDict[str, CacheEntry]  # LRU
    
    def get(self, tool_name, args) -> Optional[str]
    def set(self, tool_name, args, result) -> None
    
    # 缓存键 = MD5(tool_name + sorted_json_args)

# 配置
cache_enabled = False
cache_ttl = 300
cache_max_entries = 200
cache_exclude_tools = "mcp_*_time_*"  # 支持通配符
```

### 工具权限控制

```python
class PermissionChecker:
    _quick_deny_groups: set      # 快捷禁用群
    _quick_allow_users: set      # 快捷管理员白名单
    _rules: List[Dict]           # 高级规则
    
    def check(self, tool_name, chat_id, user_id, is_group) -> bool

# 检查优先级：
# 1. 管理员白名单 → 允许
# 2. 禁用群列表 → 拒绝
# 3. 高级规则匹配
# 4. 默认模式 (allow_all / deny_all)
```

---

## 扩展开发

### 添加新传输类型

1. `TransportType` 枚举添加类型
2. `MCPClientSession._connect_xxx()` 实现连接
3. `connect()` 添加分支

### 添加新配置项

1. `config_schema` 添加 `ConfigField`
2. `config.example.toml` 添加示例
3. `__init__` 或相应方法中读取使用

### 添加新命令

在 `MCPStatusCommand.command_pattern` 添加子命令，实现 `_handle_xxx()` 方法。

---

## 调试

```python
# 检查状态
from plugins.MCPBridgePlugin import mcp_manager, tool_call_tracer, tool_call_cache

mcp_manager.get_status()
tool_call_tracer.get_recent(10)
tool_call_cache.get_stats()

# 手动调用
result = await mcp_manager.call_tool("mcp_xxx_tool", {"arg": "value"})
```

---

## 依赖

- `mcp>=1.0.0` - MCP Python SDK

## 许可证

AGPL-3.0

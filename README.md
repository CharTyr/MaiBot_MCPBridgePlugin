# MCP 桥接插件

将 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 服务器的工具桥接到 MaiBot，使麦麦能够调用外部 MCP 工具。

## 功能特性

- 🔌 支持多个 MCP 服务器同时连接
- 🔄 自动发现并注册 MCP 工具为 MaiBot 原生工具
- 📡 支持 stdio、SSE、HTTP 三种传输方式
- 🔁 连接失败自动重试
- ⚡ 工具参数自动转换
- 🖥️ 支持 WebUI 配置（包括服务器列表）
- 💓 **v1.1.0** 心跳检测 - 定期检测服务器连接状态
- 🔄 **v1.1.0** 自动重连 - 检测到断开时自动尝试重连
- 📊 **v1.1.0** 调用统计 - 记录工具调用次数、成功率、耗时
- 🛠️ **v1.1.0** 内置状态工具 - 通过 `mcp_status` 查询连接状态
- 📦 **v1.2.0** Resources 支持 - 读取 MCP 服务器提供的资源（实验性）
- 📝 **v1.2.0** Prompts 支持 - 使用 MCP 服务器提供的提示模板（实验性）

- <img width="3012" height="1794" alt="image" src="https://github.com/user-attachments/assets/ece56404-301a-4abf-b16d-87bd430fc977" />


## 安装

### 1. 克隆插件到 MaiBot 插件目录

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/CharTyr/MaiBot_MCPBridgePlugin.git MCPBridgePlugin
```

### 2. 安装依赖

```bash
pip install mcp
```

### 3. 配置插件

复制示例配置文件：

```bash
cd MCPBridgePlugin
cp config.example.toml config.toml
```

然后编辑 `config.toml`，添加你的 MCP 服务器配置。

## 配置说明

### WebUI 配置支持

本插件完全支持通过 MaiBot WebUI 进行配置：

- ✅ 插件启用/禁用
- ✅ 全局设置（工具前缀、超时时间、重试配置等）
- ✅ 服务器列表（通过 JSON 编辑器添加/修改/删除服务器）

在 WebUI 的服务器配置中，使用 JSON 格式编辑服务器列表（以下远程mcp服务器地址均为虚构不要直接套用）：

```json
[
  {
    "name": "howtocook",
    "enabled": true,
    "transport": "http",
    "url": "https://mcp.api-inference.modelscope.net/今天吃什么/mcp"
  },
  {
    "name": "filesystem",
    "enabled": false,
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
    "env": {}
  }
]
```

### 全局设置 `[settings]`

| 配置项 | 类型 | 默认值 | 说明 | WebUI |
|--------|------|--------|------|-------|
| `tool_prefix` | string | "mcp" | 工具名称前缀，用于区分 MCP 工具 | ✅ |
| `connect_timeout` | float | 30.0 | 连接超时时间（秒） | ✅ |
| `call_timeout` | float | 60.0 | 工具调用超时时间（秒） | ✅ |
| `auto_connect` | bool | true | 启动时自动连接所有服务器 | ✅ |
| `retry_attempts` | int | 3 | 连接失败重试次数 | ✅ |
| `retry_interval` | float | 5.0 | 重试间隔（秒） | ✅ |
| `heartbeat_enabled` | bool | true | 启用心跳检测 | ✅ |
| `heartbeat_interval` | float | 60.0 | 心跳检测间隔（秒） | ✅ |
| `auto_reconnect` | bool | true | 检测到断开时自动重连 | ✅ |
| `max_reconnect_attempts` | int | 3 | 最大连续重连次数 | ✅ |
| `enable_resources` | bool | false | 启用 Resources 支持（实验性） | ✅ |
| `enable_prompts` | bool | false | 启用 Prompts 支持（实验性） | ✅ |

### 服务器配置

在 `config.toml` 中使用 JSON 格式配置服务器列表（用三个单引号包裹多行 JSON）：

```toml
[servers]
list = '''
[
  {
    "name": "my-server",
    "enabled": true,
    "transport": "http",
    "url": "https://your-mcp-server.com/mcp"
  }
]
'''
```

> 注意：TOML 中用 `'''` 三个单引号包裹多行字符串，这样 JSON 中的双引号不需要转义。

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `name` | string | 服务器名称（唯一标识） |
| `enabled` | bool | 是否启用 |
| `transport` | string | 传输方式：`stdio`、`sse` 或 `http` |
| `command` | string | (stdio) 启动命令 |
| `args` | array | (stdio) 命令参数 |
| `env` | object | (stdio) 环境变量 |
| `url` | string | (sse/http) 服务器 URL |

## 配置示例

### HTTP 方式（推荐用于远程服务器）

```json
{
  "name": "howtocook",
  "enabled": true,
  "transport": "http",
  "url": "https://mcp.api-inference.modelscope.net/今天吃什么/mcp"
}
```

### stdio 方式（用于本地 MCP 服务器）

```json
{
  "name": "filesystem",
  "enabled": true,
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
}
```

### SSE 方式

```json
{
  "name": "my-sse-server",
  "enabled": true,
  "transport": "sse",
  "url": "http://localhost:8080/sse"
}
```

## 工具命名规则

MCP 工具在 MaiBot 中的名称格式为：

```
{tool_prefix}_{server_name}_{original_tool_name}
```

例如：`mcp_howtocook_whatToEat`

## v1.2.0 新功能：Resources 和 Prompts

### Resources（资源）

MCP Resources 允许服务器暴露数据（如文件内容、数据库记录等）供客户端读取。

**启用方法：**
```toml
[settings]
enable_resources = true
```

**可用工具：**
- `mcp_status(query_type="resources")` - 列出所有可用资源
- `mcp_read_resource` - 读取指定资源的内容

**使用示例：**
```
用户：列出可用的 MCP 资源
麦麦：[调用 mcp_status，参数 query_type="resources"]

用户：读取 file:///path/to/file.txt 的内容
麦麦：[调用 mcp_read_resource，参数 uri="file:///path/to/file.txt"]
```

### Prompts（提示模板）

MCP Prompts 允许服务器提供预定义的提示模板，可以包含参数。

**启用方法：**
```toml
[settings]
enable_prompts = true
```

**可用工具：**
- `mcp_status(query_type="prompts")` - 列出所有可用提示模板
- `mcp_get_prompt` - 获取指定提示模板的内容

**使用示例：**
```
用户：列出可用的提示模板
麦麦：[调用 mcp_status，参数 query_type="prompts"]

用户：获取 code_review 模板，参数是 {"language": "python"}
麦麦：[调用 mcp_get_prompt，参数 name="code_review", arguments='{"language": "python"}']
```

### 注意事项

1. **默认关闭** - 这些功能默认禁用，需要手动开启
2. **服务器支持** - 不是所有 MCP 服务器都支持 Resources/Prompts，插件会自动检测
3. **实验性功能** - 这些功能标记为实验性，可能在未来版本中有变化

## 工作原理

1. 插件启动时，根据配置连接到各个 MCP 服务器
2. 从每个服务器获取可用工具列表
3. 为每个 MCP 工具动态创建一个 MaiBot `BaseTool` 子类
4. 将这些工具注册到 MaiBot 的组件系统
5. LLM 在决策时可以看到并选择这些工具
6. 当 LLM 选择调用某个 MCP 工具时，插件会将请求转发到对应的 MCP 服务器

## 常见问题

### Q: 为什么工具没有被注册？

检查以下几点：
1. 确保 `mcp` 库已安装：`pip install mcp`
2. 确保服务器配置中 `enabled = true`
3. 检查日志中是否有连接错误
4. 确保 MCP 服务器命令可以正常执行

### Q: 如何查看已注册的工具？

查看 MaiBot 日志，插件会输出类似：
```
✅ 注册 MCP 工具: mcp_howtocook_whatToEat
```

### Q: 工具调用超时怎么办？

增加 `call_timeout` 配置值，或检查 MCP 服务器是否响应正常。

### Q: 如何查看连接状态和统计信息？

有两种方式：

1. **通过 LLM 调用 `mcp_status` 工具**：
   - 对麦麦说："查看 MCP 服务器状态"
   - 麦麦会调用内置的 `mcp_status` 工具并返回状态信息

2. **通过代码查询**：
   ```python
   from plugins.MCPBridgePlugin import mcp_manager
   
   # 获取状态
   status = mcp_manager.get_status()
   
   # 获取详细统计
   stats = mcp_manager.get_all_stats()
   ```

### Q: 服务器断开后会自动重连吗？

是的，如果启用了 `heartbeat_enabled` 和 `auto_reconnect`（默认都启用），插件会：
1. 每 60 秒（可配置）检测一次连接状态
2. 检测到断开后自动尝试重连
3. 连续失败达到 `max_reconnect_attempts` 次后暂停重连

## 如何获取 MCP 服务器

MCP 服务器有两种类型：**远程托管服务** 和 **本地运行服务**。

### 远程 MCP 服务（HTTP/SSE 方式）

以下平台提供免费或付费的远程 MCP 服务：

| 平台 | 说明 | 链接 |
|------|------|------|
| **魔搭 ModelScope** | 阿里云提供的 MCP 服务平台，有多种免费工具 | [mcp.modelscope.cn](https://mcp.modelscope.cn/) |
| **Smithery** | MCP 服务器注册中心，可搜索各类 MCP 服务 | [smithery.ai](https://smithery.ai/) |
| **Glama** | MCP 服务器目录 | [glama.ai/mcp/servers](https://glama.ai/mcp/servers) |

使用远程服务时，复制服务提供的 URL 填入配置即可：

```json
{
  "name": "your-service",
  "enabled": true,
  "transport": "http",
  "url": "https://从平台获取的MCP服务地址"
}
```

### 本地 MCP 服务（stdio 方式）

可以在本地运行 MCP 服务器，常见的有：

| 服务 | 安装命令 | 说明 |
|------|----------|------|
| **Filesystem** | `npx @modelcontextprotocol/server-filesystem` | 文件系统操作 |
| **GitHub** | `npx @modelcontextprotocol/server-github` | GitHub API |
| **Brave Search** | `npx @modelcontextprotocol/server-brave-search` | 网页搜索 |
| **Fetch** | `uvx mcp-server-fetch` | HTTP 请求 |

更多官方服务器：[github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)

本地服务配置示例：

```json
{
  "name": "filesystem",
  "enabled": true,
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/dir"]
}
```

> 💡 提示：使用 `npx` 需要安装 Node.js，使用 `uvx` 需要安装 [uv](https://docs.astral.sh/uv/)。

## 使用疑问 Q&A

### Q: 配置了 MCP 服务器但麦麦不调用怎么办？

1. **确认工具已注册** - 查看日志是否有 `✅ 注册 MCP 工具: xxx`
2. **检查工具描述** - MCP 工具的描述要清晰，LLM 才能理解何时使用
3. **明确指示** - 尝试直接告诉麦麦使用某个工具，如"用 mcp_howtocook_whatToEat 推荐今天吃什么"
4. **检查 LLM 配置** - 确保 MaiBot 的 LLM 配置支持 function calling

### Q: 配置文件中的 JSON 格式总是报错？

常见错误：
- **忘记用三引号** - 多行 JSON 必须用 `'''` 包裹
- **JSON 语法错误** - 检查逗号、引号是否正确
- **中文引号** - 确保使用英文双引号 `"` 而不是中文引号 `""`

正确格式：
```toml
[servers]
list = '''
[
  {"name": "test", "enabled": true, "transport": "http", "url": "https://..."}
]
'''
```

### Q: stdio 模式的本地服务器启动失败？

1. **检查命令是否可用** - 在终端手动运行 `npx` 或 `uvx` 确认已安装
2. **检查路径** - stdio 模式的 `args` 中的路径必须是绝对路径
3. **查看日志** - MaiBot 日志会显示具体的错误信息
4. **权限问题** - 确保 MaiBot 进程有权限执行该命令

### Q: 如何手动重连断开的服务器？

发送命令 `/mcp reconnect` 重连所有断开的服务器，或 `/mcp reconnect 服务器名` 重连指定服务器。

### Q: WebUI 中的状态显示不更新？

WebUI 中的状态只在插件启动时更新一次。要查看实时状态，请：
- 发送 `/mcp status` 命令
- 或让麦麦调用 `mcp_status` 工具

### Q: 如何知道 MCP 工具需要什么参数？

1. **查看工具描述** - 工具注册时会显示参数信息
2. **查看 MCP 服务文档** - 各平台通常有工具说明
3. **让 LLM 尝试** - LLM 会根据工具的 schema 自动填充参数

### Q: 多个 MCP 服务器可以同时使用吗？

可以！在配置中添加多个服务器即可：
```json
[
  {"name": "server1", "enabled": true, ...},
  {"name": "server2", "enabled": true, ...}
]
```
每个服务器的工具会以 `mcp_{服务器名}_{工具名}` 的格式注册。

## 依赖

- MaiBot >= 0.11.6
- Python >= 3.10
- mcp >= 1.0.0

## 许可证

AGPL-3.0

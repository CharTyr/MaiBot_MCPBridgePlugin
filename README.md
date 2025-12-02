# MCP 桥接插件

将 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 服务器的工具桥接到 MaiBot，使麦麦能够调用外部 MCP 工具。

## 功能特性

- 🔌 支持多个 MCP 服务器同时连接
- 🔄 自动发现并注册 MCP 工具为 MaiBot 原生工具
- 📡 支持 stdio、SSE、HTTP、Streamable HTTP 四种传输方式
- 🔁 连接失败自动重试
- ⚡ 工具参数自动转换
- 🖥️ 支持 WebUI 配置（包括服务器列表）
- 💓 **v1.1.0** 心跳检测 - 定期检测服务器连接状态
- 🔄 **v1.1.0** 自动重连 - 检测到断开时自动尝试重连
- 📊 **v1.1.0** 调用统计 - 记录工具调用次数、成功率、耗时
- 🛠️ **v1.1.0** 内置状态工具 - 通过 `mcp_status` 查询连接状态
- 📦 **v1.2.0** Resources 支持 - 读取 MCP 服务器提供的资源（实验性）
- 📝 **v1.2.0** Prompts 支持 - 使用 MCP 服务器提供的提示模板（实验性）
- 🔄 **v1.3.0** 结果后处理 - 使用 LLM 对长结果进行摘要提炼
- 🚫 **v1.4.0** 工具禁用管理 - 在 WebUI 中禁用特定工具
- 🔍 **v1.4.0** 调用链路追踪 - 记录每次工具调用详情
- 🗄️ **v1.4.0** 工具调用缓存 - 缓存相同参数的调用结果
- 🔐 **v1.4.0** 工具权限控制 - 按群/用户限制工具使用

<img width="3012" height="1794" alt="image" src="https://github.com/user-attachments/assets/ece56404-301a-4abf-b16d-87bd430fc977" />

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
- ✅ **v1.4.0** 工具管理（查看工具清单、禁用特定工具）
- ✅ **v1.4.0** 权限控制（按群/用户限制工具使用）

在 WebUI 的服务器配置中，使用 JSON 格式编辑服务器列表：

```json
[
  {
    "name": "howtocook",
    "enabled": true,
    "transport": "http",
    "url": "https://mcp.example.com/mcp"
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

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `tool_prefix` | string | "mcp" | 工具名称前缀 |
| `connect_timeout` | float | 30.0 | 连接超时时间（秒） |
| `call_timeout` | float | 60.0 | 工具调用超时时间（秒） |
| `auto_connect` | bool | true | 启动时自动连接所有服务器 |
| `retry_attempts` | int | 3 | 连接失败重试次数 |
| `retry_interval` | float | 5.0 | 重试间隔（秒） |
| `heartbeat_enabled` | bool | true | 启用心跳检测 |
| `heartbeat_interval` | float | 60.0 | 心跳检测间隔（秒） |
| `auto_reconnect` | bool | true | 检测到断开时自动重连 |
| `max_reconnect_attempts` | int | 3 | 最大连续重连次数 |
| `enable_resources` | bool | false | 启用 Resources 支持（实验性） |
| `enable_prompts` | bool | false | 启用 Prompts 支持（实验性） |

### v1.3.0 结果后处理配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `post_process_enabled` | bool | false | 启用结果后处理 |
| `post_process_threshold` | int | 500 | 触发后处理的字符数阈值 |
| `post_process_max_tokens` | int | 500 | 后处理输出最大 token 数 |
| `post_process_model` | string | "" | 后处理使用的模型（留空用 utils 组） |
| `post_process_prompt` | string | ... | 后处理提示词模板 |

### v1.4.0 调用追踪配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `trace_enabled` | bool | true | 启用调用追踪 |
| `trace_max_records` | int | 100 | 内存中保留的最大记录数 |
| `trace_log_enabled` | bool | false | 是否写入日志文件 |

### v1.4.0 调用缓存配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cache_enabled` | bool | false | 启用调用缓存 |
| `cache_ttl` | int | 300 | 缓存有效期（秒） |
| `cache_max_entries` | int | 200 | 最大缓存条目数 |
| `cache_exclude_tools` | string | "" | 不缓存的工具（每行一个，支持通配符） |

### v1.4.0 工具管理 `[tools]`

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `tool_list` | string | 已注册工具清单（只读，启动后自动生成） |
| `disabled_tools` | string | 要禁用的工具名（每行一个） |

禁用工具示例：
```toml
[tools]
disabled_tools = '''
mcp_filesystem_delete_file
mcp_filesystem_write_file
'''
```

### v1.4.0 权限控制 `[permissions]`

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `perm_enabled` | bool | false | 启用权限控制 |
| `perm_default_mode` | string | "allow_all" | 默认模式：allow_all 或 deny_all |
| `perm_rules` | string | "[]" | 权限规则（JSON 数组） |

权限规则示例：
```json
[
  {
    "tool": "mcp_filesystem_*",
    "mode": "whitelist",
    "allowed": ["qq:123456789:group", "qq:111111:user"]
  },
  {
    "tool": "mcp_bing_*",
    "denied": ["qq:987654321:group"]
  }
]
```

ID 格式：
- `qq:123456:group` - QQ群
- `qq:123456:private` - 私聊
- `qq:123456:user` - 特定用户（任何场景生效）

### 服务器配置 `[servers]`

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `name` | string | 服务器名称（唯一标识） |
| `enabled` | bool | 是否启用 |
| `transport` | string | 传输方式：`stdio`、`sse`、`http`、`streamable_http` |
| `command` | string | (stdio) 启动命令 |
| `args` | array | (stdio) 命令参数 |
| `env` | object | (stdio) 环境变量 |
| `url` | string | (sse/http/streamable_http) 服务器 URL |
| `post_process` | object | (可选) 服务器级别后处理配置 |

## 命令

| 命令 | 说明 |
|------|------|
| `/mcp` 或 `/mcp status` | 查看服务器连接状态 |
| `/mcp tools` | 查看已注册的工具列表 |
| `/mcp stats` | 查看调用统计 |
| `/mcp reconnect [服务器名]` | 重连服务器 |
| `/mcp trace [n\|工具名]` | **v1.4.0** 查看调用追踪记录 |
| `/mcp cache [clear]` | **v1.4.0** 查看/清空缓存 |
| `/mcp perm [工具名]` | **v1.4.0** 查看权限配置 |

## 工具命名规则

MCP 工具在 MaiBot 中的名称格式为：

```
{tool_prefix}_{server_name}_{original_tool_name}
```

例如：`mcp_howtocook_whatToEat`

## v1.4.0 新功能详解

### 工具禁用管理

在 WebUI 的「工具管理」配置节中：
1. `tool_list` 字段会自动显示所有已注册的工具（只读）
2. 将要禁用的工具名复制到 `disabled_tools` 字段（每行一个）
3. 禁用的工具不会被 LLM 调用，但仍会注册（方便随时启用）

### 调用链路追踪

记录每次工具调用的详细信息：
- 调用时间、工具名、服务器名
- 调用参数、原始结果、处理后结果
- 耗时、成功/失败状态、错误信息
- 是否命中缓存、是否经过后处理

查看追踪记录：
```
/mcp trace        # 最近 10 条
/mcp trace 20     # 最近 20 条
/mcp trace 工具名  # 特定工具的记录
```

### 工具调用缓存

对相同参数的调用返回缓存结果，减少重复请求：
- LRU 淘汰策略
- 可配置 TTL 和最大条目数
- 支持排除特定工具（如时间类、随机类）

```toml
[settings]
cache_enabled = true
cache_ttl = 300
cache_exclude_tools = '''
mcp_*_time_*
mcp_*_random_*
'''
```

### 工具权限控制

按群/用户限制工具使用：
- 支持通配符匹配工具名
- 支持白名单/黑名单模式
- 用户级别权限优先于群级别

## 如何获取 MCP 服务器

### 远程 MCP 服务（HTTP/SSE 方式）

| 平台 | 说明 | 链接 |
|------|------|------|
| **魔搭 ModelScope** | 阿里云提供的 MCP 服务平台 | [mcp.modelscope.cn](https://mcp.modelscope.cn/) |
| **Smithery** | MCP 服务器注册中心 | [smithery.ai](https://smithery.ai/) |
| **Glama** | MCP 服务器目录 | [glama.ai/mcp/servers](https://glama.ai/mcp/servers) |

### 本地 MCP 服务（stdio 方式）

| 服务 | 安装命令 | 说明 |
|------|----------|------|
| **Filesystem** | `npx @modelcontextprotocol/server-filesystem` | 文件系统操作 |
| **GitHub** | `npx @modelcontextprotocol/server-github` | GitHub API |
| **Fetch** | `uvx mcp-server-fetch` | HTTP 请求 |

更多官方服务器：[github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)

## 常见问题

### Q: 工具没有被注册？

1. 确保 `mcp` 库已安装：`pip install mcp`
2. 确保服务器配置中 `enabled = true`
3. 检查日志中是否有连接错误

### Q: 如何查看连接状态？

- 发送 `/mcp status` 命令
- 或让麦麦调用 `mcp_status` 工具

### Q: 服务器断开后会自动重连吗？

是的，如果启用了 `heartbeat_enabled` 和 `auto_reconnect`（默认都启用）。

### Q: 如何手动重连？

发送 `/mcp reconnect` 重连所有断开的服务器，或 `/mcp reconnect 服务器名` 重连指定服务器。

### Q: 配置文件中的 JSON 格式报错？

- 多行 JSON 必须用 `'''` 三引号包裹
- 确保使用英文双引号 `"` 而不是中文引号

## 依赖

- MaiBot >= 0.11.6
- Python >= 3.10
- mcp >= 1.0.0

## 许可证

AGPL-3.0

# MCP 桥接插件

将 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 服务器的工具桥接到 MaiBot，使麦麦能够调用外部 MCP 工具。

## 功能特性

- 🔌 支持多个 MCP 服务器同时连接
- 🔄 自动发现并注册 MCP 工具为 MaiBot 原生工具
- 📡 支持 stdio、SSE、HTTP 三种传输方式
- 🔁 连接失败自动重试
- ⚡ 工具参数自动转换
- 🖥️ 支持 WebUI 配置（包括服务器列表）

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

### 服务器配置

在 `config.toml` 中使用 JSON 格式配置服务器列表：

```toml
[servers]
list = '''
[
  {
    "name": "howtocook",
    "enabled": true,
    "transport": "http",
    "url": "https://mcp.api-inference.modelscope.net/今天吃什么/mcp"
  }
]
'''
```

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

## 已测试的 MCP 服务器

### HowToCook 菜谱服务器

```json
{
  "name": "howtocook",
  "enabled": true,
  "transport": "http",
  "url": "https://mcp.api-inference.modelscope.net/今天吃什么"
}
```

提供的工具：
- `mcp_howtocook_getAllRecipes` - 获取所有菜谱
- `mcp_howtocook_getRecipesByCategory` - 按分类查询菜谱
- `mcp_howtocook_getRecipeById` - 查询菜谱详情
- `mcp_howtocook_whatToEat` - 今天吃什么推荐
- `mcp_howtocook_recommendMeals` - 智能膳食计划推荐

## 依赖

- MaiBot >= 0.11.6
- Python >= 3.10
- mcp >= 1.0.0

## 许可证

AGPL-3.0

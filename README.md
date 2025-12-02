# MCP 桥接插件

将 [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) 服务器的工具桥接到 MaiBot，使麦麦能够调用外部 MCP 工具。

<img width="3012" height="1794" alt="image" src="https://github.com/user-attachments/assets/ece56404-301a-4abf-b16d-87bd430fc977" />

## 🚀 快速开始

### 1. 安装

```bash
# 克隆到 MaiBot 插件目录
cd /path/to/MaiBot/plugins
git clone https://github.com/CharTyr/MaiBot_MCPBridgePlugin.git MCPBridgePlugin

# 安装依赖
pip install mcp

# 复制配置文件
cd MCPBridgePlugin
cp config.example.toml config.toml
```

### 2. 添加服务器

编辑 `config.toml`，在 `[servers]` 的 `list` 中添加服务器：

**免费服务器：**
```json
{"name": "time", "enabled": true, "transport": "streamable_http", "url": "https://mcp.api-inference.modelscope.cn/server/mcp-server-time"}
```

**带鉴权的服务器（v1.4.2）：**
```json
{"name": "my-server", "enabled": true, "transport": "streamable_http", "url": "https://mcp.xxx.com/mcp", "headers": {"Authorization": "Bearer 你的密钥"}}
```

**本地服务器（需要 uvx）：**
```json
{"name": "fetch", "enabled": true, "transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"]}
```

### 3. 启动

重启 MaiBot，或发送 `/mcp reconnect`

---

## 📚 去哪找 MCP 服务器？

| 平台 | 说明 |
|------|------|
| [mcp.modelscope.cn](https://mcp.modelscope.cn/) | 魔搭 ModelScope，免费推荐 |
| [smithery.ai](https://smithery.ai/) | MCP 服务器注册中心 |
| [github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | 官方服务器列表 |

---

## 💡 常用命令

| 命令 | 说明 |
|------|------|
| `/mcp` | 查看连接状态 |
| `/mcp tools` | 查看可用工具 |
| `/mcp reconnect` | 重连服务器 |
| `/mcp trace` | 查看调用记录 |
| `/mcp cache` | 查看缓存状态 |
| `/mcp perm` | 查看权限配置 |

---

## ✨ 功能特性

### 核心功能
- 🔌 多服务器同时连接
- 📡 支持 stdio / SSE / HTTP / Streamable HTTP
- 🔄 自动重试、心跳检测、断线重连
- 🖥️ WebUI 完整配置支持

### v1.4.0 新增
- 🚫 **工具禁用** - WebUI 直接禁用不想用的工具
- 🔍 **调用追踪** - 记录每次调用详情，便于调试
- 🗄️ **调用缓存** - 相同请求自动缓存
- 🔐 **权限控制** - 按群/用户限制工具使用

### 高级功能
- 📦 Resources 支持（实验性）
- 📝 Prompts 支持（实验性）
- 🔄 结果后处理（LLM 摘要提炼）

---

## ⚙️ 配置说明

### 服务器配置

```json
[
  {
    "name": "服务器名",
    "enabled": true,
    "transport": "streamable_http",
    "url": "https://..."
  }
]
```

| 字段 | 说明 |
|------|------|
| `name` | 服务器名称（唯一） |
| `enabled` | 是否启用 |
| `transport` | `stdio` / `sse` / `http` / `streamable_http` |
| `url` | 远程服务器地址 |
| `headers` | 🆕 鉴权头（如 `{"Authorization": "Bearer xxx"}`） |
| `command` / `args` | 本地服务器启动命令 |

### 权限控制（v1.4.0）

**快捷配置（推荐）：**
```toml
[permissions]
perm_enabled = true
quick_deny_groups = "123456789"      # 禁用的群号
quick_allow_users = "111111111"      # 管理员白名单
```

**高级规则：**
```json
[{"tool": "mcp_*_delete_*", "denied": ["qq:123456:group"]}]
```

### 工具禁用

```toml
[tools]
disabled_tools = '''
mcp_filesystem_delete_file
mcp_filesystem_write_file
'''
```

### 调用缓存

```toml
[settings]
cache_enabled = true
cache_ttl = 300
cache_exclude_tools = "mcp_*_time_*"
```

---

## ❓ 常见问题

**Q: 工具没有注册？**
- 检查 `enabled = true`
- 检查 MaiBot 日志错误信息
- 确认 `pip install mcp`

**Q: JSON 格式报错？**
- 多行 JSON 用 `'''` 三引号包裹
- 使用英文双引号 `"`

**Q: 如何手动重连？**
- `/mcp reconnect` 或 `/mcp reconnect 服务器名`

---

## 📋 依赖

- MaiBot >= 0.11.6
- Python >= 3.10
- mcp >= 1.0.0

## 📄 许可证

AGPL-3.0

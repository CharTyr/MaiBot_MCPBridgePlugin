#该插件的主要代码已加入MaiBot主程序中，当前仓库不再更新

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

编辑 `config.toml`，在 `[servers]` 的 `claude_config_json` 中填写 Claude Desktop 的 `mcpServers` JSON：

```toml
[servers]
claude_config_json = '''
{
  "mcpServers": {
    "time": { "transport": "streamable_http", "url": "https://mcp.api-inference.modelscope.cn/server/mcp-server-time" },
    "my-server": { "transport": "streamable_http", "url": "https://mcp.xxx.com/mcp", "headers": { "Authorization": "Bearer 你的密钥" } },
    "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] }
  }
}
'''
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
| `/mcp import <json>` | 🆕 导入 Claude Desktop 配置 |
| `/mcp export` | 🆕 导出配置 |
| `/mcp search <关键词>` | 🆕 搜索工具 |
| `/mcp chain` | 🆕 查看工具链 |
| `/mcp chain <名称>` | 🆕 查看工具链详情 |
| `/mcp chain test <名称> <参数>` | 🆕 测试执行工具链 |

---

## ✨ 功能特性

### 核心功能
- 🔌 多服务器同时连接
- 📡 支持 stdio / SSE / HTTP / Streamable HTTP
- 🔄 自动重试、心跳检测、断线重连
- 🖥️ WebUI 完整配置支持

### 双轨制架构
- 🔄 **ReAct（软流程）**：LLM 自主决策，多轮动态调用 MCP 工具（适合探索式场景）
- 🔗 **Workflow（硬流程/工具链）**：用户预定义步骤顺序与参数传递（适合可控可复用场景）

### 高级功能
- 📦 Resources 支持（实验性）
- 📝 Prompts 支持（实验性）
- 🔄 结果后处理（LLM 摘要提炼）
- 🔍 调用追踪 / 🗄️ 调用缓存 / 🔐 权限控制 / 🚫 工具禁用

### 更新日志
- 见 `plugins/MaiBot_MCPBridgePlugin/CHANGELOG.md`

---

## ⚙️ 配置说明

### 服务器配置

```json
{
  "mcpServers": {
    "server_name": {
      "transport": "streamable_http",
      "url": "https://..."
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `mcpServers.<name>` | 服务器名称（唯一） |
| `enabled` | 是否启用（可选，默认 true） |
| `transport` | `stdio` / `sse` / `http` / `streamable_http` |
| `url` | 远程服务器地址 |
| `headers` | 🆕 鉴权头（如 `{"Authorization": "Bearer xxx"}`） |
| `command` / `args` | 本地服务器启动命令 |

### 权限控制

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

## 📥 配置导入导出（Claude mcpServers）

### 从 Claude Desktop 导入

如果你已有 Claude Desktop 的 MCP 配置，可以直接导入：

```
/mcp import {"mcpServers":{"time":{"command":"uvx","args":["mcp-server-time"]},"fetch":{"command":"uvx","args":["mcp-server-fetch"]}}}
```

支持的格式：
- Claude Desktop 格式（`mcpServers` 对象）
- 兼容旧版：MaiBot servers 列表数组（将自动迁移为 `mcpServers`）

### 导出配置

```
/mcp export           # 导出为 Claude Desktop 格式（默认）
/mcp export claude    # 导出为 Claude Desktop 格式
```

### 注意事项
- 导入时会自动跳过同名服务器
- 导入后需要发送 `/mcp reconnect` 使配置生效
- 支持 stdio、sse、http、streamable_http 全部传输类型

---

## 🔗 Workflow（硬流程/工具链）

工具链允许你将多个 MCP 工具按顺序执行，后续工具可以使用前序工具的输出作为输入。

### 1 分钟上手（推荐 WebUI）
1. 先完成 MCP 服务器配置并 `/mcp reconnect`
2. 发送 `/mcp tools`，复制你要用的工具名
3. 打开 WebUI → 「Workflow（硬流程/工具链）」→ 用“快速添加”表单填入：
   - 名称/描述
   - 输入参数（每行 `参数名=描述`）
   - 执行步骤（每行 `工具名|参数JSON|输出键`）
4. 在“确认添加”中输入 `ADD` 并保存

### 快速添加工具链（推荐）

在 WebUI 的「工具链」配置区，使用表单快速添加：

1. **名称**: 填写工具链名称（英文，如 `search_and_detail`）
2. **描述**: 填写工具链用途（供 LLM 理解何时使用）
3. **输入参数**: 每行一个，格式 `参数名=描述`
   ```
   query=搜索关键词
   max_results=最大结果数
   ```
4. **执行步骤**: 每行一个，格式 `工具名|参数JSON|输出键`
   ```
   mcp_server_search|{"keyword":"${input.query}"}|search_result
   mcp_server_detail|{"id":"${prev}"}|
   ```
5. **确认添加**: 输入 `ADD` 并保存

### JSON 配置方式

也可以直接在「工具链列表」中编写 JSON：

```json
[
  {
    "name": "search_and_detail",
    "description": "先搜索模组，再获取详情",
    "input_params": {
      "query": "搜索关键词"
    },
    "steps": [
      {
        "tool_name": "mcp_mcmod_search_mod",
        "args_template": {"keyword": "${input.query}", "limit": 1},
        "output_key": "search_result",
        "description": "搜索模组"
      },
      {
        "tool_name": "mcp_mcmod_get_mod_detail",
        "args_template": {"mod_id": "${prev}"},
        "description": "获取详情"
      }
    ]
  }
]
```

### 变量替换

| 变量格式 | 说明 |
|---------|------|
| `${input.参数名}` | 用户输入的参数 |
| `${step.输出键}` | 某个步骤的输出（通过 `output_key` 指定） |
| `${prev}` | 上一步的输出 |
| `${prev.字段}` | 上一步输出（JSON）的某个字段 |
| `${step.geo.return.0.location}` | 数组下标访问（dot） |
| `${step.geo.return[0].location}` | 数组下标访问（[]） |
| `${step.geo['return'][0]['location']}` | bracket 写法（最通用） |

### 工具链字段说明

| 字段 | 说明 |
|------|------|
| `name` | 工具链名称，将生成 `chain_xxx` 工具 |
| `description` | 描述，供 LLM 理解何时使用 |
| `input_params` | 输入参数定义 `{参数名: 描述}` |
| `steps` | 执行步骤数组 |
| `steps[].tool_name` | 要调用的工具名 |
| `steps[].args_template` | 参数模板，支持变量替换 |
| `steps[].output_key` | 输出存储键名（可选） |
| `steps[].optional` | 是否可选，失败时继续执行（默认 false） |

### 命令

```bash
/mcp chain                    # 查看所有工具链
/mcp chain list               # 列出工具链
/mcp chain <名称>             # 查看详情
/mcp chain test <名称> {"query": "JEI"}  # 测试执行
/mcp chain reload             # 重新加载配置
```

---

## 🔄 双轨制架构

MCP 桥接插件支持两种工具调用模式，可根据场景选择：

### ReAct 软流程

LLM 自主决策的多轮工具调用模式，适合复杂、不确定的场景。

**工作原理：**
1. 用户提问 → LLM 分析需要什么信息
2. LLM 选择调用工具 → 获取结果
3. LLM 观察结果 → 决定是否需要更多信息
4. 重复 2-3 直到信息足够 → 生成最终回答

**启用方式：**
在 WebUI「ReAct (软流程)」配置区启用，MCP 工具将自动注册到 MaiBot 的记忆检索 ReAct 系统。

**适用场景：**
- 复杂问题需要多步推理
- 不确定需要调用哪些工具
- 需要根据中间结果动态调整

### Workflow 硬流程

用户预定义的工作流，固定执行顺序，适合可靠、可控的场景。

**工作原理：**
1. 用户定义步骤顺序和参数传递
2. 按顺序执行每个步骤
3. 后续步骤可使用前序步骤的输出
4. 返回最终结果

**适用场景：**
- 流程固定、可预测
- 需要可靠、可重复的执行
- 希望精确控制工具调用顺序

### 对比

| 特性 | ReAct 软流程 | Workflow 硬流程 |
|------|-------------|----------------|
| 决策者 | LLM 自主决策 | 用户预定义 |
| 灵活性 | 高，动态调整 | 低，固定流程 |
| 可预测性 | 低 | 高 |
| 适用场景 | 复杂、探索性任务 | 固定、重复性任务 |
| 配置方式 | 启用即可 | 需要定义步骤 |

---

## 📋 依赖

- MaiBot >= 0.11.6
- Python >= 3.10
- mcp >= 1.0.0

## 📄 许可证

AGPL-3.0

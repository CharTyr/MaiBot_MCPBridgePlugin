# 代码重构计划

## 失败教训 (2024-12-03)

### 问题
1. 直接在 main 分支重构，没有先在分支测试
2. 重写 plugin.py 时简化了 `config_schema`，丢失了大量 WebUI 配置属性：
   - `step` (数值步进)
   - `placeholder` (占位符)
   - `hint` (提示信息)
   - 详细的 `description`
3. 导致 WebUI 配置界面完全乱掉

### 教训
- **永远在分支上做大规模重构**
- **不要重写代码，要移动代码** - 保持原有代码完整性
- **config_schema 是敏感区域** - 任何改动都会影响 WebUI
- **重构后必须完整测试 WebUI**

---

## 重构目标

将 plugin.py (2959行) 拆分为模块化结构，提升可维护性。

## 目标结构

```
MaiBot_MCPBridgePlugin/
├── plugin.py              # 主入口（保持 config_schema 完整不动）
├── _manifest.json
├── config.toml
├── config.example.toml
├── README.md
├── DEVELOPMENT.md
├── requirements.txt
├── src/                   # 核心模块
│   ├── __init__.py
│   ├── mcp_client.py      # MCP 客户端（已有，直接移动）
│   ├── config_converter.py # 配置转换（已有，直接移动）
│   ├── tracer.py          # 调用追踪 (ToolCallTracer, ToolCallRecord)
│   ├── cache.py           # 调用缓存 (ToolCallCache, CacheEntry)
│   ├── permissions.py     # 权限控制 (PermissionChecker)
│   └── tool_proxy.py      # 工具代理 (MCPToolProxy, MCPToolRegistry)
└── tests/
    ├── __init__.py
    └── test_mcp_client.py
```

## 重构步骤

### 第一步：移动现有独立文件
- [ ] 创建 `src/` 和 `tests/` 目录
- [ ] 移动 `mcp_client.py` → `src/mcp_client.py`
- [ ] 移动 `config_converter.py` → `src/config_converter.py`
- [ ] 移动 `test_mcp_client.py` → `tests/test_mcp_client.py`
- [ ] 更新导入路径

### 第二步：拆分 plugin.py 中的独立类
从 plugin.py 中**剪切**（不是重写）以下类到独立文件：

- [ ] `ToolCallRecord`, `ToolCallTracer`, `tool_call_tracer` → `src/tracer.py`
- [ ] `CacheEntry`, `ToolCallCache`, `tool_call_cache` → `src/cache.py`
- [ ] `PermissionChecker`, `permission_checker` → `src/permissions.py`
- [ ] `MCPToolProxy`, `MCPToolRegistry`, `mcp_tool_registry` 等 → `src/tool_proxy.py`

### 第三步：更新 plugin.py 导入
- [ ] 在 plugin.py 顶部添加 `from .src import ...`
- [ ] **保持 config_schema 完全不变**
- [ ] **保持所有内置工具、命令、事件处理器不变**

### 第四步：测试验证
- [ ] 语法检查 `python -m py_compile`
- [ ] **启动 MaiBot 测试 WebUI 配置界面**
- [ ] 测试 /mcp 命令
- [ ] 测试工具调用

## 关键原则

1. **剪切粘贴，不要重写** - 保持代码原样
2. **config_schema 禁止修改** - 这是 WebUI 的核心
3. **每步都测试** - 不要一次性改完
4. **分支开发** - 验证通过后再合并到 main

## 待拆分的类（行号参考）

| 类名 | 起始行 | 目标文件 |
|------|--------|----------|
| ToolCallRecord | ~92 | src/tracer.py |
| ToolCallTracer | ~111 | src/tracer.py |
| CacheEntry | ~177 | src/cache.py |
| ToolCallCache | ~187 | src/cache.py |
| PermissionChecker | ~315 | src/permissions.py |
| MCPToolProxy | ~495 | src/tool_proxy.py |
| MCPToolRegistry | ~818 | src/tool_proxy.py |

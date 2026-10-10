# 04 MCP：Model Context Protocol 工具接入

## 1. MCP 协议简介

**Model Context Protocol**（MCP）是 Anthropic 于 2024.11 发布的开放协议，旨在标准化 LLM 应用与外部工具/数据源的连接方式。

核心思想：
- **统一协议**：不再每个框架自定义 Tool 接口，而是用 MCP 标准描述工具
- **进程间通信**：MCP Server 是独立进程，通过 stdio/SSE/HTTP 与 Agent 通信
- **动态发现**：Agent 启动时连接 MCP Server，自动获取可用工具列表

```
┌─────────────┐    MCP 协议    ┌─────────────┐
│  Qwen-Agent │ ◄──────────► │  MCP Server  │
│  (Client)   │    stdio/SSE  │  (独立进程)   │
└─────────────┘               └─────────────┘
                                    │
                              ┌─────┴─────┐
                              │ 外部服务    │
                              │ 数据库/API  │
                              └───────────┘
```

---

## 2. MCPManager 架构

### 2.1 单例模式（tools/mcp_manager.py:31）

MCPManager 使用 `__new__` + `__init__` 守卫实现单例：

```python
class MCPManager(object):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, 'clients'):
            self.clients = {}  # {server_name: MCPClient}
            # 创建专用异步事件循环线程
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, daemon=True
            )
            self._thread.start()
```

**为什么是单例？**
- 管理一个全局的 `asyncio` 事件循环线程
- 所有 MCP 客户端共享这个循环
- 避免创建多个事件循环线程导致资源浪费

### 2.2 专用事件循环线程

MCP 的所有异步操作（连接、调用、清理）都在一个后台 daemon 线程中运行：

```python
# 同步 → 异步桥接
future = asyncio.run_coroutine_threadsafe(
    client.execute_function(tool_name, tool_args),
    self._loop
)
result = future.result()  # 阻塞等待
```

### 2.3 进程管理

MCPManager monkey-patch 了 `mcp.client.stdio._create_platform_compatible_process` 来追踪子进程，确保退出时清理。

`atexit` 注册了 `_cleanup_mcp` 函数，确保 MCP Server 子进程在 Agent 退出时被终止。

---

## 3. MCPClient 连接模式

### 3.1 三种传输方式（mcp_manager.py:325+）

| 模式 | 条件 | 传输 | 用途 |
|------|------|------|------|
| **Stdio** | `command` 存在 | stdin/stdout 管道 | 本地命令行工具 |
| **SSE** | `url` 存在，`type != 'streamable-http'` | Server-Sent Events | HTTP 长连接 |
| **Streamable HTTP** | `url` 存在，`type == 'streamable-http'` | HTTP 流式 | 现代 HTTP 传输 |

### 3.2 自动重连（mcp_manager.py:390）

当 `execute_function` 检测到会话已断开时，自动调用 `reconnect()`：

```python
async def execute_function(self, tool_name, tool_args):
    try:
        session = await self.session_ping()
    except:
        new_client = await self.reconnect()
        # 新客户端替换旧客户端
        MCPManager().clients[self.server_name] = new_client
    # 执行工具调用
    result = await session.call_tool(tool_name, tool_args)
    return result
```

---

## 4. 动态工具创建

### 4.1 create_tool_class（mcp_manager.py:265）

MCPManager 的核心魔法——在运行时动态创建 `BaseTool` 子类：

```python
def create_tool_class(self, register_name, register_client_id,
                      tool_name, tool_desc, tool_parameters):
    """动态创建一个 BaseTool 子类，代理 MCP 工具调用"""

    class ToolClass(BaseTool):
        name = register_name
        description = tool_desc
        parameters = tool_parameters
        client_id = register_client_id

        def call(self, params, **kwargs):
            args = self._verify_json_format_args(params)
            manager = MCPManager()
            client = manager.clients[self.client_id]
            future = asyncio.run_coroutine_threadsafe(
                client.execute_function(self.name, args),
                manager._loop
            )
            return future.result()

    ToolClass.__name__ = f'{register_name}_Class'
    return ToolClass()
```

关键设计：
- **运行时类生成**：不需要预定义工具类，MCP Server 提供什么工具就创建什么
- **闭包捕获**：`client_id` 在闭包中，`call()` 通过它找到对应的 MCPClient
- **同步包装**：`call()` 是同步方法，内部通过 `run_coroutine_threadsafe` 桥接到异步循环

### 4.2 Resource 支持

如果 MCP Server 暴露了资源（如文件、数据库记录），MCPManager 会额外创建两个工具：
- `{server_name}-list_resources`：列出可用资源
- `{server_name}-read_resource`：读取指定资源

Resource templates 会被追加到 `read_resource` 工具的描述中，让 LLM 知道有哪些资源可用。

---

## 5. MCP vs 内置 Tool 对比

| 维度 | 内置 Tool | MCP Tool |
|------|-----------|----------|
| **定义方式** | Python 类 + `@register_tool` | MCP Server 声明 |
| **注册** | `TOOL_REGISTRY` | `MCPManager.clients` |
| **执行** | 本地 Python 调用 | 进程间通信 (stdio/SSE/HTTP) |
| **发现** | 代码中引用 | 运行时动态获取 |
| **隔离** | 与 Agent 同进程 | 独立进程，沙箱安全 |
| **生态** | 框架内置 | MCP 开放生态 |
| **配置** | Python 代码 | JSON 配置文件 |

**选择建议**：
- 简单的本地操作 → 内置 Tool
- 需要进程隔离/沙箱 → MCP
- 使用第三方 MCP Server → MCP
- 高性能需求 → 内置 Tool（无 IPC 开销）

---

## 6. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `tools/mcp_manager.py` | 31 | `MCPManager` 单例 |
| `tools/mcp_manager.py` | 34 | `__new__` 单例实现 |
| `tools/mcp_manager.py` | 57 | monkey-patch 子进程追踪 |
| `tools/mcp_manager.py` | 94 | `is_valid_mcp_servers` |
| `tools/mcp_manager.py` | 139 | `initConfig` (同步入口) |
| `tools/mcp_manager.py` | 152 | `init_config_async` |
| `tools/mcp_manager.py` | 265 | `create_tool_class` (动态工具创建) |
| `tools/mcp_manager.py` | 289 | `shutdown` |
| `tools/mcp_manager.py` | 313 | `MCPClient` 类 |
| `tools/mcp_manager.py` | 325 | `connection_server` (三种模式) |
| `tools/mcp_manager.py` | 390 | `reconnect` (自动重连) |
| `tools/mcp_manager.py` | 401 | `execute_function` |
| `tools/mcp_manager.py` | 463 | `cleanup` |
| `tools/mcp_manager.py` | 467 | `_cleanup_mcp` (atexit 处理) |

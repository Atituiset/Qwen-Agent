# MCP 配置与集成

## 1. Stdio 模式配置

```json
{
  "mcpServers": {
    "my_tool": {
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "env": {"API_KEY": "xxx"}
    }
  }
}
```

适用场景：本地命令行工具，MCP Server 作为子进程运行。

## 2. SSE 模式配置

```json
{
  "mcpServers": {
    "remote_tool": {
      "url": "https://mcp.example.com/sse",
      "type": "sse"
    }
  }
}
```

适用场景：远程 MCP Server，通过 HTTP 长连接通信。

## 3. Streamable HTTP 模式配置

```json
{
  "mcpServers": {
    "modern_api": {
      "url": "https://mcp.example.com/mcp",
      "type": "streamable-http"
    }
  }
}
```

适用场景：现代 HTTP 传输，支持流式响应。

## 4. 与 Agent 集成

### 4.1 基本集成

```python
from qwen_agent.agents import Assistant
from qwen_agent.tools.mcp_manager import MCPManager

# MCP Server 配置
mcp_config = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        }
    }
}

# 初始化 MCP
manager = MCPManager()
mcp_tools = manager.initConfig(mcp_config)

# 创建 Agent，同时使用内置工具 + MCP 工具
agent = Assistant(
    llm={'model': 'qwen-max', 'model_type': 'qwen_dashscope'},
    function_list=['code_interpreter'] + mcp_tools,  # 混合使用
)
```

### 4.2 多个 MCP Server

```python
mcp_config = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
        },
        "database": {
            "command": "python",
            "args": ["db_mcp_server.py"],
            "env": {"DB_URL": "sqlite:///data.db"}
        },
        "web_search": {
            "url": "https://mcp-search.example.com/sse",
            "type": "sse"
        }
    }
}

manager = MCPManager()
mcp_tools = manager.initConfig(mcp_config)
# mcp_tools 是 BaseTool 实例列表，可直接加入 function_list
```

### 4.3 工具调用流程

```
LLM 输出 function_call: {name: "filesystem_read_file", arguments: {...}}
    │
    ▼
Agent._call_tool()
    │
    ▼
BaseTool.call()  ← 这是动态创建的 ToolClass 实例
    │
    ▼
asyncio.run_coroutine_threadsafe(
    MCPClient.execute_function("read_file", args),
    mcp_loop
)
    │
    ▼
MCP Server 进程执行 read_file
    │
    ▼
返回文件内容 → Agent 将结果注入消息 → 继续对话
```

## 5. 清理连接

```python
# 程序退出时自动清理（atexit 已注册）
# 或手动清理
manager.shutdown()
```

## 6. 验证配置

```python
manager = MCPManager()
if manager.is_valid_mcp_servers(config):
    tools = manager.initConfig(config)
else:
    print("MCP 配置格式无效")
```

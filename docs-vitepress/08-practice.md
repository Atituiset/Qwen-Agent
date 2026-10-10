# 08 实战指南：从零构建 Qwen-Agent 应用

## 1. 环境准备

### 1.1 安装

```bash
pip install qwen-agent
# 或从源码
pip install -e .
```

### 1.2 可选依赖

```bash
# Code Interpreter (Docker 沙箱)
pip install docker

# 文档解析
pip install python-docx python-pptx openpyxl pdfplumber

# MCP 支持
pip install mcp

# Web UI
pip install gradio
```

### 1.3 API Key 配置

```bash
# 阿里 DashScope
export DASHSCOPE_API_KEY='sk-xxx'

# 或 OpenAI 兼容
export OPENAI_API_KEY='sk-xxx'
export OPENAI_API_BASE='https://api.openai.com/v1'
```

---

## 2. 最简 Agent

### 2.1 纯对话（无工具）

```python
from qwen_agent.llm import get_chat_model

llm = get_chat_model({'model': 'qwen-max', 'model_type': 'qwen_dashscope'})
messages = [{'role': 'user', 'content': '你好，请介绍一下你自己'}]

for response in llm.chat(messages):
    print(response[-1]['content'])
```

### 2.2 带工具的 FnCallAgent

```python
from qwen_agent.agents import FnCallAgent

agent = FnCallAgent(
    llm={'model': 'qwen-max', 'model_type': 'qwen_dashscope'},
    function_list=['web_search'],  # 内置工具名
    system_message='你是一个智能助手，请使用工具回答问题。',
)

messages = [{'role': 'user', 'content': '今天北京天气如何？'}]
for response in agent.run(messages):
    print(response[-1]['content'])
```

---

## 3. RAG 知识库问答

```python
from qwen_agent.agents import Assistant

agent = Assistant(
    llm={'model': 'qwen-max', 'model_type': 'qwen_dashscope'},
    function_list=['code_interpreter'],
    files=['/path/to/report.pdf', '/path/to/data.xlsx'],
    rag_cfg={
        'max_ref_token': 15000,
        'parser_page_size': 500,
        'rag_searchers': ['keyword_search', 'front_page_search'],
        'rag_keygen_strategy': 'GenKeyword',
    },
    system_message='你是一个文档问答助手，请根据知识库内容回答。',
)

messages = [{'role': 'user', 'content': '报告第三季度的收入是多少？'}]
for response in agent.run(messages):
    print(response[-1]['content'])
```

---

## 4. MCP 工具接入

```python
from qwen_agent.tools.mcp_manager import MCPManager
from qwen_agent.agents import Assistant

mcp_config = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        },
        "web_fetcher": {
            "url": "https://mcp.example.com/sse",
            "type": "sse",
        }
    }
}

manager = MCPManager()
mcp_tools = manager.initConfig(mcp_config)

agent = Assistant(
    llm={'model': 'qwen-max', 'model_type': 'qwen_dashscope'},
    function_list=mcp_tools,
    files=['/tmp/readme.md'],
)
```

---

## 5. 多 Agent 编排

### 5.1 Router：专家路由

```python
from qwen_agent.agents import Router, Assistant

coder = Assistant(llm=llm_cfg, name='coder',
    description='擅长编程和技术问题',
    function_list=['code_interpreter'])

researcher = Assistant(llm=llm_cfg, name='researcher',
    description='擅长搜索和分析信息',
    function_list=['web_search'],
    files=['/path/to/knowledge_base'])

router = Router(llm=llm_cfg, agents=[coder, researcher])

messages = [{'role': 'user', 'content': '帮我写一个排序算法'}]
for response in router.run(messages):
    print(response[-1]['content'])
```

### 5.2 GroupChat：多 Agent 协作

```python
from qwen_agent.agents import GroupChat, Assistant

writer = Assistant(llm=llm_cfg, name='writer',
    system_message='你是一个写作者，负责撰写文章。')

editor = Assistant(llm=llm_cfg, name='editor',
    system_message='你是一个编辑，负责审阅和改进文章。')

group = GroupChat(
    agents=[writer, editor],
    agent_selection_method='auto',
    llm=llm_cfg,
)

messages = [{'role': 'user', 'content': '请写一篇关于 AI 的文章'}]
for response in group.run(messages, max_round=4):
    for msg in response:
        if msg.get('name'):
            print(f"[{msg['name']}]: {msg['content'][:100]}...")
```

---

## 6. FnCall 模板配置

```python
llm_cfg = {
    'model': 'qwen-max',
    'model_type': 'qwen_dashscope',
    'generate_cfg': {
        'fncall_prompt_type': 'nous',  # 'nous'(默认,通用) 或 'qwen'(Qwen原生)
    }
}
```

当模型服务端原生支持 FnCall 时（如 vLLM 部署），跳过客户端模板转换：

```python
import os
os.environ['QWEN_AGENT_USE_RAW_API'] = 'true'
```

---

## 7. 完整示例：知识库 + MCP + 多工具

```python
from qwen_agent.agents import Assistant
from qwen_agent.tools.mcp_manager import MCPManager

# 1. 初始化 MCP
mcp = MCPManager()
mcp_tools = mcp.initConfig({
    "mcpServers": {
        "db": {"command": "python", "args": ["db_mcp_server.py"]}
    }
})

# 2. 创建 Agent
agent = Assistant(
    llm={
        'model': 'qwen-max',
        'model_type': 'qwen_dashscope',
        'generate_cfg': {'fncall_prompt_type': 'nous'},
    },
    function_list=['code_interpreter', 'web_search'] + mcp_tools,
    files=['/data/report.pdf'],
    rag_cfg={
        'rag_keygen_strategy': 'SplitQueryThenGenKeyword',
        'max_ref_token': 20000,
    },
    system_message='你是一个全能分析师。先用知识库查找，必要时用代码和搜索补充。',
)

# 3. 运行
messages = [{'role': 'user', 'content': '分析报告中各季度收入趋势，画一个图表'}]
for response in agent.run(messages):
    print(response[-1]['content'][:200])

# 4. 清理
mcp.shutdown()
```

---

## 8. 流式获取中间过程

```python
for response in agent.run(messages):
    # response 是累积的消息列表
    # 每次 yield 包含最新的中间状态
    last_msg = response[-1]
    if last_msg.get('function_call'):
        print(f"调用工具: {last_msg['function_call']['name']}")
    elif last_msg.get('role') == 'function':
        print(f"工具结果: {last_msg['content'][:50]}...")
    else:
        print(f"LLM: {last_msg['content'][:50]}...")
```

---

## 9. 使用 OpenAI 兼容接口

```python
llm_cfg = {
    'model': 'gpt-4o',
    'model_type': 'openai',
    'api_key': 'sk-xxx',           # 或环境变量 OPENAI_API_KEY
    'api_base': 'https://api.openai.com/v1',  # 或自定义端点
}
```

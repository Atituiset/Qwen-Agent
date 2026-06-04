# 01 框架全景：从 Agent 范式到 Qwen-Agent 架构

## 1. Agent 范式的历史演进

### 1.1 从 LLM 到 Agent：为什么需要框架？

大语言模型（LLM）的核心能力是「根据上下文生成文本」。但现实世界的任务远不止文本生成——需要搜索信息、执行代码、操作数据库、调用 API。于是业界形成了一个共识：

> **Agent = LLM + Tool + Workflow + Memory**

这一等式概括了 Agent 的四大支柱：
- **LLM**：推理与决策的大脑
- **Tool**：与外部世界交互的双手
- **Workflow**：组织思考与行动的流程
- **Memory**：记住过去、积累知识的存储

### 1.2 时间线：Agent 框架的演进脉络

```
2023.03 ─── ReAct 论文 (Yao et al.)
│  提出 Thought→Action→Observation 循环
│
2023.03 ─── Toolformer (Schick et al.)
│  让 LLM 学会自我插入工具调用
│
2023.06 ─── OpenAI Function Calling
│  GPT 系列原生支持结构化函数调用
│  从此，"Prompt Hack" 走向 "API 标准"
│
2023.08 ─── LangChain / AutoGen 等框架爆发
│  Agent 编排框架成为独立赛道
│
2023.09 ─── Qwen-Agent 开源
│  阿里 Qwen 团队推出，深度绑定 Qwen 模型
│
2024.06 ─── OpenAI Structured Output
│  强化 JSON Schema 约束，提升 FnCall 可靠性
│
2024.11 ─── Anthropic MCP 发布
│  Model Context Protocol 标准化工具接入
│  从"各自定义 Tool"走向"协议统一"
│
2025.03 ─── Qwen3 + QwQ 支持
│  Qwen-Agent 适配推理模型，支持思考链+FnCall
│
2025.05 ─── Qwen3-VL Tool-call
│  多模态 Agent：视觉+工具联动
│
2025.07 ─── Qwen3-Coder + vLLM 原生 FnCall
│  支持 API 级别的 tool_call 解析
│
2026.02 ─── Qwen3.5 + DeepPlanning Benchmark
│  Qwen-Agent 成为 Qwen Chat 的后端
```

### 1.3 Qwen-Agent 的定位

Qwen-Agent 在 Agent 框架生态中的独特定位：

| 维度 | LangChain | AutoGen | **Qwen-Agent** |
|------|-----------|---------|----------------|
| 核心理念 | 通用编排链 | 多 Agent 对话 | **模型原生能力优先** |
| Tool 接入 | 自定义接口 | 自定义接口 | **FnCall + MCP 双通道** |
| 模型绑定 | 模型无关 | 模型无关 | **深度适配 Qwen 系列** |
| RAG 能力 | 外接向量库 | 外接向量库 | **内置文档解析+混合检索** |
| 生产验证 | 社区驱动 | 学术驱动 | **Qwen Chat 后端** |

**核心设计哲学**：不过度抽象，让 LLM 的原生 Function Call 能力做主力，框架做胶水和编排。

---

## 2. 项目结构全景

```
Qwen-Agent/
├── qwen_agent/                    # 核心框架代码
│   ├── __init__.py                # 导出 Agent, MultiAgentHub
│   ├── agent.py                   # Agent 基类 + BasicAgent
│   ├── settings.py                # 全局配置常量
│   ├── log.py                     # 日志工具
│   │
│   ├── llm/                       # LLM 层：模型接入与消息处理
│   │   ├── __init__.py            # get_chat_model() 工厂
│   │   ├── schema.py              # Message, FunctionCall, ContentItem 数据模型
│   │   ├── base.py                # BaseChatModel 抽象基类
│   │   ├── function_calling.py    # BaseFnCallModel + FnCall 预处理/后处理
│   │   ├── oai.py                 # OpenAI 兼容接口
│   │   ├── qwen_dashscope.py      # 阿里 DashScope 接口
│   │   └── fncall_prompts/        # 两种 FnCall Prompt 模板
│   │       ├── base_fncall_prompt.py
│   │       ├── nous_fncall_prompt.py   # Nous Hermes 格式 (默认，推荐)
│   │       └── qwen_fncall_prompt.py   # Qwen 原生特殊标记格式
│   │
│   ├── tools/                     # Tool 层：工具注册与执行
│   │   ├── __init__.py            # 导出 TOOL_REGISTRY, BaseTool, MCPManager
│   │   ├── base.py                # BaseTool + register_tool 装饰器
│   │   ├── mcp_manager.py         # MCP 协议管理器 (单例)
│   │   ├── retrieval.py           # RAG 检索工具
│   │   ├── doc_parser.py          # 文档解析+分块
│   │   ├── code_interpreter.py    # Docker 沙箱代码执行
│   │   ├── image_gen.py           # 图片生成
│   │   └── search_tools/          # 检索子模块
│   │       ├── keyword_search.py
│   │       ├── front_page_search.py
│   │       └── hybrid_search.py
│   │
│   ├── memory/                    # Memory 层：记忆与知识管理
│   │   ├── __init__.py
│   │   └── memory.py              # Memory Agent (RAG 入口)
│   │
│   ├── agents/                    # Agent 层：各种工作流实现
│   │   ├── fncall_agent.py        # FnCallAgent (FnCall 循环)
│   │   ├── assistant.py           # Assistant (FnCall + RAG)
│   │   ├── react_chat.py          # ReActChat (ReAct 格式)
│   │   ├── router.py              # Router (多 Agent 路由)
│   │   ├── group_chat.py          # GroupChat (多 Agent 群聊)
│   │   ├── doc_qa/                # 文档问答 Agent 集群
│   │   ├── writing/               # 写作 Agent 集群
│   │   └── keygen_strategies/     # RAG 关键词生成策略
│   │
│   ├── multi_agent_hub.py         # 多 Agent 管理基类
│   └── gui/                       # Gradio Web UI
│
├── examples/                      # 官方示例
├── tests/                         # 测试
└── docs/                          # 源解析文档
```

---

## 3. 核心抽象：三层四类

Qwen-Agent 的核心可以用「三层四类」来概括：

```
┌──────────────────────────────────────────┐
│  Agent 层 (编排)                          │
│    Agent -> FnCallAgent -> Assistant      │
│    Agent -> ReActChat                    │
│    Agent -> Router / GroupChat           │
├──────────────────────────────────────────┤
│  LLM 层 (推理)                            │
│    BaseChatModel -> BaseFnCallModel      │
│      -> OAI / QwenDashScope / ...       │
├──────────────────────────────────────────┤
│  Tool 层 (行动)                           │
│    BaseTool -> 内置 Tool / 自定义 Tool    │
│    MCPManager -> MCP Tool (动态)         │
└──────────────────────────────────────────┘
```

### 3.1 四个核心类

| 类 | 位置 | 职责 | 关键方法 |
|----|------|------|----------|
| `Agent` | `agent.py:31` | 编排基类 | `_run()`, `_call_llm()`, `_call_tool()`, `_detect_tool()` |
| `BaseChatModel` | `llm/base.py:61` | LLM 抽象 | `chat()`, `_preprocess_messages()`, `_postprocess_messages()` |
| `BaseTool` | `tools/base.py:109` | 工具抽象 | `call()`, `_verify_json_format_args()` |
| `Message` | `llm/schema.py:132` | 消息模型 | Pydantic BaseModel，含 role/content/function_call |

### 3.2 数据流：一次 Agent 运行的完整路径

```
用户输入 (messages)
    │
    ▼
Agent.run()              # 消息类型转换 + system_message 注入
    │
    ▼
Agent._run()             # 子类实现的具体工作流
    │
    ├──→ _call_llm()     # 调用 LLM
    │       │
    │       ▼
    │   BaseChatModel.chat()
    │       │
    │       ├──→ _preprocess_messages()      # FnCall 模板注入/消息格式化
    │       ├──→ _chat_with_functions()      # 发送到模型 API
    │       └──→ _postprocess_messages()     # FnCall 输出解析 -> FunctionCall 对象
    │
    ├──→ _detect_tool()  # 检测 LLM 输出中是否有函数调用
    │
    ├──→ _call_tool()    # 执行工具
    │       │
    │       ▼
    │   BaseTool.call()  # 工具具体逻辑
    │
    └──→ 循环 (LLM -> Tool -> LLM -> ...) 直到无工具调用
    │
    ▼
yield response           # 流式返回
```

### 3.3 注册机制

Qwen-Agent 使用全局注册表模式实现可扩展性：

```python
# 工具注册
TOOL_REGISTRY = {}  # tools/base.py:24

@register_tool('my_tool')  # 装饰器注册
class MyTool(BaseTool):
    name = 'my_tool'
    ...

# LLM 注册
LLM_REGISTRY = {}  # llm/base.py:32

@register_llm('qwen_dashscope')  # 装饰器注册
class QwenDashScopeChat(BaseFnCallModel):
    ...

# 使用时通过工厂方法获取
llm = get_chat_model({'model': 'qwen-max', 'model_type': 'qwen_dashscope'})
tool = TOOL_REGISTRY['code_interpreter'](cfg)
```

---

## 4. 关键设计决策

### 4.1 为什么 FnCall 而非纯 Prompt？

Qwen-Agent 将 Function Call 作为核心范式，而非像早期框架那样用纯文本 Prompt 拼接工具描述。原因是：
- **结构化**：JSON 格式的函数调用比正则匹配文本更可靠
- **并行性**：单次响应可包含多个函数调用
- **兼容性**：与 OpenAI API、vLLM 等服务端原生兼容
- **可验证**：JSON Schema 可校验参数合法性

### 4.2 为什么同时支持 Nous 和 Qwen 两种 FnCall 格式？

- **Nous 格式**（默认）：使用纯文本标记，任何 chat 模型都能用，无需特殊 tokenizer
- **Qwen 格式**：使用花形特殊标记，需要 Qwen 系列 tokenizer 支持，但提供双语模板和 function_choice 约束

### 4.3 为什么 Memory 是 Agent？

Memory 继承自 Agent 而非独立模块，因为它内部需要调用 LLM（关键词生成）和 Tool（检索/文档解析），复用 Agent 的基础设施是自然选择。

### 4.4 为什么 MCPManager 是单例？

MCPManager 管理一个全局 asyncio 事件循环线程和所有 MCP 客户端连接。多个实例会导致多个事件循环线程和资源浪费，单例模式确保全局共享。

### 4.5 为什么消息截断是 4 步渐进式？

简单截断会丢失关键上下文。4 步策略按信息重要性递减截断：先截长函数结果（最不重要的细节），再移除中间轮次，再截最近函数结果，最后才截用户/助手文本。始终保留 system message 和最后一轮。

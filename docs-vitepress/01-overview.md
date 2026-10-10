# 01 框架全景：从 Agent 范式到 Qwen-Agent 架构

> 这一系列不再抄 README，目标是讲清"为什么这样设计、不那样会怎样、改了会踩什么坑"。本章是骨架级全景，每个层次的下钻见对应后续章节。

读完应能回答的 7 个判断题：
1. 加一个新 Agent 应继承谁，必须实现什么？
2. 加一个新 Tool，三种注册方式哪种适合我？
3. 加一个新模型后端，要实现哪几个抽象方法？
4. Agent 的 `run` 和 `_run` 为什么不合并？
5. `_call_tool` 吞 Exception 回喂 LLM，bug 还是设计？
6. 何时该用 MCP tool 而不是注册 Tool？
7. 何时该用 ReAct 而不是 FnCall？

完整 11 节深度版：仓库 `docs/01_框架全景.md`。

---

## 1. 时间线坐标（不是目录，是判断依据）

```
2023.03 ReAct          Thought→Action→Observation 循环
2023.03 Toolformer      LLM 自插工具
2023.06 OpenAI FnCall   JSON 结构化 → "API 标准"
2023.08 LangChain/AutoGen   框架成独立赛道
2023.09 Qwen-Agent 开源    深度绑定 Qwen，模型原生能力优先
2024.06 OpenAI Structured Output  JSON Schema 强约束
2024.11 Anthropic MCP   tool 接入走向协议统一
2025.03 Qwen3 + QwQ     推理模型 + FnCall
2025.05 Qwen3-VL        多模态 Agent
2025.07 Qwen3-Coder + vLLM 原生 FnCall
2026.02 Qwen3.5         Qwen Chat 后端
```

| 维度      | LangChain        | AutoGen          | **Qwen-Agent**                  |
|-----------|------------------|------------------|---------------------------------|
| 核心理念  | 通用编排链        | 多 Agent 对话     | **模型原生能力优先**            |
| Tool 接入 | 自定义接口        | 自定义接口        | **FnCall + MCP 双通道**          |
| 模型绑定  | 模型无关          | 模型无关          | **深度适配 Qwen 系列**          |
| RAG 能力 | 外接向量库        | 外接向量库        | **内置文档解析+混合检索**        |
| 生产验证 | 社区驱动          | 学术驱动          | **Qwen Chat 后端**               |

**核心哲学**：不过度抽象，让 LLM 的原生 Function Call 能力做主力。后面所有"为什么不那样"都从这条推出来。

---

## 2. 三层四类一图

```
┌─────────────────────────────────────────────┐
│            Agent 层（编排）                  │
│   Agent → FnCallAgent → Assistant            │
│   Agent → ReActChat                          │
│   Agent → Router / GroupChat                 │
├─────────────────────────────────────────────┤
│            LLM 层（推理）                    │
│   BaseChatModel → BaseFnCallModel            │
│      → OAI / QwenDashScope / ...             │
├─────────────────────────────────────────────┤
│            Tool 层（行动）                   │
│   BaseTool → 内置 Tool / 自定义 Tool         │
│   MCPManager → MCP Tool（动态）              │
└─────────────────────────────────────────────┘
```

四个核心类，都在本仓库的真实位置：

| 类               | 文件:行              | 关键方法                                  |
|------------------|----------------------|-------------------------------------------|
| `Agent`          | `qwen_agent/agent.py:31`   | `run()` / `_run()` / `_call_llm()` / `_call_tool()` / `_init_tool()` |
| `BaseChatModel`  | `qwen_agent/llm/base.py:61` | `chat()` / `_preprocess_messages()` / `_chat_with_functions()` / `_postprocess_messages()` |
| `BaseTool`       | `qwen_agent/tools/base.py:109` | `call()` / `_verify_json_format_args()` |
| `Message`        | `qwen_agent/llm/schema.py:132` | `role` / `content` / `function_call`     |

---

## 3. 设计决策速查（8 项核心，详细的 §2~§8 见扁平版）

| #    | 决策点                            | 替代方案不选的理由                 | 详见扁平版 §N |
|------|-----------------------------------|------------------------------------|--------------|
| 1    | `run` 永远不子类重写、`_run` 必须重写 | 子类每次要重复 deepcopy/lang 推断/system 注入 | §2 |
| 2    | Tool 三入口：BaseTool 实例 / MCP dict / 字符串名 | 覆盖"已构造 / MCP 配置 / registry 快路径"三种实际场景 | §3 |
| 3    | LLM 支持传 dict 自动工厂路由        | examples 一行起，代价是多 isinstance 判断 | §4 |
| 4    | 一般 Exception 转 string 回喂 LLM，ToolServiceError 透传 | 让 LLM 自愈小 hiccup，基础设施级故障让 Agent 流停 | §5 |
| 5    | 三套全局注册表 `ToolRegistry / LLMRegistry / MCPManager` | 进程级 import-time 注册，比 entry_points 简单 | §6 |
| 6    | 内置 Tool 进程内、MCP Tool 跨进程   | MCP 利于语言/生态复用，代价是 IPC 延迟          | §7 |
| 7    | FnCall 与 ReAct 双工作流           | 兼容不支持原生 FnCall 的开源模型                | §8 |
| 8    | Agent 构造时传 system_message，不在 `_run` 内重复注入 | `run` 已注入，子类复注入会变成多 system 行    | §2 |

---

## 4. 数据流：一次 Agent 运行的完整路径

```
用户 messages
   │
   ▼
Agent.run() (agent.py:78)
   ├── copy.deepcopy(messages)                       # 防污染
   ├── 类型推断：dict → Message，默认返回 dict
   ├── lang 自动推断（has_chinese_messages）
   ├── system_message 注入逻辑：
   │     - 没 system 行就 insert(0, ...) 那条
   │     - 有 system 字符串就拼到前部
   │     - 有 system list（多模态）就放 ContentItem 前部
   └── 调 _run(...)
         │
         ▼
   _run（子类实现）：
   ├── _call_llm → BaseChatModel.chat
   │     └─ 内部 _preprocess（FnCall 模板注入/消息格式化）
   │        → _chat_with_functions → _postprocess（→ FunctionCall 对象）
   ├── _detect_tool（検 LLM 输出含 function_call）
   ├── _call_tool：
   │     - ToolServiceError / DocParserError → raise
   │     - 其他 Exception → 转 string 回喂 LLM
   └── 循环直到无 function_call → yield response
```

`Agent.run` 在基类做完所有"对所有 Agent 一致"的事，子类只关心业务流。

---

## 5. 项目结构速览

```
qwen_agent/
├── agent.py                # Agent + BasicAgent
├── settings.py             # 全局默认常量
├── llm/                    # LLM 抽象 + 多后端
│   ├── base.py             # BaseChatModel、LLM_REGISTRY
│   ├── function_calling.py # BaseFnCallModel（FnCall 预/后处理）
│   ├── oai.py / qwen_dashscope.py
│   └── fncall_prompts/      # Nous / Qwen 两种 FnCall 模板（见 02 章）
├── tools/                  # 工具层
│   ├── base.py             # BaseTool、TOOL_REGISTRY、ToolServiceError
│   ├── mcp_manager.py      # MCPManager 单例
│   ├── retrieval.py / doc_parser.py / simple_doc_parser.py / ...
│   └── search_tools/       # keyword / front_page / hybrid_search
├── memory/
│   └── memory.py           # Memory._run（见 09 章）
├── agents/
│   ├── fncall_agent.py     # FnCall 循环骨架（FnCallAgent）
│   ├── assistant.py        # FnCallAgent + RAG = Assistant
│   ├── react_chat.py       # ReActChat 继承 FnCallAgent，override _run + prompt
│   ├── router.py / group_chat.py / ...
│   └── keygen_strategies/  # 4 个 keygen 策略（见 09 章）
└── multi_agent_hub.py      # MultiAgentHub 基类
```

---

## 6. 关键代码片段手册

### 6.1 注册并使用一个 Tool

```python
from qwen_agent.tools import register_tool, BaseTool

@register_tool('my_tool')
class MyTool(BaseTool):
    name = 'my_tool'
    def call(self, params, **kwargs):
        ...
```

### 6.2 起一个最简 Assistant

```python
from qwen_agent.agents import Assistant
agent = Assistant(
    llm={'model': 'qwen-max', 'api_key': '...'},
    function_list=['code_interpreter'],
)
for rsp in agent.run([{'role': 'user', 'content': 'hi'}]):
    print(rsp)
```

### 6.3 接一个 MCP server

```python
mcp_cfg = {
    'mcpServers': {
        'filesystem': {
            'command': 'npx',
            'args': ['-y', '@modelcontextprotocol/server-filesystem', '/tmp']
        }
    }
}
agent = Assistant(function_list=[mcp_cfg], llm=cfg)
```

---

## 7. 改这块代码前的检查清单（详细见扁平版 §10）

- [ ] 新 Agent 子类必须实现 `_run` 返回 `Iterator[List[Message]]`
- [ ] 新 Agent 子类 `_run` 内不要重复注入 system message
- [ ] 新 Tool 用 `@register_tool('name')` 装饰，`name` 严格匹配类 attribute
- [ ] 新 Tool 的 cfg 字段名**不能叫 `mcpServers`**
- [ ] 新 Tool 异常：希望 LLM 可见 = 普通 Exception；希望立即中断 = `ToolServiceError`
- [ ] 新模型后端：实现 `chat + _preprocess + _chat_with_functions + _postprocess`
- [ ] 切换 MCP Tool：评估 20~50ms IPC 延迟对 FnCall 循环点的影响
- [ ] ReAct 模式：仅当模型不支持原生 FnCall 时选；FnCall 路径效率更高

---

## 8. 下一站

- 想知道 FnCall 两种 prompt 模板的差异：[02 Function Call](./02-fncall.md)
- 想知道 BaseTool 的 `call` 内部如何解析 JSON：[03 Tool 系统](./03-tool.md)
- 想知道 MCPManager 单例的细节：[04 MCP](./04-mcp.md)
- 想知道 Memory 在 Agent 基类中怎么用：[05 Memory 与 RAG](./05-memory.md)
- 想知道多 Agent 怎么编排：[06 Agent 编排](./06-agent.md)
- 想知道 LLM preprocess/postprocess 在做什么：[07 LLM 层](./07-llm.md)
- 想看一个完整决策权衡版的 Memory 子系统拆解：[09 Memory 机制深度剖析](./09-memory-deep-dive.md)

完。

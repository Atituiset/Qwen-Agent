# 06 Agent 编排：从单 Agent 到多 Agent 协作

## 1. Agent 家族总览

```
Agent (agent.py:31)                    ← 所有 Agent 的基类
│
├── FnCallAgent (agents/fncall_agent.py:27)  ← FnCall 循环（核心）
│   └── Assistant (agents/assistant.py:81)   ← FnCall + RAG
│       └── Router (agents/router.py:36)     ← 多 Agent 路由
│
├── ReActChat (agents/react_chat.py:50)      ← ReAct 格式
│
├── GroupChat (agents/group_chat.py:29)      ← 多 Agent 群聊
│
└── Memory (memory/memory.py:32)             ← 记忆检索（也是 Agent）
```

---

## 2. Agent 基类

### 2.1 核心接口（agent.py:31）

```python
class Agent:
    def run(self, messages, **kwargs):
        """外部调用入口：消息类型转换 + system_message 注入"""
        ...

    def _run(self, messages, **kwargs):
        """子类实现的具体工作流（抽象方法）"""
        ...

    def _call_llm(self, messages, functions=None, extra_generate_cfg=None):
        """调用 LLM"""
        ...

    def _call_tool(self, tool_name, tool_args, **kwargs):
        """执行工具"""
        ...

    def _detect_tool(self, text):
        """检测 LLM 输出中是否有函数调用"""
        ...
```

### 2.2 消息流转换

`run()` 是外部接口，负责：
1. 将 dict 消息转为 `Message` 对象
2. 注入 `system_message`
3. 调用 `_run()`
4. 流式返回结果

---

## 3. FnCallAgent：核心编排

### 3.1 _run 循环（agents/fncall_agent.py:73）

这是 Qwen-Agent 最核心的编排逻辑——**LLM + Tool 循环**：

```
┌─────────────────────────────────────────┐
│ num_llm_calls_available = MAX (20)      │
│                                         │
│  ┌─── while calls > 0 ───────────────┐ │
│  │                                    │ │
│  │  1. _call_llm(messages, functions)│ │
│  │     → 流式输出 LLM 响应           │ │
│  │                                    │ │
│  │  2. 检测每个输出消息:             │ │
│  │     _detect_tool(out)             │ │
│  │     → 有 function_call?           │ │
│  │                                    │ │
│  │  3a. 有工具调用:                  │ │
│  │     _call_tool(name, args)        │ │
│  │     → 创建 FUNCTION 角色消息      │ │
│  │     → 追加到 messages             │ │
│  │     → yield 中间结果              │ │
│  │     → 继续循环                    │ │
│  │                                    │ │
│  │  3b. 无工具调用:                  │ │
│  │     → break（LLM 给出最终回答）   │ │
│  │                                    │ │
│  └────────────────────────────────────┘ │
│                                         │
│  yield 最终 response                     │
└─────────────────────────────────────────┘
```

### 3.2 安全限制

`MAX_LLM_CALL_PER_RUN`（默认 20）防止无限循环。每次调用 LLM 递减计数器，归零则强制退出。

### 3.3 _call_tool 文件处理

```python
def _call_tool(self, tool_name, tool_args, **kwargs):
    tool = self.function_map[tool_name]
    if tool.file_access:
        # 从消息历史 + self.mem.system_files 提取文件
        files = self._extract_files_from_messages(messages)
        return tool.call(tool_args, files=files, **kwargs)
    else:
        return super()._call_tool(tool_name, tool_args, **kwargs)
```

---

## 4. Assistant：RAG 增强的 FnCall

### 4.1 继承关系

```python
class Assistant(FnCallAgent):  # assistant.py:81
```

### 4.2 知识注入

Assistant 在 FnCall 循环之前，自动调用 Memory 检索知识并注入 system prompt：

```
用户消息 → Memory.run() → 检索结果 → 格式化 → 追加到 system → FnCall 循环
```

详见 [Memory 与 RAG](./05-memory.md)。

---

## 5. ReActChat：思维链 + 行动

### 5.1 格式（agents/react_chat.py:50）

ReActChat 使用经典的 ReAct 格式：

```
Question: 用户的问题
Thought: 我需要先搜索相关信息
Action: web_search
Action Input: {"query": "Qwen3 发布时间"}
Observation: [系统注入的工具结果]
Thought: 根据搜索结果，Qwen3 于...
Final Answer: Qwen3 于 2025 年 3 月发布。
```

### 5.2 与 FnCallAgent 的区别

| 维度 | FnCallAgent | ReActChat |
|------|-------------|-----------|
| **工具调用格式** | 结构化 FunctionCall 对象 | 文本 `Action:` / `Action Input:` |
| **思考过程** | 隐式（模型内部） | 显式 `Thought:` 输出 |
| **停止控制** | 模型自动 | Stop words: `Observation:` |
| **多模态** | ✅ 支持 | ❌ 仅文本 |
| **适用场景** | 通用（推荐） | 需要显式推理链的场景 |

### 5.3 Stop Words 机制

```python
extra_generate_cfg['stop'] = ['Observation:', 'Observation:\n']
```

模型输出 `Action Input:` 后，遇到 `Observation:` 停止，系统注入工具结果，然后追加 `Thought: ` 让模型继续。

---

## 6. 选择指南

| 需求 | 推荐 Agent | 原因 |
|------|-----------|------|
| 单一任务，需要工具 | `FnCallAgent` | 最简洁，FnCall 原生 |
| 单一任务 + 文档知识 | `Assistant` | 自动 RAG |
| 显式推理链 | `ReActChat` | Thought 可观察 |
| 多专家路由 | `Router` | LLM 自动选择专家 |
| 多 Agent 协作 | `GroupChat` | 多轮群聊 |
| 自定义编排 | 继承 `Agent` | 完全自由 |

---

## 7. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `agent.py` | 31 | `Agent` 基类 |
| `agents/fncall_agent.py` | 27 | `FnCallAgent` |
| `agents/fncall_agent.py` | 73 | `_run` (FnCall 循环) |
| `agents/fncall_agent.py` | 110 | `_call_tool` (文件处理) |
| `agents/assistant.py` | 81 | `Assistant` |
| `agents/assistant.py` | 100 | `_run` (RAG 注入) |
| `agents/react_chat.py` | 50 | `ReActChat` |
| `agents/react_chat.py` | 73 | `_run` (ReAct 循环) |
| `agents/react_chat.py` | 134 | `_detect_tool` (文本解析) |

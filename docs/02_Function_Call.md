# 02 Function Call：让 LLM 学会使用工具

## 1. 历史脉络：从 Prompt Hack 到 API 标准

### 1.1 前函数调用时代（2023 年之前）

早期的工具使用完全依赖 Prompt 工程：

```
你可以使用以下工具：
1. 天气查询：输入城市名，返回天气
2. 计算器：输入表达式，返回结果

请用以下格式调用工具：
Action: 工具名
Action Input: 工具参数
```

这种方式的问题：
- LLM 经常格式错乱（缺少换行、多余空格等）
- 无法并行调用多个工具
- 需要复杂的正则/字符串匹配来解析输出
- 不同模型的格式差异大

### 1.2 OpenAI Function Calling（2023.06）

OpenAI 在 Chat Completions API 中引入了 `functions` 参数和结构化的函数调用输出：

```json
{
  "role": "assistant",
  "content": null,
  "function_call": {
    "name": "get_weather",
    "arguments": "{\"location\": \"Beijing\"}"
  }
}
```

这是范式转变——从「让模型模仿文本格式」到「模型原生输出结构化调用」。但有一个问题：**只有 OpenAI 的 API 支持这种格式**。

### 1.3 社区标准化之路

为了让开源模型也能支持函数调用，社区形成了多种方案：

| 方案 | 代表 | 格式 | 特点 |
|------|------|------|------|
| Hermes/Nous | NousResearch | `[TOOL_CALL]{"name":...,"arguments":...}[TOOL_END]` | 纯文本标记，模型内嵌 |
| Qwen 原生 | Qwen 团队 | `✿FUNCTION✿: name ✿ARGS✿: args` | 花形特殊标记，双语模板 |
| vLLM 原生 | vLLM 社区 | OpenAI 兼容 `tool_calls` 格式 | 服务端解析，API 级别 |
| GLM | 智谱 | `<|tool_call_block|>` | XML 风格标记 |

Qwen-Agent 同时支持 Nous 和 Qwen 两种格式，通过 `fncall_prompt_type` 配置切换。

---

## 2. Qwen-Agent 的 FnCall 架构

### 2.1 类层次

```
BaseChatModel (llm/base.py:61)
  └── BaseFnCallModel (llm/function_calling.py:23)  ← FnCall 核心逻辑
        ├── OAI (llm/oai.py)                        ← OpenAI 兼容接口
        └── QwenDashScope (llm/qwen_dashscope.py)  ← 阿里 DashScope 接口
```

### 2.2 FnCall 预处理与后处理流程

```
原始消息 (Message 列表)
    │
    ▼
BaseFnCallModel._preprocess_messages()    ← function_calling.py:41
    │
    ├── 1. super()._preprocess_messages()  ← 多模态格式化 (BaseChatModel)
    │
    ├── 2. self.fncall_prompt.preprocess_fncall_messages()
    │       ├── 将 functions 列表注入 system prompt
    │       ├── 将 assistant 的 function_call 转为纯文本标记
    │       └── 将 FUNCTION 角色的结果转为 USER 消息
    │
    ▼
发送给模型 API
    │
    ▼
BaseFnCallModel._postprocess_messages()   ← function_calling.py:68
    │
    ├── 1. 从 LLM 输出文本中提取工具调用标记
    │       ├── 解析 name / arguments
    │       └── 重建 FunctionCall 对象
    │
    ├── 2. self.fncall_prompt.postprocess_fncall_messages()
    │
    ▼
结构化 Message 列表 (含 function_call 字段)
```

### 2.3 降级策略：_remove_fncall_messages

当 `functions` 为空或 `function_choice == 'none'` 时，`_remove_fncall_messages()`（function_calling.py:84）将历史中的 function_call 和 FUNCTION 消息转为 USER 消息的可读描述，避免模型尝试工具调用。

---

## 3. Nous FnCall 模板（默认，推荐）

### 3.1 System Prompt 模板

```
# Tools

You may call one or more functions to assist with the user query.
You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name": "get_weather", "description": "...", "parameters": {...}}}
{"type": "function", "function": {"name": "calculator", "description": "...", "parameters": {...}}}
</tools>

For each function call, return a json object with the function name and arguments:
[TOOL_CALL]{"name": "<function-name>", "arguments": "<args-json-object>"}[TOOL_END]
```

关键设计：
- 工具描述用 `<tools></tools>` XML 标签包裹
- 每个工具一行 JSON，格式与 OpenAI tool schema 兼容
- 调用标记：`[TOOL_CALL]` / `[TOOL_END]`（纯文本，不依赖特殊 tokenizer）

### 3.2 消息转换规则

| 原始消息 | 转换后 |
|----------|--------|
| assistant + function_call | `[TOOL_CALL]{"name": "x", "arguments": {...}}[TOOL_END]` |
| function (工具结果) | user + `<tool_result>\n结果\n</tool_result>` |

### 3.3 Code Interpreter 特殊模式

当 `SPECIAL_CODE_MODE=true`（环境变量）且工具名为 `code_interpreter` 时，使用 `FN_CALL_TEMPLATE_WITH_CI`，增加 `<code></code>` XML 标签包裹代码参数。

### 3.4 限制

- **不支持 `function_choice` 约束**：只能是 `'auto'`，不支持指定调用某个函数
- 纯文本标记，任何 chat 模型都能用，无需特殊 tokenizer

---

## 4. Qwen 原生 FnCall 模板

### 4.1 特殊标记

| 标记 | 含义 | 对应场景 |
|------|------|----------|
| `✿FUNCTION✿` | 函数名前缀 | LLM 要调用工具时输出 |
| `✿ARGS✿` | 参数前缀 | 函数参数开始 |
| `✿RESULT✿` | 结果前缀 | 工具返回结果 |
| `✿RETURN✿` | 结束标记 | LLM 不再调用工具，输出最终回答 |

这些标记作为 **stop words** 注入：`FN_STOP_WORDS = ['✿RESULT✿', '✿RETURN✿']`，模型遇到它们会停止生成，等待系统注入工具结果。

### 4.2 模板变体（4 种）

| 模板 | 语言 | 并行 | 用途 |
|------|------|------|------|
| `FN_CALL_TEMPLATE['zh']` | 中文 | 顺序 | 中文单次工具调用 |
| `FN_CALL_TEMPLATE['en']` | 英文 | 顺序 | 英文单次工具调用 |
| `FN_CALL_TEMPLATE['zh_parallel']` | 中文 | 并行 | 中文多次工具调用 |
| `FN_CALL_TEMPLATE['en_parallel']` | 英文 | 并行 | 英文多次工具调用 |

### 4.3 消息转换规则

| 原始消息 | 转换后 |
|----------|--------|
| assistant + function_call | `✿FUNCTION✿: name\n✿ARGS✿: arguments` |
| function (工具结果) | 续接到上一条 assistant：`✿RESULT✿: result\n✿RETURN✿: ` |

### 4.4 function_choice 支持

与 Nous 格式不同，Qwen 格式**支持约束调用指定函数**：

```python
# 当 function_choice == 'get_weather' 时，
# 预处理时在消息末尾追加：
# "✿FUNCTION✿: get_weather"
# 引导模型必须调用该函数
```

### 4.5 独特设计

- **双语模板**：完整的中英文 prompt 支持
- **花形 Unicode 标记**：`✿...✿` 在自然文本中几乎不会出现，避免误触发
- **stop word 控制**：`✿RESULT✿` 和 `✿RETURN✿` 作为停止词，让模型停下来等待系统注入结果

---

## 5. 两种模板对比与选择指南

| 维度 | Nous 格式 | Qwen 格式 |
|------|-----------|-----------|
| **默认** | ✅ 是 | 否 |
| **标记风格** | 文本标记 `[TOOL_CALL]`/`[TOOL_END]` | 花形特殊标记 `✿FUNCTION✿` 等 |
| **模型兼容性** | 任何 chat 模型 | 需要特殊 tokenizer 支持 |
| **双语** | 仅英文 | 中文 + 英文 |
| **并行调用** | ✅ | ✅ |
| **function_choice** | ❌ 仅 auto | ✅ 支持指定函数 |
| **Code Interpreter** | ✅ 专用模板 | 无特殊处理 |
| **推荐场景** | 开源模型、vLLM 部署 | Qwen 系列原生模型 |

**选择建议**：
- 使用 Qwen 系列模型 + DashScope → 两种都行，Qwen 格式在中文场景略优
- 使用开源模型 + vLLM/Ollama → **Nous 格式**（更通用）
- 需要 function_choice 约束 → **Qwen 格式**

---

## 6. 配置方式

```python
# 在 generate_cfg 中指定 FnCall 模板类型
llm_cfg = {
    'model': 'qwen-max',
    'model_type': 'qwen_dashscope',
    'generate_cfg': {
        'fncall_prompt_type': 'nous',  # 'nous' (默认) 或 'qwen'
    }
}

# 环境变量方式
import os
os.environ['SPECIAL_CODE_MODE'] = 'true'  # 启用 Code Interpreter 特殊模板
```

---

## 7. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `llm/function_calling.py` | 23 | `BaseFnCallModel` 类定义 |
| `llm/function_calling.py` | 41 | `_preprocess_messages` |
| `llm/function_calling.py` | 68 | `_postprocess_messages` |
| `llm/function_calling.py` | 84 | `_remove_fncall_messages` (降级) |
| `llm/function_calling.py` | 120 | `_chat_with_functions` |
| `llm/function_calling.py` | 148 | `simulate_response_completion_with_chat` |
| `llm/fncall_prompts/base_fncall_prompt.py` | 21 | `BaseFnCallPrompt` 接口 |
| `llm/fncall_prompts/nous_fncall_prompt.py` | 27 | `NousFnCallPrompt` 实现 |
| `llm/fncall_prompts/nous_fncall_prompt.py` | 254 | `FN_CALL_TEMPLATE` |
| `llm/fncall_prompts/nous_fncall_prompt.py` | 270 | `FN_CALL_TEMPLATE_WITH_CI` |
| `llm/fncall_prompts/qwen_fncall_prompt.py` | 24 | `QwenFnCallPrompt` 实现 |
| `llm/fncall_prompts/qwen_fncall_prompt.py` | 233-237 | 特殊标记常量 |
| `llm/fncall_prompts/qwen_fncall_prompt.py` | 327 | `FN_CALL_TEMPLATE` (4 变体) |
| `llm/fncall_prompts/qwen_fncall_prompt.py` | 335 | `get_function_description` |

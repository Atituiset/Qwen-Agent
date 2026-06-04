# Nous FnCall 模板（默认，推荐）

> 详见源码：`llm/fncall_prompts/nous_fncall_prompt.py`

## 1. System Prompt 模板

Nous 格式将工具描述注入到 system prompt 中：

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

## 2. 消息转换规则

| 原始消息 | 转换后 |
|----------|--------|
| assistant + function_call | `[TOOL_CALL]{"name": "x", "arguments": {...}}[TOOL_END]` |
| function (工具结果) | user + `<tool_result>\n结果\n</tool_result>` |

### 预处理细节

1. **工具描述注入**（preprocess_fncall_messages, line 29）：
   - 每个 function dict 包装为 `{"type": "function", "function": f}` 并序列化为 JSON
   - JSON 行用 `\n` 拼接，注入 `FN_CALL_TEMPLATE.format(tool_descs=...)`
   - 生成的 `tool_system` 追加到第一条 SYSTEM 消息，或创建新的 SYSTEM 消息

2. **Assistant 消息转换**：
   - `function_call` → 纯文本 `[TOOL_CALL]{"name": ..., "arguments": ...}\n[TOOL_END]`

3. **FUNCTION 消息转换**：
   - 角色 `function` → `user`，内容包裹 `<tool_result>\n...\n</tool_result>`

4. **相邻消息合并**：
   - 相邻的 assistant 消息合并
   - 相邻的 user 消息合并

### 后处理细节

从 LLM 输出文本中解析 `[TOOL_CALL]` / `[TOOL_END]` 标记：
- 提取 `name` 和 `arguments`
- 重建 `FunctionCall` 对象
- 支持 `thought_in_content`：如果模型先输出思考再输出工具调用，将思考部分放入 `reasoning_content`

## 3. Code Interpreter 特殊模式

当 `SPECIAL_CODE_MODE=true`（环境变量）且工具名为 `code_interpreter` 时：

- 使用 `FN_CALL_TEMPLATE_WITH_CI`（line 270）
- 增加 `<code></code>` XML 标签包裹代码参数
- 让 LLM 更清晰地区分代码与普通文本

```python
# 环境变量启用
import os
os.environ['SPECIAL_CODE_MODE'] = 'true'
```

## 4. 限制

- **不支持 `function_choice` 约束**：只能是 `'auto'`，不支持指定调用某个函数
- 纯文本标记，任何 chat 模型都能用，无需特殊 tokenizer

## 5. 辅助函数

| 函数 | 行号 | 功能 |
|------|------|------|
| `remove_incomplete_special_tokens` | 294 | 流式输出时清除不完整的尾部标记 |
| `extract_fn` | 300 | 从文本块中提取函数名和参数 |

## 6. 源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `nous_fncall_prompt.py` | 27 | `NousFnCallPrompt` 类 |
| `nous_fncall_prompt.py` | 29 | `preprocess_fncall_messages` |
| `nous_fncall_prompt.py` | 103 | `postprocess_fncall_messages` |
| `nous_fncall_prompt.py` | 254 | `FN_CALL_TEMPLATE` |
| `nous_fncall_prompt.py` | 268 | `SPECIAL_CODE_MODE` 环境变量 |
| `nous_fncall_prompt.py` | 270 | `FN_CALL_TEMPLATE_WITH_CI` |
| `nous_fncall_prompt.py` | 294 | `remove_incomplete_special_tokens` |
| `nous_fncall_prompt.py` | 300 | `extract_fn` |

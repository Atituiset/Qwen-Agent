# Qwen FnCall 模板

> 详见源码：`llm/fncall_prompts/qwen_fncall_prompt.py`

## 1. 特殊标记

| 标记 | 含义 | 对应场景 |
|------|------|----------|
| `✿FUNCTION✿` | 函数名前缀 | LLM 要调用工具时输出 |
| `✿ARGS✿` | 参数前缀 | 函数参数开始 |
| `✿RESULT✿` | 结果前缀 | 工具返回结果 |
| `✿RETURN✿` | 结束标记 | LLM 不再调用工具，输出最终回答 |

这些标记作为 **stop words** 注入：

```python
FN_STOP_WORDS = ['✿RESULT✿', '✿RETURN✿']  # qwen_fncall_prompt.py:237
```

模型遇到它们会停止生成，等待系统注入工具结果。

## 2. 模板变体（4 种）

| 模板 | 语言 | 并行 | 用途 |
|------|------|------|------|
| `FN_CALL_TEMPLATE['zh']` | 中文 | 顺序 | 中文单次工具调用 |
| `FN_CALL_TEMPLATE['en']` | 英文 | 顺序 | 英文单次工具调用 |
| `FN_CALL_TEMPLATE['zh_parallel']` | 中文 | 并行 | 中文多次工具调用 |
| `FN_CALL_TEMPLATE['en_parallel']` | 英文 | 并行 | 英文多次工具调用 |

### 模板结构

- **INFO 模板**（line 239-249）："You have access to the following tools: {tool_descs}"
- **FMT 模板**（line 251-273）：顺序格式：`FN_NAME` → `FN_ARGS` → `FN_RESULT` → `FN_EXIT`
- **FMT_PARA 模板**（line 275-325）：并行格式：N 个工具调用，N 个结果，然后一个 `FN_EXIT`

## 3. 消息转换规则

| 原始消息 | 转换后 |
|----------|--------|
| assistant + function_call | `✿FUNCTION✿: name\n✿ARGS✿: arguments` |
| function (工具结果) | 续接到上一条 assistant：`✿RESULT✿: result\n✿RETURN✿: ` |

### 预处理细节

1. **工具描述生成**（`get_function_description`, line 335）：
   - 每个函数渲染为 markdown 风格：`### {name_for_human}\n\n{name_for_model}: {description} Parameters: {parameters} {args_format}`
   - 描述用 `\n\n` 拼接，注入到根据 `lang` + `parallel_function_calls` 选择的模板中

2. **Assistant 消息转换**（line 44-56）：
   - `function_call` → `✿FUNCTION✿: name\n✿ARGS✿: arguments`

3. **FUNCTION 消息转换**（line 57-70）：
   - 续接到前一条 assistant 消息：`\n✿RESULT✿: result\n✿RETURN✿: `
   - 注意 `✿RETURN✿:` 后的空格——在继续生成时会被移除以避免形成单 token

4. **function_choice 约束**（line 96-108）：
   - 如果 `function_choice` 是具体函数名（非 `'auto'`/`'none'`），追加 `✿FUNCTION✿: {function_choice}` 引导模型

### 后处理细节

- 按 `✿FUNCTION✿:` 标记分割 assistant 文本，提取工具调用
- 每个调用块按 `✿ARGS✿:` 分割为 name 和 arguments
- `parallel_function_calls=False` 时只保留第一个函数调用
- 恢复 `✿RETURN✿: ` 中被移除的空格

## 4. function_choice 支持

与 Nous 格式不同，Qwen 格式**支持约束调用指定函数**：

```python
# 当 function_choice == 'get_weather' 时，
# 预处理时在消息末尾追加：
# "✿FUNCTION✿: get_weather"
# 引导模型必须调用该函数
```

## 5. 独特设计

- **双语模板**：完整的中英文 prompt 支持
- **花形 Unicode 标记**：`✿...✿` 在自然文本中几乎不会出现，避免误触发
- **stop word 控制**：`✿RESULT✿` 和 `✿RETURN✿` 作为停止词，让模型停下来等待系统注入结果

## 6. 辅助函数

| 函数 | 行号 | 功能 |
|------|------|------|
| `get_function_description` | 335 | 生成人类可读的工具描述字符串 |
| `remove_incomplete_special_tokens` | 369 | 流式输出时清除不完整的尾部花形标记 |
| `remove_trailing_comment_of_fn_args` | 389 | 修复模型在 JSON 参数后输出注释的问题 |

## 7. 源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `qwen_fncall_prompt.py` | 24 | `QwenFnCallPrompt` 类 |
| `qwen_fncall_prompt.py` | 233-237 | 特殊标记常量 |
| `qwen_fncall_prompt.py` | 239-249 | INFO 模板（中英文） |
| `qwen_fncall_prompt.py` | 251-273 | FMT 模板（顺序，中英文） |
| `qwen_fncall_prompt.py` | 275-325 | FMT_PARA 模板（并行，中英文） |
| `qwen_fncall_prompt.py` | 327 | `FN_CALL_TEMPLATE` (4 变体) |
| `qwen_fncall_prompt.py` | 335 | `get_function_description` |
| `qwen_fncall_prompt.py` | 369 | `remove_incomplete_special_tokens` |
| `qwen_fncall_prompt.py` | 389 | `remove_trailing_comment_of_fn_args` |

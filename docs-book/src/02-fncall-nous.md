# 02-A Nous FnCall 模板（默认）

> 配套 [02 Function Call](./02-fncall.md)。本子页只展开 Nous 模板的 pre/post 细节，深度决策与"为什么不那样"见扁平版 `docs/02_Function_Call.md`。
> 行号基于 `main` 分支；`nous_fncall_prompt.py` 指 `qwen_agent/llm/fncall_prompts/nous_fncall_prompt.py`。

## §1. System Prompt 的形状

`nous_fncall_prompt.py:264-285` 的 `FN_CALL_TEMPLATE`：
- 工具描述用 `<tools>...</tools>` XML 包裹，每工具一行 JSON，与 OpenAI tool schema 兼容
- 调用语法教学：`[TOOL_CALL]{"name": "...", "arguments": "..."}[/TOOL_CALL]`
- 拼到 system 时若有 system 字符串就 append `'\n\n' + tool_system`，否则 `messages.insert(0, Message(SYSTEM, ...))`（行 97-100）

注意：`</tools>` 是**纯文本**XML 标签无 parser 处理。tool description 字符串里含 `</tools>` 会提前闭合 prompt。

## §2. Pre 处理：function_call 字段 → 文本标记（行 41-101）

```
assistant含 function_call  →  content.append(ContentItem(text='[TOOL_CALL]\n{json}\n[/TOOL_CALL]'))
                               连续 assistant 会 extend 到上一条而非新起 message（行 71-75）
                            
FUNCTION role tool result →  USER role 包 <tool_result>\n{content}\n</tool_result>
                               拼到上一条 USER（行 82-84）或起新 USER message（行 85-86）

无 function_call 的 assistant →  保留原样
```

注意 `assistant` 的连续 append 逻辑：**多次 tool_call 的 assistant 历史被合并为一条 message**，与 OpenAI 的"每次 assistant 单独 ack"语义不同。

## §3. Post 处理：从 `[TOOL_CALL]` 提取 + thought 分离（行 103-196）

关键流程：
1. SYSTEM/USER 消息：直接 append 保留
2. assistant 的 reasoning_content 优先 push 成独立 message（行 126-127）
3. **若 content 含 `<tag>`：进 thought 模式**
   - 在出现 `[/TOOL_CALL]` 之前所有内容 append 为 thought ContentItem
   - 出现 `[/TOOL_CALL]` 后 split：前段入 thought，最末段进入下一步 tool parse
4. `item_text.find('[TOOL_CALL]')` < 0 → 当作普通文本回复
5. ≥ 0 → 提取 `{"name": ..., "arguments": ...}`，重建 `FunctionCall` 对象放 `message.function_call`

注意：**若模型一直不输出 `[/TOOL_CALL]`**，整段 assistant 输出被当 thought 保留，**没 tool_call 被 parse**——这等价于 LLM 决定不调工具。Qwen3 推理模型在长思考场景下出现这条路径。

## §4. SPECIAL_CODE_MODE：Code Interpreter 专属 path（行 52-68 + 188-198）

`SPECIAL_CODE_MODE=true`（env）且工具名含 `CODE_TOOL_PATTERN`（`code_interpreter`）时启用：
- pre 端：把 arguments.code 字段**提到外层 `<code>...</code>` XML 标签**，arguments 里 code 置空
- post 端：识别 `<code>...</code>`，把内容拼回 arguments.code

**为什么不**：Python 代码塞 JSON 字符串值要求模型转义 `"`、`\n`，模型容易弄错转义。code 提到外层让模型 free-form 写代码。

**坑**：pre 和 post 必须都开 `SPECIAL_CODE_MODE`，环境变量全程稳定；任何 tool 名含 `code_interpreter` 都会进这条路径，若你 arguments 无 code 字段会 KeyError。

## §5. 不支持的特性

`function_choice != 'auto'` 直接 `raise NotImplementedError`（行 38-39）。 caller 必须预先判断模板类型与 cfg 兼容性。

## §6. Nous 模板踩坑速查

- `<tools>...</tools>` 无 XML parser，tool description 含 `</tools>` 字符串会破坏 prompt 完整性
- 历史连续 assistant tool_call 被合并到一条 message，破坏 OpenAI 协议语义但兼容 OSS 模型
- Qwen3 推理模型在 Nous 路径下，模型不闭合 `[/TOOL_CALL]` → 整段输出被当 thought → **没 tool_call 被 parse**
- post 的 thought 识别仅在 assistant role 触发，user/system 含 `<tag>` 不会误判
- SPECIAL_CODE_MODE 必须前后端一致开关；环境变量跨进程稳定
- tool description 中文不 escaping，透传 `ensure_ascii=False`，vLLM 默认 ascnormal 无碍

完。

## §7. 关键源码索引

| 行号     | 内容                                |
|----------|-------------------------------------|
| 27       | `NousFnCallPrompt` 类定义           |
| 38-39    | `function_choice != 'auto'` raise  |
| 41-101   | preprocess_fncall_messages          |
| 52-68    | SPECIAL_CODE_MODE pre path          |
| 71-75    | 连续 assistant append 兼并           |
| 97-100   | system 拼接 tool_descs               |
| 103-196  | postprocess_fncall_messages         |
| 136-147  | thought 包裹分离（Qwen3 推理）       |
| 188-198  | SPECIAL_CODE_MODE post path         |
| 233-237  | `[TOOL_CALL]` 等常量                |
| 264-285  | FN_CALL_TEMPLATE 全文               |
| 294+     | FN_CALL_TEMPLATE_WITH_CI 全文       |
| 300      | `extract_fn` 兜底 parser            |

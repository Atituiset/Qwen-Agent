# 02-B Qwen FnCall 模板

> 配套 [02 Function Call](./02-fncall.md)。本子页只展开 Qwen 原生模板的 pre/post 细节与平行调用的实现，深度决策与"为什么不那样"见扁平版 `docs/02_Function_Call.md`。
> 行号基于 `main` 分支；`qwen_fncall_prompt.py` 指 `qwen_agent/llm/fncall_prompts/qwen_fncall_prompt.py`。

## §1. 特殊标记（行 233-237）

```python
FN_NAME = '✿FUNCTION✿'        # LLM 调工具时输出
FN_ARGS = '✿ARGS✿'             # 函数参数开始
FN_RESULT = '✿RESULT✿'         # 工具返回结果
FN_EXIT = '✿RETURN✿'           # 不再调用工具，输出最终回答
FN_STOP_WORDS = [FN_RESULT, FN_EXIT]
```

`✿` 是 Unicode 花形（U+273F），自然文本里几乎不会出现，避免误触发。要求 tokenizer 把这串当单 token 编码——Qwen 系列 tokenizer 原生支持，第三方或裸 BPE tokenizer 不可控。

`FN_STOP_WORDS` 注入 LLM stop 列表，模型遇到这两个标记会**暂停生成等待系统注入** tool result。

## §2. 4 变体模板（行 327-331）

```python
FN_CALL_TEMPLATE = {
    'zh':          FN_CALL_TEMPLATE_INFO_ZH + '\n\n' + FN_CALL_TEMPLATE_FMT_ZH,
    'en':          FN_CALL_TEMPLATE_INFO_EN + '\n\n' + FN_CALL_TEMPLATE_INFO_EN,
    'zh_parallel': FN_CALL_TEMPLATE_INFO_ZH + '\n\n' + FN_CALL_TEMPLATE_FMT_PARA_ZH,
    'en_parallel': FN_CALL_TEMPLATE_INFO_EN + '\n\n' + FN_CALL_TEMPLATE_FMT_PARA_EN,
}
```

preprocess 时按 `lang + ('_parallel' if parallel_function_calls else '')` 选 key（行 75）。双语 + 并行是 Qwen 模板的独有配置。

`_parallel` 模板教模型一次性输出多个 `FN_NAME: T1 / FN_ARGS: A1 / FN_NAME: T2 / FN_ARGS: A2 / ... / FN_RESULT: R1 / FN_RESULT: R2 / ...` 这种**链式序列**。`FnCallAgent` 收到后会把所有 function_call 并发执行。

## §3. Pre 处理：function_call → 文本（行 29-110）

```
assistant含 function_call  →  ::FUNCTION:: name
                               ::ARGS:: arguments
                               拼到上一条 assistant 末尾（行 78-87）

FUNCTION role tool result →  续接到上一条 assistant：
                              ::RESULT:: result
                              ::RETURN::       ← 让模型从这里续写最终回复
                              （行 89-100）

function_choice != 'auto'/'none' → 在 last user message 后追加
                                     ::FUNCTION:: <指定函数名>   （行 95-107）
```

注意：Qwen 模板里 tool result 是**续接到上一条 assistant**（不是新起 USER message），这与 Nous 完全不同——输出序列更接近模型在 training corpus 里见过的形式。

`function_choice` 强引导在 prompt 层是把前缀拼到最末消息，**靠 stop word 兜底**：模型若输出了 `✿FUNCTION✿: X` 但想反悔不调，需要先 emit `✿RESULT✿` 或 `✿RETURN✿`——但若模型想"反悔"通常不 emit 这俩，会卡循环。

## §4. Post 处理：解析 ✿FUNCTION✿ 等标记

关键步骤：
1. SYSTEM/USER 消息 append 保留
2. assistant content 含 `FN_NAME` → 提取 name 与 arguments
3. function_choice != 'auto'/'none' → 在 assistant 输出前 prepend `FN_NAME: function_choice\n`（行 119-125）
4. 移除尾部 `FN_RESULT` / `FN_EXIT` 等不闭合的特殊 token（`remove_incomplete_special_tokens`，行 369）
5. 重建 `FunctionCall` 对象放到 `message.function_call`

`remove_trailing_comment_of_fn_args`（行 389）会**移除 arguments 中的尾部注释**——Qwen3 推理模型有时会在 arguments JSON 后写一段说明，post 抹掉这部分防 JSON parse 报错。

## §5. 与 Nous 完全不互通的差异

| 维度                  | 实现差异                                                    |
|----------------------|--------------------------------------------------------------|
| 标记                  | Unicode `✿FUNCTION✿` vs 文本 `[TOOL_CALL]` — 同一 string 互不识别              |
| tool result role      | Qwen 续接上一 assistant；Nous 包 user `<tool_result>` tag    |
| function_choice       | Qwen 行 todo，写入 last user message；Nous raise NotImplementedError |
| thought 处理          | Qwen 靠 stop word 自动停；Nous 显式分离 `` tag  |
| SPECIAL_CODE_MODE     | Qwen 无 path；Nous 有专门 code_interpreter code 提外层 path |
| 模板变体              | Qwen 4 变体跨 lang/parallel；Nous 仅 2 变体（含 WITH_CI）       |

两模板**共用同一 BaseFnCallModel 实例**——LLM cfg 里指定 fncall_prompt_type 后，所有调用都走那一套 pre/post。不能在 history 中混合用两种模板的输出。

## §6. Qwen 模板踩坑速查

- `✿` Unicode 在不支持该 token 的 tokenizer 上会分解为多 token，模型可能根本识别不出标记
- `function_choice` 只强引导 name 不强引导 arguments；模型输出 `✿FUNCTION✿: get_weather` 后照样可能错参数
- 用 Qwen 模板时**不支持**Code Interpreter code 提外层 path —— 代码全塞 JSON arguments 转义
- 模板 prompt 中教的"图片需要渲染为 ![](url)" 是中文场景特别强引导，模型偶发把 tool result 当图像 url
- `remove_incomplete_special_tokens` 移除半截 `✿` 字符，**若模型生成的 tool name 包含 ✿** 会破坏 prompt
- `parallel_function_calls=False` 但模型仍并行输出 → postprocess 仍按 parallel 路径处理（无强约束），caller 要警觉
- Qwen3-Coder + vLLM 0.6+ 原生支持 OpenAI tool_calls 字段时可以**完全跳过 Qwen 模板**走 OAI 直连——Qwen 文本模板是兼容层，不是性能路径

## §7. 关键源码索引

| 行号     | 内容                                       |
|----------|--------------------------------------------|
| 24       | `QwenFnCallPrompt` 类定义                  |
| 75       | `FN_CALL_TEMPLATE[lang + ('_parallel'...)]` 选变体 |
| 95-107   | function_choice 强引导：last user message 追加 `✿FUNCTION✿: <name>` |
| 119-125  | function_choice postprocess：prepend 到 assistant 输出 |
| 157      | FN_STOP_WORDS 注入 stop                    |
| 233-237  | 特殊标记常量                               |
| 327-331  | 4 变体 dict                                |
| 335      | `get_function_description` 单工具描述生成  |
| 369      | `remove_incomplete_special_tokens`         |
| 389      | `remove_trailing_comment_of_fn_args`       |

完。

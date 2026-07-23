# 02 Function Call：让 LLM 学会使用工具

> 本文是 Qwen-Agent 系列拆解的**第 2 章**。目标是讲清"为什么 Qwen-Agent 同时保留两种 FnCall 模板"，而不是再抄一次模板原文。
> 每节按 **决策点 / 代码证据 / 为什么不那样 / 踩坑清单** 四件套展开。所有行号基于 `main` 分支。`fncall_prompts/` 指 `qwen_agent/llm/fncall_prompts/`。

读完应能回答的 7 个判断题：

1. Nous 和 Qwen 两种模板，哪一个不依赖 tokenizer 特殊标记？（§1）
2. vLLM 部署 Qwen2.5-Instruct 走哪一条？（§1）
3. Qwen3 推理模型在 Nous 路径下，`<tag>thought</tag>` 怎么被分离？（§3）
4. Code Interpreter 的代码为什么不放在 JSON arguments 里？（§4）
5. `function_choice='get_weather'` 哪个模板支持？怎么实现强引导？（§5）
6. 历史里的 `function_call` 字段会被怎么转回 LLM 可见文本？（§6）
7. 多函数并行调用时，模板 prompt 怎么教的？（§7）

---

## §1. 为什么 FnCall 范式从 Prompt Hack 转向 API 标准（历史不抄，只讲为什么必须转）

**决策点**：Qwen-Agent 同时保留 **Nous 与 Qwen 两种 FnCall 模板**。二者均非 OpenAI 的 `function_call` 字段协议——都是**纯文本 prompt + 后处理 parse** 路径。这是 Qwen-Agent 在生态约束下的设计选择。

**为什么不那样（不抄 OpenAI 字段协议）**：OpenAI 字段协议需要服务端（vLLM、SGLang、自部署 OAI-compat server 等）实现 tool_call 字段的解析与 tokenization。早期开源模型未做这件，纯文本 prompt 让任何 chat 模型都能跑 Function Call。Qwen-Agent 选择保留文本路径，代价是要维护两套**互不兼容的 prompt 模板与后处理逻辑**。

模板位置与对应类：

| 模板       | 类文件                              | 类名                | 默认 |
|------------|-------------------------------------|---------------------|------|
| Nous       | `fncall_prompts/nous_fncall_prompt.py:27` | `NousFnCallPrompt`  | ✅    |
| Qwen 原生  | `fncall_prompts/qwen_fncall_prompt.py:24` | `QwenFnCallPrompt`  | 否    |

切换通过 `generate_cfg={'fncall_prompt_type': 'nous'|'qwen'}`。

**踩坑清单**：
- 这两种模板**输出的语义不同**——Qwen 模板教模型输出 `✿FUNCTION✿: name ✿ARGS✿: args`，Nous 模板教模型输出 `[TOOL_CALL]{json}[/TOOL_END]`。**preprocess 与 postprocess 必须用同一套**，否则历史消息被一种模板 pre、被另一种 post 会乱。代码层面 `BaseFnCallModel` 在 `__init__` 时按 cfg 选定 fncall_prompt 类型，运行期不切换，所以一次 Agent 实例固定模板。
- vLLM 部署 Qwen3 系列时，vLLM 0.6+ 原生支持 OpenAI 兼容 `tool_calls` 字段——这时其实可以直接用 OAI 字段路径（`OAI` 子类，03 章详）。Qwen-Agent 的 Nous/Qwen 文本路径是给**不支持原生 FnCall 的模型**的兼容层，不是唯一路径。
- `class QwenFnCallPrompt(BaseFnCallPrompt)` 与 `NousFnCallPrompt` 都继承自 `fncall_prompts/base_fncall_prompt.py:21` 的 `BaseFnCallPrompt`，但 **`BaseFnCallPrompt.preprocess_fncall_messages` 直接 `raise NotImplementedError`**（行 34-35）—— 是接口式抽象，没有默认实现。这意味着自定义模板必须从头写 preprocess + postprocess，没有可复用骨架。

---

## §2. Nous 模板：纯文本 `[TOOL_CALL]...[/TOOL_END]` 不依赖 tokenizer

**决策点**：Nous 格式将 functions 描述塞 system prompt，用 `<tools>.../tools>` 包裹；assistant 调用工具时输出纯文本 `[TOOL_CALL]{json}[/TOOL_END]`，tool 结果以 `<tool_result>...</tool_result>` 形式注入 user 消息。**整套无 Unicode 特殊标记**，无须 tokenizer 支持。

**代码证据**：
```python
# nous_fncall_prompt.py:97-100  system 拼接
if messages and messages[0].role == SYSTEM:
    messages[0].content.append(ContentItem(text='\n\n' + tool_system))   # 拼到现有 system 后
else:
    messages = [Message(role=SYSTEM, content=[ContentItem(text=tool_system)])] + messages

# nous_fncall_prompt.py:264-285 FN_CALL_TEMPLATE 原文
FN_CALL_TEMPLATE = """# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tool_descs}
</tools>

For each function call, return a json object with the function name and arguments:
[TOOL_CALL]{"name": "<function-name>", "arguments": "<args-json-object>"}[/TOOL_END]
"""
```

**为什么不那样（不教模型输出 JSON Schema OpenAI 风格）**：OpenAI 格式需要模型把 tool_call 写在 message 的 `function_call` 字段——这是 API 层面而不是生成层面。对纯文本推理，文本教学中**最稳的格式**是大括号包裹附件（如 `[TOOL_CALL]...[/TOOL_END]`），既有天然边界，又能在普通文本流中被字符串 parse 锁定。 Nous 模板选择纯文本标记是**模型兼容性的最大化**。

**踩坑清单**：
- `<tools>` XML 包裹只是文本提示，**没经过 XML parser**——若你再写一个 tool 它的 description 里含 `</tools>` 字符串，会**破坏 system prompt 的 XML 整体性**（LLM 可能误读提早闭合）。tool description 在生成时无 escaping 处理，是 prompt 安全隐患。
- `tool_descs` 是 `\n'.join(json.dumps(f, ensure_ascii=False))` —— JSON 中文透传。**vLLM/Ollama 后端**默认对中文 ascnormal，无问题；但若模型 chat template 是英文训练样本主导，中文 description 模型理解略低。
- 模板**只教一次**如何调用（`For each function call, return a json object ...`），没教"何时停止"。模型在多次工具调用后如何终止由 `FnCallAgent` 的 `_detect_tool` 判断：当 LLM 输出不含 `[TOOL_CALL]` 时 return。若模型偶发把这段 markdown 字符串当作文本输出而不调工具，`_detect_tool` 正确返回 False，但 caller 看不到任何工具执行结果。

---

## §3. Nous 路径如何分离 Qwen3 推理模型的 `<tag>thought</tag>`

**决策点**：postprocess 阶段先看 assistant content 是否含 `<tag>thought</tag>`（推理模型的思维链包裹），含时**在到达 `` 之前全部当 thought 保留**，只有 ``**之后**的文本进 FnCall 解析。

**代码证据**（`nous_fncall_prompt.py:136-147`）：
```python
# Do not parse [TOOL_CALL] in thought!!!
if '<tag>' in item_text:
    thought_in_content = True
if thought_in_content:
    if '[/TOOL_CALL]' not in item_text:
        new_content.append(ContentItem(text=item_text))
        continue
    _item_text = item_text.split('[/TOOL_CALL]')
    new_content.append(ContentItem(text='[/TOOL_CALL]'.join(_item_text[:-1]) + '[/TOOL_CALL]'))
    item_text = _item_text[-1]

i = item_text.find('[TOOL_CALL]')
# If no function call:
if i < 0:
    show_text = item_text
    ...
```

**为什么不那样**：若 postprocess 不识别 thought 包裹，把整段含 `[TOOL_CALL]` 的 thought 也 parse 一次，会把模型在思维链里"假想调用 X"的片段当成真 tool_call——`<tag>` 分隔让 thought 块纯文本保留为 content，只有 thought 结束后的 assistant 主回复才解析 FnCall。

**踩坑清单**：
- `'<tag>' in item_text` 是**字符串包含**检查，若普通 user message 或知识库里含字面 `<tag>` 会被误识——但这条只在 assistant role 的 postprocess 触发，user/system 都直接 append，所以 user 输入含 `<tag>` 不会引发误判。
- thought 与 [TOOL_CALL] 之间用 `split('[/TOOL_CALL]')` 切割。**最末段才进 tool parse**。若 thought 内部含 `[TOOL_CALL]` 但无 [/TOOL_END closure、之后又有真实 tool call，split 会把真实部分也卷入前段——但实际是 `join(_item_text[:-1]) + [/TOOL_CALL]`，相当于把倒数第二段及之前 join 进 thought，把最后段做 parse。这是**保守正确**：只有 thought 闭合后才接 tool_call。
- Qwen3 推理模型的 `<tag>thought</tag>` 标签**不会**作为 stop words 注入，所以模型可以无限续写 thought。若 thought 一直不闭合 [/TOOL_CALL]，`thought_in_content=True` 状态共存且 content append；只有出现 [/TOOL_CALL] 才进 split 路径——这等价于"无闭合就一直是 thought"，**模型的整个回复都被当 thought 保留，没有 tool_call 被 parse**。FnCallAgent 会判定无 tool，把整段返给 user。
- 这是 Qwen-Agent 对**推理模型原生思维链**的深度集成。Qwen2.5-Instruct 没 thought，这条代码无害地走 if False 路径；Qwen3 / QwQ 系列触发实际分支。

---

## §4. SPECIAL_CODE_MODE：Code Interpreter 的代码不放在 JSON arguments 里

**决策点**：当 `SPECIAL_CODE_MODE=true`（环境变量）且 tool 名匹配 `CODE_TOOL_PATTERN`（`code_interpreter`），Nous 模板用 `FN_CALL_TEMPLATE_WITH_CI`；该模板**把 code 字段从 JSON arguments 里提出来**，单独放 `<code>...</code>` XML 标签。postprocess 再把它拼回去。

**代码证据**：
```python
# nous_fncall_prompt.py:52-68
fn_call = msg.function_call
if fn_call:
    if (not SPECIAL_CODE_MODE) or (CODE_TOOL_PATTERN not in fn_call.name):
        arguments = fn_call.arguments
        try:
            arguments = json5.loads(arguments)
        except Exception:
            logger.warning('Invalid json tool-calling arguments')
        fc = {'name': fn_call.name, 'arguments': arguments}
        fc = json.dumps(fc, ensure_ascii=False)
        fc = f'[TOOL_CALL]\n{fc}\n[/TOOL_CALL]'
    else:
        # Code Interpreter 特殊路径
        para = json5.loads(fn_call.arguments)
        code = para['code']
        para['code'] = ''                                    # ★ code 字段置空
        fc = {'name': fn_call.name, 'arguments': para}
        fc = json.dumps(fc, ensure_ascii=False)
        fc = f'[TOOL_CALL]\n{fc}\n<code>\n{code}\n</code>\n[/TOOL_CALL]'
```

**为什么不那样**：把 Python 代码塞 JSON 字符串值会逼着模型把所有 `"`、`\n` 转义成 `\"`、`\\n`。模型在 tokenization 时不可控地弄错转义（漏转义、双层转义），常见现象是 JSON parse 出来的 code 与模型想写的 code 不一致。**把 code 拉到 JSON 外层**就让模型 free-form 写代码，转义 JSON 字符串的负担没了。

**踩坑清单**：
- `CODE_TOOL_PATTERN` 是 substring 匹配（行 53: `CODE_TOOL_PATTERN not in fn_call.name`）——任何 tool 名包含 `code_interpreter` 都命中。若你的自定义 tool 名叫 `my_code_interpreter_v2`，也会进这条路径，**意外把 arguments.code 提到外层**——若你 arguments 没 code 字段则会 KeyError。
- postprocess 端（`nous_fncall_prompt.py:188-198`）只在 `SPECIAL_CODE_MODE and '<code>' in one_tool_call_txt[0] and '</code>' in one_tool_call_txt[0]` 才走 code 提取路径；否则用 fallback try `json5.loads(...)`。**pre 与 post 必须都开 SPECIAL_CODE_MODE**，否则 pre 把 code 提到外层但 post 没识别 code 标签 → 把整段 `<code>...</code>` 当作 arguments JSON 的尾部噪声，parse 失败。环境变量要在 process 全程稳定。
- `FN_CALL_TEMPLATE_WITH_CI` 额外教学模型"代码放 `<code>` 标签里"——而原 `FN_CALL_TEMPLATE` 没这条教学，模型可能不知道有 code 标签这回事。pre 端会拼到 system，但这意味着同一个 code_interpreter tool 在 `SPECIAL_CODE_MODE` 开关时**system prompt 大变**，是 behavior drift 源。
- Qwen 模板**没**这种 code interpreter 专门处理——只有 Nous 有。若你用 Qwen3-Coder + code_interpreter 但议用 Qwen prompt 模板，代码会被压回 JSON arguments 转义地狱。

---

## §5. Qwen 模板的 `function_choice` 强引导：把 `✿FUNCTION✿: <name>` 拼到消息末尾

**决策点**：Qwen 模板**支持**`function_choice` 任意字符串（非 `auto`/`none`），通过在最后一条消息末尾追加 `✿FUNCTION✿: <指定函数名>` 引导模型必须调该函数。Nous 模板遇到非 auto 直接 `raise NotImplementedError`。

**代码证据**：
```python
# qwen_fncall_prompt.py:95-107（preprocess）
# Add the function_choice prefix:
if function_choice not in ('auto', 'none'):
    assert isinstance(function_choice, str)
    last_msg = messages[-1]
    last_msg.content.append(ContentItem(text=f'{FN_NAME}: {function_choice}'))  # ✿FUNCTION✿: get_weather

# qwen_fncall_prompt.py:119-125（postprocess）
# Prepend a prefix for function_choice:
if function_choice not in ('auto', 'none'):
    assert isinstance(function_choice, str)
    for rsp in messages:
        if rsp.role == ASSISTANT:
            output = rsp.content ...
            output = f'{FN_NAME}: {function_choice}\n' + output      # 让模型输出也以该前缀开头

# nous_fncall_prompt.py:38-39
if function_choice != 'auto':
    raise NotImplementedError
```

**为什么不那样**：另一实现是改 system prompt 把 function_choice 写进去——但 system prompt 改动**影响整段历史消息的模型理解**。Qwen 模板选择在**最末消息**追加前缀，是"贴着 query 走"的最小污染策略。

**踩坑清单**：
- Qwen 的 function_choice 是**软引导**——它把前缀贴上去让模型从这里续写，但若回复模型有 thought 模式可能写完 thought 反悔不调指定函数。强约束靠 stop word `✿RESULT✿` / `✿RETURN✿` 触发——但模型若一直不输出这些标记，函数调用循环不会结束。
- Nous 的硬拒绝 `raise NotImplementedError` —— caller 传 `function_choice='get_weather'` 给 Nous 模板会直接 raise，而非 silent degrade。**call site 必须预先判断**模板类型与 cfg.function_choice 兼容性，否则运行期 raise。
- Qwen 模板 4 变体（`zh` / `en` / `zh_parallel` / `en_parallel`）由 `parallel_function_calls` 与 `lang` 组合选择，行 `tool_desc_template = FN_CALL_TEMPLATE[lang + ('_parallel' if parallel_function_calls else '')]`。`parallel_function_calls=False` 但模型还是偶尔并行输出，**模板语言已教过但模型行为不能强制**。
- 用 `function_choice` 时 caller 期望的是"必须调用 X"，但即使模型输出了 `✿FUNCTION✿: X`，**参数不对**时还是失败。这是 function_choice 在 prompt 层的根本局限——它强引导了 name 不强引导 arguments。

---

## §6. 历史中的 `function_call` 字段如何转回文本让 LLM 看见（与 FnCallAgent 联动）

**决策点**：preprocess 把历史里 assistant 消息的 `function_call` 字段重新转成 `[TOOL_CALL]...[/TOOL_CALL]` 文本（Nous）或 `✿FUNCTION✿: name ✿ARGS✿: args` 文本（Qwen），把 `FUNCTION` role 的 tool result 转成 `<tool_result>...</tool_result>`（Nous）或拼到上一条 assistant 尾部（Qwen）。**HF/OSS 模型拒绝 FUNCTION role 的消息**，所以必须降为 USER 输入。

**为什么不那样**：另一选择是保留 OpenAI 协议式的 `function_call` 字段和 `role=FUNCTION` 输入——但开源 chat 模型的 training corpus 几乎没见过这格式，会出现 role 不识别报错。文本化还原是兼容性最高的选择。

**踩坑清单**：
- 多轮多 tool 调用历史里，连续的 assistant/function/assistant/function 在转回时被合并：`if messages and messages[-1].role == ASSISTANT: messages[-1].content.extend(content)`（`nous_fncall_prompt.py:71-75`）。**助手连续多 tool call 被合到一条 assistant message**。这与 OpenAI 的"每次 assistant 单独一个 function_call"语义不同，但兼容 model 上下文的多 assistant 看起来更连续。
- 重放历史时若是 multi-agent 场景（一个 Agent 输出被另一个 Agent 收到作为 user 输入），中间这位"辅助 Agent" 的 function_call 输出也会被 preprocess 文本化。下游 Agent 看到的是 `[TOOL_CALL]...` 文本埋在 user message 里，**当成普通文本**——会发生把上一 Agent 的工具调用当 user 指令被执行的幻觉。多 Agent 编排时要警觉。
- tool result 在历史里是 ContentItem（图像、文件等），文本化时被包在 `<tool_result>...</tool_result>`。**多模态 ContentItem 不会被还原** —— `<tool_result>` 只接文本。若上次 Tool 返回了图像，下次历史中图像会被作为 ContentItem.image 保留，但没文本说明来自哪个 tool。caller 端如何描述上次图像的来源是 caller 的责任。

---

## §7. FnCall 预处理 / 后处理流程（一图）

```
原始 messages
    │
    ▼
BaseChatModel._preprocess_messages   (multimodal 格式归一)
    │
    ▼
BaseFnCallModel._preprocess_messages (function_calling.py:41)
    ├── super()._preprocess_messages(...)
    └── self.fncall_prompt.preprocess_fncall_messages(...)
           ├── functions 列表注入 system prompt（与 lang/parallel/function_choice 协调）
           ├── assistant.function_call 字段 → 文本标记
           └── FUNCTION role tool result → USER role 包 tag

发送到 LLM API（chat completion with tools-as-text）

    │
    ▼
BaseFnCallModel._postprocess_messages (function_calling.py:68)
    └── self.fncall_prompt.postprocess_fncall_messages(...)
           ├── 处理 thought 包裹（Qwen3 推理路径）
           ├── 从文本标记 [TOOL_CALL] / ✿FUNCTION✿ 提取 name/args
           └── 重建 FunctionCall 对象放 message.function_call

结构化 Message 列表（含 function_call 字段）返给 FnCallAgent
```

**`_remove_fncall_messages` 降级**（`function_calling.py:84`）：当 `functions=[]` 或 `function_choice='none'`，把历史里 function_call 字段和 FUNCTION 消息转成"调用过 X 工具，返回 Y 结果"的可读 USER 描述，**避免模型尝试工具调用**。这条降级路径常被忽略但实际是 _preprocess 的另一分支。

---

## §8. 改这块代码前的检查清单

- [ ] 自定义模板类必须实现 `preprocess_fncall_messages` 与 `postprocess_fncall_messages`，无默认实现可继承
- [ ] 切换模板类型只能在 LLM `__init__` 配置期决定，不能跨调用切换
- [ ] tool description 字符串里**不能含** `<tools>` 或 `</tools>`——会被 prompt 误闭合
- [ ] 启用 `SPECIAL_CODE_MODE` 必须前后端（pre + post）一致，环境变量在整个 Python 进程稳定
- [ ] 用 Qwen `function_choice` 时只强引导 name 不强引导 arguments，参数错仍是失败
- [ ] 用 Nous 模板时不要传 `function_choice != 'auto'` —— 直接 NotImplementedError
- [ ] 多 Agent 编排：上游 Agent 的 function_call 输出会被下游 Agent 当作普通文本，是幻觉源
- [ ] History 里多模态 tool result：postprocess 只还原文本占位，图像/文件作为 ContentItem 保留但缺"来自哪个 tool"的标注
- [ ] Qwen3 推理模型在 Nous 路径下：模型不闭合 `[/TOOL_CALL]` → 整段输出被当 thought，**没 tool_call 被 parse**——这等价 LLM 决定不调工具

---

## §9. 与其他章的衔接

- preprocess/postprocess 的具体细节归属 07 章（BaseChatModel）
- BaseFnCallModel 在 `llm/function_calling.py:23`，与 BaseChatModel 关系见 01 章 §4
- FnCallAgent 的 while 循环骨架见 01 章 §8
- Code Interpreter 这个 Tool 本身的实现（Docker 沙箱等）见 03 章

# 02 Function Call：让 LLM 学会使用工具

> 这一系列讲"为什么这样设计、不那样会怎样、改了会踩什么坑"。本章只回答 FnCall 模板的选型与运行机制。完整深度版见仓库 `docs/02_Function_Call.md`。

读完应能回答的 7 个判断题：
1. Nous 与 Qwen 两种模板，哪个不依赖 tokenizer 特殊标记？→ Nous
2. vLLM 部署 Qwen2.5-Instruct 走哪条？→ Nous
3. Qwen3 推理模型的 thought 怎么在 Nous 路径分离？→ `<tag>thought</tag>` 包裹，postprocess 切割
4. Code Interpreter 的代码为什么不直接放 JSON arguments？→ 转义地狱让 JSON parse 易碎
5. `function_choice='get_weather'` 哪个模板支持？→ Qwen，靠把 `✿FUNCTION✿: get_weather` 追加到末消息末尾强引导
6. 历史 `function_call` 字段怎么转回 LLM 可见文本？→ preprocess 文本化注入 system/user
7. 多函数并行调用时 prompt 怎么教？→ Qwen 有 `zh_parallel` / `en_parallel` 4 变体

---

## 1. 关键定位

Qwen-Agent 同时保留两种 FnCall 模板，二者均非 OpenAI `function_call` 字段协议——**都是纯文本 prompt + 后处理 parse 路径**。这是为兼容不支持原生 FnCall 的开源模型而作的取舍，代价是维护两套互不兼容的 prompt 与 postprocess 逻辑。

| 模板   | 类文件                                          | 默认 |
|--------|-------------------------------------------------|------|
| Nous   | `fncall_prompts/nous_fncall_prompt.py:27`        | ✅    |
| Qwen   | `fncall_prompts/qwen_fncall_prompt.py:24`        | 否    |
| 共同基类 | `fncall_prompts/base_fncall_prompt.py:21`      | —    |

切换：`generate_cfg={'fncall_prompt_type': 'nous'|'qwen'}`。

**`BaseFnCallPrompt.preprocess_fncall_messages` 行 34-35 直接 `raise NotImplementedError`**——接口式抽象，自定义模板必须从头写 pre、post，无 skeleton 可继承。

---

## 2. Nous 模板：纯文本 `[TOOL_CALL]...[/TOOL_END]`

System prompt 用 `<tools>...</tools>` XML 包裹 functions 列表（`FNCALL_TEMPLATE`，行 264-285）。assistant 调工具时输出 `[TOOL_CALL]\n{json}\n[/TOOL_END]` 纯文本。

特点：
- 任何 chat 模型都能跑（无 Unicode 特殊标记）
- 与 OpenAI tool schema 兼容（每工具一行 JSON）
- **不支持 `function_choice` 约束**：行 38-39 遇到非 auto 直接 raise `NotImplementedError`

详细决策与坑见扁平版 §2~§6。这里强调两点：
1. `<tools>` XML 只是文本提示，**无 XML parser**——tool description 含字面 `</tools>` 会提前闭合 prompt，是 prompt 安全隐患。
2. Nous 的 postprocess 处理 `<tag>thought</tag>` 思维链包裹（行 136-147），是 Qwen-Agent 对 Qwen3/QwQ 推理模型的深度集成点（05 章不提，02 章独有）。

---

## 3. Qwen 模板：花形 Unicode 标记 + 4 变体

**特殊标记**（`qwen_fncall_prompt.py:233-237`）：
```
FN_NAME = '✿FUNCTION✿'
FN_ARGS = '✿ARGS✿'
FN_RESULT = '✿RESULT✿'
FN_EXIT = '✿RETURN✿'
FN_STOP_WORDS = [FN_RESULT, FN_EXIT]
```

4 变体 (`qwen_fncall_prompt.py:327-331`)：`zh` / `en` / `zh_parallel` / `en_parallel`。

**双语 + 并行调用支持**，是 Qwen 原生 prompt 设计的最大差异点。Unicode 标记要求 tokenizer 支持；Qwen 系列 tokenizer 对此原生兼容。

**`function_choice` 实现**（行 95-107）：preprocess 时在 last user message 末尾追加 `✿FUNCTION✿: <指定函数名>`——这是**软引导**——贴着 query 走最小污染，但模型仍可能反悔不调；强约束靠 stop word `✿RESULT✿` 触发暂停等结果。

---

## 4. 两种模板对比（速查）

| 维度              | Nous                                    | Qwen                                          |
|-------------------|-----------------------------------------|-----------------------------------------------|
| 默认               | ✅ 是                                   | 否                                            |
| 标记风格          | 文本 `[TOOL_CALL]` / `[/TOOL_END]`     | Unicode 花形 `✿FUNCTION✿` 等                   |
| 模型兼容          | 任何 chat 模型                          | 需 tokenizer 支持 `✿` 标记                     |
| 双语              | 仅英文                                  | 中文 + 英文                                    |
| 并行调用          | ✅                                       | ✅（额外模板 _parallel 变体）                  |
| `function_choice` | ❌ 仅 auto，传其他 raise                | ✅ 支持任意字符串                              |
| Code Interpreter  | ✅ `SPECIAL_CODE_MODE` 专 path          | 无 special 处理                                |
| 推理模型 thought  | ✅ `<tag>thought</tag>` 包裹识别         | 无（Qwen 模板内 thought 直接在 `✿RESULT✿` 前文）|
| 推荐场景          | 开源模型 + vLLM/Ollama 部署              | Qwen 系列原生模型 + DashScope                 |

选择建议：用 Qwen 系列模型 + DashScope → 二者均可（Qwen 模板在中文略优）；用开源模型 + vLLM/Ollama → **Nous**；需要 function_choice 约束 → **Qwen**。

---

## 5. FnCall 预处理 / 后处理流程

```
原始 messages
  ▼
BaseChatModel._preprocess_messages   (multimodal 格式归一)
  ▼
BaseFnCallModel._preprocess_messages (function_calling.py:41)
  ├─ super()._preprocess_messages(...)
  └─ self.fncall_prompt.preprocess_fncall_messages(
        把 functions 列表注入 system prompt
        把 assistant.function_call 字段 → 文本标记
        把 FUNCTION role tool result → USER role 包 tag
        function_choice 强引导（仅 Qwen 模板）
     )

[发送到 LLM API]

  ▼
BaseFnCallModel._postprocess_messages (function_calling.py:68)
  └─ self.fncall_prompt.postprocess_fncall_messages(
        处理 thought 包裹（Qwen3 推理路径，Nous 独有此处理器）
        从文本标记提取 name/args → 重建 FunctionCall 对象
     )
  ▼
结构化 Message 列表返给 FnCallAgent
```

**降级路径 `_remove_fncall_messages`**（`function_calling.py:84`）：`functions=[]` 或 `function_choice='none'` 时，把历史里 function_call 字段转为"调用过 X，返回 Y"的可读 USER 描述。常被忽略但是 _preprocess 的另一分支。

详细见扁平版 §6。

---

## 6. 改这块代码前的检查清单

- [ ] 自定义模板必须实现 pre + post，**无 skeleton 可继承**
- [ ] `fncall_prompt_type` 只能 `__init__` 时定，不能跨调用切换
- [ ] tool description 字符串**不能含 `<tools>` 或 `</tools>`**——会被 prompt 误闭合
- [ ] 启用 `SPECIAL_CODE_MODE` 必须 pre + post 同步，环境变量全程稳定
- [ ] 用 Qwen `function_choice` 只强引导 name 不强引导 arguments，参数错仍失败
- [ ] 用 Nous 时传 `function_choice != 'auto'` → 直接 raise NotImplementedError
- [ ] 多 Agent 编排：上游 Agent 的 function_call 输出会被下游当**普通文本**接收，是幻觉源
- [ ] History 里多模态 tool result：postprocess 只还原文本占位，图像 ContentItem 保留但缺"来自哪个 tool"的标注
- [ ] Qwen3 推理模型在 Nous 路径下，模型不闭合 `[/TOOL_END]` → 整段输出被当 thought，**没 tool_call 被 parse**

---

## 7. 配置方式

```python
llm_cfg = {
    'model': 'qwen-max',
    'model_type': 'qwen_dashscope',
    'generate_cfg': {
        'fncall_prompt_type': 'nous',  # 'nous'（默认）或 'qwen'
    }
}

# 环境变量
import os
os.environ['SPECIAL_CODE_MODE'] = 'true'  # 启用 Code Interpreter 专门模板（仅 Nous）
```

---

## 8. 下一步深入学习

- Nous 详细 prompt 模板与 pre/post 处理：[02-fncall-nous.md](./02-fncall-nous.md)
- Qwen 花形标记模板与 function_choice 详解：[02-fncall-qwen.md](./02-fncall-qwen.md)
- 完整"决策点+为什么不那样+踩坑清单"深度版：仓库 `docs/02_Function_Call.md`
- FnCallAgent 的循环骨架与终止条件：[01 Overview](./01-overview.md) §8

完。

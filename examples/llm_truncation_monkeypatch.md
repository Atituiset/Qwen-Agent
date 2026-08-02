# Qwen-Agent 输入截断：全链路、封装结构与裁剪算法深度分析

> 适用场景：pip install 安装的 qwen_agent + MCP 工具（如 mcp-language-server）在千万行级 C/C++ 代码仓上做代码审计。
> 核心源码：`qwen_agent/llm/base.py` `_truncate_input_messages_roughly`（日志行 `ALL tokens: ..., Available tokens: ...` 在 780 行附近）。

## 1. 结论

`ALL tokens > Available tokens` 是这段代码的**截断触发器**，在你这种"工具结果动辄几万 token"的场景下必然频繁出现，不是故障信号。但它有三个计数盲区（低估真实输入）、截断策略有两个方向性问题（先删最老一轮、不保任务描述），并且整套逻辑被封装成一个"巨函数 + 嵌套闭包"，导致任何局部修复都必须整函数替换。本文先把全链路、封装层次、裁剪算法完整拆开，再给出 monkey patch 方案。

## 2. 全链路总览：从 Agent 到 API 请求

### 2.1 调用栈

```
FnCallAgent._run (qwen_agent/agents/fncall_agent.py)
 └─ _call_llm(messages, functions=[工具schema列表])
     └─ llm.chat(messages, functions, stream, extra_generate_cfg)      # base.py:118
         ├─ messages = copy.deepcopy(messages)，统一转成 Message
         ├─ 开头不是 SYSTEM 则前置 DEFAULT_SYSTEM_MESSAGE
         ├─ generate_cfg = merge_generate_cfgs(...)                    # 合并 llm 配置
         ├─ max_input_tokens = generate_cfg.pop('max_input_tokens',
         │                       DEFAULT_MAX_INPUT_TOKENS)             # 默认 58000
         ├─ messages = _truncate_input_messages_roughly(               # ★ 截断在这一层
         │       messages=messages, max_tokens=max_input_tokens)
         ├─ messages = self._preprocess_messages(messages, functions)  # ★ 截断之后才渲染
         │       └─ BaseFnCallModel: fncall_prompt.preprocess_fncall_messages
         │            → function_call 渲染成 <tool_call>...</tool_call>
         │            → FUNCTION 结果包上 <tool_response>...</tool_response>
         │            → 工具 schema 拼进 system 消息（FN_CALL_TEMPLATE）
         ├─ 非多模态 → format_as_text_message(msg)
         └─ _call_model_service()
             ├─ fncall_mode → _chat_with_functions → _chat_stream/_chat_no_stream
             │     → openai / dashscope 客户端（messages + tools 发出去）
             ├─ 末尾 assistant → _continue_assistant_response
             └─ 否则 → _chat
```

### 2.2 两个关键位置

1. **截断发生在 `chat()` 内部、请求发出之前**，而且拿到的还是**结构化 Message**（`content` 是 str/list，`function_call` 是独立字段）。模型真实看到的 `<tool_call>`、`<tool_response>`、工具 schema、聊天模板，全部是截断之后在 `_preprocess_messages` 里才渲染出来的——这就是"截断器计数 ≠ 真实请求"的结构性原因。
2. 截断是**同步**发生在 `chat()` 调用内的，返回值无论是不是生成器，截断结果在返回前已经定型。这决定了补丁用 contextvar 透传 `functions` 是安全的（见 6.3）。

## 3. 封装结构：怎么封装的

### 3.1 消息与数据结构的封装

截断器内部只依赖四种数据结构：

| 结构 | 形态 | 作用 |
|---|---|---|
| `Message` | `role / content / function_call / name / extra` | 单条消息（system/user/assistant/function） |
| `indexed_messages_per_user` | `dict: user_idx -> [(msg_idx, msg), ...]` | 按"最近一个 USER"把消息分成 turn |
| `message_tokens` | `dict: msg_idx -> int` | 记账本：每条消息的估算 token 数，裁剪后回写 |
| `messages_per_step` | `list[list[(msg_idx, msg)]]` | turn 内部再按角色连续段切成 step |

step 切分规则：**连续同角色合并成一个 step，角色切换才开新 step；FUNCTION 永远挂到前一个 step 上**。典型的一轮多工具调用：

```
step0: [user]
step1: [assistant, function]   ← 第一次工具调用 + 结果
step2: [assistant, function]   ← 第二次工具调用 + 结果（最后一步）
```

### 3.2 截断器的内部封装：一个大函数 + 三个嵌套闭包

`_truncate_input_messages_roughly` 内部把三个助手全部封装成**嵌套闭包**（base.py:627-760）：

```python
def _truncate_input_messages_roughly(messages, max_tokens):
    def _count_tokens(msg): ...      # 单条消息计数
    def _truncate_message(msg, max_tokens, keep_both_sides): ...  # 单条正文截断
    def _truncate_turn(indexed_messages1, message_tokens1, exceedance, is_last_turn): ...  # 整轮治理

    # 预算 → 分组 → 主循环调用 _truncate_turn ...
```

含义：

- 三个助手只在大函数体内可见，**模块级没有任何独立函数可以单独 patch**；
- 唯一的替换粒度就是整个 `_truncate_input_messages_roughly`；
- 好在 `chat()` 是通过模块全局名调用它的（`messages = _truncate_input_messages_roughly(...)`），所以补丁只要 `llm_base._truncate_input_messages_roughly = 新函数` 即生效，不需要动 `chat` 本体（详见 6.2）。

### 3.3 chat 的多态封装与请求组装

`chat()` 本身是模板方法，真正的请求由子类实现：`_chat_stream` / `_chat_no_stream` / `_chat_with_functions` / `_continue_assistant_response`。截断对所有路径都生效，因为它挂在 `chat()` 公共段。

工具调用的组装有两条路径，**都发生在截断之后**：

| 路径 | 组装方式 | 工具 schema 的去向 |
|---|---|---|
| `raw_api=True`（如 Qwen3-Max） | `raw_chat`：`generate_cfg['tools'] = functions` | 作为 `tools` 独立字段发给 dashscope |
| `raw_api=False`（默认） | `NousFnCallPrompt.preprocess_fncall_messages` | 渲染成文本：`# Tools ... <tools>{tool_descs}</tools>` 拼进 system 消息；`<tool_call>`/`<tool_response>` 包进对话文本 |

### 3.4 截断器看到的 vs 模型真实看到的

```
截断器看到的（计数对象）          模型真实看到的（计费对象）
---------------------------------  ----------------------------------------
user/assistant/function 正文      正文 + <tool_call> 包裹 + <tool_response> 包裹
assistant 的 function_call        正文（截断器常常漏数）
FUNCTION 的 name                  工具结果里的名字
system 消息                       系统提示 + # Tools + <tools>{全部工具schema}</tools>
                                  + 聊天模板/角色标记
```

这就是 7.1 节"计数低估"的结构性原因：**截断器只数了结构化消息的一部分，而模型的输入里还有渲染开销和全部工具 schema**。

## 4. 裁剪算法（上游怎么裁剪的）

### 4.1 预算与主循环

```python
available_token = max_tokens
for msg in messages:
    if msg.role == SYSTEM:
        available_token = max_tokens - _count_tokens(msg)   # 预算只扣 system
    else:
        message_tokens[msg_idx] = _count_tokens(msg)        # 其余消息进 all_tokens

all_tokens = sum(message_tokens.values())
logger.info(f'ALL tokens: {all_tokens}, Available tokens: {available_token}')
if all_tokens <= available_token:
    return messages
if available_token <= 0:
    raise ModelServiceError(code='400', ...)

exceedance = all_tokens - available_token      # 还需要省出的 token 数
for it, (user_msg_idx, indexed_messages) in enumerate(indexed_messages_per_user.items()):
    if exceedance <= 0:
        new_messages += indexed_messages       # 额度够了，整轮原样保留
        continue
    is_last_turn = (it == len(...) - 1)
    new_turn, exceedance = _truncate_turn(...)
    if new_turn:
        new_messages += new_turn
return new_messages
```

要点：

- **`ALL tokens` 不含 system**，`Available tokens` = `max_tokens - system`；
- `exceedance` 是递减的全局额度，从**最老的 turn 开始**逐轮治理，一轮省不够就删整轮、把缺口留给下一轮；
- 最后一轮标记 `is_last_turn`，其最新工具结果受到额外保护。

### 4.2 `_truncate_turn`：单轮治理

签名与契约：`(保留消息, 剩余额度)`。

**第 0 步：整轮丢弃**

```python
if 本轮总token <= exceedance:
    return [], exceedance - 本轮总token      # 整轮删掉，额度还回去
```

这是"任务轮先死"的根源：主循环从最老的一轮开始，老轮最先撞上这个判断。

**特殊分支：单消息 turn**

```python
if len(本轮) == 1:
    assert is_last_turn                     # 隐含假设：单消息 turn 只在末尾
    return [_truncate_message(该消息, 该消息token - exceedance)], 0
```

对应"用户一次性粘贴超大内容"：直接截这一条就收工。

**step 切分后，按四步牺牲顺序裁剪：**

| 步骤 | 裁剪对象 | 规则 |
|---|---|---|
| step1 | 非最后一步的 FUNCTION 结果 | 单个结果 > exceedance → 截断到 `token - exceedance` 并清零；否则 content 变 `'omit'`、记账归 0、继续 |
| step2 | 中间的 step | 保留第一 step 和最后一步；中间 step 从最老的开始，够大就删（`keep_idx` 指向其后一步），不够就整组删并递减 |
| step3 | 最后一步的 FUNCTION 结果 | 与 step1 相同规则（`is_last_turn` 保护的最新结果在这里才动） |
| step4 | 第一 step + 最后一步的 user/assistant 正文 | 与 step1 相同规则 |

每步之间都有 `if exceedance <= 0: return ...`，一旦省够立刻收工。**末尾无条件 `return [...], 0`，不再复检**——这是"截断后依然超限"的直接原因。

### 4.3 实测走查（原版代码 + 仓库 tokenizer）

**E1：单 turn、两次工具调用、预算 200**

```
输入: user(15) assistant(11) function(1700) assistant(11) function(1100)   合计 2837
日志: ALL tokens: 2837, Available tokens: 200
输出: user(15) assistant(11) function(174)                                 合计 200
```

过程：step1 把 `function(1700)` 省略 → step2 删掉中间 step（assistant+function）→ step3 把最后一步的 function 截到 174。输出按代码口径刚好 200，但 assistant 正文漏数，真实请求其实超过 200。

**E4：两轮对话、预算 300，第一轮是任务**

```
输入: [任务轮] user(3) assistant(11) function(1380) assistant(11) function(1700)
      [继续轮] user(1) assistant(11) function(2180)                        非system合计 5297
日志: ALL tokens: 5297, Available tokens: 299
输出: system(1) user(1) assistant(11) function(287)                        非system 299
```

过程：任务轮整轮 3105 ≤ exceedance 4998 → **整轮被删，任务描述丢失**；继续轮是最后一轮，step1 跳过其 FUNCTION（`is_last_turn` 保护），最终在 step3 把 function 从 2180 截到 287。

## 5. 裁剪算法（补丁怎么裁剪的）

补丁不再沿用 `_truncate_turn`，主函数内部改为**四阶段 + 复检**：

```
预算 = max_tokens - system - functions/schema - SAFETY_MARGIN_TOKENS(512)
  ↓
Phase A  整轮丢弃中间轮（保留第一轮=任务、最后一轮=当前工作），从最老的中间轮开始
  ↓
Phase B  压缩 FUNCTION 结果：先旧轮、最后一轮最后；大结果截断、小结果 'omit'
  ↓
Phase C  压缩 user/assistant 正文（跳过任务描述）
  ↓
Phase D  最后手段：压缩任务描述本身
  ↓
组装 + 复检：非 system 真实计数 ≤ available
  ├─ 不满足 → 兜底循环：优先压缩/省略最大的非任务消息（最多 len+2 次）
  └─ 仍不满足 → 抛 ModelServiceError(400)，绝不把超限请求发给模型
```

### 5.1 与上游的裁剪差异

| | 上游 | 补丁 |
|---|---|---|
| 预算 | `max - system` | `max - system - functions/schema - 余量` |
| 丢弃顺序 | 从最老一轮开始 | 从中间轮开始，首尾轮保底 |
| 任务描述 | 可能整轮被删（E4） | 最后才动，兜底循环也优先非任务消息 |
| 工具结果 | 只截断进入上下文的旧结果 | 源头限流（`Agent._call_tool` 包装，默认 24k） |
| 截断单条 | `Message(role, content)` 重建，丢字段 | `copy.copy(msg)` 只换 content，保留 function_call/name/extra |
| 复检 | 无，末尾无条件返回 0 | 真实重计 + 兜底循环 + 超限抛 400 |
| 截断误差 | `truncate` 后重计可能差 ±2 token | `_truncate_to_total` 迭代收敛（最多 3 次） |

## 6. 补丁模块的封装（怎么封装的）

补丁是**一个独立模块 + 三个挂接点**，不依赖上游内部嵌套函数：

### 6.1 模块结构（`examples/qwen_agent_truncation_monkeypatch.py`）

```
模块顶层
├─ 配置常量  TOOL_RESULT_MAX_TOKENS / SAFETY_MARGIN_TOKENS（支持环境变量覆盖）
├─ contextvar _cv_functions                     # 透传 functions 的通道
├─ 工具函数   _count_functions / _count_tokens / _truncate_message / _truncate_to_total
├─ 主函数     _truncate_input_messages_roughly  # 四阶段 + 复检（见第 5 节）
├─ 工具结果限流 _cap_tool_result
├─ 包装器     _wrap_chat / _wrap_call_tool
└─ 入口       apply_patch()                     # import 时自动调用，幂等
```

### 6.2 三个挂接点

| # | 挂接点 | 方式 | 为什么是这里 |
|---|---|---|---|
| 1 | `qwen_agent.llm.base._truncate_input_messages_roughly` | **整函数替换** | 内部全是嵌套闭包（3.2），只能替换整函数；`chat()` 通过模块全局名调用，替换即生效 |
| 2 | `BaseChatModel.chat` | **包装**（`_wrap_chat`） | 把 `functions` 写进 contextvar，让截断函数能算 tool schema 开销；不修改 chat 内部逻辑 |
| 3 | `Agent._call_tool` | **包装**（`_wrap_call_tool`） | 工具结果源头限流；`FnCallAgent._call_tool` 走 `super()._call_tool`，打一个点覆盖所有 agent |

`apply_patch()` 幂等：模块级 `_applied` 标志，重复调用直接返回。

### 6.3 contextvar 透传原理

```python
def _wrap_chat(chat_fn):
    def _chat(self, *args, **kwargs):
        tok = _cv_functions.set(kwargs.get('functions'))
        try:
            return chat_fn(self, *args, **kwargs)
        finally:
            _cv_functions.reset(tok)

def _truncate_input_messages_roughly(messages, max_tokens):
    functions = _cv_functions.get()          # 读取同一线程栈里 chat 的 functions
    overhead = _count_functions(functions) + SAFETY_MARGIN_TOKENS
    ...
```

为什么安全：截断在 `chat()` 调用内**同步**完成（2.2），所以 `set` 之后、`reset` 之前一定被消费，与 stream 返回生成器的延迟迭代无关。

### 6.4 可调参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `QWEN_PATCH_TOOL_RESULT_MAX_TOKENS` | 24000 | 单次工具结果上限，超了保留头尾 + 省略标记 |
| `QWEN_PATCH_SAFETY_MARGIN_TOKENS` | 512 | 为聊天模板/角色标记预留的余量 |
| `QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS` | 58000 | 截断预算（框架自带，建议按模型上下文调大） |

## 7. 缺陷清单

### 7.1 计数低估（all_tokens 小于真实请求）

1. **assistant 带 function_call 时只数 function_call，不数正文**（base.py:627-630）。工具调用轮通常"正文 + function_call"都有。
2. **functions/tool schema 从未计入**。raw_api 路径作为 `tools` 字段发送；nous 路径渲染进 system 消息——两条路径都发生在截断之后。
3. **聊天模板/角色标记未计入**，`<tool_call>`/`<tool_response>` 包裹开销未计入。

后果：日志里显示还没超，真实请求可能已经超限；截断后依然可能被模型 API 返回 400。

### 7.2 截断策略方向性问题

4. **从最老的 turn 开始整轮丢弃**（4.3 E4：任务轮被整体删除）。
5. **超长代码"保头保尾、中间全丢"**：`keep_both_sides=True` 把大工具结果截成 `头部 + "..." + 尾部`。
6. **截断后不复检**：`_truncate_turn` 末尾无条件返回 0，且 step3/step4 截断后不回写 `message_tokens1`。
7. **`_truncate_message` 丢字段**：用 `Message(role, content)` 重建，带 function_call 的 assistant 消息被截断会丢掉 function_call，破坏工具调用配对；多模态 content 无 text item 时返回 `None`。

### 7.3 源头问题

8. **工具结果无上限进入上下文**：`FnCallAgent._run` 把 `_call_tool` 原文直接作为 FUNCTION 消息追加（fncall_agent.py:98-100），后续每轮全量重发。mcp-language-server 除 `search` 有 12KB 上限外，`definition/references/callers/callees/treesitter_query/astgrep` 都可能返回数万 token。

## 8. 生产使用方式

### 8.1 直接 import 即生效

补丁模块底部自带 `apply_patch()`，`import` 时会自动完成三处挂接（替换截断函数、包装
`chat` 透传 functions、包装 `Agent._call_tool` 限流工具结果），并且幂等——重复 import
不会重复打补丁。

```python
# 入口文件（app.py / server.py 等）最顶部，任何 agent 创建之前
import qwen_agent_truncation_monkeypatch

from qwen_agent.agents import FnCallAgent
agent = FnCallAgent(...)
```

前提是 patch 文件在入口脚本同目录、或所在目录已在 `sys.path` 里。如果不在：

```python
import sys, os
sys.path.insert(0, "/部署路径/containing/patch")
import qwen_agent_truncation_monkeypatch
```

### 8.2 每个进程都要 import 一次

如果生产是 gunicorn/多进程 worker、或通过 subprocess 起的独立服务，**每个 worker 进程
都必须执行一次这个 import**。只在主进程 import 不会自动覆盖子进程。

### 8.3 确认生效

import 后应看到日志：

```
qwen_agent_truncation_monkeypatch applied: tool_result_max_tokens=24000, safety_margin=512
```

也可以用一行代码验证：

```python
import qwen_agent.llm.base as b
assert b._truncate_input_messages_roughly.__module__ == 'qwen_agent_truncation_monkeypatch'
```

### 8.4 环境变量

```bash
# 可选：环境变量覆盖默认值
export QWEN_PATCH_TOOL_RESULT_MAX_TOKENS=30000
export QWEN_PATCH_SAFETY_MARGIN_TOKENS=512
```

| 变量 | 默认 | 作用 |
|---|---|---|
| `QWEN_PATCH_TOOL_RESULT_MAX_TOKENS` | 24000 | 单次工具结果上限，超了保留头尾 + 省略标记 |
| `QWEN_PATCH_SAFETY_MARGIN_TOKENS` | 512 | 为聊天模板/角色标记预留的余量 |
| `QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS` | 58000 | 截断预算（框架自带），建议按模型上下文调大 |

### 8.5 显式控制（可选）

```python
import qwen_agent_truncation_monkeypatch as patch
patch.apply_patch(enable_tool_result_cap=True)
```

如果业务代码不想 import 时自动打补丁，可以去掉文件末尾的 `apply_patch()` 行，改为在合适时机显式调用。

### 8.6 注意事项

- 建议同步调大 `QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS`（或 `generate_cfg.max_input_tokens`），按模型真实上下文留余量，例如 128k 上下文的模型设 100k-110k。
- 如果生产 agent 是自定义类且**重写了 `_call_tool` 但不调用 `super()._call_tool`**，"工具结果源头限流"这一项不会生效（截断与计数修正不受影响）；此时需要在自定义 `_call_tool` 里自行调用 `qwen_agent_truncation_monkeypatch._cap_tool_result(result, TOOL_RESULT_MAX_TOKENS)`。

## 9. 验证结果（用仓库 tokenizer 实测）

对补丁模块做完整加载测试（桩掉 qwen_agent 依赖后 import，走真实 `_truncate_input_messages_roughly`，`max_tokens=58000`、`SAFETY_MARGIN_TOKENS=512`）：10 组场景 + 30 组随机压力对话，全部满足"非 system 消息 ≤ available"且首轮任务保留。

### 场景 A：两轮，每轮 24k~30k 工具结果（预算 58k）

| | 补丁后实测 |
|---|---|
| ALL / Available | 67618 / 57487（含 system+余量扣减） |
| 输出 | 两轮全保留，任务文本完整 |
| 最终计数 | 非 system 57487，预算内 |

### 场景 B：四轮，每轮约 24.8k 工具结果（预算 58k）

| | 补丁后实测 |
|---|---|
| ALL / Available | 99236 / 57487 |
| 输出轮次 | 保留第 0 轮（任务）和第 3 轮，**丢弃中间第 1 轮**，第 2 轮工具结果被压缩 |
| 任务描述 | 完整保留 `第0轮问题` |
| 最终计数 | 非 system 57487，预算内 |

### 其余验证项

| 项目 | 结果 |
|---|---|
| functions/schema 开销 | 2 个 MCP 工具 schema（约 1177 token）自动扣减，仍不超限 |
| assistant 计数 | content + function_call 都计入 |
| 单次工具结果限流 | 87,780 token 压到 24,003（含省略标记） |
| 截断保字段 | function_call 保留，不破坏工具调用配对 |
| 单条超大结果（~70k） | 截断到预算内，任务文本保留 |
| 随机压力（30 组，2-8 轮） | 全部预算内，首轮任务无一丢失 |

对照上游（4.3 E4），同样的多轮对话会把任务轮整体删掉，这是补丁要解决的核心问题。

## 10. 风险与回滚

- **版本兼容**：补丁只替换 `_truncate_input_messages_roughly` 并包装 `chat`/`_call_tool`，不依赖上游内部嵌套函数，qwen_agent 小版本升级一般不影响；升级后建议回归一次。
- **行为变化**：工具结果默认压到 24k（可配），会丢部分代码中间内容，但对"几万 token 塞进上下文"的场景收益远大于损失。
- **回滚**：去掉 import 即可，不修改 site-packages，无残留。
- **边界**：system + functions/schema 本身超过预算时补丁抛 400（与原逻辑一致，只是更早、更明确）。

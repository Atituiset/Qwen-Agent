# -*- coding: utf-8 -*-
"""Qwen-Agent 输入截断逻辑的生产环境 Monkey Patch。

针对 pip install 安装的 qwen_agent，在进程启动时 import 本模块即可自动生效，
不需要改动 site-packages 里的源码：

    import qwen_agent_truncation_monkeypatch   # 放在创建 agent / LLM 之前

本补丁解决四个问题（详见同目录 llm_truncation_monkeypatch.md）：

1. assistant 消息同时带 content 和 function_call 时，content 不计入 token；
2. functions/tool schema 每次请求都会带上，但从未计入预算；
3. 截断时从最老的 user turn 开始删，最初的审计任务描述最先被丢弃；
4. MCP/工具结果无上限地进入 FUNCTION 消息，导致历史快速膨胀。

可调参数（也可用环境变量覆盖）：

- QWEN_PATCH_TOOL_RESULT_MAX_TOKENS: 单次工具结果上限，默认 24000
- QWEN_PATCH_SAFETY_MARGIN_TOKENS:  为聊天模板/角色标记预留的余量，默认 512
"""
from __future__ import annotations

import contextvars
import copy
import functools
import json
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from qwen_agent.llm import base as llm_base
from qwen_agent.llm.base import BaseChatModel, ModelServiceError
from qwen_agent.llm.schema import ASSISTANT, FUNCTION, SYSTEM, USER
from qwen_agent.log import logger
from qwen_agent.utils.tokenization_qwen import tokenizer
from qwen_agent.utils.utils import extract_text_from_message

TOOL_RESULT_MAX_TOKENS: int = int(os.getenv('QWEN_PATCH_TOOL_RESULT_MAX_TOKENS', '24000'))
SAFETY_MARGIN_TOKENS: int = int(os.getenv('QWEN_PATCH_SAFETY_MARGIN_TOKENS', '512'))

# 用于把 chat(functions=...) 的 functions 透传给截断函数（chat 内部同步调用截断）
_cv_functions: contextvars.ContextVar[Optional[List[Dict]]] = contextvars.ContextVar(
    'qwen_agent_patch_functions', default=None)


def _count_functions(functions: Optional[List[Dict]]) -> int:
    """估算 tool schema 的 token 开销（每次请求都会随 functions 一起发送）。"""
    if not functions:
        return 0
    try:
        return tokenizer.count_tokens(json.dumps(functions, ensure_ascii=False, separators=(',', ':')))
    except Exception:
        return 0


def _count_tokens(msg: Any) -> int:
    """修正后的 token 计数：assistant 消息同时数 content 和 function_call。"""
    if msg.role == ASSISTANT:
        n = 0
        text = extract_text_from_message(msg, add_upload_info=True)
        if text:
            n += tokenizer.count_tokens(text)
        if msg.function_call:
            n += tokenizer.count_tokens(f'{msg.function_call}')
        return n
    n = tokenizer.count_tokens(extract_text_from_message(msg, add_upload_info=True))
    if msg.role == FUNCTION and msg.name:
        # FUNCTION 消息的 name 字段也会进入请求
        n += tokenizer.count_tokens(msg.name)
    return n


def _truncate_message(msg: Any, max_tokens: int, keep_both_sides: bool = False) -> Optional[Any]:
    """截断消息正文；保留 role/name/function_call/extra 等字段（原版会丢字段）。"""
    if isinstance(msg.content, str):
        content = tokenizer.truncate(msg.content, max_token=max_tokens, keep_both_sides=keep_both_sides)
    else:
        text = []
        for item in msg.content:
            if not item.text:
                return None
            text.append(item.text)
        text = '\n'.join(text)
        content = tokenizer.truncate(text, max_token=max_tokens, keep_both_sides=keep_both_sides)
    new_msg = copy.copy(msg)
    new_msg.content = content
    return new_msg


def _truncate_to_total(msg: Any, total_target: int) -> Optional[Any]:
    """把消息总 token（含 function_call）压到 total_target。"""
    total_target = max(0, int(total_target))
    content_target = total_target
    if msg.role == ASSISTANT and msg.function_call:
        fc_tokens = tokenizer.count_tokens(f'{msg.function_call}')
        content_target = max(0, total_target - fc_tokens)
    cur = _truncate_message(msg, content_target, keep_both_sides=True)
    if cur is None:
        return None
    # tokenizer 截断后重计可能略高于目标（±2 token），按超出量迭代收敛
    for _ in range(3):
        t = _count_tokens(cur)
        if t <= total_target:
            return cur
        content_target = max(0, content_target - (t - total_target))
        cur = _truncate_message(cur, content_target, keep_both_sides=True)
    return cur


def _truncate_input_messages_roughly(messages: List[Any], max_tokens: int) -> List[Any]:
    """重写 qwen_agent.llm.base._truncate_input_messages_roughly。

    与上游的差异：
    - 预算 = max_tokens - functions/schema 开销 - 安全余量；
    - token 计数包含 assistant 正文 + function_call、FUNCTION name；
    - 截断时保留第一轮（任务）与最后一轮（当前工作），优先整轮丢弃中间轮；
    - 工具结果先于对话正文被压缩，任务文本最后才被动；
    - 截断后按真实计数复检，仍超限则抛 400，而不是静默返回超限请求。
    """
    if len([m for m in messages if m.role == SYSTEM]) >= 2:
        raise ModelServiceError(
            code='400',
            message='The input messages must contain no more than one system message. '
            'And the system message, if exists, must be the first message.',
        )
    if not messages:
        return messages

    # ---- 计数并按 user turn 分组 ----
    message_tokens = defaultdict(int)
    indexed = defaultdict(list)  # user_msg_idx -> [(msg_idx, msg)...]
    last_user_idx = None
    for msg_idx, msg in enumerate(messages):
        if msg.role == SYSTEM:
            continue
        message_tokens[msg_idx] = _count_tokens(msg)
        if msg.role == USER:
            last_user_idx = msg_idx
        indexed[last_user_idx].append([msg_idx, msg])
    turns: List[List[Tuple[int, Any]]] = list(indexed.values())

    # ---- 预算：把请求里"看不见"的开销也扣掉 ----
    # 口径与上游一致：ALL tokens 不含 system，Available = max - system - overhead
    functions = _cv_functions.get()
    overhead = _count_functions(functions) + SAFETY_MARGIN_TOKENS
    system_tokens = sum(_count_tokens(m) for m in messages if m.role == SYSTEM)
    available_token = max_tokens - overhead - system_tokens
    all_tokens = sum(message_tokens.values())
    logger.info(
        f'ALL tokens: {all_tokens}, Available tokens: {available_token}, '
        f'Overhead(functions+safety+system): {overhead + system_tokens}',
    )
    if all_tokens <= available_token:
        return messages
    if available_token <= 0:
        raise ModelServiceError(
            code='400',
            message=f'The input system has exceed the maximum input context length '
            f'({max_tokens} tokens, functions+safety overhead {overhead} tokens)',
        )

    exceedance = all_tokens - available_token
    dropped = [False] * len(turns)

    # 第一轮里的第一条 user 消息 = 任务描述，最后才动它
    task_pos: Optional[Tuple[int, int]] = None
    if turns:
        for j, (idx, msg) in enumerate(turns[0]):
            if msg.role == USER:
                task_pos = (0, j)
                break

    # ---- Phase A: 整轮丢弃中间轮（保留第一轮和最后一轮），从最老的中间轮开始 ----
    for i in range(1, len(turns) - 1):
        if exceedance <= 0:
            break
        turn_tokens = sum(message_tokens[idx] for idx, _ in turns[i])
        if turn_tokens <= exceedance:
            dropped[i] = True
            exceedance -= turn_tokens

    def _iter_msg_positions(roles, last_turn_last=True):
        """按"旧轮在前、最后一轮最后"的顺序产出 (turn_i, msg_j, msg_idx, msg)。"""
        order = list(range(len(turns) - 1))
        if last_turn_last:
            order.append(len(turns) - 1)
        for i in order:
            if dropped[i]:
                continue
            for j, (idx, msg) in enumerate(turns[i]):
                if msg.role in roles:
                    yield i, j, idx, msg

    # ---- Phase B: 先压缩 FUNCTION 工具结果 ----
    for i, j, idx, msg in _iter_msg_positions(FUNCTION):
        if exceedance <= 0:
            break
        t = message_tokens[idx]
        if t > exceedance:  # 单个结果足够大：截断到刚好省出 exceedance
            new_msg = _truncate_to_total(msg, t - exceedance)
            if new_msg is None:
                msg.content = 'omit'
            else:
                turns[i][j][1] = new_msg
            exceedance = 0
        else:  # 结果较小：整体省略
            msg.content = 'omit'
            exceedance -= t

    # ---- Phase C: 再压缩 user/assistant 正文（任务描述跳过） ----
    for i, j, idx, msg in _iter_msg_positions((USER, ASSISTANT)):
        if exceedance <= 0:
            break
        if (i, j) == task_pos:
            continue
        t = message_tokens[idx]
        if t > exceedance:
            new_msg = _truncate_to_total(msg, t - exceedance)
            if new_msg is None:
                msg.content = 'omit'
            else:
                turns[i][j][1] = new_msg
            exceedance = 0
        else:
            msg.content = 'omit'
            exceedance -= t

    # ---- Phase D: 最后手段——压缩任务描述本身 ----
    if exceedance > 0 and task_pos is not None:
        i, j = task_pos
        idx, msg = turns[i][j]
        t = message_tokens[idx]
        if t > exceedance:
            new_msg = _truncate_to_total(msg, t - exceedance)
            if new_msg is not None:
                turns[i][j][1] = new_msg
            exceedance = 0
        else:
            msg.content = 'omit'
            exceedance -= t

    # ---- 组装并复检 ----
    result = [m for m in messages if m.role == SYSTEM]
    for i, turn in enumerate(turns):
        if dropped[i]:
            continue
        for idx, msg in turn:
            result.append(msg)
    # 记住任务消息对象，兜底压缩时优先动其他消息
    task_msg = None
    if task_pos is not None:
        task_msg = turns[task_pos[0]][task_pos[1]][1]

    # 复检口径与预算一致：只统计非 system 消息
    final_tokens = sum(_count_tokens(m) for m in result if m.role != SYSTEM)
    if final_tokens > available_token:
        # 兜底：逐轮压缩/省略最大的消息，直到满足预算。
        # tokenizer 截断重计存在少量误差，这里不依赖精确算术；优先压缩非任务消息。
        for _ in range(len(result) + 2):
            if final_tokens <= available_token:
                break
            deficit = final_tokens - available_token
            candidates = [(i, m, _count_tokens(m))
                          for i, m in enumerate(result)
                          if m.role != SYSTEM and m is not task_msg and _count_tokens(m) > 0]
            if not candidates:
                candidates = [(i, m, _count_tokens(m))
                              for i, m in enumerate(result)
                              if m.role != SYSTEM and _count_tokens(m) > 0]
            if not candidates:
                break
            i, m, t = max(candidates, key=lambda c: c[2])
            if t > deficit:
                new_msg = _truncate_to_total(m, t - deficit)
                if new_msg is not None:
                    result[i] = new_msg
                else:
                    result[i].content = 'omit'
            else:
                result[i].content = 'omit'
            final_tokens = sum(_count_tokens(x) for x in result if x.role != SYSTEM)

    if final_tokens > available_token:
        logger.warning(f'Truncation still exceeds budget: {final_tokens} > {available_token}')
        raise ModelServiceError(
            code='400',
            message=f'The input messages still exceed the maximum input context length '
            f'after truncation ({final_tokens} > {available_token} tokens)',
        )
    return result


def _cap_tool_result(text: str, max_tokens: int) -> str:
    """把单个工具结果压到 max_tokens（保留头尾，附带省略标记）。"""
    n = tokenizer.count_tokens(text)
    if n <= max_tokens:
        return text
    marker_fmt = '\n\n... [qwen_agent_patch truncated, %d tokens omitted]'
    marker_tokens = tokenizer.count_tokens(marker_fmt % 0)
    cut_target = max(0, max_tokens - marker_tokens)
    cut = tokenizer.truncate(text, max_token=cut_target, keep_both_sides=True)
    omitted = n - tokenizer.count_tokens(cut)
    return cut + (marker_fmt % omitted)


def _wrap_chat(chat_fn):
    """把 chat(functions=...) 暂存到 contextvar，供截断函数读取。"""

    @functools.wraps(chat_fn)
    def _chat(self, *args, **kwargs):
        functions = kwargs.get('functions')
        if functions is None and len(args) >= 2:
            functions = args[1]
        tok = _cv_functions.set(functions)
        try:
            return chat_fn(self, *args, **kwargs)
        finally:
            _cv_functions.reset(tok)

    return _chat


def _wrap_call_tool(fn):
    """给工具结果加单次 token 上限。"""

    @functools.wraps(fn)
    def _wrapped(self, *args, **kwargs):
        result = fn(self, *args, **kwargs)
        if isinstance(result, str):
            return _cap_tool_result(result, TOOL_RESULT_MAX_TOKENS)
        return result

    return _wrapped


_applied = False


def apply_patch(enable_tool_result_cap: bool = True) -> None:
    """应用全部补丁（幂等，重复调用不重复打补丁）。"""
    global _applied
    if _applied:
        logger.info('qwen_agent_truncation_monkeypatch already applied, skip.')
        return

    # 1) 替换截断函数（chat 内部通过模块全局名调用，替换即生效）
    llm_base._truncate_input_messages_roughly = _truncate_input_messages_roughly

    # 2) 包装 chat，把 functions 透传给截断函数
    BaseChatModel.chat = _wrap_chat(BaseChatModel.chat)

    # 3) 工具结果源头限流（覆盖 FnCallAgent 经由 super()._call_tool 的路径）
    if enable_tool_result_cap:
        from qwen_agent.agent import Agent as _Agent
        _Agent._call_tool = _wrap_call_tool(_Agent._call_tool)

    _applied = True
    logger.info(
        'qwen_agent_truncation_monkeypatch applied: '
        f'tool_result_max_tokens={TOOL_RESULT_MAX_TOKENS}, safety_margin={SAFETY_MARGIN_TOKENS}',
    )


# import 即生效
apply_patch()

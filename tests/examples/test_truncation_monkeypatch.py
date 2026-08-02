# -*- coding: utf-8 -*-
"""回归测试：examples/qwen_agent_truncation_monkeypatch.py。

针对 pip install 的 qwen_agent 的场景，本测试自包含、不依赖 numpy/openai/mcp
等完整依赖：qwen_agent 无法真实导入时自动注入最小桩（真实 tiktoken + pydantic
v2 Message 复刻），加载补丁后跑完整回归。

运行方式：

    pytest tests/examples/test_truncation_monkeypatch.py
    或（无需 pytest）
    python tests/examples/test_truncation_monkeypatch.py

注意：如果同进程已真实导入 qwen_agent（例如跑全量测试套件），本文件会跳过——
桩注入与真实验会互相污染，建议单独运行。
"""
import importlib
import importlib.util
import json
import random
import sys
import types
from pathlib import Path

try:
    import pytest
except ImportError:  # 允许无 pytest 环境直接 python 运行
    pytest = None

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / 'examples'
MAX_TOKENS = 58000

STATE = {}


def _install_stub_env():
    """qwen_agent 无法真实导入时，注册最小桩（真实 tokenizer + pydantic Message 复刻）。"""
    qa = types.ModuleType('qwen_agent')
    qa.__path__ = []

    log = types.ModuleType('qwen_agent.log')

    class _Logger:
        def info(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    log.logger = _Logger()

    utils_pkg = types.ModuleType('qwen_agent.utils')
    utils_pkg.__path__ = []
    utils = types.ModuleType('qwen_agent.utils.utils')

    def extract_text_from_message(msg, add_upload_info=True, lang='auto'):
        if isinstance(msg.content, str):
            return msg.content.strip()
        if isinstance(msg.content, list):
            return ''.join(getattr(i, 'text', '') or '' for i in msg.content).strip()
        return str(msg.content).strip()

    utils.extract_text_from_message = extract_text_from_message

    sys.modules.update({
        'qwen_agent': qa,
        'qwen_agent.log': log,
        'qwen_agent.utils': utils_pkg,
        'qwen_agent.utils.utils': utils,
    })

    # 真实 tokenizer（唯一硬依赖 tiktoken，仓库自带 qwen.tiktoken 词表）
    spec = importlib.util.spec_from_file_location(
        'qwen_agent.utils.tokenization_qwen',
        REPO_ROOT / 'qwen_agent' / 'utils' / 'tokenization_qwen.py',
    )
    tokmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tokmod)
    sys.modules['qwen_agent.utils.tokenization_qwen'] = tokmod

    schema = types.ModuleType('qwen_agent.llm.schema')
    from pydantic import BaseModel

    class Message(BaseModel):
        """与 qwen_agent.llm.schema.Message 同构的最小复刻（pydantic v2）。"""
        role: str
        content: str = ''
        reasoning_content: object = None
        name: object = None
        function_call: object = None
        extra: object = None

    schema.Message = Message
    schema.USER, schema.ASSISTANT, schema.FUNCTION, schema.SYSTEM = 'user', 'assistant', 'function', 'system'

    base = types.ModuleType('qwen_agent.llm.base')

    class ModelServiceError(Exception):
        def __init__(self, exception=None, code=None, message=None, extra=None):
            super().__init__(f'\nError code: {code}. Error message: {message}')
            self.code, self.message = code, message

    class BaseChatModel:
        """复刻真实 chat() 的关键路径：截断在 chat() 内部同步调用。"""

        def chat(self, messages, functions=None, stream=True, delta_stream=False, extra_generate_cfg=None):
            import qwen_agent.llm.base as b
            return b._truncate_input_messages_roughly(messages=messages, max_tokens=MAX_TOKENS)

    base.ModelServiceError = ModelServiceError
    base.BaseChatModel = BaseChatModel

    llm_pkg = types.ModuleType('qwen_agent.llm')
    llm_pkg.__path__ = []
    llm_pkg.base = base

    agent_mod = types.ModuleType('qwen_agent.agent')

    class Agent:
        def __init__(self):
            self.function_map = {}

        def _call_tool(self, tool_name, tool_args='{}', **kwargs):
            if tool_name not in self.function_map:
                return f'Tool {tool_name} does not exists.'
            return self.function_map[tool_name]

    agent_mod.Agent = Agent

    fncall_pkg = types.ModuleType('qwen_agent.agents')
    fncall_pkg.__path__ = []
    fncall_mod = types.ModuleType('qwen_agent.agents.fncall_agent')

    class FnCallAgent(Agent):
        def _call_tool(self, tool_name, tool_args='{}', **kwargs):
            return super()._call_tool(tool_name, tool_args, **kwargs)

    fncall_mod.FnCallAgent = FnCallAgent

    sys.modules.update({
        'qwen_agent.llm': llm_pkg,
        'qwen_agent.llm.schema': schema,
        'qwen_agent.llm.base': base,
        'qwen_agent.agent': agent_mod,
        'qwen_agent.agents': fncall_pkg,
        'qwen_agent.agents.fncall_agent': fncall_mod,
    })
    return schema, tokmod.tokenizer


def _load_patch():
    """加载补丁模块（幂等）。"""
    if 'patch' in STATE:
        return STATE
    if 'qwen_agent' in sys.modules:
        # 真实验已被其他测试加载，桩注入会污染会话
        if pytest is not None:
            pytest.skip('real qwen_agent already imported; run this file standalone', allow_module_level=True)
        raise RuntimeError('real qwen_agent already imported; run this file standalone')
    schema, tokenizer = _install_stub_env()
    spec = importlib.util.spec_from_file_location(
        'qwen_agent_truncation_monkeypatch',
        EXAMPLES_DIR / 'qwen_agent_truncation_monkeypatch.py',
    )
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)  # 模块底部 apply_patch()，import 即生效
    STATE.update(patch=patch, schema=schema, tokenizer=tokenizer)
    return STATE


_load_patch()

if pytest is not None:

    @pytest.fixture(scope='module', autouse=True)
    def _cleanup_stub_modules():
        """模块测试结束后移除桩，避免污染同会话里后续真实 qwen_agent 导入。"""
        yield
        for key in [k for k in sys.modules if k.startswith('qwen_agent')]:
            del sys.modules[key]


# --------------------------------------------------------------------------
# 测试工具
# --------------------------------------------------------------------------
def _patch():
    return STATE['patch']


def _chat(messages, functions=None):
    from qwen_agent.llm.base import BaseChatModel
    return BaseChatModel().chat(messages, functions=functions)


def _check_budget(out, max_tokens=MAX_TOKENS, functions=None):
    patch = _patch()
    ns = sum(patch._count_tokens(m) for m in out if m.role != 'system')
    sys_tok = sum(patch._count_tokens(m) for m in out if m.role == 'system')
    avail = max_tokens - patch._count_functions(functions) - patch.SAFETY_MARGIN_TOKENS - sys_tok
    total = ns + sys_tok
    assert ns <= avail, f'non-system {ns} > available {avail}'
    assert total <= max_tokens, f'total {total} > max_tokens {max_tokens}'
    return ns, avail, total


def _big(n=1500):
    return ' '.join(f'int f_{i}(int x){{return x+{i};}}' for i in range(n))


def _make_turns(n_turns, fn_size):
    msgs = [Message(role='system', content='s')]
    for t in range(n_turns):
        msgs += [
            Message(role='user', content=f'第{t}轮问题'),
            Message(
                role='assistant',
                content=f'第{t}轮思考',
                function_call={'name': 'read_file', 'arguments': json.dumps({'path': f'f{t}.c'})},
            ),
            Message(role='function', content=_big(fn_size), name='read_file'),
        ]
    return msgs


Message = STATE['schema'].Message


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
def test_small_conversation_untouched():
    small = [Message(role='system', content='s'), Message(role='user', content='hi'),
             Message(role='assistant', content='ok')]
    out = _chat(small)
    assert out is small


def test_two_turns_task_and_budget():
    out = _chat(_make_turns(2, 1800))
    ns, avail, _ = _check_budget(out)
    assert ns <= avail
    assert any(m.role == 'user' and m.content == '第0轮问题' for m in out)
    assert any(m.role == 'user' and m.content == '第1轮问题' for m in out)


def test_four_turns_keep_first_and_last():
    out = _chat(_make_turns(4, 1500))
    ns, avail, _ = _check_budget(out)
    assert ns <= avail
    users = [m.content for m in out if m.role == 'user']
    assert users[0] == '第0轮问题', f'task must be intact, got {users[0]!r}'
    assert '第3轮问题' in users
    assert '第1轮问题' not in users, 'middle turn should be dropped first'


def test_functions_schema_overhead():
    funcs = [
        {'type': 'function', 'function': {
            'name': 'read_file', 'description': 'x' * 3000,
            'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}},
        }},
        {'type': 'function', 'function': {
            'name': 'references', 'description': 'y' * 3000, 'parameters': {'type': 'object'},
        }},
    ]
    msgs = [Message(role='system', content='s'), Message(role='user', content='task'),
            Message(role='assistant', content='think', function_call={'name': 'read_file', 'arguments': '{}'}),
            Message(role='function', content=_big(2500), name='read_file')]
    out = _chat(msgs, functions=funcs)
    ns, avail, _ = _check_budget(out, functions=funcs)
    assert ns <= avail
    assert _patch()._count_functions(funcs) > 0


def test_assistant_content_and_fc_counted():
    patch, tok = _patch(), STATE['tokenizer']
    m = Message(role='assistant', content='思考内容占若干 token', function_call={'name': 'x', 'arguments': '{}'})
    assert patch._count_tokens(m) == tok.count_tokens('思考内容占若干 token') + tok.count_tokens(f'{m.function_call}')


def test_tool_result_capped():
    from qwen_agent.agents.fncall_agent import FnCallAgent
    agent = FnCallAgent()
    agent.function_map['read_file'] = _big(5000)
    r = agent._call_tool('read_file', '{}')
    rt = STATE['tokenizer'].count_tokens(r)
    assert rt <= _patch().TOOL_RESULT_MAX_TOKENS + 20


def test_truncation_keeps_function_call_and_not_mutating():
    patch = _patch()
    big_asst = Message(role='assistant', content=_big(3000), function_call={'name': 'read_file', 'arguments': '{}'})
    new = patch._truncate_to_total(big_asst, 500)
    assert new.function_call == big_asst.function_call
    assert new.role == 'assistant'
    assert big_asst.content == _big(3000), 'original must not be mutated'


def test_giant_single_result():
    msgs = [Message(role='system', content='s'), Message(role='user', content='任务：审计这个文件'),
            Message(role='assistant', content='好的', function_call={'name': 'read_file', 'arguments': '{}'}),
            Message(role='function', content=_big(4000), name='read_file')]
    out = _chat(msgs)
    ns, avail, _ = _check_budget(out)
    assert ns <= avail
    assert any(m.role == 'user' and m.content == '任务：审计这个文件' for m in out)


def test_ten_turns_first_and_last_kept():
    out = _chat(_make_turns(10, 1200))
    ns, avail, _ = _check_budget(out)
    assert ns <= avail
    users = [m.content for m in out if m.role == 'user']
    assert users[0] == '第0轮问题' and users[-1] == '第9轮问题'


def test_stress_random_conversations():
    random.seed(42)
    for k in range(30):
        n = random.randint(2, 8)
        out = _chat(_make_turns(n, random.randint(300, 2500)))
        ns, avail, _ = _check_budget(out)
        assert ns <= avail
        users = [m.content for m in out if m.role == 'user']
        assert users[0].startswith('第0轮'), f'stress {k}: task lost'


def test_reasoning_counted_and_truncated():
    patch, tok = _patch(), STATE['tokenizer']
    reasoning = ''.join(f'推理第{i}步 ' for i in range(200))
    m = Message(role='assistant', content='', reasoning_content=reasoning)
    rc_tokens = tok.count_tokens(reasoning)
    assert patch._count_tokens(m) == rc_tokens
    target = rc_tokens - 100
    new = patch._truncate_to_total(m, target)
    assert new.reasoning_content != reasoning, 'reasoning must be truncated'
    assert patch._count_tokens(new) <= target
    assert m.reasoning_content == reasoning, 'original must not be mutated'


def test_entry_validation_first_non_user():
    bad = [Message(role='system', content='s'), Message(role='assistant', content='x'),
           Message(role='user', content='hi')]
    from qwen_agent.llm.base import ModelServiceError
    try:
        _chat(bad)
    except ModelServiceError as e:
        assert e.code == '400'
        assert 'must start with a user message' in str(e)
    else:
        raise AssertionError('expected ModelServiceError')


def test_function_fields_preserved():
    patch = _patch()
    m = Message(role='function', content=_big(2000), name='read_file', extra={'function_id': '3'})
    new = patch._truncate_to_total(m, 300)
    assert new.name == 'read_file'
    assert new.extra == {'function_id': '3'}


if __name__ == '__main__':
    tests = sorted((k, v) for k, v in globals().items() if k.startswith('test_') and callable(v))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'PASS {name}')
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'FAIL {name}: {type(e).__name__}: {e}')
    print(f'{len(tests) - failed}/{len(tests)} passed')
    sys.exit(1 if failed else 0)

# Router 与 GroupChat

## 1. Router：智能分发

### 1.1 多继承设计

```python
class Router(Assistant, MultiAgentHub):  # router.py:36
```

Router 同时拥有：
- `Assistant` 的 RAG + FnCall 能力
- `MultiAgentHub` 的多 Agent 管理能力

### 1.2 路由流程

```
用户消息
    │
    ▼
Router._run()
    │
    ├── 1. 预处理消息：为历史 assistant 消息补充 "Call: {name}\nReply:" 前缀
    │       → 教会 LLM 历史上选择了哪个 Agent
    │
    ├── 2. super()._run()  ← Assistant._run (含 RAG + FnCall)
    │       → LLM 可能直接回答，或输出 "Call: agent_name"
    │
    ├── 3. 检查 response 中是否包含 "Call:"
    │       ├── 包含 → 提取 Agent 名称
    │       │   ├── 名称有效 → 运行该 Agent
    │       │   └── 名称无效 → 默认第一个 Agent
    │       └── 不包含 → 直接回答，不委托
    │
    └── 4. yield 被选 Agent 的响应
```

### 1.3 Stop Words

```python
extra_generate_cfg['stop'] = ['Reply:', 'Reply:\n']
```

LLM 输出 `Call: agent_name` 后，遇到 `Reply:` 停止——模型不需要自己模拟 Agent 的回复。

### 1.4 补充名称特殊标记

```python
@staticmethod
def supplement_name_special_token(message):
    """为有 name 的 assistant 消息追加 "Call: {name}\nReply:" 前缀"""
    if message.name:
        message.content = f'Call: {message.name}\nReply:' + message.content
    return message
```

这教会 Router LLM：「历史上选了哪个 Agent」的模式，从而在新对话中正确选择。

### 1.5 使用示例

```python
from qwen_agent.agents import Router, Assistant

coder = Assistant(llm=llm_cfg, name='coder',
    description='擅长编程和技术问题',
    function_list=['code_interpreter'])

researcher = Assistant(llm=llm_cfg, name='researcher',
    description='擅长搜索和分析信息',
    function_list=['web_search'])

router = Router(llm=llm_cfg, agents=[coder, researcher])

# Router 会根据问题自动选择合适的 Agent
messages = [{'role': 'user', 'content': '帮我写一个排序算法'}]
for response in router.run(messages):
    print(response[-1]['content'])
```

---

## 2. GroupChat：多 Agent 群聊

### 2.1 继承

```python
class GroupChat(Agent, MultiAgentHub):  # group_chat.py:29
```

### 2.2 发言者选择策略

| 策略 | 代码 | 说明 |
|------|------|------|
| `auto` | `GroupChatAutoRouter` | LLM 自动选择下一个发言者 |
| `round_robin` | 轮流 | 按 Agent 列表顺序轮流 |
| `random` | 随机 | 随机选一个 Agent |
| `manual` | 手动 | 通过 input() 人工指定 |
| `@mention` | 解析 @ | 消息中 `@agent_name` 直接指定 |

**优先级**：`@mention` > 策略选择

### 2.3 消息管理：_manage_messages

GroupChat 需要将多 Agent 的共享对话历史转换为每个 Agent 能理解的「两方对话」格式：

```python
def _manage_messages(self, messages, name):
    """
    为 Agent {name} 转换消息：
    - {name} 自己的消息 → assistant 角色
    - 其他 Agent 的消息 → user 角色，格式为 "Name: content"
    - 保留 function_call/result 消息
    - 末尾追加 "{name}: " 作为生成引导
    """
```

### 2.4 批量 vs 单次响应

- `need_batch_response=True`（默认）：多轮对话，直到 `max_round` 或对话结束
- `need_batch_response=False`：单个 Agent 回复一次

### 2.5 对话终止条件

- `max_round` 耗尽
- Auto Router 返回 `'[STOP]'`
- Agent 返回空响应（话题结束）
- Agent 返回 `PENDING_USER_INPUT`（等待人类输入）

### 2.6 使用示例

```python
from qwen_agent.agents import GroupChat, Assistant

writer = Assistant(llm=llm_cfg, name='writer',
    system_message='你是一个写作者，负责撰写文章。')

editor = Assistant(llm=llm_cfg, name='editor',
    system_message='你是一个编辑，负责审阅和改进文章。')

group = GroupChat(
    agents=[writer, editor],
    agent_selection_method='auto',
    llm=llm_cfg,
)

messages = [{'role': 'user', 'content': '请写一篇关于 AI 的文章'}]
for response in group.run(messages, max_round=4):
    for msg in response:
        if msg.get('name'):
            print(f"[{msg['name']}]: {msg['content'][:100]}...")
```

---

## 3. MultiAgentHub：多 Agent 管理基类

### 3.1 设计（multi_agent_hub.py:22）

```python
class MultiAgentHub(ABC):
    @property
    def agents(self) -> List[Agent]:
        """验证 _agents 列表：非空、全为 Agent 实例、名称唯一"""
        ...

    @property
    def agent_names(self) -> List[str]:
        return [a.name for a in self.agents]

    @property
    def nonuser_agents(self) -> List[Agent]:
        """过滤掉 UserAgent"""
        ...
```

### 3.2 约束

- 所有 Agent 必须有非空名称
- 名称必须唯一（Router 通过名称查找 Agent）
- 必须至少有一个 Agent

### 3.3 Mixin 模式

MultiAgentHub 不是独立使用，而是通过多继承混入：

```python
Router(Assistant, MultiAgentHub)   # RAG + 路由
GroupChat(Agent, MultiAgentHub)    # 群聊
```

---

## 4. 源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `agents/router.py` | 36 | `Router(Assistant, MultiAgentHub)` |
| `agents/router.py` | 61 | `_run` (路由逻辑) |
| `agents/router.py` | 92 | `supplement_name_special_token` |
| `agents/group_chat.py` | 29 | `GroupChat(Agent, MultiAgentHub)` |
| `agents/group_chat.py` | 81 | `_run` (群聊流程) |
| `agents/group_chat.py` | 168 | `_select_agent` (选择策略) |
| `agents/group_chat.py` | 214 | `_manage_messages` (消息转换) |
| `multi_agent_hub.py` | 22 | `MultiAgentHub(ABC)` |

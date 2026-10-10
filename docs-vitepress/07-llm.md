# 07 LLM 层：模型接入与消息处理

## 1. 类层次

```
BaseChatModel (llm/base.py:61)              ← LLM 抽象基类
│
└── BaseFnCallModel (llm/function_calling.py:23)  ← FnCall 预处理/后处理
    │
    ├── OAI (llm/oai.py)                    ← OpenAI 兼容接口
    └── QwenDashScope (llm/qwen_dashscope.py)  ← 阿里 DashScope 接口
```

---

## 2. BaseChatModel 核心能力

### 2.1 chat() 方法（llm/base.py:118）

这是 LLM 的主入口，封装了完整的调用流程：

```
chat(messages, functions, stream, extra_generate_cfg)
    │
    ├── 1. Deep copy + 消息类型转换 (dict → Message)
    ├── 2. Cache 查找
    ├── 3. 合并 generate_cfg
    ├── 4. 设置随机种子
    ├── 5. 语言检测 (has_chinese_messages)
    ├── 6. 注入默认 system message
    ├── 7. 消息截断 (_truncate_input_messages_roughly)
    ├── 8. 判断 fncall_mode
    ├── 9. _preprocess_messages (多模态 + FnCall 模板)
    ├── 10. 调用模型 (fncall → _chat_with_functions, 否则 → _chat)
    ├── 11. _postprocess_messages (FnCall 解析)
    ├── 12. 多模态输出转文本 (如不支持多模态输出)
    ├── 13. Cache 存储
    └── 14. 返回结果 (Message 或 dict)
```

### 2.2 关键属性

| 属性 | 默认值 | 含义 |
|------|--------|------|
| `support_multimodal_input` | `False` | 是否支持多模态输入 |
| `support_multimodal_output` | `False` | 是否生成多模态输出 |
| `support_audio_input` | `False` | 是否支持音频输入 |
| `max_retries` | `0` | API 重试次数 |
| `cache_dir` | — | diskcache 缓存目录 |

---

## 3. 消息模型：schema.py

### 3.1 Message（llm/schema.py:132）

```python
class Message(BaseModelCompatibleDict):
    role: str                              # system / user / assistant / function
    content: Union[str, List[ContentItem]] # 文本或多模态内容
    reasoning_content: Optional[...]       # 思考链（推理模型）
    name: Optional[str]                    # 发言者名称（群聊/函数结果）
    function_call: Optional[FunctionCall]  # 函数调用
    extra: Optional[dict]                  # 额外元数据
```

### 3.2 BaseModelCompatibleDict（llm/schema.py:37）

Pydantic `BaseModel` 的扩展，支持 dict 风格访问：

```python
msg = Message(role='user', content='hello')
msg['role']          # → 'user'  (dict 风格)
msg.role             # → 'user'  (属性风格)
msg.get('content')   # → 'hello' (dict.get)
```

### 3.3 FunctionCall（llm/schema.py:69）

```python
class FunctionCall(BaseModelCompatibleDict):
    name: str       # 函数名
    arguments: str  # JSON 字符串
```

### 3.4 ContentItem（llm/schema.py:80）

```python
class ContentItem(BaseModelCompatibleDict):
    text: Optional[str]                  # 文本
    image: Optional[str]                 # 图片 URL
    file: Optional[str]                  # 文件 URL
    audio: Optional[Union[str, dict]]    # 音频
    video: Optional[Union[str, list]]    # 视频
```

**互斥验证**：`check_exclusivity` 确保五个字段中只有一个非空。

---

## 4. 注册机制

### 4.1 LLM_REGISTRY（llm/base.py:32）

```python
LLM_REGISTRY = {}

@register_llm('qwen_dashscope')
class QwenDashScopeChat(BaseFnCallModel):
    ...

@register_llm('openai')
class OAIChat(BaseFnCallModel):
    ...
```

### 4.2 工厂方法

```python
from qwen_agent.llm import get_chat_model

llm = get_chat_model({
    'model': 'qwen-max',
    'model_type': 'qwen_dashscope',  # 查找 LLM_REGISTRY
})
```

---

## 5. use_raw_api 模式

当 `QWEN_AGENT_USE_RAW_API=true` 或 `generate_cfg` 中设置时，跳过 FnCall 模板格式化，直接使用 OpenAI 兼容的 raw API：

```python
# 正常模式：FnCall 模板预处理 → 模型调用 → FnCall 模板后处理
# Raw API 模式：跳过预处理/后处理，直接用 OpenAI 格式的 tools 参数
```

适用场景：模型服务端（如 vLLM）已原生支持 FnCall，无需客户端模板转换。

```python
import os
os.environ['QWEN_AGENT_USE_RAW_API'] = 'true'
```

---

## 6. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `llm/base.py` | 32 | `LLM_REGISTRY` |
| `llm/base.py` | 35 | `register_llm` 装饰器 |
| `llm/base.py` | 61 | `BaseChatModel` |
| `llm/base.py` | 118 | `chat()` 主入口 |
| `llm/base.py` | 341 | `_preprocess_messages` |
| `llm/base.py` | 364 | `_postprocess_messages` |
| `llm/schema.py` | 37 | `BaseModelCompatibleDict` |
| `llm/schema.py` | 69 | `FunctionCall` |
| `llm/schema.py` | 80 | `ContentItem` |
| `llm/schema.py` | 132 | `Message` |
| `llm/function_calling.py` | 23 | `BaseFnCallModel` |

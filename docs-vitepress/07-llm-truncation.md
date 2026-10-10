# 消息截断与重试

## 1. 消息截断策略

### 1.1 _truncate_input_messages_roughly（llm/base.py:602）

当消息总 token 数超过 `max_input_tokens` 时，按 4 步渐进截断：

```
步骤 1: 最小化 function 结果
    → 截断长 function 输出，替换为 'omit'
    → 保留函数调用结构

步骤 2: 移除中间轮次
    → 保留第一轮和最后一轮
    → 删除中间的 user/assistant/function 轮次

步骤 3: 截断最后一步的 function 结果
    → 缩短最近一次工具调用的输出

步骤 4: 截断 user/assistant 内容
    → 缩短剩余文本消息
```

**不变量**：始终保留 system message 和最后一轮对话。

### 1.2 轮次分组

消息按「每个 user 消息开始一个新轮次」来分组：

```
Turn 1: [system, user, assistant, function, function, assistant]
Turn 2: [user, assistant]
Turn 3: [user, ...]  ← 当前轮（不截断）
```

### 1.3 默认配置

```python
DEFAULT_MAX_INPUT_TOKENS = 58000  # settings.py
```

可通过 `generate_cfg` 调整 `max_input_tokens`。

---

## 2. 重试机制

### 2.1 retry_model_service（llm/base.py:807）

指数退避 + 抖动：

```python
delay = min(max_delay, exponential_base ** attempt + jitter)
```

默认参数：
- `max_delay = 300.0`
- `exponential_base = 2.0`

### 2.2 不重试的情况

立即抛出异常，不进行重试：
- `max_retries <= 0`
- HTTP 400 错误（请求格式错误）
- `DataInspectionFailed`（内容过滤）
- 错误消息包含 `'inappropriate content'`
- 错误消息包含 `'maximum context length'`（输入过长）

### 2.3 配置重试

```python
llm_cfg = {
    'model': 'qwen-max',
    'model_type': 'qwen_dashscope',
    'generate_cfg': {
        'max_retries': 3,  # 最多重试 3 次
    }
}
```

---

## 3. 缓存机制

### 3.1 DiskCache（llm/base.py:98-109）

当 `cache_dir` 配置时，使用 `diskcache.Cache`：

```python
# 缓存 key = json_dumps_compact({messages, functions, extra_generate_cfg})
# 缓存 value = json_dumps_compact(output)
```

- **命中**：直接返回缓存结果（流式模式包装为 `iter()`）
- **未命中**：调用模型后存储结果

### 3.2 配置缓存

```python
llm_cfg = {
    'model': 'qwen-max',
    'model_type': 'qwen_dashscope',
    'cache_dir': '/tmp/qwen_agent_cache',  # 启用缓存
}
```

**用途**：开发和调试时避免重复调用 API，节省费用。

---

## 4. 源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `llm/base.py` | 602 | `_truncate_input_messages_roughly` |
| `llm/base.py` | 807 | `retry_model_service` |
| `llm/base.py` | 839 | `_raise_or_delay` (指数退避) |
| `llm/base.py` | 98-109 | DiskCache 缓存配置 |
| `llm/base.py` | 157-168 | 缓存查找逻辑 |
| `llm/base.py` | 267-268 | 缓存存储逻辑 |

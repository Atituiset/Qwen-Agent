# 关键词生成策略

## 1. 策略列表

| 策略名 | 类 | 功能 |
|--------|-----|------|
| `None` | — | 不生成关键词，直接用原始 query |
| `GenKeyword` | `GenKeyword` | 用 LLM 从 query 提取搜索关键词 |
| `SplitQueryThenGenKeyword` | `SplitQueryThenGenKeyword` | 先拆分复杂 query，再分别生成关键词 |
| `GenKeywordWithKnowledge` | `GenKeywordWithKnowledge` | 结合已有知识生成关键词 |
| `SplitQueryThenGenKeywordWithKnowledge` | 组合 | 拆分 + 结合知识 |

## 2. 动态策略加载

```python
# memory.py:107 附近
if query and self.rag_keygen_strategy != 'none':
    # 动态导入策略类
    module = import_module('qwen_agent.agents.keygen_strategies')
    strategy_cls = getattr(module, self.rag_keygen_strategy)
    # 用 LLM 实例化策略
    keygen_agent = strategy_cls(llm=self.llm)
    # 运行策略获取增强关键词
    result = keygen_agent.run([...messages...])
    # 解析 JSON 输出
    enriched = json5.loads(result_text)
    query = enriched.get('text', query)
```

**为什么动态导入？** 避免循环依赖，且策略可扩展——添加新策略只需在 `keygen_strategies/` 目录下新建文件。

## 3. 策略选择指南

| 策略 | 适用场景 |
|------|----------|
| `None` | Query 已经是很好的关键词 |
| `GenKeyword` | 通用场景（默认） |
| `SplitQueryThenGenKeyword` | 复杂多部分问题，如"比较 A 和 B 的优劣并给出建议" |
| `GenKeywordWithKnowledge` | 需要领域知识生成关键词，如专业术语 |
| `SplitQueryThenGenKeywordWithKnowledge` | 复杂+专业领域 |

## 4. 关键词生成流程

```
用户 Query: "Qwen3 和 Llama3 在代码生成任务上谁更强？"
    │
    ▼
SplitQueryThenGenKeyword 策略
    │
    ├── 拆分: ["Qwen3 代码生成能力", "Llama3 代码生成能力", "Qwen3 vs Llama3 对比"]
    │
    ├── 为每个子 query 生成关键词
    │
    └── 合并为增强 query
    │
    ▼
增强 Query: "Qwen3 code generation benchmark | Llama3 code generation benchmark | Qwen3 Llama3 comparison"
```

## 5. 自定义策略

在 `qwen_agent/agents/keygen_strategies/` 下创建新的策略文件：

```python
# qwen_agent/agents/keygen_strategies/my_strategy.py
from qwen_agent.agents.keygen_strategies.base import BaseKeygenStrategy

class MyStrategy(BaseKeygenStrategy):
    def _run(self, messages, lang='en', **kwargs):
        # 自定义关键词生成逻辑
        query = messages[-1].content
        keywords = self.llm.quick_chat(
            f"从以下问题中提取3-5个搜索关键词：{query}"
        )
        return keywords
```

然后在 RAG 配置中使用：

```python
rag_cfg = {
    'rag_keygen_strategy': 'MyStrategy',
}
```

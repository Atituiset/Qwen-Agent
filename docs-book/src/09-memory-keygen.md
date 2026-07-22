# 关键词生成策略详解

keygen 的目的：把用户自然语言问题改写成适合 BM25 匹配的关键词 JSON。策略类在 `qwen_agent/agents/keygen_strategies/`，由 `rag_keygen_strategy` 配置指定，Memory 运行时动态 import（memory.py:107-132）。不传 llm 时强制 `'none'`（memory.py:61-63）。

---

## 1. GenKeyword（默认策略）

gen_keyword.py:26-42 的中文 prompt 原文：

```
请提取问题中的关键词，需要中英文均有，可以适量补充不在问题中但相关的关键词。关键词尽量切分为动词、名词、或形容词等单独的词，不要长词组（目的是更好的匹配检索到语义相关但表述不同的相关资料）。关键词以JSON的格式给出，比如{"keywords_zh": ["关键词1", "关键词2"], "keywords_en": ["keyword 1", "keyword 2"]}

Question: 这篇文章的作者是谁？
Keywords: {"keywords_zh": ["作者"], "keywords_en": ["author"]}
Observation: ...

Question: 解释下图一
Keywords: {"keywords_zh": ["图一", "图 1"], "keywords_en": ["Figure 1"]}
Observation: ...

Question: 核心公式
Keywords: {"keywords_zh": ["核心公式", "公式"], "keywords_en": ["core formula", "formula", "equation"]}
Observation: ...

Question: {user_request}
Keywords:
```

设计要点：

- 三组 few-shot 教模型输出**中英文双语、短词、含同义扩展**（"图一"→"图 1"/"Figure 1"），针对 BM25 的字面匹配弱点；
- **不设 max_tokens**，靠 stop 词 `Observation:` 截断（gen_keyword.py:75-78），防止模型继续编 observation；
- `_run` 只做模板替换后 `_call_llm`（gen_keyword.py:80-83），输出**不含 `text` 字段**——由 Memory 侧补齐（见第 4 节）。

---

## 2. SplitQuery 与 SplitQueryThenGenKeyword

`SplitQuery`（split_query.py:25-104）先把 query 拆成"信息片段 + 任务描述"：

```
请提取问题中的可以帮助检索的重点信息片段和任务描述，以JSON的格式给出：{"information": ["重点信息片段1", "重点信息片段2"], "instruction": ["任务描述片段1", "任务描述片段2"]}。
如果是提问，则默认任务描述为：回答问题

Question: 总结
Result: {"information": [], "instruction": ["总结"]}
...
```

一个省 token 的技巧（split_query.py:85-90）：instruction 不参与后续检索，直接把 stop 词设为 `"], "instruction":` 让模型在 information 输出完就停，`_run` 再补 `"]}` 还原 JSON。

`SplitQueryThenGenKeyword`（split_query_then_gen_keyword.py:28-69）两段式：先 SplitQuery 取 `information` 拼接，若 `0 < len(information) <= len(query)` 则替换原 query，再跑 GenKeyword，最后把改写后的 query 塞进 `keyword_dict['text']` 输出。

---

## 3. WithKnowledge 变体

`GenKeywordWithKnowledge`（gen_keyword_with_knowledge.py:26-83）在 prompt 里注入**文档词表**，让关键词贴合文档用词风格：

```
根据问题提取中文或英文关键词，不超过10个，可以适量补充不在问题中但相关的关键词。
请依据给定参考资料的语言风格来生成（目的是方便利用关键词匹配参考资料）。
...
<Refs>本场景词汇列表：
{ref_doc}
```

词表来自 `extract_doc_vocabulary` 工具（TF-IDF 排序，见主篇第 4 节），按 token 预算截断后填入（gen_keyword_with_knowledge.py:72-79）。`SplitQueryThenGenKeywordWithKnowledge` 仅替换 keygen 为该变体，流程复用。

---

## 4. Memory 侧的输出处理

memory.py:121-132：剥掉 ```` ```json ```` 围栏 → `json5.loads` → **若缺 `text` 键则补 `keyword_dict['text'] = query`**（纯 GenKeyword 路径的 text 在此补齐）→ `json.dumps` 后作为 retrieval 的 query。解析失败则退回原始 query 字符串。

`text` 字段的意义：检索时 `parse_keyword` 会对它做完整分词后**追加**到关键词表（见[分词与检索排序](./09-memory-retrieval.md)），相当于"关键词 + 原始问题分词"双保险。

---

## 5. mem_llm 的选择

FnCallAgent 初始化 Memory 时对推理模型特判（fncall_agent.py:56-71）：

```python
if 'qwq' in self.llm.model.lower() or 'qvq' in self.llm.model.lower() or 'qwen3' in self.llm.model.lower():
    if 'dashscope' in self.llm.model_type:
        mem_llm = {'model': 'qwen-turbo', 'model_type': 'qwen_dashscope',
                   'generate_cfg': {'max_input_tokens': 30000}}
    else:
        mem_llm = None   # → keygen 强制关闭
else:
    mem_llm = self.llm
```

原因：关键词生成不需要推理能力，推理模型做 keygen 又慢又贵，换轻量的 qwen-turbo；非 dashscope 渠道干脆关掉 keygen 退化为纯 BM25。

---

## 6. 策略对照表

| 策略 | 流程 | 输出含 `text` | 额外 LLM 调用 | 适用场景 |
|------|------|---------------|---------------|----------|
| `None` | 不生成，原始 query 直接检索 | — | 0 | 无 LLM / 追求低延迟 |
| `GenKeyword` | query → 关键词 | 否（Memory 补齐） | 1 | 默认，通用 |
| `SplitQueryThenGenKeyword` | 拆 query → 关键词 | 是 | 2 | 复杂多意图问题 |
| `GenKeywordWithKnowledge` | 取词表 → 关键词 | 否（Memory 补齐） | 1 + 词表缓存 | 专业文档、术语密集 |
| `SplitQueryThenGenKeywordWithKnowledge` | 拆 query → 取词表 → 关键词 | 是 | 2 + 词表缓存 | 复杂问题 + 专业文档 |

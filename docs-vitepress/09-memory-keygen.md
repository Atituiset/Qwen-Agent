# 09.1 关键词生成（keygen）策略详解

> 本文聚焦 Memory 子系统的 **keygen 环节**：从用户问题到结构化关键词 JSON 的 LLM 改写。配套 [09 导览](./09-memory-deep-dive.md) 与 [09.2 检索排序](./09-memory-retrieval.md)。
> 每节按 **决策点 / 代码证据 / 为什么不那样 / 踩坑清单** 四件套展开。所有行号基于 `main` 分支。

## §1. 4 个 keygen 策略的继承关系一眼图

| 类名                                       | 继承自                   | 关联工具            | 默认 stop words     |
|--------------------------------------------|--------------------------|---------------------|---------------------|
| `GenKeyword`                               | `Agent`                  | 无                  | `['Observation:']`  |
| `SplitQuery`                               | `GenKeyword`             | 无                  | `['"], "instruction":']` |
| `GenKeywordWithKnowledge`                  | `GenKeyword`             | `extract_doc_vocabulary` | 同 `GenKeyword`     |
| `SplitQueryThenGenKeyword`                 | `Agent`（**直接，不继承 SplitQuery**） | 无（组合 `SplitQuery` + `GenKeyword`） | 同内部两步 |
| `SplitQueryThenGenKeywordWithKnowledge`    | `SplitQueryThenGenKeyword` | 同 `GenKeywordWithKnowledge` | 继承父类 |

**决策点**：`SplitQueryThenGenKeyword` 没继承 `SplitQuery`，而是**组合**了 `SplitQuery` 实例 + `GenKeyword` 实例。策略类靠"组合 + 一层薄重写"实现差异。

**代码证据**：
```python
# split_query_then_gen_keyword.py:28-37
class SplitQueryThenGenKeyword(Agent):
    def __init__(self, ..., llm=None, ...):
        super().__init__(...)
        self.split_query = SplitQuery(llm=self.llm)
        self.keygen = GenKeyword(llm=llm)        # 注意 llm vs self.llm 风格不统一，但二者一致
```

**为什么不那样**：另一选择是让 `SplitQueryThenGenKeyword` 继承 `SplitQuery`，复用其 `_run`。但 `SplitQuery._run` 返回 split JSON 后流程就停了，组合方式能干净地把 split 输出接到 GenKeyword 输入。**继承会绑死流程顺序**，组合能换步骤（虽然本仓目前不换）。

**踩坑清单**：
- `settings.py:34-36` 的 `Literal` 类型标注里只列了 4 个类名，**未列 `'None'`**——运行期 `'none'` 是合法值（memory.py:63 强制改），类型标注漏配。
- 类型标注是 `'None'`（大写 N），代码里比较是 `self.rag_keygen_strategy.lower() != 'none'`——**显式配 `'None'` 会走 `getattr` 路径直接 AttributeError**，配 `'none'` 才正确。配置项保持小写。
- 类名拼写错：`getattr(module, 'GenKeywordXXX')` 会直接抛栈，**没 try/except**——自定义策略要么改 `__init__.py`，要么保证名字完全匹配（含大小写）。

---

## §2. GenKeyword 的 prompt 全文（默认策略，结构最纯）

**决策点**：prompt 用 3-shot few-shot，且**结尾停在 `Keywords:` 后换行**——靠 stop word `['Observation:']` 在模型自续生成下一个同样例时截断。

**代码证据**（`gen_keyword.py:25-42` 的 ZH 模板、EN 模板对称）：
```python
PROMPT_TEMPLATE_ZH = """请提取问题中的关键词，需要中英文均有，可以适量补充不在问题中但相关的关键词。
关键词尽量切分为动词、名词、或形容词等单独的词，不要长词组
（目的是更好的匹配检索到语义相关但表述不同的相关资料）。
关键词以JSON的格式给出，比如{"keywords_zh": ["关键词1", "关键词2"], "keywords_en": ["keyword 1", "keyword 2"]}

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
"""

# gen_keyword.py:75-78
self.extra_generate_cfg = merge_generate_cfgs(
    base_generate_cfg=self.extra_generate_cfg,
    new_generate_cfg={'stop': ['Observation:']},
)
```

**为什么不那样**：替代设计是"只给 1 个 shot 或 0 shot"。3-shot 的好处是稳定输出 JSON 结构（3 个例子硬约束了 JSON 形状），代价是 prompt 长（约 200~300 token 占用）。在 RAG 总预算里这点开销可接受。

**踩坑清单**：
- 模板尾部不等 `Observation: ...`，依赖 stop word 截断。**换不同 inference 后端**（如 vllm / TGI / OpenAI）时 stop word 行为略有差异，要么实测一致，要么把 stop word 改成也加 `\nObservation` 之类更鲁棒的字符串。
- 例子里 `keywords_zh: ["图一", "图 1"]` 教模型"同义词要写多种形式"，但**没教"切成短词而不是长词组"**——实际产出 `keywords_zh: ["论文摘要"]` 这种 4 字长 phrase 时，BM25 词表里 jieba 切过是 `论文 / 摘要`，**`论文摘要` 作为整 token 在 BM25 上拿 0 分**（见 09.2 §4）。
- few-shot 的 `核心公式 → ["核心公式", "公式"]` 教模型"原词 + 子词都保留"，这反而巩固了长短语输出。

---

## §3. SplitQuery 的双字段设计：instruction 字段被废弃但仍生成

**决策点**：split 步骤的 prompt 让 LLM 产出 `{"information": [...], "instruction": [...]}` 两段，**但下游只用 `information`**——prompt 里明确告诉模型 "instruction is not utilized"，把它设成 stop word 在生成半路截断省 token。

**代码证据**（`split_query.py:27-72` prompt、79-90 初始化）：
```python
# split_query.py:27-49  的 EN 模板核心
PROMPT_TEMPLATE_EN = """Please extract the key information fragments that can help retrieval
and the task description in the question, and give them in JSON format:
{"information": ["information fragment 1", "information fragment 2"],
 "instruction": ["instruction fragment 1", "instruction fragment 2"]}.

Question: Summarize
Result: {"information": [], "instruction": ["Summarize"]}

Question: {user_request}
Result:
"""

# split_query.py:85-90  stop words
# Currently, instruction is not utilized,
# so in order to avoid generating redundant tokens, set 'instruction' as stop words
self.extra_generate_cfg = merge_generate_cfgs(
    base_generate_cfg=self.extra_generate_cfg,
    new_generate_cfg={'stop': ['"], "instruction":']},
)
```

**为什么不那样**：另一选择是直接改成"只产出 information 字段"的单字段 prompt。这能省去 stop word trick，但意味着 prompt 需要保持信息抽取能力而把"任务描述" 完全留待 GenKeyword 重新推断——本系统选择保留双字段 prompt（LLM 更容易分割 info vs task），只是后处理把 instruction 丢掉。这是个**prompt 设计 vs token 节省**的取舍。

**踩坑清单**：
- stop word `"], "instruction":` 是在 information 数组刚结束的位置截断——会产生**未闭合的 JSON**。`split_query.py` 末尾有 try/except 把缺 `}` 或缺 `"]` 都补回来。若 inference 后端 stopword 触发時序不同（如 OpenAI 是 "after token"，vllm 是 "before token"），产物可能差一个 `"`——`json5.loads` 容错可吸收，但自实现后端要测。
- 失败兜底在 `split_query_then_gen_keyword.py:48-53`：`json5.loads` 失败或 `len(information) > len(query)` 时 `query = query`（用原 query）。这里 `len(information) <= len(query)` 是个**经验性保护**——如果 split 把 problem 变得更长（LLM 偶发吐多段长 fragment）直接抛弃。这条规则在 README 里没写，改这一段前要警觉。
- `instruction` 字段被废弃，但 prompt 仍教模型产出它——若做 prompt 优化想压缩 prompt，可以删 instruction 字段约省 1/5 token；但要在 split_query_then_gen_keyword.py 行 49 的 `['information']` 取值改成 `information` 的别的形态时同步留存。

---

## §4. GenKeywordWithKnowledge：先调工具抽词表，再把词表塞进 prompt

**决策点**：`GenKeywordWithKnowledge` 是唯一**调工具用 keygen 之前**的策略——它会先调 `extract_doc_vocabulary` 跑 TF-IDF 把全文高频词抽出来，再把这些词当**参考词汇列表**塞进 prompt。

**代码证据**（`gen_keyword_with_knowledge.py:27-83` 关键片段）：
```python
PROMPT_TEMPLATE_ZH = """根据问题提取中文或英文关键词，不超过10个，
可以适量补充不在问题中但相关的关键词.

<Refs>本场景词汇列表：
{ref_doc}

Question: {user_request}
Thought: 我应该依据参考资料中的词汇风格来提取问题的关键词..."""

def __init__(self, function_list=None, llm=None, system_message=..., **kwargs):
    super().__init__(['extract_doc_vocabulary'] + (function_list or []), llm, system_message, **kwargs)

def _run(self, messages, lang='en', **kwargs):
    ...
    available_token = DEFAULT_MAX_INPUT_TOKENS - count_tokens(self.PROMPT_TEMPLATE[lang]) \
                    - count_tokens(messages[-1][CONTENT]) - 100
    voc = self._call_tool('extract_doc_vocabulary', {'files': files})
    available_voc = tokenizer.truncate(voc, max_token=available_token)
    messages[-1][CONTENT] = self.PROMPT_TEMPLATE[lang].format(ref_doc=available_voc, ...)
```

**为什么不那样**：另一选择是"把全文所有词都让 LLM 看到"。但 LLM 上下文窗口有限（DEFAULT_MAX_INPUT_TOKENS 默认 58000），不可能塞全文——只能塞 TF-IDF 排名靠前的高频词。这条路径比纯 GenKeyword 重得多（多 1 次 sklearn TF-IDF 计算和一次 storage 读写），适合 **term 集中且重复** 的领域文档（如智库报告、手顺书）。

**踩坑清单**：
- `available_token = 58000 - count_tokens(template) - count_tokens(query) - 100` 是个硬公式。对小文档，TF-IDF 词表可能只占 200 token；对没重复词的稀疏文档（如纯文学作品），TF-IDF 词表可能塞满 ~54000 token——LLM 几乎全部 attention 都放在词表噪声上，反而效果差。
- `extract_doc_vocabulary` 缓存键是 `str(files)`，即 Python 的 `repr()`（`extract_doc_vocabulary.py:55` `document_id = str(files)`）——**list 顺序不同会落到不同 cache key**，但语义上词表应一致。多文件场景下传 files 时顺序要稳定。
- 词表是按 TF-IDF 排序**不截断**地 join 成逗号串（`extract_doc_vocabulary.py:78`），再在 `gen_keyword_with_knowledge.py:79` 用 `tokenizer.truncate(voc, max_token=available_token)` 截。**这意味着截断的边界 = 排名末尾的 term**，靠 TF-IDF 权重靠后的 term 排到 prompt 外；但 LLM 看到的词表里**没有"还有 N 个词被截掉了"的信息**，模型可能误把已显示词表当作完整词表。
- `SplitQueryThenGenKeywordWithKnowledge` 整类只有一行重写（覆盖 `self.keygen = GenKeywordWithKnowledge(...)`），完全靠继承的 split+keygen 流程复用——意味着它既要承担 SplitQuery 失败回退的复杂度，又要承担 WithKnowledge 的工具调用，**是 4 个策略里最复杂、最易调坏的**。

---

## §5. Memory 侧的输出处理：JSON 字符串作为下游 query

**决策点**：keygen 的输出在 memory.py 这一层不解析成结构体，而是**先 json.loads 再 json.dumps成一个 compact JSON 字符串**，再用这个字符串当 retrieval 的 query。

**代码证据**（`memory.py:107-140` 核心片段）：
```python
# memory.py:107-116  路由
if query and self.rag_keygen_strategy.lower() != 'none':
    module = import_module('qwen_agent.agents.keygen_strategies')
    cls = getattr(module, self.rag_keygen_strategy)
    keygen = cls(llm=self.llm)
    response = keygen.run([Message(USER, query)], files=rag_files)   # 只传 1 条 user 消息
    ...

# memory.py:117-132  规整
if last:
    keyword = last[-1].content.strip()
if keyword.startswith('```json'): keyword = keyword[len('```json'):]
if keyword.endswith('```'): keyword = keyword[:-3]
try:
    keyword_dict = json5.loads(keyword)
    if 'text' not in keyword_dict:
        keyword_dict['text'] = query       # ← 不同策略 text 字段语义不同！
    query = json.dumps(keyword_dict, ensure_ascii=False)
except Exception:
    query = query                          # 解析失败兜底

# memory.py:134-140  下送
content = self.function_map['retrieval'].call({'query': query, 'files': rag_files}, **kwargs)
```

**为什么不那样**：另一选择是直接构造 dict 并传给 retrieval。这么做要求 retrieval.call 的签名改成接受结构化对象——而 retrieval.call 当前签名是 `params: Union[str, dict]`，里面 `_verify_json_format_args` 又会把它序列化。维持字符串形式**与"query 是 string"的旧契约对齐**，让 retrieval 不必知道 keygen 内部结构。

**踩坑清单**：
- 行 127-128 的 `keyword_dict['text'] = query`：当 GenKeyword 直接成功输出（不含 Split 流程）时 text 字段会被填入**原始 query 字符串**；而 SplitQueryThenGenKeyword 在内层 `split_query_then_gen_keyword.py:66` 已经把 text 设为**split 后的 information**。所以**不同策略 query JSON 中 `text` 字段语义不一致**——retrieval 端 `parse_keyword` 和 `vector_search` 都直接读 `res['text']`，二者行为会随策略切换而 drift。
- 行 132 `except Exception: query = query`：兜底用原 query 字符串。**没日志**——keygen LLM 输出格式坏掉时 caller 不可见。这条对应 `keyword_search.py:195` 的 broad except，是调 keygen 调度时静默失败的两个点之一。
- 失败兜底后下游 retrieval 拿到的是原 query 字符串而非 JSON——`parse_keyword` 会走 `split_text_into_keywords` 退化路径（见 09.2 §3），始终能拿到 wordlist 但少了一路 keywords_zh/en 的扩充。**不会 crash，但少了 keygen 的收益**。
- **整套 keygen 没有缓存**——每次 `Memory._run` 都会重新调一次 LLM。root cause 是 query 的时效性（不同 turn 同一 query 可能在不同 context 下该取不同关键词），但既然 §2 已说多轮历史**完全不用**，cache by (query, files) 是可行的，**当前未实现**。注意：DocParser 有缓存（§5 §9），但 keygen 这层不缓存。如果 caller 想省 LLM 调用，要在 caller 侧做一层 (query, files) 缓存，**不能在 Memory 实例属性里**——Memory 实例在 caller 反复构造时不复用，缓存要放在外部 Storage。

---

## §6. 多轮指代陷阱：keygen 完全看不到历史

**决策点**：Memory 拿 messages 给 keygen 时只构造 `[Message(USER, query)]` 一条，与历史无关。

**代码证据**：
```python
# memory.py:104
query = extract_text_from_message(messages[-1], add_upload_info=False)

# memory.py:112
response = keygen.run([Message(USER, query)], files=rag_files)
```

**为什么不那样**：业界多数 RAG 系统会做 multi-turn query rewrite，本系统**完全不做**。理由是：在 keygen 的 prompt 设计里插多轮历史会破坏 3-shot few-shot 的纯净（few-shot 例子都是单问题→关键词），让模型的行为 drift。

**踩坑清单**：
- 用户第二轮问"它的作者是谁？"（第一轮提到论文 X）：keygen 抽出的关键词不会有 "X"、"论文X作者"。**这是已知短板**，靠 `SplitQueryThenGenKeyword` 也救不回来——它只做**单问题内部**的 information/instruction 切分，不跨轮。
- 工程上要补救只能在 **caller 侧**预先 rewrite：在调 Memory 之前把多轮指代展开成完整 query 注入，**不要在 `_run` 内部改写**——因为 `_run` 内部如果改写 query，会让 retrieval 缓存键错位（虽然 memory 不缓存 keygen，但 DocParser 通过 files hash 缓存，不会受 query 改写影响）。
- 多轮保存的指代如果在上轮 assistant 输出里，本轮 user 直接是 "继续"——`messages[-1].role==USER` 成立，但 `extract_text_from_message` 抽出的就是"继续"两字，会被 stop word 全杀，进入 retrieval 的"无 query 兜底"（09.2 §4）。这是另一个静默降级路径。

---

## §7. 4 个策略对比与选择建议

| 策略                                              | LLM 调用次数 | 工具调用 | 适用场景                              | 风险点                  |
|---------------------------------------------------|---------------|----------|---------------------------------------|-------------------------|
| `GenKeyword`                                       | 1             | 0        | 通用，短到中等长度单问题              | 长短语命中率，见 §2     |
| `SplitQueryThenGenKeyword`（settings 默认）        | 2             | 0        | 复杂单问题需信息抽取（如复合指令）    | split 失败回退要兜底    |
| `GenKeywordWithKnowledge`                          | 1 + 工具      | 1        | 领域术语密集的文档                    | 词表噪声 / TF-IDF 截断 |
| `SplitQueryThenGenKeywordWithKnowledge`            | 2 + 工具      | 1        | 复杂问题 + 领域文档                   | 最复杂，调试最折磨       |

**默认是 settings.py:34-36 的 `GenKeyword`**，不是 `SplitQueryThenGenKeyword`。这与 memory.py:45-54 的 docstring 描述不一致——docstring 暗示默认是更上层的组合策略，**实际 settings.py 的 DEFAULT_RAG_KEYGEN_STRATEGY 是 `GenKeyword`**。改默认前要确认你的 instance 走的是哪条。

## §8. 调 keygen 之前的检查清单

修改 keygen 这一环前逐项确认：

- [ ] 自定义策略类要 re-export 到 `keygen_strategies/__init__.py`，且类名严格匹配 `getattr(module, name)`
- [ ] 自定义策略需 `llm=None` 兼容路径（虽然 memory.py:61-63 会拦截，但单测里直接构造策略时 llm 可能 None）
- [ ] 改 stop word 切换 inference 后端时，要测后端 stop word 触发时序对齐
- [ ] 给 `text` 字段填值的语义要在策略内部一致——SplitQueryThen 系列填 split 后 information，纯 GenKeyword 系列填原 query。新增策略要选一边并保持
- [ ] `extract_doc_vocabulary` 的 cache 文件不会自动过期，运维需要清 workspace tools 目录；调用 files 顺序要稳定才能命中复用
- [ ] 若在多轮场景下加 query rewrite：在 caller 侧做，**不要在 Memory._run 内**

完。

# 09 Memory 机制深度剖析：从分块到 BM25 的原码级拆解

> 本文是 [05 Memory 与 RAG](./05_Memory与RAG.md) 的姊妹篇。05 讲架构与流程，本文下钻到原码级细节：keygen prompt 原文、分词器、BM25 打分、RRF 融合、切块 overlap、缓存键设计，以及工程上的坑。所有行号基于仓库 `main` 分支。

---

## 1. 全景调用链

一次带文件的问答，完整链路如下：

```
用户 messages (含 item.file)
    │
    ▼
Assistant._run (assistant.py:113)
    │
    ├── _prepend_knowledge_prompt (assistant.py:116)
    │       │
    │       ▼
    │   Memory.run → Memory._run (memory.py:81)
    │       │
    │       ├── 1. get_rag_files (memory.py:146)
    │       │      extract_files_from_messages + system_files
    │       │      过滤为 9 种受支持格式
    │       │      （无文件 → yield 空消息，提前结束）
    │       │
    │       ├── 2. 取最后一条 user 消息文本作为 query (memory.py:103)
    │       │
    │       ├── 3. keygen 关键词生成 (memory.py:107-132)
    │       │      动态 import 策略类（默认 GenKeyword）
    │       │      产出 {"keywords_zh": [...], "keywords_en": [...], "text": ...}
    │       │
    │       └── 4. retrieval 工具调用 (memory.py:134)
    │              │
    │              ▼
    │          Retrieval.call (retrieval.py:79)
    │              │
    │              ├── 逐文件 DocParser.call (doc_parser.py:80)
    │              │      ├── 查缓存（sha256(url)_page_size）
    │              │      ├── SimpleDocParser 解析 → 结构化分页文档
    │              │      ├── 全文 ≤ max_ref_token → 整篇单 chunk
    │              │      └── 否则 split_doc_to_chunk 切块（带 overlap）
    │              │      → Record(url, raw=[Chunk...], title)，写缓存
    │              │
    │              └── search.call (base_search.py:56)
    │                     ├── 总 token ≤ max_ref_token → 全量返回
    │                     ├── sort_by_scores（BM25 / front_page / RRF 融合）
    │                     └── get_topk 按预算装入、截断
    │
    │       yield Message(name='memory', content=检索结果 JSON)
    │
    ├── format_knowledge_to_source_and_content (assistant.py:52)
    │       → [{'source': '[文件](xx.pdf)', 'content': '...'}]
    │
    ├── KNOWLEDGE_SNIPPET / KNOWLEDGE_TEMPLATE 拼装
    │       追加到 system 消息末尾
    │
    ▼
FnCallAgent._run  ← 进入 LLM + Tool 循环
```

两个关键认知：

- **Memory 不走 LLM 对话**。它的产出是 `name='memory'` 的 assistant 消息，LLM 仅在 keygen 环节被调用。
- **注入方式是"写死"在 system prompt 里**，不是 function call 结果，用户无感、每轮必发生（有文件时）。

---

## 2. 检索触发与文件收集

`get_rag_files`（memory.py:146-154）合并两个来源后去重：

- `extract_files_from_messages(messages, include_images=False)`（utils/utils.py:465）——从消息 content 中取 `item.file`；
- `self.system_files`——构造 Memory 时传入的常驻文件（memory.py:79）。

随后用 `get_file_type`（utils.py:242）过滤，只保留 `PARSER_SUPPORTED_FILE_TYPES`（simple_doc_parser.py:368）：

```python
PARSER_SUPPORTED_FILE_TYPES = ['pdf', 'docx', 'pptx', 'txt', 'html', 'csv', 'tsv', 'xlsx', 'xls']
```

query 取自最后一条 user 消息（memory.py:103-104，`extract_text_from_message(..., add_upload_info=False)`）。

**无文件兜底**：`if not rag_files: yield [Message(ASSISTANT, content='', name='memory')]`（memory.py:98-99）。上游 `if knowledge:`（assistant.py:128）为假 → 不注入知识库段落，system 里**完全不会出现**"知识库"字样。兜底策略是"不写"，而非写"未找到"。

---

## 3. 关键词生成（keygen）策略详解

keygen 的目的：把用户自然语言问题改写成适合 BM25 匹配的关键词 JSON。策略类在 `qwen_agent/agents/keygen_strategies/`，由 `rag_keygen_strategy` 配置指定，Memory 运行时动态 import（memory.py:107-132）。不传 llm 时强制 `'none'`（memory.py:61-63）。

### 3.1 GenKeyword（默认策略）

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
- `_run` 只做模板替换后 `_call_llm`（gen_keyword.py:80-83），输出**不含 `text` 字段**——由 Memory 侧补齐（见 3.4）。

### 3.2 SplitQuery 与 SplitQueryThenGenKeyword

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

### 3.3 WithKnowledge 变体

`GenKeywordWithKnowledge`（gen_keyword_with_knowledge.py:26-83）在 prompt 里注入**文档词表**，让关键词贴合文档用词风格：

```
根据问题提取中文或英文关键词，不超过10个，可以适量补充不在问题中但相关的关键词。
请依据给定参考资料的语言风格来生成（目的是方便利用关键词匹配参考资料）。
...
<Refs>本场景词汇列表：
{ref_doc}
```

词表来自 `extract_doc_vocabulary` 工具（见第 7 节），按 token 预算截断后填入。`SplitQueryThenGenKeywordWithKnowledge` 仅替换 keygen 为该变体，流程复用。

### 3.4 Memory 侧的输出处理

memory.py:121-132：剥掉 ```` ```json ```` 围栏 → `json5.loads` → **若缺 `text` 键则补 `keyword_dict['text'] = query`**（纯 GenKeyword 路径的 text 在此补齐）→ `json.dumps` 后作为 retrieval 的 query。解析失败则退回原始 query 字符串。

`text` 字段的意义：检索时 `parse_keyword` 会对它做完整分词后**追加**到关键词表（见 6.2），相当于"关键词 + 原始问题分词"双保险。

### 3.5 mem_llm 的选择

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

## 4. 文档解析与切块原码拆解

### 4.1 统一产出结构

`SimpleDocParser`（simple_doc_parser.py）把 9 种格式统一解析为按页列表，每个段落带 token 计数（:475-478）：

```python
[{'page_num': 1, 'content': [{'text': ...} 或 {'table': ...}], 'title'?}, ...]
```

各格式要点：

| 格式 | 解析方式 | 页的概念 |
|------|----------|----------|
| pdf | pdfminer 提取版面 + pdfplumber 抽表格，按字号/行高合并误拆行（:240-327） | PDF 真实页 |
| docx | python-docx 逐段 + 表格转 markdown（:59-77） | 整篇 1 页 |
| pptx | 文本框段落 + 表格转 markdown（:80-113） | 每页幻灯片 1 页 |
| txt | 按 `\n` 分段（:116-124） | 整篇 1 页 |
| html | BeautifulSoup 取纯文本 + `<title>`（:202-237） | 整篇 1 页 |
| csv/tsv/xlsx/xls | pandas → markdown 管道表（:127-199） | Excel 每个 sheet 1 页 |

表格统一转 markdown 管道格式参与切块；**图片完全不支持**，`extract_image=True` 一律报错。

### 4.2 split_doc_to_chunk 的三段逻辑

切块按 **token**（不按页/段落硬切），预算 = `parser_page_size`（默认 500）。`chunk` 内部混合两种元素：页标记字符串 `[page: n]` 和内容项 `[txt, page_num]`。

1. **段落放得下**（doc_parser.py:173-177）：扣 token、追加，标记 `has_para=True`。
2. **段落放不下且 chunk 已有内容**（:179-202）：封 chunk（`token = parser_page_size - available_token`），取 overlap 开新 chunk（见 4.3）。
3. **段落放不下且 chunk 为空（超长段落）**（:203-260）：先按 `re.split(r'\. |。', txt)` 拆句；单句仍超预算则 **token 级定长滑窗硬切**（:216-220）：

```python
token_list = tokenizer.tokenize(s)
for si in range(0, len(token_list), available_token):
    ss = tokenizer.convert_tokens_to_string(
        token_list[si:min(len(token_list), si + available_token)])
    sentences.append([ss, min(available_token, len(token_list) - si)])
```

装句时 `if token <= available_token or (not has_para)` 保证至少塞进一句，防止死循环。

### 4.3 _get_last_part：150 字符 overlap 的取法

doc_parser.py:275-309，从 chunk 末尾**倒序**收集与最后一页同页的内容，预算 150 **字符**（不是 token）：

- 整段能装 → 整段前插（段落间 `\n` 连接）；
- 装不下的段落 → 拆句（`. ` 或 `。`），按句前插，遇到装不下的句子即停；
- 跨页即停。

overlap 以纯字符串形式进新 chunk 头部且 `has_para=False`——它不计入正式段落，只是上下文粘合剂。

### 4.4 数据结构

Chunk / Record 直接定义在 doc_parser.py:32-53（`tool_defs.py` 不存在）：

```python
class Chunk(BaseModel):
    content: str
    metadata: dict   # {'source': path, 'title': title, 'chunk_id': n}
    token: int

class Record(BaseModel):
    url: str
    raw: List[Chunk]
    title: str
```

---

## 5. 分词体系

### 5.1 QWenTokenizer（token 计数）

`qwen_agent/utils/tokenization_qwen.py`：基于 tiktoken，但加载 Qwen 官方 BPE 词表 `qwen.tiktoken` 手工构造 `tiktoken.Encoding`（:93-98）。特殊 token 含 `<|endoftext|>`、`<|im_start|>`、`<|im_end|>` 及 205 个 `<|extra_i|>`（`SPECIAL_START_ID = 151643`）。

- `count_tokens(text)` = `len(tokenize(text))`（:218），tokenize 前做 NFC 归一化；
- `truncate(text, max_token, keep_both_sides=False)`（:221-239）：`keep_both_sides=True` 时中间插 `...`，左右各半；否则前截。

### 5.2 string_tokenizer：检索分词（中英文分流）

keyword_search.py:132-156：

```python
def string_tokenizer(text: str) -> List[str]:
    text = text.lower().strip()
    if has_chinese_chars(text):          # 任一 CJK 字符即走中文路径
        import jieba
        _wordlist_tmp = list(jieba.lcut(text))
        _wordlist = [w for w in _wordlist_tmp if not all(c in PUNCTUATIONS for c in w)]
    else:
        _wordlist = tokenize_and_filter(text)   # 英文正则分词
    # 过停用词
    _wordlist_res = [w for w in _wordlist if w not in WORDS_TO_IGNORE]
    # 统一英文词干化（对中文词基本无影响）
    import snowballstemmer
    return snowballstemmer.stemmer('english').stemWords(_wordlist_res)
```

注意 `has_chinese_chars`（utils.py:94，正则 `[\u4e00-\u9fff]`）的判定是"**出现任一** CJK 字符"——中英混排文本整体走 jieba，英文正则过滤被跳过。

英文分词正则（keyword_search.py:112-117）保留缩写、数字/百分比、连字符词、邮箱：

```python
patterns = r"""(?x)
            (?:[A-Za-z]\.)+          # 缩写，如 U.S.A.
            |\d+(?:\.\d+)?%?         # 数字，含百分比
            |\w+(?:[-']\w+)*         # 单词，允许连字符/撇号
            |(?:[\w\-\']@)+\w+       # 邮箱
            """
```

### 5.3 WORDS_TO_IGNORE：场景化停用词表

keyword_search.py:69-87 共约 200 个词：英文常规停用词（代词/系动词/介词）+ 标点 + 一批中文任务词：

```python
..., '的', '吗', '是', '了', '啊', '呢', '怎么', '如何', '什么', ...,
'讲了', '描述', '讲', '总结', 'summarize', '总结下', '总结一下', '文档', '文章',
'article', 'paper', '文稿', '稿子', '论文', 'PDF', 'pdf', '这个', '这篇', '这',
'我', '帮我', '那个', '下', '翻译', '说说', '讲讲', '介绍', 'summary']
```

这批"文档问答专用"停用词解释了为什么 **"总结一下"这类 query 会得到空词表**，从而走 front-part 兜底（见 6.4）。

---

## 6. 检索与排序

### 6.1 BM25 打分

`KeywordSearch.sort_by_scores`（keyword_search.py:44-66）：

```python
wordlist = parse_keyword(query)
if not wordlist:
    return []
all_chunks = ...  # 所有文档的 chunk 平铺
from rank_bm25 import BM25Okapi
bm25 = BM25Okapi([split_text_into_keywords(x.content) for x in all_chunks])
doc_scores = bm25.get_scores(wordlist)
```

- `BM25Okapi` **未传任何参数**，用 rank_bm25 默认值（k1=1.5、b=0.75、epsilon=0.25）；
- 语料**每次查询现场分词重建**，无持久化索引——大文档场景开销随 chunk 数线性增长。

### 6.2 parse_keyword：keygen JSON 三字段的用法

keyword_search.py:169-196：

- 非 JSON → 直接对原文 `split_text_into_keywords`；
- `keywords_zh` / `keywords_en`：lower 合并 → stem → 去停用词；
- `text`：走完整 `split_text_into_keywords` 后**追加**——SplitQuery 系策略把改写后的 query 塞这里的原因；JSON 缺 `text` 键会抛 KeyError 落入兜底，对原始 text 整体再分词。

### 6.3 front_page_search：开头保底

front_page_search.py 全文 49 行。单文档时给前 2 个 chunk（`DEFAULT_FRONT_PAGE_NUM = 2`）打 `math.inf`：

```python
if max_ref_token >= page.token * DEFAULT_FRONT_PAGE_NUM * 2:  # 防止首页独占窗口
    chunk_and_score.append((doc.url, chunk_id, POSITIVE_INFINITY))
else:
    break
```

保证文档开头（标题/摘要/目录）必被注入；多文档直接返回空列表，不影响排序。

### 6.4 HybridSearch：RRF 融合

`rag_searchers` 多于 1 个时自动包成 `HybridSearch`（retrieval.py:72-77）。融合逻辑（hybrid_search.py:35-60）：

```python
for chunk_and_score in chunk_and_score_list:
    for i in range(len(chunk_and_score)):
        doc_id, chunk_id, score = chunk_and_score[i]
        if score == POSITIVE_INFINITY:
            chunk_score_map[doc_id][chunk_id] = POSITIVE_INFINITY
        else:
            chunk_score_map[doc_id][chunk_id] += 1 / (i + 1 + 60)
```

- 标准 **RRF（Reciprocal Rank Fusion）**：`score += 1 / (rank + k)`，k=60；
- 子检索器**原始分数完全丢弃**，只用名次——异构分数（BM25 分 vs +inf）无需归一化；
- `+inf` 直接置顶。

### 6.5 get_topk 与三级兜底

`BaseSearch.call`（base_search.py:56-87）的分支顺序：

```
query 为空 ────────────────→ _get_the_front_part（预算均分到每篇文档）
总 token ≤ max_ref_token ──→ 全量返回（use full ref）
sort_by_scores 为空 ───────→ _get_the_front_part（如"总结一下"空词表）
BM25 最高分为 0 ───────────→ _get_the_front_part（关键词与文档零重合）
否则 ──────────────────────→ get_topk 按分装入
```

`get_topk`（base_search.py:107-137）贪心装入：按分数从高到低，预算装不下整个 chunk 时**截断该 chunk 后立即 break**（不再尝试后续更小的 chunk）；用占位数组保证最终按原始 chunk_id 顺序输出。

`_get_the_front_part`（base_search.py:165-184）：`int(max_ref_token / len(docs))` 均分预算，每篇从头顺序装，装不下截断当前 chunk 并停止。

---

## 7. extract_doc_vocabulary：文档词表工具

`qwen_agent/tools/extract_doc_vocabulary.py`（82 行），仅供 WithKnowledge 系 keygen 策略调用：

1. SimpleDocParser 把每个文件解析为纯文本；
2. 以 `str(files)` 为缓存 key 查 Storage，命中直接返回；
3. 未命中：`TfidfVectorizer(tokenizer=string_tokenizer, stop_words=WORDS_TO_IGNORE)`（复用检索分词器和停用词表，sklearn 延迟导入）fit_transform，按 TF-IDF 值降序拼接全部词项，写缓存。

缓存目录 `workspace/tools/extract_doc_vocabulary`。

---

## 8. 两级缓存与 Storage

### 8.1 缓存键设计

| 层级 | 键 | 内容 | 目录 |
|------|-----|------|------|
| SimpleDocParser | `sha256(path)_ori` | 解析后的结构化分页文档 | `workspace/tools/simple_doc_parser/` |
| DocParser（切块） | `sha256(url)_<page_size>` | 切好的 Record JSON | `workspace/tools/doc_parser/` |
| DocParser（整篇） | `sha256(url)_without_chunking` | 单 chunk Record | 同上 |
| ExtractDocVocabulary | `str(files)` | TF-IDF 词表 | `workspace/tools/extract_doc_vocabulary/` |

page_size 进缓存键，改配置不会读到旧切块。HTTP URL 会先下载到 `workspace/tools/simple_doc_parser/<hash>/`。

**已知缺陷**：键基于**路径字符串**的 sha256，本地文件内容变更但路径不变时会命中旧缓存。

### 8.2 Storage 实现

`tools/storage.py`（120 行）：文件系统 KV，"一个 key 一个文件"，key 中的 `/` 映射为子目录。API：`put` / `get`（不存在抛 `KeyNotExistsError`）/ `delete` / `scan`（遍历目录拼 `key: value`）。

工程注意：

- **无任何锁/原子写**，并发 put 同 key 会撕裂写；
- key 未过滤 `..`，理论上可路径逃逸出 root；
- 定位是 demo 级实现，非生产 KV。

---

## 9. 知识注入与预算控制

### 9.1 模板原文

assistant.py:27-49：

```python
KNOWLEDGE_TEMPLATE_ZH = """# 知识库

{knowledge}"""

KNOWLEDGE_SNIPPET_ZH = """## 来自 {source} 的内容：

```
{content}
```"""
```

`format_knowledge_to_source_and_content`（assistant.py:52-78）把 retrieval 输出转为 `{source: '[文件](xx.pdf)', content: '...'}`，多 chunk 用 `\n\n...\n\n` 连接表示省略；非 JSON 输入整体作为 `{'source': '上传的文档', ...}` 一条。最终**追加到 system 消息末尾**（无 system 则新建一条插最前，assistant.py:140-148）。

### 9.2 max_ref_token 的三处生效点

全链路唯一的内容上限是 `max_ref_token`（默认 20000）：

1. `BaseSearch.call` 全文短路（base_search.py:80）；
2. `get_topk` 装入预算（base_search.py:111）；
3. `_get_the_front_part` 均分预算（base_search.py:167）。

注入 system 后**没有再做 token 校验**，超出 LLM 上下文由 LLM 层的 `DEFAULT_MAX_INPUT_TOKENS`（58000）截断机制兜底。

---

## 10. MemoAssistant：另一种"记忆"

`MemoAssistant`（agents/memo_assistant.py）是对话级长期记忆，与文档 RAG 是两个维度：

- 强制注册 `storage` 工具，MEMORY_PROMPT（memo_assistant.py:25-38）引导模型**自主决定**存什么（"你的记忆很短暂，请频繁的调用工具存储或读取重要对话内容"）；
- `_prepend_storage_info_to_sys`（:65-91）**回放历史消息里的 storage 调用记录**重建 KV 状态（不读数据库，从对话历史推导"当前记忆"），全量嵌入 system；
- 注释声称"仅保留最近三轮对话"，实际代码 `available_turn = 400`（:93-110）——**注释已过时**。

两者可叠加：MemoAssistant 本身是 Assistant 子类，`files` 参数仍走文档 RAG 链路。

---

## 11. 配置全表

`rag_cfg` dict 传入（`Memory(rag_cfg=...)` 或 `Assistant(rag_cfg=...)` 透传），默认值在 `qwen_agent/settings.py`（全文仅 39 行）：

| 参数 | 默认值 | 环境变量 | 作用 |
|------|--------|----------|------|
| `max_ref_token` | 20000 | `QWEN_AGENT_DEFAULT_MAX_REF_TOKEN` | 检索材料总 token 窗口 |
| `parser_page_size` | 500 | `QWEN_AGENT_DEFAULT_PARSER_PAGE_SIZE` | 单 chunk token 上限（也是缓存键一部分） |
| `rag_keygen_strategy` | `GenKeyword` | `QWEN_AGENT_DEFAULT_RAG_KEYGEN_STRATEGY` | 关键词生成策略，可选 `None` / `SplitQueryThenGenKeyword` / 两个 `WithKnowledge` 变体 |
| `rag_searchers` | `['keyword_search', 'front_page_search']` | `QWEN_AGENT_DEFAULT_RAG_SEARCHERS`（ast.literal_eval 解析） | 子检索器列表，>1 自动 RRF 融合 |
| —（LLM 层） | 58000 | `QWEN_AGENT_DEFAULT_MAX_INPUT_TOKENS` | 输入消息截断上限 |
| —（Agent 层） | 20 | `QWEN_AGENT_MAX_LLM_CALL_PER_RUN` | 单轮最多 LLM 调用次数 |
| —（工作区） | `workspace` | `QWEN_AGENT_DEFAULT_WORKSPACE` | 缓存/下载根目录 |

硬编码常量：`DEFAULT_FRONT_PAGE_NUM = 2`（front_page_search.py:24）、overlap 上限 150 字符（doc_parser.py:278）、RRF k=60（hybrid_search.py:53）。

---

## 12. 工程注意事项汇总

1. **docstring 与 settings 不一致**：memory.py:51 示例写 `max_ref_token=4000`、keygen 用 `SplitQueryThenGenKeyword`，实际默认 20000 / `GenKeyword`，以 settings 为准。
2. **缓存按路径 hash**：本地文件改了内容但路径不变，命中旧缓存。排查解析问题时先清 `workspace/tools/`。
3. **BM25 每次重建**：无持久化索引，大文档多轮问答的分词开销线性累积。
4. **中英混排走 jieba**：任一 CJK 字符触发中文路径，英文正则过滤被跳过。
5. **storage 非生产级**：无锁、可路径逃逸。
6. **MemoAssistant 注释过时**：声称保留 3 轮，实际 400 轮。
7. **get_topk 截断即 break**：预算不足时后面的小 chunk 没有机会递补。
8. **keygen 失败静默降级**：JSON 解析失败退回原始 query，不报错。

---

## 13. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `memory/memory.py` | 32 | `Memory(Agent)` 类 |
| `memory/memory.py` | 81 | `_run` 检索主流程 |
| `memory/memory.py` | 107-132 | keygen 动态加载与 text 补齐 |
| `agents/keygen_strategies/gen_keyword.py` | 26-42 | GenKeyword prompt 原文 |
| `agents/keygen_strategies/split_query.py` | 85-90 | stop 词省 token 技巧 |
| `agents/keygen_strategies/gen_keyword_with_knowledge.py` | 72-79 | 文档词表注入 |
| `tools/retrieval.py` | 72-77 | searcher 选择 / HybridSearch 包装 |
| `tools/doc_parser.py` | 32-53 | Chunk / Record 数据结构 |
| `tools/doc_parser.py` | 152-273 | `split_doc_to_chunk` 三段逻辑 |
| `tools/doc_parser.py` | 275-309 | `_get_last_part` 150 字符 overlap |
| `tools/search_tools/keyword_search.py` | 44-66 | BM25 打分 |
| `tools/search_tools/keyword_search.py` | 69-87 | WORDS_TO_IGNORE 停用词表 |
| `tools/search_tools/keyword_search.py` | 132-156 | string_tokenizer 中英文分流 |
| `tools/search_tools/keyword_search.py` | 169-196 | parse_keyword 三字段处理 |
| `tools/search_tools/hybrid_search.py` | 35-60 | RRF 融合（k=60） |
| `tools/search_tools/front_page_search.py` | 27-49 | 前 2 chunk +inf 保底 |
| `tools/search_tools/base_search.py` | 56-87 | call 分支与全文短路 |
| `tools/search_tools/base_search.py` | 107-137 | get_topk 装入 |
| `tools/search_tools/base_search.py` | 165-184 | _get_the_front_part 兜底 |
| `tools/extract_doc_vocabulary.py` | 52-82 | TF-IDF 词表提取 |
| `tools/storage.py` | 75-120 | KV 存储 API |
| `utils/tokenization_qwen.py` | 218-239 | count_tokens / truncate |
| `agents/assistant.py` | 27-49 | KNOWLEDGE 模板原文 |
| `agents/assistant.py` | 52-78 | 检索结果格式化 |
| `agents/fncall_agent.py` | 56-71 | mem_llm 推理模型特判 |
| `agents/memo_assistant.py` | 25-110 | 对话级长期记忆 |
| `settings.py` | 1-39 | 全部默认值与环境变量 |

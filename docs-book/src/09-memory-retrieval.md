# 分词与检索排序

本篇拆解 Memory 检索的排序内核：token 计数用的 QWenTokenizer、检索分词的中英文分流、BM25 打分、front-page 保底、RRF 融合，以及装入预算的三级兜底。

---

## 1. QWenTokenizer（token 计数）

`qwen_agent/utils/tokenization_qwen.py`：基于 tiktoken，但加载 Qwen 官方 BPE 词表 `qwen.tiktoken` 手工构造 `tiktoken.Encoding`（:93-98）。特殊 token 含 `<|endoftext|>`、`<|im_start|>`、`<|im_end|>` 及 205 个 `<|extra_i|>`（`SPECIAL_START_ID = 151643`）。

- `count_tokens(text)` = `len(tokenize(text))`（:218），tokenize 前做 NFC 归一化；
- `truncate(text, max_token, keep_both_sides=False)`（:221-239）：`keep_both_sides=True` 时中间插 `...`，左右各半；否则前截。

---

## 2. string_tokenizer：检索分词（中英文分流）

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

---

## 3. WORDS_TO_IGNORE：场景化停用词表

keyword_search.py:69-87 共约 200 个词：英文常规停用词（代词/系动词/介词）+ 标点 + 一批中文任务词：

```python
..., '的', '吗', '是', '了', '啊', '呢', '怎么', '如何', '什么', ...,
'讲了', '描述', '讲', '总结', 'summarize', '总结下', '总结一下', '文档', '文章',
'article', 'paper', '文稿', '稿子', '论文', 'PDF', 'pdf', '这个', '这篇', '这',
'我', '帮我', '那个', '下', '翻译', '说说', '讲讲', '介绍', 'summary']
```

这批"文档问答专用"停用词解释了为什么 **"总结一下"这类 query 会得到空词表**，从而走 front-part 兜底（见第 7 节）。

---

## 4. BM25 打分

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

---

## 5. parse_keyword：keygen JSON 三字段的用法

keyword_search.py:169-196：

- 非 JSON → 直接对原文 `split_text_into_keywords`；
- `keywords_zh` / `keywords_en`：lower 合并 → stem → 去停用词；
- `text`：走完整 `split_text_into_keywords` 后**追加**——SplitQuery 系策略把改写后的 query 塞这里的原因；JSON 缺 `text` 键会抛 KeyError 落入兜底，对原始 text 整体再分词。

---

## 6. front_page_search 与 RRF 融合

### 6.1 front_page_search：开头保底

front_page_search.py 全文 49 行。单文档时给前 2 个 chunk（`DEFAULT_FRONT_PAGE_NUM = 2`）打 `math.inf`：

```python
if max_ref_token >= page.token * DEFAULT_FRONT_PAGE_NUM * 2:  # 防止首页独占窗口
    chunk_and_score.append((doc.url, chunk_id, POSITIVE_INFINITY))
else:
    break
```

保证文档开头（标题/摘要/目录）必被注入；多文档直接返回空列表，不影响排序。

### 6.2 HybridSearch：RRF 融合

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

---

## 7. get_topk 与三级兜底

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

## 8. 可选：vector_search

`vector_search.py`（:24）用 langchain + DashScopeEmbeddings（text-embedding-v1）+ FAISS 做向量检索，需 `DASHSCOPE_API_KEY` 及额外依赖，**不在默认配置中**。加入 `rag_searchers` 列表后经 RRF 与 BM25 融合，可弥补关键词检索的同义词盲区。

# 09.2 检索与排序：BM25、RRF、front_page、三级兜底

> 本文聚焦 Memory 子系统的 **检索排序环节**：从用户问题（或 keygen 改写后的 JSON 串）到一批选中的 chunk。配套 [09 导览](./09-memory-deep-dive.md) 与 [09.1 keygen](./09-memory-keygen.md)。
> 节按 **决策点 / 代码证据 / 为什么不那样 / 踩坑清单** 四件套展开。所有行号基于 `main` 分支。`keyword_search.py` 指 `qwen_agent/tools/search_tools/keyword_search.py`。

## §1. 检索整体管线（一图）

```
caller 传入  query 字符串（可能是 keygen JSON 或纯文本）+ files
                    │
                    ▼
   BaseSearch.call (base_search.py:56)
     ├── format_docs：str → Record（旁路 DocParser，生产流不走）
     ├── if all_tokens <= max_ref_token: 全装，不检索  ← 第 1 级兜底
     └── else: 走 search
                    │
                    ▼
   HybridSearch.search（rag_searchers 长度为 2 时默认走）
     ├── KeywordSearch.sort_by_scores
     │     parse_keyword → wordlist
     │     BM25Okapi(k1=1.5,b=0.75) 拿 score
     │     按 score 降序返回  ← 决策见 §4 §5
     ├── FrontPageSearch.sort_by_scores
     │     if len(docs)>1: return []
     │     else 给前 2 个 chunk 打 POSITIVE_INFINITY  ← §3
     └── RRF 累加 1/(i+1+60)
          按 score 降序返回
                    │
                    ▼
   get_topk (base_search.py:107-137)
     按 sort 后顺序往装，预算 = max_ref_token
     单 chunk 超剩余预算 → 截断后停  ← 第 3 级
                    │
                    ▼
   [RefMaterialOutput(url, text=[snippets]), ...]
```

三层兜底（`base_search.call` 80 行 → `keyword_search.search` 35/42 行 → `get_topk` 127-128 处截断）分别对应不同失效模式 — 见 §6。

---

## §2. parse_keyword：query 是 JSON 字符串，要先 json5 解析

**决策点**：上游 keygen 把 query 改写成 `{"keywords_zh":[...], "keywords_en":[...], "text":"..."}` 的 JSON 字符串（见 09.1 §5），**retrieval 侧再去解析这个 JSON 把三路关键词合并**，而非定义结构化的 query 对象。

**代码证据**（`keyword_search.py:169-196`）：
```python
def parse_keyword(text):
    try:
        res = json5.loads(text)        # 解析 keygen 输出的 JSON
    except Exception:
        return split_text_into_keywords(text)   # 不是 JSON 就退化为纯文本分词

    _wordlist = []
    if 'keywords_zh' in res and isinstance(res['keywords_zh'], list):
        _wordlist.extend([kw.lower() for kw in res['keywords_zh']])   # ★ 不再分词
    if 'keywords_en' in res and isinstance(res['keywords_en'], list):
        _wordlist.extend([kw.lower() for kw in res['keywords_en']])
    _wordlist = stemmer.stemWords(_wordlist)                          # 跑 english stemmer
    wordlist = [x for x in _wordlist if x not in WORDS_TO_IGNORE]
    split_wordlist = split_text_into_keywords(res['text'])            # text 又走一遍 jieba
    wordlist += split_wordlist
    return wordlist
```

**为什么不那样**：另一条路是让 retrieval call 接收结构化对象而非 string。但这样会让 retrieval 的签名与 `BaseSearch.call(params: Union[str, dict])` 旧契约不一致——retrieval 内部 `_verify_json_format_args` 会序列化，再绕一圈进来。维持 string 形式保留了"query 永远是字符串"的概括不变。

**踩坑清单**：
- `'keywords_zh'` 是 raw lower，**不再 jieba 分词**。这是整个检索里最深的对齐错位（§4 单独讲）。
- `res['text']` 走 `split_text_into_keywords`，即 jieba + stemmer。**所以最终 BM25 wordlist 实际包含**：keywords_zh raw + keywords_en raw + text 切碎。当策略是 GenKeyword 时 text = 原 query；当策略是 SplitQueryThen* 时 text = split 后 information —— 不同策略下 wordlist 的"语义组成"不一样（见 09.1 §5 末段）。
- `except Exception: # TODO: This catch is too broad.` 在 `keyword_search.py:194-196`：res 中没有 `'text'` key 会直接 raise KeyError 被吞，**改返回 split_text_into_keywords(原 text) 的兜底**。如果 keygen 产物格式漂（如 `info` 而非 `text`），损失是静默的。

---

## §3. FrontPageSearch：单文档给前 2 个 chunk 打 inf

**决策点**：单文档且 token 窗口富余时给前 2 个 chunk 标 `POSITIVE_INFINITY`，让它在 RRF 融合里**强行排第一二位**。多文档场景主动放弃 — `return []`。

**代码证据**（`front_page_search.py:30-48`）：
```python
def sort_by_scores(self, query, docs, max_ref_token=DEFAULT_MAX_REF_TOKEN, **kwargs):
    if len(docs) > 1:
        # This is a trick for improving performance for one doc
        # It is not recommended to splice multiple documents directly, so return [], which will not effect the rank
        return []
    chunk_and_score = []
    for doc in docs:
        for chunk_id in range(min(DEFAULT_FRONT_PAGE_NUM, len(doc.raw))):
            page = doc.raw[chunk_id]
            if max_ref_token >= page.token * DEFAULT_FRONT_PAGE_NUM * 2:  # ★ 2 倍 page × 2 倍裕度
                chunk_and_score.append((doc.url, chunk_id, POSITIVE_INFINITY))
            else:
                break
    return chunk_and_score
```

`DEFAULT_FRONT_PAGE_NUM = 2`，所以判定是 `max_ref_token >= page.token × 2 × 2`，即**总预算 ≥ 4× 前 2 页 token** 才放行。对默认 `max_ref_token=20000`、单 chunk ~500 token 的常见情况几乎总成立；遇到大表格 chunk（4000 token）则立刻不满足。

**为什么不那样**：另一选择是"多文档也按每 doc 前 2 页置顶"，本系统拒绝 — 多文档时评不出谁的"前 2 页"更重要，与其污染排序不如不做。这是 RAG 工程里常见的"abstract / introduction 优先假设"，作者意识到该假设只在单文档下成立。

**踩坑清单**：
- `break` 是单 break——一旦 chunk_id=0 不满足，不再尝试 chunk_id=1。意味着开头是大表格时，紧随其后的小 chunk 也拿不到 inf。**显式优先连续性**。
- `HybridSearch` 融合时（§5），若同一 chunk 同两路都得 inf：`inf + inf = inf`（数值 `math.inf`，不影响排序比较）；但 vector_search 自定义实现时**慎用 inf**——若一个 search_obj 把所有 chunk 都打 inf，融合后所有分数都为 inf，会让 RRF 退化成"按 dict 遍历顺序的反向"，相当于没排序。
- 当 keygen 返回空 wordlist（query 被 WORDS_TO_IGNORE 全杀，如 query 是"总结"）时，`keyword_search.sort_by_scores` 行 49 直接 `return []`，触发 `keyword_search.search` 行 42 的兜底 `return self._get_the_front_part(...)` — 但**这条兜底不区分单/多文档**，多文档场景下"BM25 失败" 仍按各 doc 平均配额塞回前 chunk。这与 front_page_search 的多文档拒绝**不冲突**，但行为是**两套兜底并存**。

---

## §4. 中文关键词长短语 vs 文档侧 jieba 切碎：对齐错位

**决策点**：query 侧 `keywords_zh` 直接 lower 进 BM25，**不再 jieba**；文档侧 chunk content 用 `split_text_into_keywords`（jieba 切碎 + stem）建索引。

**代码证据**：
```python
# query 侧（keyword_search.py:181-185）
if 'keywords_zh' in res and isinstance(res['keywords_zh'], list):
    _wordlist.extend([kw.lower() for kw in res['keywords_zh']])   # raw lower 不切

# doc 侧（keyword_search.py:57-58）
from rank_bm25 import BM25Okapi
bm25 = BM25Okapi([split_text_into_keywords(x.content) for x in all_chunks])
# split_text_into_keywords → string_tokenizer → jieba.lcut → stemmer.stemWords
```

`BM25Okapi` 在 `rank_bm25` 库里默认用 raw token set 建索引和 query 比较；token 比较是精确字符串相等。`"论文摘要"` 这串在文档侧 jieba 切成 `['论文', '摘要']`，在索引里只有 `论文` 和 `摘要` 两个 token，**`"论文摘要"` 作为整串在 BM25 词汇表里完全不存在**。`get_scores` 对这个 token 项是 0 贡献。

**为什么不那样**：另一选择是 query 侧也 jieba 一次。但这会破坏 keygen 的设计意图——keygen 的 prompt 教模型产出"短词"（如 `["核心公式", "公式"]`）。再 jieba 切一刀 `"核心公式" → ['核心', '公式']`，会让"核心公式"和"核心" weight 不分。本系统选择**信任 keygen 的切词粒度**。

**踩坑清单**：
- LLM 不总是听话。当 keygen 产出 `keywords_zh: ["论文摘要"]` 这种 4 字短语时 hit rate = 0。**这是 BM25 上被 silently swallow 的 recall loss**。
- 修法两种：（a）`keywords_zh` 也过一次 `string_tokenizer`；（b）改 keygen prompt 强制要求"中文关键词切成单字或双字"。后者更可控，因为模型对短词判断更稳定。
- `stemmer.stemWords(_wordlist)` 在行 185 对 CJK 是 no-op，但**对中文短词也跑一遍**——纯浪费但不报错，profiling 时会看到 snowball 在 fire。
- 文档侧的 `split_text_into_keywords` 行 162-165 还做了一次 `WORDS_TO_IGNORE` 过滤（二次过滤，与 `string_tokenizer` 行 149 那次重叠）。**这是防御性双重过滤**，无害但表明这块代码来回改过。

---

## §5. RRF 融合公式：rank index 复用 + 常数 60 与 Cormack 标准一致

**决策点**：RRF 用 `1 / (rank + 1 + 60)`，`rank` 就是子搜索器**返回列表的下标**；常量 60 与 Cormack 2009 一致；额外用 `POSITIVE_INFINITY` 实现 front_page 强置顶是私有扩展。

**代码证据**（`hybrid_search.py:35-60`）：
```python
for chunk_and_score in chunk_and_score_list:        # 每个子搜索器返回的降序列表
    for i in range(len(chunk_and_score)):
        doc_id = chunk_and_score[i][0]
        chunk_id = chunk_and_score[i][1]
        score = chunk_and_score[i][2]
        if score == POSITIVE_INFINITY:
            chunk_score_map[doc_id][chunk_id] = POSITIVE_INFINITY
        else:
            # TODO: This needs to be adjusted for performance
            chunk_score_map[doc_id][chunk_id] += 1 / (i + 1 + 60)   # ★ rank index 作为权重

all_chunk_and_score.sort(key=lambda item: item[2], reverse=True)
```

**为什么不那样**：与"分数加权融合"对比——把 BM25 分 + front_page inf（如 score=100）+ vector cosine 加权。本系统拒绝，理由是 BM25 分与 cosine 量纲完全不可比，加权得调系数，工业上很难调准；RRF 用 rank index 把所有搜索器归一化到 [0, 1/61] 区间内，**只看顺序不看绝对分**，工程上立得住。

**踩坑清单**：
- `i + 1 + 60` 的 `i` 是子搜索器ър返回列表的下标，等价于 rank index。**隐式契约**：每个子搜索器 `sort_by_scores` 必须返回**全部 chunk 的降序完整 list**——返回不完整会丢 rank 信息，乱序会让 RRF 失真。新增 `your_searcher` 要遵守。
- 同一 chunk 在两路都得非 inf 时分数**累加**：`1/(i1+1+60) + 1/(i2+1+60)`，这意味着"黑马 chunk"在多路都排前列会比单路第一吸收更多分。这正是 RRF 的玄学。
- 同一 chunk 在两路都得 inf 时：`inf + inf = inf`（不会 NaN）——但**不是任意路都 inf 才 inf**，只要有一路 inf 它就是 inf（行 49-50 是 `if score == POSITIVE_INFINITY: = inf`，并非 `+=`）。这是对 front_page 强置顶的逻辑保护 — 只要 front_page 命中，无论另一路 BM25 把这个 chunk 排名第几都强置顶。
- `# TODO: This needs to be adjusted for performance`（行 52）是源码里明确标出的开放问题。Cormack 2009 论文的 60 是 IR 论文的经验值，对**只取 top-10 chunk**的 RAG 场景是否仍最优未实证；调到 30 能让前列排名差距拉大，调到 120 让分布变平。
- `sort` 是 stable：相同 RRF 分下 chunk 顺序由 `chunk_score_map` 的 dict 遍历顺序决定，而 `chunk_score_map` 是按 doc.url 初始化的——所以**多文档同时打 0 分的 chunk 顺序与文档 url 字典序挂钩**。

---

## §6. 三级兜底：全文够→BM25 选 top-k→按平均配额塞前部

**决策点**：retrieval 的装入预算有**三层兜底**，每一层走不同路径，分别对应三种失效模式：
- L1：全文 total token ≤ `max_ref_token` → 全装，不检索
- L2：BM25 拿到了非空结果 → 按 score 降序往装，单 chunk 超剩余预算截断后停
- L3：BM25 全 0 分（query 词全没命中）→ `_get_the_front_part` 按 doc 平均配额从头塞

**代码证据**：

L1（`base_search.py:78-87`）：
```python
new_docs, all_tokens = self.format_docs(docs)
logger.info(f'all tokens: {all_tokens}')
if all_tokens <= max_ref_token:
    logger.info('use full ref')
    return [RefMaterialOutput(url=doc.url, text=[page.content for page in doc.raw]).to_dict() for doc in new_docs]
return self.search(query=query, docs=new_docs, max_ref_token=max_ref_token)
```

L2（`base_search.py:107-137`，`get_topk`）：
```python
available_token = max_ref_token
...
for doc_id, chunk_id, _ in chunk_and_score:
    if available_token <= 0:
        break
    page = docs_map[doc_id].raw[chunk_id]
    if docs_retrieved[doc_id].text[chunk_id]:
        continue                                # 已选过跳过
    if available_token < page.token:
        docs_retrieved[doc_id].text[chunk_id] = tokenizer.truncate(page.content, max_token=available_token)
        break                                    # ★ 截断后立即停，后续高分 chunk 被丢
    docs_retrieved[doc_id].text[chunk_id] = page.content
    available_token -= page.token
```

L3（`base_search.py:165-184`，`_get_the_front_part`）：
```python
@staticmethod
def _get_the_front_part(docs, max_ref_token=DEFAULT_MAX_REF_TOKEN) -> list:
    single_max_ref_token = int(max_ref_token / len(docs))
    _ref_list = []
    for doc in docs:
        available_token = single_max_ref_token
        text = []
        for page in doc.raw:
            if available_token <= 0:
                break
            if page.token <= available_token:
                text.append(page.content)
                available_token -= page.token
            else:
                text.append(tokenizer.truncate(page.content, max_token=available_token))
                break
        _ref_list.append(RefMaterialOutput(url=doc.url, text=text).to_dict())
    return _ref_list
```

**为什么不那样**：另一选择是"统一搜一遍 BM25，把 0 分的 chunk 也照样按 sort 顺序塞"。本系统拒绝："0 分" 表示**完全不命中**，按分数塞任何次序都是噪声；不如按平均配额切头部，至少给 LLM 一个"每篇稍有内容"的视角。这是直觉性而不是数据驱动的选择。

**踩坑清单**：
- **L1 没有段数上限**。理论上单 doc 1 万个段落只要总 token 没超就全塞。极端 case：单 doc 19999 token 切成 40 个 chunk——`all_tokens=19999 ≤ 20000`，L1 直接全塞，**不走检索**。意味着 BM25 白干（虽然这次根本没调 BM25），下次同 query 更多 doc 才走 BM25。**L1 是 "all-or-nothing"，没有 partial fall-through**。
- L2 行 127-128 的"截断后 break"：剩 600 token，下一个高分 chunk 是 1500 token → 截到 600 后立即停。**后续更高 rank 但更小的 chunk 不会进来填充**——丢预算。`# include them with truncation` 的 TODO 也写在源码里有暗示，未实现。
- L3 `_get_the_front_part` 在多 doc 时按 `single_max_ref_token = max_ref_token / len(docs)` 平均分配，但**短的 doc 节省下来的预算不会借给长 doc** — 单 doc 用完后只是 break，剩余预算浪费。这意味着 1 个长 doc + 9 个短 doc 的场景下，9 个短 doc 凑不满分配，长 doc 也只拿 1/10。
- 进入 `search()` 的前置：L1 判断为否 → 调用 `self.search`（`KeyWordSearch.search`）。`KeywordSearch.search` 行 34-42 还有一个额外兜底：`chunk_and_score` 为空 或 `max_sims == 0` 时也走 L3。所以实际是 **L1 → L2 → L3 三层 → (额外兜底) L3** — L3 被两层都可能触发。

---

## §7. 三级兜底与 retrieval.call 早期的 `if not query:` 兜底并存

**决策点**：`base_search.call` 里还有一个**比 L1 更早**的兜底 — `if not query: return self._get_the_front_part(...)`。这条与 L3 复用同一个函数但触发条件不同。

**代码证据**（`base_search.py:74-87`）：
```python
if not docs:
    return []
if not query:
    return self._get_the_front_part(docs, max_ref_token)   # ★ 比 L1 还早，query 是空串直接塞前部
new_docs, all_tokens = self.format_docs(docs)
...
if all_tokens <= max_ref_token:
    ...                                                    # 这里是 L1
```

**为什么不那样**：`if not query` 这条对应 caller 端 query 为空的两类来源：（a）`memory.py:103` `messages[-1].role != USER` 时 query 留空；（b）keygen 失败兜底把 query 设为空（虽然实际 `memory.py:132` 是 `query = query` 原值不空，但理论上存在空 query 路径）。在这种情况下"什么都不检索"是错的，**至少给 LLM 看到文件存在 + 前部内容**，便于 LLM 自己识别"用户没具体问题"但仍能回答概述性问题。

**踩坑清单**：
- 这条会让"最后一条非 user"的对话场景进入无 query 全兜底，结果**每 doc 都按平均配额塞前部**注入 prompt。如果用户问"刚才说的那篇论文的核心是什么"（最后是 assistant 说了一段，user 问"继续"，触发此路径），最终注入的是不同 doc 的前 chunk，**完全可能不相关**。这是 09.1 §6 讲的"多轮指代陷阱"的检索侧表现。
- `chunk_and_score` 为空时（query 词全被 WORDS_TO_IGNORE 清光，如 query="总结下"），`keyword_search.search` 行 34-35 进 L3。与 L1 早期兜底共用 `_get_the_front_part`，**但触发链不同**：L1 是 retrieval.call 早期就触发，BM25 根本没跑；keyword_search.search 回退则是 BM25 跑了拿到全 0 才触发。**性能差异：前者省了 BM25**。
- 调 BM25 也算计费：长 chunk × 大 doc 数时，`BM25Okapi(...)` 构造一次会做 IDF 计算和 term-document matrix，list 已构造好的 chunk 来说也得 O(N×M)。在生产实例上对一个长 PDF 多次 query 时 BM25 重构是个隐藏 hot path。

---

## §8. 切块和缓存的简单结论

详细推导见扁平版；本节列两条可直接拿走的事实：

- **切块按 token，默认 500**，整 doc total token ≤ max_ref_token 不切做成单大 chunk。
- **跨 chunk overlap 用字符 = 150，hard-coded 在 `_get_last_part`**；跨页不 overlap。chunk budget 用 token、overlap 用 char，**这是两套独立计量**，中英混排时 overlap 占 chunk 大小波动 10%~40%。

缓存键两条（`doc_parser.py:106-150`）：
```python
cached_name_chunking = f'{hash_sha256(url)}_{str(parser_page_size)}'      # 走切块
cached_name_chunking = f'{hash_sha256(url)}_without_chunking'             # 不切块（整 doc 一 chunk）
```
**`max_ref_token` 不入键**——但 `_without_chunking` 与 `_{parser_page_size}` 是两个独立 string key，不会互相命中。**真正的坑是调小 parser_page_size 后旧 cache 不会被清理**，storage 是文件级 append-only，要运维定期清。

同一文件两个 URL 表达形式（`a.pdf` 与 `file:///.../a.pdf`）→ `hash_sha256` 不同 → 两条独立 cache。retrieval 内 `docs_map[doc.url]` 用 url 作 key 去重，但**不会规范化路径**，所以会重复解析、且在 `chunk_score_map` 里成两条独立 chunk 集合。caller 必须保证 url 规范化。

---

## §9. 检索侧的"数据库"：每次都重建 BM25 与 FAISS

**决策点**：BM25 每次 retrieval 都**临时构造 `BM25Okapi`**——不缓存 BM25 索引；vector_search（默认未启用）同样每次 `FAISS.from_documents` 构建。

**代码证据**：
```python
# keyword_search.py:57-59
from rank_bm25 import BM25Okapi
bm25 = BM25Okapi([split_text_into_keywords(x.content) for x in all_chunks])
doc_scores = bm25.get_scores(wordlist)
```
`all_chunks` 是从 `doc.raw`（已经在 DocParser 缓存里）临时组装。**即使 DocParser 缓存了 Record，BM25 也是每次重新构造的** — IDF 计算 + inverted index 在每次 call 都算一遍。

`vector_search.py:58` 同理 — 每次构造 `FAISS.from_documents(all_chunks, embeddings)`，连向量都被重新计算。源码 `vector_search.py:24` 注释 `# TODO: Optimize the accuracy of the embedding retriever.`。

**为什么不那样**：BM25 缓存键该用什么？`(doc_url_list, parser_page_size, max_ref_token)`——多变量 key，命中条件极窄。Python 层还要维护这个缓存防止内存膨胀，得不偿失。选择"每次重算"换取代码简单度与低内存：长 PDF 场景里 BM25 重算时间约 50-200ms，可接受。

**踩坑清单**：
- **生产长跑实例上 BM25 重算是 hot path**。如果 caller 在循环里反复用同 query+同 docs，应在 caller 侧做 (query, sorted(files)) 缓存。
- 同一文件 URL 不同表达形式（绝对路径 vs `file://`）→ `hash_sha256` 不同 → 两条独立 DocParser cache；`docs_map[doc.url]` 也不会规范化 → 同一文件以两个 url 进入 retrieval 时 BM25 会把它当两份独立 doc 算 IDF — **IDF 分布被稀释**，单文件信息密度被低估。
- toString 个 chunk 的 `split_text_into_keywords` 在 `[split_text_into_keywords(x.content) for x in all_chunks]` 里，每个 chunk 都跑 jieba 分词。jieba 第一次 `import` 会加载默认词典（~3 MB 内存，~1秒 initialize），但单例 — 内存只占一次；CPU 在每次 BM25 构造时被分词吃掉一截。
- 自定义 search_obj 接入时记得：1) `@register_tool('your_name')`；2) 实现 `sort_by_scores` 返回完整降序列表（RRF 依赖 rank index）；3) 不能含 `hybrid_search` 自身（`hybrid_search.py:31-32` 会 raise 防递归）。

---

## §10. 检索侧的开放 TODO 汇总

| #  | TODO 描述                            | 位置                        |
|----|--------------------------------------|-----------------------------|
| 1  | RRF 常数是否需要调优                 | hybrid_search.py:52         |
| 2  | vector_search 性能优化 / 索引持久化  | vector_search.py:24         |
| 3  | parse_keyword 的 broad except 太宽   | keyword_search.py:195       |
| 4  | get_topk 是否应该 partial fall-through | base_search.py:127 注释暗示 |

## §11. 调检索之前的检查清单

- [ ] 改 `max_ref_token` 默认：要在测试里加 case 覆盖"调大后命中 _without_chunking 缓存、再调小是否仍正确切块" — 实际两套 key 不串，但要回归测
- [ ] 新增子搜索器：实现 `sort_by_scores` 返回**全部 chunk 的降序完整 list**；不能自我引用 `hybrid_search`
- [ ] 启用 `vector_search`：环境变量 `DASHSCOPE_API_KEY` 必备；首次拉模型；每次 query 重建 FAISS 是 hot path，长跑实例要警觉
- [ ] 多 retrieval 配置切换：caller 必须保证同一文件 URL 形式稳定，否则 BM25 IDF 会被同文件多 url 表达稀释
- [ ] 改 chunk overlap：`_get_last_part` 的 `available_len=150` hard-coded，要 fork 改源码；注意跨页不 overlap 的逻辑
- [ ] 调小 parser_page_size 后磁盘膨胀：需要清 workspace tools 目录（Storage 不自动过期）

完。

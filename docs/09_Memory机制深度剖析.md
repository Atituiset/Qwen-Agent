# 09 Memory 机制深度剖析：从分块到 BM25 的原码级拆解

> 本文是 [05 Memory 与 RAG](./05_Memory与RAG.md) 的姊妹篇。05 讲**架构与流程**，本文只回答一件事：**为什么是这样做、不这样做会怎样、改了会踩什么坑**。
> 所有行号基于仓库 `main` 分支；下文反复出现的 `memory.py` 指 `qwen_agent/memory/memory.py`，`keyword_search.py` 指 `qwen_agent/tools/search_tools/keyword_search.py`。

读完本文应能回答的 8 个判断题：

1. 用户没传 llm 时，keygen 是被跳过还是降级？（§1）
2. 多轮对话里 "它/上一篇" 这类指代，检索会被还原吗？（§2）
3. `keywords_zh: ["论文摘要"]` 这种 LLM 输出的长中文短语，在 BM25 上能命中吗？（§3）
4. 多文档场景下"前 2 个 chunk 强置顶"还生效吗？（§4）
5. 第一次以大 `max_ref_token` 取了整块，第二次调小 max_ref_token 会不会复用旧缓存导致错分？（§5）
6. `parser_page_size` 与 overlap 的 150 是同一个度量吗？（§6）
7. BM25 的 k1/b 是不是自实现的、有没有非标准改造？（§7）
8. 同一份内容两个不同 URL（绝对路径 vs file://），DocParser 会重复解析吗？（§8）

---

## §1. llm 缺失时不降级而是整体跳过 keygen

**决策点**：用户未配 LLM 时，keygen 策略被强制改 `none`，**整条 LLM 改写链路直接关掉**，retrieval 拿到的是抽象过的原 query 字符串。

**代码证据**：
```python
# memory.py:60-63
self.rag_keygen_strategy = self.cfg.get('rag_keygen_strategy', DEFAULT_RAG_KEYGEN_STRATEGY)
if not llm:
    # There is no suitable model available for keygen
    self.rag_keygen_strategy = 'none'
```
判断条件是 `not llm`——传入 `None`/`''`/空 dict 都会触发。

**为什么不那样**：另一个显而易见的设计是"无 LLM 时降级用本地分词抽关键词"。本系统没选这条，因为检索分词这块在 `keyword_search.parse_keyword` 已经对纯文本做了 `split_text_into_keywords`（jieba + 英文 stem）。也就是说"无 LLM 时把原 query 直接送给 BM25"和"本地分词抽关键词"在行为上几乎是同一件事，再额外实现一套本地 keygen 是重复造轮子。

**踩坑清单**：
- `settings.py:34-36` 的 `DEFAULT_RAG_KEYGEN_STRATEGY` 类型标注里写的是大写 `'None'`，运行期比较是 `self.rag_keygen_strategy.lower() != 'none'`——大小写都吃，但代码里别处 `getattr(module, self.rag_keygen_strategy)` 又是大小写敏感的，**显式配 `'none'` 走跳过路径 OK，配 `'None'` 走 import_module 路径直接 AttributeError**。配置命名要保持全小写。
- 若用 Assistant 时显式传 `llm=""`（空字符串），同样会走进 `not llm` 分支；这与你"已经传了 LLM config 字典但 model 字段空"的语义是冲突的——这不是 bug，但容易踩。

---

## §2. 多轮对话的指代无法被 keygen 还原

**决策点**：query 只取**最后一条 user 消息**，且 keygen 也只看这一条——历史完全丢弃。

**代码证据**：
```python
# memory.py:101-104
query = ''
# Only retrieval content according to the last user query if exists
if messages and messages[-1].role == USER:
    query = extract_text_from_message(messages[-1], add_upload_info=False)

# memory.py:112
response = keygen.run([Message(USER, query)], files=rag_files)   # 仅 1 条 user message
```

**为什么不那样**：业界多数 RAG 会做 multi-turn query rewrite（"用最近 N 轮拼一段 query"，或单独训一个 query rewrite 模型）。本系统没做：一次 LLM 调用就够了，叠加 rewrite 要么多耗一次 LLM，要么把历史塞进 query prompt 让 keygen LLM 兼职 rewrite——后者对 keygen 的 prompt 模板会形成耦合，得不偿失。

**踩坑清单**：
- 用户第二轮问"它的作者是谁？"，第一轮提到一个论文：检索基本零召回。**已知短板**，靠 `SplitQueryThenGenKeyword` 也救不回来——它只做**单问题内部**的 information/instruction 拆分，不跨轮。
- 最后一条非 user（assistant ask、tool response）时 `query=''`，会跳过 keygen 直奔 retrieval 的"无 query"分支（见 §4），实际命中率显著下降；在 tool call 流水线里这是要警觉的中间状态。
- 工程上的补救只能放在 caller 侧：caller 在调 Memory 前自己把多轮指代展开成完整 query 再注入。

---

## §3. LLM 抽出的中文长短语在 BM25 上基本命不中

**决策点**：query 侧 `keywords_zh` 直接 lowercase 后进 BM25，**不再 jieba 切分**；但文档侧每个 chunk 都被 `split_text_into_keywords`（jieba + stemmer）切碎过。

**代码证据**：
```python
# keyword_search.py:181-185（query 侧）
if 'keywords_zh' in res and isinstance(res['keywords_zh'], list):
    _wordlist.extend([kw.lower() for kw in res['keywords_zh']])   # raw lower only
...
_wordlist = stemmer.stemWords(_wordlist)                          # 跑 english stemmer（对中文无害但浪费）

# keyword_search.py:58（doc 侧）
bm25 = BM25Okapi([split_text_into_keywords(x.content) for x in all_chunks])
# split_text_into_keywords 内部走 string_tokenizer → jieba.lcut → stemmer
```

**为什么不那样**：另一条路是对 `keywords_zh` 也再 `jieba.lcut` 一次。这会让"作者"被切成 [`作`, `者`]`，反而把短词打散；`keyword_search` 选择信任 LLM 切词粒度，拿整串去比。文档侧必须切（chunks 是大段文本），这是无奈，不是设计。

**踩坑清单**：
- 当 LLM 产出 `keywords_zh: ["论文摘要"]` 这样的多字短语，BM25 文档词表里它是两个词 `论文 / 摘要`，`"论文摘要"` 这个 token 在 BM25 索引里**完全不存在，得 0 分**。这不是 noise，是被 silently swallow 的 recall loss。
- 修法有两种：（a）`keywords_zh` 也过一次 `string_tokenizer`；（b）让 keygen prompt 强制要求"中文关键词切成单字或双字"——后者更可靠，因为模型对短词更可控。
- 与之镜像的另一个错位：`keywords_en` 跑 `stemmer.stemWords`，但 jieba 切过的中文短词**也跑同一个 english stemmer**（行 154-156），整段 `stemmer.stemWords` 把 list 一起喂进去——对 CJK 字符是 no-op，但不会报错。这不是 bug，是浪费。

---

## §4. front_page_search 给单文档前 2 个 chunk 打 inf 分强置顶

**决策点**：只在**单文档**且 token 窗口富余时，把前 2 个 chunk 标 `POSITIVE_INFINITY`，在 RRF 融合里强制按第 1、2 位返回；**多文档场景主动放弃**这个激励。

**代码证据**：
```python
# front_page_search.py:35-47
if len(docs) > 1:
    # It is not recommended to splice multiple documents directly, so return [], which will not effect the rank
    return []

chunk_and_score = []
for doc in docs:
    for chunk_id in range(min(DEFAULT_FRONT_PAGE_NUM, len(doc.raw))):
        page = doc.raw[chunk_id]
        if max_ref_token >= page.token * DEFAULT_FRONT_PAGE_NUM * 2:   # 前两页 token × 2 才放行
            chunk_and_score.append((doc.url, chunk_id, POSITIVE_INFINITY))
        else:
            break                                                       # 单 break 不 continue
```

**为什么不那样**：另一个设计是"多文档也把每个文档前 2 页置顶"，本系统拒绝了——多文档时评不出谁的前 2 页更重要，与其污染排序不如不做。这是 RAG 工程里常见的"开篇优先假设"（abstract / introduction 出现在文档前 chunk），但作者意识到该假设只在单文档下成立。

**踩坑清单**：
- 条件 `max_ref_token >= page.token * DEFAULT_FRONT_PAGE_NUM * 2`，要求总预算至少够 4 倍前 2 页 token。对 `DEFAULT_MAX_REF_TOKEN=20000`、单 chunk ~500 token 量级，几乎总能命中；但**遇到大 chunk（如全表格 4000 token 一段）就不满足**，开篇激励自然失效。
- `break` 是单 break——一旦 chunk_id=0 不满足，不再尝试 chunk_id=1。意味着开头是个大表格时，紧随其后的小 chunk 也拿不到 inf。这是显式选择：连续性优先。
- 在 `HybridSearch` 融合时（§7），inf 与 RRF 累加同 chunk 仍是 inf，但**两路都给 inf 会变成 `2*inf=inf`**，数值比较不受影响，但 playground 调试时会看到 `inf` 字样而非具体分。
- 当 keygen 返回空 wordlist（query 被 stop word 全杀）时，`keyword_search.sort_by_scores` 行 49 直接 `return []`，触发 base_search 的 `_get_the_front_part` 兜底——**这条兜底不区分单/多文档**，所以多文档场景下"开篇全是 BM25 失败" 仍会按各 doc 平均配额把每 doc 的前 chunk 塞回来。这与 front_page_search 的多文档拒绝不冲突，但行为上是**两套兜底**。

---

## §5. DocParser 缓存键不含 max_ref_token，可能错读"整块 vs 切块"

**决策点**：缓存键是 `hash_sha256(url)_{parser_page_size}` 或 `hash_sha256(url)_without_chunking`，**`max_ref_token` 不入键**。

**代码证据**：
```python
# doc_parser.py:106-150（关键行）
cached_name_chunking = f'{hash_sha256(url)}_{str(parser_page_size)}'
...
if total_token <= max_ref_token:
    # The whole doc is one chunk
    cached_name_chunking = f'{hash_sha256(url)}_without_chunking'    # 切换到另一个键
    ...
    content = [{'page_num': 0, 'content': [{'text': <full>, 'token': total_token}]}]
    ...
else:
    content = self.split_doc_to_chunk(doc, url, ..., parser_page_size=parser_page_size)

self.db.put(cached_name_chunking, new_record_str)
return new_record
```

**为什么不那样**：把 `max_ref_token` 也入键会让缓存键空间翻倍——同一个 url 在不同 `max_ref_token` 下要存多份。本系统的选择是：键固定为 (url, parser_page_size)，把 `max_ref_token` 当成"是不是要分块"的边界，分块结果只缓存一份。这假设 `max_ref_token` 在一个项目周期内稳定不变——对多数 RAG 场景成立。

**踩坑清单**：
- **第一次**用 `max_ref_token=50000` 命中 `_without_chunking` 路径，整 doc 存为 1 个大 chunk；**第二次**用 `max_ref_token=5000`，代码走到行 124，**按 doc total_token 判断不会命中上次的 `_without_chunking` cache**——`_without_chunking` 和 `_parser_page_size` 是两个独立 string key，不会串。但本认知曾被 explore 的初稿误描述成 bug——这里行号确认是**两套独立缓存键不会串**，不是真 bug。
- 真正的 pitfall：**调小 parser_page_size 后旧 cache（大 page_size 切出的）不会被清**。Storage 是文件级 append-only 的（§9），用户改 `parser_page_size` 后旧文件留存，磁盘膨胀。如果 workspace 是只读或长跑实例，需要运维侧定期清 cache 目录。
- 同一文件 URL 形式不同（绝对路径 `a.pdf` vs `file:///a.pdf`）→ `hash_sha256` 不同 → 两条独立 cache。但 retrieval 在 base_search 层用 `docs_map[doc.url]` 做去重；如果同一文件以两个不同 url 进入 retrieval，**会重复解析、重复塞回 prompt 同一段内容**。caller 端必须保证 url 规范化。

---

## §6. 切块按 token=500，overlap 按 char=150 — 两条独立计量体系

**决策点**：chunk 容量预算用 token，跨 chunk 的 overlap 用字符长度，**两条不共用度量**。

**代码证据**：
```python
# doc_parser.py:152-205（split_doc_to_chunk 核心）
parser_page_size = DEFAULT_PARSER_PAGE_SIZE   # 默认 500 token（settings.py:32-33）
available_token = parser_page_size
for page in doc:
    ...
    if token <= available_token:
        available_token -= token
        chunk.append([txt, page_num])
    else:
        ...
        overlap_txt = self._get_last_part(chunk)   # overlap 来源
        if overlap_txt.strip():
            chunk = [f'[page: {str(chunk[-1][1])}]', overlap_txt]
            available_token = parser_page_size - count_tokens(overlap_txt)

# doc_parser.py:275-309（_get_last_part 的字符预算）
available_len = 150                              # hard-coded char length
for i in range(len(chunk) - 1, -1, -1):
    if chunk[i][1] != need_page:
        return overlap                            # 跨页立刻停
    ...
```

**为什么不那样**：另一选择是把 overlap 也按 token 算（cropping to N token）。本系统选 char 是因为：overlap 关心的是"读者衔接"，对 LLM 不需要严格对齐到 BPE；用字符更便于按段落规模控制，不依赖 tokenizer 加载。代价是中英混排时 token/char 比不稳定——中文 1 字≈1 token，英文 1 词≈1.3 token 但 ≈5 char，所以 overlap 在英文段更长。

**踩坑清单**：
- 中英混排文档（例如论文含中英 abstract）时，overlap 的 token 占比波动在 10%~40%。chunk budget 500 token / overlap 150 char：纯中文段 token-overlap ≈ 30%；纯英文段 ≈ 10%。
- 跨页**完全不 overlap**（行 282-283：`chunk[i][1] != need_page: return overlap`）——意味着页边切割处零冗余，对 PDF 类强结构文档是合理取舍，但对单页长文档（一页 = 数千 token）这种切割点会显著断语义。
- `available_len` 是 hard-coded 150，没有从 cfg 注出的口子——若要按文档类型调（parsing 大表格 vs 论文），目前只能 fork 改源码。
- `is_overlap` 没有公开 API；caller 无法知道某个 chunk 含多少比例的 overlap 文本——这影响 chunk-content 加权打分时去重的策略（如果调用方在 retrieval 之外做 weight refinement）。

---

## §7. BM25 用外库默认 k1=1.5 / b=0.75，RRF 常数 60 与 Cormack 标准一致

**决策点**：BM25 不自己实现，直接用 `rank_bm25.BM25Okapi` **库默认参数**；RRF 融合常数 `60` 与 Cormack 2009 标准一致；额外的 `POSITIVE_INFINITY` 是 front_page 强置顶的私有扩展。

**代码证据**：
```python
# keyword_search.py:57-63
from rank_bm25 import BM25Okapi
bm25 = BM25Okapi([split_text_into_keywords(x.content) for x in all_chunks])   # 默认 k1=1.5, b=0.75, epsilon=0.25
doc_scores = bm25.get_scores(wordlist)
chunk_and_score = [...]
chunk_and_score.sort(key=lambda item: item[2], reverse=True)

# hybrid_search.py:35-60（RRF 融合）
for chunk_and_score in chunk_and_score_list:
    for i in range(len(chunk_and_score)):
        ...
        if score == POSITIVE_INFINITY:
            chunk_score_map[doc_id][chunk_id] = POSITIVE_INFINITY
        else:
            # TODO: This needs to be adjusted for performance
            chunk_score_map[doc_id][chunk_id] += 1 / (i + 1 + 60)        # rank(i) + 1 + 60
```

**为什么不那样**：BM25 调自定义 k1/b 是检索系统工程里常见但收效甚微的优化——k1=1.5/b=0.75 是经验上对中等长度文档（200~800 token）泛化最好的默认。本系统选择不调；是否改 k1 是可观察的实验题，不是设计题。RRF 常数 60 同理，作者也明确标了 TODO 注释。

**踩坑清单**：
- `i + 1 + 60` 的 `i` 是 chunk 在子搜索器**返回列表中的位置**，等价于 rank index。这隐式要求每个子搜索器 `sort_by_scores` 必须按 score 降序返回完整列表——不返回完整列表或乱序会导致 RRF 失真。`vector_search`（默认未启用）用 FAISS 同样要遵守这个契约。
- RRF 是**累加**而非合取。同一 chunk 在 keyword_search 列 rank=0、front_page_search 列 rank=0：分数 = `inf + 1/61`（依然 inf）——但若 isInfinite 路径被屏蔽，则会 = `1/61 + 1/61 ≈ 0.0328`。要在自定义子搜索器中慎练使用 inf。
- `sort` 是 stable，相同 RRF 分下保留 dict 遍历顺序——`chunk_score_map` 用 dict 仍按 url 顺序遍历，多文档同时打 0 分的 chunk 顺序**与文档 url 字典序挂钩**，调用方应保证 url 顺序可控。
- 自定义子搜索器接入时记得 `from qwen_agent.tools.base import TOOL_REGISTRY, register_tool`，并 `@register_tool('your_searcher')`；同时 **HybridSearch 在初始化时会 raise 自包含检查**（`hybrid_search.py:31-32`：`if self.name in self.rag_searchers: raise ValueError`）防止无限递归。

---

## §8. 不支持的文件类型被静默丢弃，无报错

**决策点**：`memory.get_rag_files` 把不在白名单的文件**默默过滤**掉，不抛异常，也不写日志；用户问"为什么没检索到 X.jpg"时系统不会主动提示。

**代码证据**：
```python
# memory.py:146-153
def get_rag_files(self, messages: List[Message]):
    session_files = extract_files_from_messages(messages, include_images=False)
    files = self.system_files + session_files
    rag_files = []
    for file in files:
        f_type = get_file_type(file)
        if f_type in PARSER_SUPPORTED_FILE_TYPES and file not in rag_files:
            rag_files.append(file)
    return rag_files

# simple_doc_parser.py:368
PARSER_SUPPORTED_FILE_TYPES = ['pdf', 'docx', 'pptx', 'txt', 'html', 'csv', 'tsv', 'xlsx', 'xls']
```

**为什么不那样**：另一选择是直接 raise 或至少 warn。本系统不做的原因：在 chat 流水线里，未支持的文件可能是模型 100% 走 multimodal image 路径处理的（如图片走 raw image），不是 retrieval 对象。强制报错会破坏多模态链路。

**踩坑清单**：
- `include_images=False` 是 memory 这一处的显式选择（行 147）。在 `fncall_agent.py:117` 的 tool 文件提取处是 `include_images=True`——两条路径对图片处理不同：memory 丢弃，tool 调用保留。混传场景下图片不会进 RAG 但会被 tool 直接消费，行为分裂。
- `memory.py:86` 的 docstring 列出的 9 种类型**顺序与源码实际不一致**（注释里把 html 提前到第六位，实际列表里 html 在 txt 之后）。这是文档腐化迹象，应以 `simple_doc_parser.PARSER_SUPPORTED_FILE_TYPES` 为准。
- 去 `file not in rag_files` 是**字符串相等**比较，不规范化路径：`a.pdf` 与 `./a.pdf` 会被当两个不同文件，retrieval 各自解析一遍（storage 端用 hash url 缓存，所以解析不重复，但 retrieval 内 `chunk_score_map` 会有两条独立 chunk 集合）。
- `get_file_type`（`utils.py:242-271`）对扩展名为 `.txt` 和 `.html` 做**内容识别**——本地无扩展名文件会读首若干字节判断是否含 html_tags，HTTP URL 走 HEAD 看 content-type。这意味着 `.xml` 文件会被映射到 `txt` 进 retrieval、`.png` 落到 `unk` 被丢——如果有人误把图片改名为 `.txt` 期待它进 RAG，会发生什么取决于 `contains_html_tags` 的判定结果。

---

## §9. 图层关系：MCP 工具之间通过 retrieval 单入口串联

**决策点**：Memory 不直接调 `doc_parser`，而是把 `doc_parser` 注入 retrieval 工具的内部依赖；Memory 仅调 retrieval。

**代码证据**：
```python
# memory.py:65-77
function_list = function_list or []
super().__init__(function_list=[{
    'name': 'retrieval',
    'max_ref_token': self.max_ref_token,
    'parser_page_size': self.parser_page_size,
    'rag_searchers': self.rag_searchers,
}, {
    'name': 'doc_parser',
    'max_ref_token': self.max_ref_token,
    'parser_page_size': self.parser_page_size,
}] + function_list, ...)

# memory.py:134
content = self.function_map['retrieval'].call({...}, **kwargs)

# retrieval.py:99-101
for file in files:
    _record = self.doc_parse.call(params={'url': file}, **kwargs)  # retrieval 内部按需解析
    records.append(_record)
```

**为什么不那样**：Memory 是个 Agent（继承 `Agent`），但实际只调用一个子工具——直接组合 `doc_parser + search` 会让 Memory 失去 `Agent` 抽象给的好处（function 之间的标准化 message yield 与 error handling）。也没把 retrieval 合并进 Memory，是为了在多 Agent / 多 retrieval 场景里方便替换 retrieval 实现或挂载并发搜索器。

**踩坑清单**：
- Memory 实例化时的 cfg 同时传给 retrieval 和 doc_parser（行 66-75 两处）；若希望两个工具用不同的 `parser_page_size`，**当前不支持**——同一 cfg 注入两份。
- `**kwargs` 透传给 retrieval 的 call（行 134-140），但 base_search.call（`base_search.py:69`）只用 `max_ref_token` 这一项 kwargs。若 caller 在 kwargs 里塞了别的（如 `lang`），会被 retrieval 上层 `_verify_json_format_args` 忽略；除非 `**kwargs` 命中 base_search 内某个参数名才被取——目前 base_search 只取 `max_ref_token`，其他 kwargs 全逐渐失吸收。
- retrieval 实例化时如果 `rag_searchers` 长度=1，直接用单一 search 而非 HybridSearch（`retrieval.py:73-76`）；调成多 search 会自动走 hybrid。**默认配置 `['keyword_search', 'front_page_search']` 长度=2**，所以默认走 hybrid——很多用户不开 front_page_search 的预期可能是默认不开的，实则默认开。

---

## §10. 完整组件关系图与设计锚速查

```
                    ┌─────────────────────────────────────────┐
                    │  Memory._run (memory.py:81)             │
                    │    ├ get_rag_files ─→ PARSER 白名单过滤   │
                    │    ├ 取 messages[-1] 当 query (仅 user)    │  §2 §8
                    │    ├ keygen 选 strategy (4 个类)           │  §1 §2 §3
                    │    └ function_map['retrieval'].call        │  §9
                    └──────────────┬────────────────────────────┘
                                   │
                                   ▼
              ┌──────────────────────────────────────────┐
              │  Retrieval.call (retrieval.py:66-105)     │
              │  对每个 file 调 DocParser 得 Record      │
              │  调 HybridSearch.call / 单一 search.call │
              └────────────┬─────────────────────────────┘
                           │
        ┌──────────────────┴──────────────────────┐
        │                                         │
        ▼                                         ▼
┌──────────────────┐                       ┌──────────────────────┐
│  DocParser       │  两级缓存               │ HybridSearch         │
│  split_doc_to_   │  SimpleDocParser →     │  ├ KeywordSearch     │
│  chunk (§6)      │  DocParser (§5 §9)    │  │  BM25Okapi (§7)    │
│  page_size=500   │  key,url hash,_ps/_wc  │  └ FrontPageSearch   │
│  overlap=150字    │                       │     inf 置顶 (§4)    │
└──────────────────┘                       │  RRF i+1+60 累加 (§7) │
                                           └──────────────────────┘
```

**设计锚速查表**（§1~§9 一句话总结，便于回看）：

| §  | 设计决策                | 替代方案未选的理由         |
|----|-------------------------|----------------------------|
| 1  | 无 LLM → 直接跳过 keygen  | 本地分词已能覆盖，无需双轨 |
| 2  | query 只取最末 user      | 多轮 rewrite 会破坏 keygen prompt 的纯净性 |
| 3  | 中文关键词不再二次分词    | 防止短词打散，但代价是长短语失命中 |
| 4  | front_page 仅单文档生效  | 多文档时无法公平比较前 2 页 |
| 5  | 缓存键不含 max_ref_token | 实际是两套独立 string key，不会串读写，但调小 page_size 后旧 cache 留存 |
| 6  | overlap 用 char 而非 token | 不依赖 tokenizer 加载，代价是中英比例波动 |
| 7  | BM25 用库默认 + RRF=60   | 调参收益小，多次实验未验证 |
| 8  | 不支持的文件类型静默丢弃   | 避免破坏多模态路径，代价是用户不可见 |
| 9  | Memory 只调 retrieval     | 保持 Agent 抽象统一，便于替换搜索器 |

---

## §11. 改这块代码前的检查清单

如果你要扩展或修改 Memory 子系统，逐项确认：

- [ ] 改 `parser_page_size` 默认值：检查是否清理过 workspace tools 目录（保留旧 cache 会让混合 chunk 大小共存）
- [ ] 改 keygen 策略类名：确认在 `keygen_strategies/__init__.py` export；确认 `getattr(module, name)` 大小写匹配
- [ ] 新增 retrieval 子搜索器：实现 `sort_by_scores` 返回完整降序列表（RRF 依赖 rank index）；不能 self-reference（HybridSearch init 会 raise）
- [ ] 改 chunk overlap 长度：注意 `_get_last_part` 的 150 是 hard-coded，改源码要确认 chunk 中 chunk 元素的 [text, page_num] 二元组结构不变
- [ ] 启用 `vector_search`：需要 `DASHSCOPE_API_KEY` env；首次会拉 `text-embedding-v1`，每次 query 都重建 FAISS 索引（TODO 待优化）
- [ ] 改 max_ref_token 默认值：检查 `doc_parser` 的 `_without_chunking` cache 路径不会被新参数错误命中（实际不会，但要在测试里加 case）
- [ ] 多轮上下文增强 query：必须在 Memory 之前扩展 — caller 在 messages 上拼 rewrite 后再调，**不要在 `_run` 内重写 query**，那会破坏跟 retrieval 缓存的对齐

---

## §12. 真实路径修正（与 05 章对照）

下列文件在 05 章可能描述为旧路径，本文已确认生产代码的真实位置：

| 真实路径                                              | 作用                          |
|-------------------------------------------------------|-------------------------------|
| `qwen_agent/memory/memory.py`                         | Memory._run 主入口            |
| `qwen_agent/agents/keygen_strategies/__init__.py`      | 4 个 keygen 策略类的 re-export |
| `qwen_agent/agents/keygen_strategies/gen_keyword.py`   | 默认策略 prompt               |
| `qwen_agent/tools/search_tools/keyword_search.py`      | BM25 + 分词 + parse_keyword   |
| `qwen_agent/tools/search_tools/hybrid_search.py`      | RRF 融合                      |
| `qwen_agent/tools/search_tools/front_page_search.py`  | 前 2 页 inf 置顶              |
| `qwen_agent/tools/search_tools/base_search.py`        | BaseSearch.call + get_topk + 三级兜底 |
| `qwen_agent/tools/doc_parser.py`                      | 切块 + 缓存                   |
| `qwen_agent/tools/simple_doc_parser.py`               | OCR + 单 doc 结构提取         |
| `qwen_agent/tools/retrieval.py`                       | 入口工具封装                  |
| `qwen_agent/tools/data_tools/extract_doc_vocabulary.py`| WithKnowledge 系列的 TF-IDF 词表 |
| `qwen_agent/utils/tokenization_qwen.py`               | QWenTokenizer（token 计数）   |
| `qwen_agent/storages/storage.py`                      | file-based kv，无内存层、无 flush |

---

## §13. 仍待决策的 4 个开放问题

代码里写了 TODO 但没解的设计量：

1. **RRF 常数是否需要调优** —— `hybrid_search.py:52` 注释 `# TODO: This needs to be adjusted for performance`。Cormack 2009 的 60 对长 rank list 是经验值，对 RAG 这种往往只取 top-10 场景是否仍最优，未被实证。
2. **vector_search 性能优化** —— `vector_search.py:24` 注释 `# TODO: Optimize the accuracy of the embedding retriever.`。当前每次 query 都重新 `FAISS.from_documents` 构建索引，长 doc 上是秒级开销；改 persistent index 是显著性能提升点。
3. **parse_keyword 的 broad except** —— `keyword_search.py:195` `except Exception: # TODO: This catch is too broad.`。JSON 解析失败的兜底被吞，调 keygen 输出非 JSON 时无日志，调试体验差。
4. **multi-turn query rewrite** —— §2 已讨论；本系统未做，是 RAG 完整性的最大空白。

---

本文完。05 章看"系统怎么跑"，09 章看"这样跑会不会出事"。

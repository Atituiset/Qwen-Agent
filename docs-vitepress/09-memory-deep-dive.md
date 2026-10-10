# 09 Memory 机制深度剖析：导览

> 本文是 [05 Memory 与 RAG](./05-memory.md) 的姊妹篇。05 讲架构与流程，本系列下钻到原码级——**只回答一件事：为什么这样做、不那样做会怎样、改了会踩什么坑**。所有行号基于仓库 `main` 分支。

## 这套 Memory 子系统由三个问题贯穿

读这一系列前先确立心智模型：当你传一个文件给 Qwen-Agent 问问题时，背后发生了什么——

- **抽 query**：取**最后一条 user 消息**当 query，多轮历史完全丢弃（这是设计选择，不是 bug）。
- **重写 query**：用 LLM 把自然语言改写成结构化 JSON（含中英文关键词 + 改写后的 query），这一步叫 keygen。无 LLM 配置时**整条链路跳过**。
- **检索 + 排序**：BM25 打分 + front_page 强置顶 + RRF 融合，按 token 预算装回 prompt。

每个环节都有“如果不这样做会怎样”的具体答案。后面两个子页就分别拆 keygen 环节和检索环节，每个子页以原码级证据 + 设计权衡表 + 踩坑清单方式展开。

## 阅读地图

| 看这一篇时关心的问题                                       | 跳转到 |
|-----------------------------------------------------------|--------|
| Memory 和 retrieval 怎么挂上、文件流转管线是什么          | 本篇 §1 |
| keygen 的 4 个策略有什么差异、默认是哪个、缓存机制        | [09-memory-keygen.md](./09-memory-keygen.md) |
| BM25 怎么打分、RRF 公式形状、front_page 强置顶什么时候失效 | [09-memory-retrieval.md](./09-memory-retrieval.md) |
| 切块大小是 token 还是 char、overlap 怎么定义、跨页怎么处理 | 本篇 §2 |
| 缓存键怎么设计、为什么会污染、怎么清                       | [09-memory-retrieval.md](./09-memory-retrieval.md) §4 |
| 全部 8 个设计决策 + 4 个开放 TODO 一张速查表              | 本文末 §3 |

## §1. 完整管线一图

```
用户输入 messages + 挂载文件 (item.file)
         │
         ▼
Memory._run (memory.py:81)
         │
         ├── get_rag_files        → PARSER_SUPPORTED_FILE_TYPES 静默过滤
         ├── 取 messages[-1]      → 仅最后一条 USER 当 query
         ├── keygen 选 strategy   → 4 个策略反射路由
         │   └── 输出 JSON 串  {"keywords_zh": [...], "keywords_en": [...], "text": <rewrite>}
         │
         └── retrieval.call       → 内部对每个 file 调 DocParser 得 Record
                                  → HybridSearch  融合 KeywordSearch + FrontPageSearch
                                      ├─ BM25Okapi(k1=1.5,b=0.75)
                                      ├─ FrontPageSearch（单 doc，前 2 chunk 给 inf）
                                      └─ RRF: 1/(rank+1+60) 累加
                                  → get_topk：按 token 预算装 chunk 到 max_ref_token
                                  → 返回 RefMaterialOutput[url, text=[snippet list]]
```

## §2. 切块和缓存的简单结论

详细推理见子页和扁平版；本节给可以直接拿走的两条结论：

- **切块按 token，默认 500**：来自 `settings.py` 的 `DEFAULT_PARSER_PAGE_SIZE`。当整文档总 token ≤ `max_ref_token`（默认 20000）时根本不切，做成单大 chunk。
- **overlap 在跨 chunk 时按字符 150**：`_get_last_part` 的 `available_len=150` 是 hard-coded，与 token 预算不共用度量；跨页处不 overlap，零冗余。代价是中英混排文档时 overlap 占 chunk 大小波动在 10%~40%。

## §3. 全设计决策速查表（8 项）

| 决策 # | 决策点                                                | 替代方案不选的理由             |
|--------|------------------------------------------------------|--------------------------------|
| 1      | 无 LLM → 直接跳过 keygen                              | 本地分词已能覆盖，避免双轨     |
| 2      | query 只取最末 user 消息                               | 多轮 rewrite 会破坏 keygen prompt 纯净 |
| 3      | 中文关键词不再二次分词                                 | 防短词打散，代价是长短语失命中 |
| 4      | front_page 仅单文档生效                                | 多文档无法公平比较前 2 页       |
| 5      | 缓存键不含 max_ref_token                              | 实际两套独立 string key 不串读写 |
| 6      | overlap 用 char=150 而非 token                         | 不依赖 tokenizer 加载，代价是中英比例波动 |
| 7      | BM25 用外库默认 + RRF 常数 60                          | 调参收益小，未实证             |
| 8      | 不支持的文件类型静默丢弃                                | 避免破坏多模态路径             |

## §4. 4 个开放 TODO（源码里写着但未解决）

| #  | TODO                                        | 位置                         |
|----|---------------------------------------------|------------------------------|
| 1  | RRF 常数是否需要调优                        | hybrid_search.py:52          |
| 2  | vector_search 性能优化                      | vector_search.py:24          |
| 3  | parse_keyword 的 broad except 太宽          | keyword_search.py:195        |
| 4  | multi-turn query rewrite 未做                | 设计空白，无 TODO 标注        |

## §5. 真实源码路径修正

下表为生产真实位置，与 05 章 / 官方早期 doc 不一致：

| 真实路径                                              | 作用                          |
|-------------------------------------------------------|-------------------------------|
| `qwen_agent/memory/memory.py`                         | Memory._run 主入口            |
| `qwen_agent/agents/keygen_strategies/__init__.py`      | 4 个 keygen 策略类的 re-export |
| `qwen_agent/tools/search_tools/keyword_search.py`      | BM25 + 分词 + parse_keyword   |
| `qwen_agent/tools/search_tools/hybrid_search.py`      | RRF 融合                      |
| `qwen_agent/tools/search_tools/front_page_search.py`  | 前 2 页 inf 置顶              |
| `qwen_agent/tools/search_tools/base_search.py`        | BaseSearch.call + 三级兜底    |
| `qwen_agent/tools/doc_parser.py`                      | 切块 + 缓存                   |
| `qwen_agent/tools/simple_doc_parser.py`               | OCR + 单 doc 结构提取         |
| `qwen_agent/tools/retrieval.py`                       | 入口工具封装                  |
| `qwen_agent/storages/storage.py`                      | file-based kv，无内存层、无 flush |

## §6. 接下来怎么读

- 对 keygen 这一环的 prompt 模板、split 失败回退、with_knowledge 词表注入感兴趣：[09-memory-keygen.md](./09-memory-keygen.md)
- 对检索排序的 BM25 参数选择、RRF 公式的"rank 复用"隐式契约、front_page 的"4 倍 token 裕度"判定条件感兴趣：[09-memory-retrieval.md](./09-memory-retrieval.md)
- 想要 13 节完整深度版（含每节“为什么不那样” + 逐项踩坑清单）：仓库 `docs/09_Memory机制深度剖析.md`

读完应能回答的 8 个判断题（答案都在扁平版对应小节）：

1. 用户没传 llm 时，keygen 是被跳过还是降级？
2. 多轮里 “它 / 上一篇” 这种指代，检索能被还原吗？
3. `keywords_zh: ["论文摘要"]` 这种长短语在 BM25 上能命中吗？
4. 多文档场景下“前 2 个 chunk 强置顶”还生效吗？
5. 第一次取整块、第二次调小 max_ref_token 会不会复用旧缓存错分？
6. `parser_page_size` 与 overlap 的 150 是同一个度量吗？
7. BM25 的 k1 / b 是不是自实现的、有没有非标准改造？
8. 同一文件两个_URL 表达形式会让 DocParser 重复解析吗？

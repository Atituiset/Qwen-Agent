# 09 Memory 机制深度剖析：从分块到 BM25 的原码级拆解

> 本文是 [05 Memory 与 RAG](./05-memory.md) 的姊妹篇。05 讲架构与流程，本文下钻到原码级细节：切块 overlap、缓存键设计、知识注入，以及工程上的坑。所有行号基于仓库 `main` 分支。

深入专题（因篇幅拆为子页）：

- [关键词生成策略详解](./09-memory-keygen.md)：5 种 keygen 策略的 prompt 原文、stop 词技巧、mem_llm 选择
- [分词与检索排序](./09-memory-retrieval.md)：QWenTokenizer、中英文分流分词、BM25、RRF 融合、装入与兜底

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

## 3. 文档解析与切块原码拆解

### 3.1 统一产出结构

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

### 3.2 split_doc_to_chunk 的三段逻辑

切块按 **token**（不按页/段落硬切），预算 = `parser_page_size`（默认 500）。`chunk` 内部混合两种元素：页标记字符串 `[page: n]` 和内容项 `[txt, page_num]`。

1. **段落放得下**（doc_parser.py:173-177）：扣 token、追加，标记 `has_para=True`。
2. **段落放不下且 chunk 已有内容**（:179-202）：封 chunk（`token = parser_page_size - available_token`），取 overlap 开新 chunk（见 3.3）。
3. **段落放不下且 chunk 为空（超长段落）**（:203-260）：先按 `re.split(r'\. |。', txt)` 拆句；单句仍超预算则 **token 级定长滑窗硬切**（:216-220）：

```python
token_list = tokenizer.tokenize(s)
for si in range(0, len(token_list), available_token):
    ss = tokenizer.convert_tokens_to_string(
        token_list[si:min(len(token_list), si + available_token)])
    sentences.append([ss, min(available_token, len(token_list) - si)])
```

装句时 `if token <= available_token or (not has_para)` 保证至少塞进一句，防止死循环。

### 3.3 _get_last_part：150 字符 overlap 的取法

doc_parser.py:275-309，从 chunk 末尾**倒序**收集与最后一页同页的内容，预算 150 **字符**（不是 token）：

- 整段能装 → 整段前插（段落间 `\n` 连接）；
- 装不下的段落 → 拆句（`. ` 或 `。`），按句前插，遇到装不下的句子即停；
- 跨页即停。

overlap 以纯字符串形式进新 chunk 头部且 `has_para=False`——它不计入正式段落，只是上下文粘合剂。

### 3.4 数据结构

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

## 4. extract_doc_vocabulary：文档词表工具

`qwen_agent/tools/extract_doc_vocabulary.py`（82 行），仅供 WithKnowledge 系 keygen 策略调用：

1. SimpleDocParser 把每个文件解析为纯文本；
2. 以 `str(files)` 为缓存 key 查 Storage，命中直接返回；
3. 未命中：`TfidfVectorizer(tokenizer=string_tokenizer, stop_words=WORDS_TO_IGNORE)`（复用检索分词器和停用词表，sklearn 延迟导入）fit_transform，按 TF-IDF 值降序拼接全部词项，写缓存。

缓存目录 `workspace/tools/extract_doc_vocabulary`。

---

## 5. 两级缓存与 Storage

### 5.1 缓存键设计

| 层级 | 键 | 内容 | 目录 |
|------|-----|------|------|
| SimpleDocParser | `sha256(path)_ori` | 解析后的结构化分页文档 | `workspace/tools/simple_doc_parser/` |
| DocParser（切块） | `sha256(url)_<page_size>` | 切好的 Record JSON | `workspace/tools/doc_parser/` |
| DocParser（整篇） | `sha256(url)_without_chunking` | 单 chunk Record | 同上 |
| ExtractDocVocabulary | `str(files)` | TF-IDF 词表 | `workspace/tools/extract_doc_vocabulary/` |

page_size 进缓存键，改配置不会读到旧切块。HTTP URL 会先下载到 `workspace/tools/simple_doc_parser/<hash>/`。

**已知缺陷**：键基于**路径字符串**的 sha256，本地文件内容变更但路径不变时会命中旧缓存。

### 5.2 Storage 实现

`tools/storage.py`（120 行）：文件系统 KV，"一个 key 一个文件"，key 中的 `/` 映射为子目录。API：`put` / `get`（不存在抛 `KeyNotExistsError`）/ `delete` / `scan`（遍历目录拼 `key: value`）。

工程注意：

- **无任何锁/原子写**，并发 put 同 key 会撕裂写；
- key 未过滤 `..`，理论上可路径逃逸出 root；
- 定位是 demo 级实现，非生产 KV。

---

## 6. 知识注入与预算控制

### 6.1 模板原文

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

### 6.2 max_ref_token 的三处生效点

全链路唯一的内容上限是 `max_ref_token`（默认 20000）：

1. `BaseSearch.call` 全文短路（base_search.py:80）；
2. `get_topk` 装入预算（base_search.py:111）；
3. `_get_the_front_part` 均分预算（base_search.py:167）。

注入 system 后**没有再做 token 校验**，超出 LLM 上下文由 LLM 层的 `DEFAULT_MAX_INPUT_TOKENS`（58000）截断机制兜底。

---

## 7. MemoAssistant：另一种"记忆"

`MemoAssistant`（agents/memo_assistant.py）是对话级长期记忆，与文档 RAG 是两个维度：

- 强制注册 `storage` 工具，MEMORY_PROMPT（memo_assistant.py:25-38）引导模型**自主决定**存什么（"你的记忆很短暂，请频繁的调用工具存储或读取重要对话内容"）；
- `_prepend_storage_info_to_sys`（:65-91）**回放历史消息里的 storage 调用记录**重建 KV 状态（不读数据库，从对话历史推导"当前记忆"），全量嵌入 system；
- 注释声称"仅保留最近三轮对话"，实际代码 `available_turn = 400`（:93-110）——**注释已过时**。

两者可叠加：MemoAssistant 本身是 Assistant 子类，`files` 参数仍走文档 RAG 链路。

---

## 8. 配置全表

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

## 9. 工程注意事项汇总

1. **docstring 与 settings 不一致**：memory.py:51 示例写 `max_ref_token=4000`、keygen 用 `SplitQueryThenGenKeyword`，实际默认 20000 / `GenKeyword`，以 settings 为准。
2. **缓存按路径 hash**：本地文件改了内容但路径不变，命中旧缓存。排查解析问题时先清 `workspace/tools/`。
3. **BM25 每次重建**：无持久化索引，大文档多轮问答的分词开销线性累积。
4. **中英混排走 jieba**：任一 CJK 字符触发中文路径，英文正则过滤被跳过。
5. **storage 非生产级**：无锁、可路径逃逸。
6. **MemoAssistant 注释过时**：声称保留 3 轮，实际 400 轮。
7. **get_topk 截断即 break**：预算不足时后面的小 chunk 没有机会递补。
8. **keygen 失败静默降级**：JSON 解析失败退回原始 query，不报错。

---

## 10. 核心源码索引

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

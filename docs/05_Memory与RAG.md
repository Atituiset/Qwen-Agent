# 05 Memory 与 RAG：让 Agent 拥有知识

## 1. 为什么 Memory 是 Agent？

Qwen-Agent 的 Memory 不是独立的模块，而是一个 **Agent**：

```python
class Memory(Agent):  # memory.py:32
    ...
```

**原因**：Memory 的核心工作——关键词生成——需要调用 LLM，而检索和文档解析需要调用 Tool。将 Memory 设计为 Agent，它就天然拥有 LLM 和 Tool 的能力，复用了 Agent 的全部基础设施。

```
Memory Agent
├── LLM (用于关键词生成)
├── Tools:
│   ├── retrieval  (RAG 检索)
│   └── doc_parser (文档解析+分块)
└── _run() (记忆检索流程)
```

---

## 2. Memory 初始化

### 2.1 构造函数（memory.py:38）

```python
class Memory(Agent):
    def __init__(self, function_list=None, llm=None,
                 system_message='', files=None, rag_cfg=None):
        # 内置工具始终注册
        function_list = function_list or []
        function_list += [
            'retrieval',   # RAG 检索工具
            'doc_parser',  # 文档解析工具
        ]
        super().__init__(function_list=function_list, llm=llm, ...)

        # RAG 配置
        self.rag_cfg = rag_cfg or {}
        self.max_ref_token = rag_cfg.get('max_ref_token', 20000)
        self.parser_page_size = rag_cfg.get('parser_page_size', 500)
        self.rag_searchers = rag_cfg.get('rag_searchers',
            ['keyword_search', 'front_page_search'])
        self.rag_keygen_strategy = rag_cfg.get('rag_keygen_strategy', 'GenKeyword')
```

### 2.2 RAG 配置参数

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `max_ref_token` | 20000 | 最大检索引用 token 数 |
| `parser_page_size` | 500 | 文档分块大小（字符数） |
| `rag_searchers` | `['keyword_search', 'front_page_search']` | 检索策略列表 |
| `rag_keygen_strategy` | `'GenKeyword'` | 关键词生成策略 |

---

## 3. 记忆检索流程

### 3.1 _run 流程（memory.py:81）

```
用户消息 (messages)
    │
    ▼
1. get_rag_files()          ← 提取 RAG 可用文件列表
    │
    ├── 无文件 → yield 空消息，结束
    │
    ▼
2. 提取 query              ← 从最后一条 user 消息中提取
    │
    ▼
3. 关键词生成（可选）       ← 根据 rag_keygen_strategy
    │                       动态导入策略类
    │                       用 LLM 生成增强关键词
    │
    ▼
4. retrieval.call()         ← 调用检索工具
    │   params: {
    │     'query': enriched_query,
    │     'files': rag_files
    │   }
    │
    ▼
5. yield 检索结果           ← 以 assistant+name='memory' 返回
```

### 3.2 get_rag_files（memory.py:146）

```python
def get_rag_files(self, messages):
    files = set(self.system_files)  # 初始化时指定的文件
    # 从消息历史中提取文件引用
    for msg in messages:
        if msg.role == USER:
            for item in (msg.content if isinstance(msg.content, list) else []):
                if isinstance(item, ContentItem) and item.file:
                    files.add(item.file)
    # 过滤为文档解析器支持的格式
    return [f for f in files if f.endswith(tuple(PARSER_SUPPORTED_FILE_TYPES))]
```

---

## 4. 关键词生成策略

### 4.1 策略列表

| 策略名 | 类 | 功能 |
|--------|-----|------|
| `None` | — | 不生成关键词，直接用原始 query |
| `GenKeyword` | `GenKeyword` | 用 LLM 从 query 提取搜索关键词 |
| `SplitQueryThenGenKeyword` | `SplitQueryThenGenKeyword` | 先拆分复杂 query，再分别生成关键词 |
| `GenKeywordWithKnowledge` | `GenKeywordWithKnowledge` | 结合已有知识生成关键词 |
| `SplitQueryThenGenKeywordWithKnowledge` | 组合 | 拆分 + 结合知识 |

### 4.2 动态策略加载

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

---

## 5. 文档解析：DocParser

### 5.1 支持的文件类型

DocParser（tools/doc_parser.py）能处理多种文档格式：

| 格式 | 扩展名 | 解析方式 |
|------|--------|----------|
| PDF | `.pdf` | pdfplumber / PyMuPDF |
| Word | `.docx` | python-docx |
| PPT | `.pptx` | python-pptx |
| Excel | `.xlsx` | openpyxl |
| Markdown | `.md` | 文本读取 |
| 纯文本 | `.txt` | 文本读取 |
| 图片 | `.png/.jpg` | OCR（可选） |
| 网页 | URL | html2text |

### 5.2 分块策略

DocParser 将文档分割为固定大小的页（page），每页 `parser_page_size` 个字符：

```
原始文档
    │
    ▼
解析为纯文本
    │
    ▼
按 parser_page_size 分块
    │
    ▼
[page_1, page_2, page_3, ...]
```

---

## 6. 检索工具：Retrieval

### 6.1 Retrieval 工作流程

```
retrieval.call({query, files})
    │
    ▼
1. DocParser 解析文件 → 分块
    │
    ▼
2. 多路检索
    ├── keyword_search     ← 关键词匹配
    ├── front_page_search  ← 前几页优先
    └── (可扩展其他搜索器)
    │
    ▼
3. RRF 融合（Reciprocal Rank Fusion）
    │
    ▼
4. 截断到 max_ref_token
    │
    ▼
5. 返回检索结果
```

### 6.2 HybridSearch（search_tools/hybrid_search.py）

HybridSearch 实现了 RRF（Reciprocal Rank Fusion）融合算法：

```python
# RRF 核心公式
score = sum(1 / (k + rank_i) for each searcher i)
# k 是平滑常数（通常 k=60）
```

每路检索返回一个排序结果，RRF 根据各路的排名计算综合分数，最终按综合分数排序。

### 6.3 检索策略

| 搜索器 | 实现 | 特点 |
|--------|------|------|
| `keyword_search` | 关键词匹配 | 精确匹配，适合专业术语 |
| `front_page_search` | 前几页优先 | 文档开头通常包含摘要 |
| 自定义 | 实现 searcher 接口 | 可扩展 |

---

## 7. Assistant 中的 RAG 集成

### 7.1 Assistant._run（assistant.py:100）

Assistant 继承自 FnCallAgent，在 FnCall 循环**之前**注入知识：

```
用户消息
    │
    ▼
Assistant._prepend_knowledge_prompt()    ← assistant.py:116
    │
    ├── 1. 如果没有外部 knowledge，调用 Memory.run() 检索
    │
    ├── 2. 格式化检索结果
    │       format_knowledge_to_source_and_content()
    │       → [{'source': '文件名', 'content': '检索内容'}]
    │
    ├── 3. 构建 knowledge prompt
    │       KNOWLEDGE_TEMPLATE_ZH: "# 知识库\n\n{knowledge}"
    │       KNOWLEDGE_SNIPPET_ZH: "## 来自 {source} 的内容：\n```\n{content}\n```"
    │
    └── 4. 追加到 system message
    │
    ▼
super()._run(messages)  ← FnCallAgent 的 LLM+Tool 循环
```

### 7.2 知识注入格式

```
# 知识库

## 来自 report.pdf 的内容：

```
Qwen3 在多项基准测试中表现优异...
```

## 来自 data.csv 的内容：

```
季度收入：Q1=100万, Q2=150万...
```
```

知识被追加到 system message 末尾，LLM 在回答时会优先参考这些内容。

---

## 8. FnCallAgent 中的 Memory 初始化

FnCallAgent 在初始化 Memory 时有一个特殊优化：

```python
# fncall_agent.py:56-71
# 对于 qwq/qvq/qwen3 等推理模型，Memory 使用 qwen-turbo 作为 LLM
# 原因：RAG 关键词生成不需要推理能力，用轻量模型更快更省
if model_name in ('qwq', 'qvq', 'qwen3') and model_server == 'dashscope':
    mem_llm = {'model': 'qwen-turbo', 'model_type': 'qwen_dashscope'}
else:
    mem_llm = self.llm  # 复用 Agent 的 LLM
```

---

## 9. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `memory/memory.py` | 32 | `Memory(Agent)` 类 |
| `memory/memory.py` | 38 | `__init__` (内置 retrieval + doc_parser) |
| `memory/memory.py` | 81 | `_run` (检索流程) |
| `memory/memory.py` | 107 | 关键词策略动态加载 |
| `memory/memory.py` | 146 | `get_rag_files` |
| `tools/retrieval.py` | — | `Retrieval` 工具 |
| `tools/doc_parser.py` | — | `DocParser` 工具 |
| `tools/search_tools/keyword_search.py` | — | 关键词搜索 |
| `tools/search_tools/front_page_search.py` | — | 前页搜索 |
| `tools/search_tools/hybrid_search.py` | — | RRF 融合搜索 |
| `agents/assistant.py` | 100 | `_run` (知识注入) |
| `agents/assistant.py` | 116 | `_prepend_knowledge_prompt` |
| `agents/assistant.py` | 27 | `KNOWLEDGE_TEMPLATE_ZH` |
| `agents/assistant.py` | 37 | `KNOWLEDGE_SNIPPET_ZH` |
| `agents/assistant.py` | 52 | `format_knowledge_to_source_and_content` |
| `agents/fncall_agent.py` | 56 | Memory LLM 优化（推理模型用 qwen-turbo） |

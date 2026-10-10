# 自定义 Tool 实战

> 配套 [03 Tool 系统](./03-tool.md)。本子页只展开三个实战模式与对应坑点。完整"决策点+为什么不那样"深度版见扁平版 `docs/03_Tool_系统.md`。

## 1. 最简 Tool：纯文本返回

```python
from qwen_agent.tools.base import BaseTool, register_tool

@register_tool('hello')
class HelloTool(BaseTool):
    name = 'hello'                         # 必须与 register_tool 参数严格一致
    description = '打招呼工具'               # 正面具体，告诉 LLM 在什么场景用
    parameters = {                         # 推荐 dict 形态（OpenAI schema）
        'type': 'object',
        'properties': {
            'name': {'type': 'string', 'description': '对方的名字'},
        },
        'required': ['name'],
    }

    def call(self, params, **kwargs) -> str:
        args = self._verify_json_format_args(params)   # 宽松 json5 解析
        return f'你好，{args["name"]}！'
```

逐项检查：
- `name = 'hello'` 与 `@register_tool('hello')` 必须一致，否则基类 raise（详见扁平版 §1）
- `parameters` 用 dict 形态：经 `is_tool_schema` 在 `__init__` 严格校验，缺 `type`/`properties` 字段立即 raise
- `description` 写正面：教 LLM 何时用；不要写"不要用 X 时调用"这种 negative 让小模型困惑
- 默认 `_verify_json_format_args` 走 `json_loads`（json5 宽松），可解析单引号、尾随逗号——LLM 偶发不标准 JSON 不会崩
- 返回 str 是最常见的 Tool 输出；agent 把 str 接回 LLM 作为 tool result 文本

## 2. 带文件访问的 Tool：继承 BaseToolWithFileAccess

```python
from qwen_agent.tools.base import BaseToolWithFileAccess, register_tool

@register_tool('file_reader')
class FileReaderTool(BaseToolWithFileAccess):
    name = 'file_reader'
    description = '读取用户上传的文件内容'
    parameters = {
        'type': 'object',
        'properties': {
            'filename': {'type': 'string', 'description': '要读取的文件名'},
        },
        'required': ['filename'],
    }

    def call(self, params, files=None, **kwargs) -> str:
        # files 是 Agent 主动注入的（来自 messages 里 item.file），不是 LLM 可见参数
        args = self._verify_json_format_args(params)
        filename = args['filename']
        for f in (files or []):
            if filename in f:
                with open(f, 'r') as fh:
                    return fh.read()
        return f'文件 {filename} 未找到'
```

逐项检查：
- `BaseToolWithFileAccess` 的 `super().call(...)` 在你重写 `call` 时**不会被自动调用**——除非显式 `super().call(params, files=files, **kwargs)`，否则远程文件不会被 fetch 到 work_dir（见扁平版 §5）
- `files` 参数**不进 LLM-visible schema**——Agent 在 `fncall_agent.py:110-118` 根据 `self.file_access` 决定是否注入。`self.parameters` 里**不要列 files**
- `save_url_to_local_work_dir` 失败时基类 `except Exception: print_traceback()` 静默吞——Tool 要应对"文件可能没拉下来"的情况（路径不存在）
- 多 Agent / 多 Tool 共用同一 `work_dir`（按 name 默认）——同名 Tool 实例并发访问 work_dir 时要注意写文件 race

## 3. 多模态返回：List[ContentItem]

```python
from qwen_agent.llm.schema import ContentItem
from qwen_agent.tools.base import BaseTool, register_tool

@register_tool('image_captioner')
class ImageCaptionerTool(BaseTool):
    name = 'image_captioner'
    description = '为图片生成简短描述'
    parameters = {
        'type': 'object',
        'properties': {'image_url': {'type': 'string', 'description': '图片 URL'}},
        'required': ['image_url'],
    }

    def call(self, params, **kwargs):
        args = self._verify_json_format_args(params)
        caption = describe_image(args['image_url'])
        # ★ 返回 List[ContentItem] —— list 必须 all ContentItem，混 str 会落到 JSON 路径
        return [
            ContentItem(text=caption),
            ContentItem(image=args['image_url']),
        ]
```

逐项检查：
- agent.py:205-210 中 `_call_tool` 检查 `isinstance(tool_result, list) and all(isinstance(item, ContentItem) for item in tool_result)`——**混入 str 会落到 `json.dumps` 路径丢语义**（见扁平版 §4）
- `image=...` 字段的值会被作为 ContentItem.image 走多模态协议；非多模态模型接到的还是 ContentItem 但模型不消费——caller 要警觉
- 文本 ContentItem 的 text 会被作为 assistant 内容继续走 prompt——LLM 像看到普通文本回复

## 4. 自定义异常：选哪种？

```python
from qwen_agent.tools.base import ToolServiceError

@register_tool('db_query')
class DBQueryTool(BaseTool):
    def call(self, params, **kwargs):
        args = self._verify_json_format_args(params)
        try:
            return db.execute(args['sql'])
        except DatabaseDownError as ex:
            # 基础设施级 → 透传 raise，Agent 流停
            raise ToolServiceError(exception=ex, code='DB_DOWN',
                                    message='数据库无法连接，立即人工介入') from ex
        except SQLSyntaxError as ex:
            # 业务级 → 转 string 回喂 LLM，让 LLM 重试或换种写法
            raise ValueError(f'SQL 语法错: {ex}') from ex
```

逐项检查：
- `ToolServiceError` 在 agent.py:193-194 的 `except (ToolServiceError, DocParserError): raise ex` 透传，caller 必接 stop Agent 循环
- 普通 `Exception` 在 agent.py:195-203 被捕获转 string，含 **full traceback 直接拼回 LLM**——生产 Tool 必须 redact 敏感字段（如 SQL 里的访问凭据）
- 设计选择基线：**LLM 能合理重试的错 → 普通 Exception；基础设施彻底坏了 → ToolServiceError**

## 5. 自定义 cfg override 字段

```python
@register_tool('rate_limited')
class RateLimitedTool(BaseTool):
    name = 'rate_limited'
    parameters = {...}

    def __init__(self, cfg=None):
        super().__init__(cfg)               # 别漏：基类校验 self.name / parameters
        self.rate_limit_per_minute = self.cfg.get('rate_limit_per_minute', 60)
        self.timeout = self.cfg.get('timeout', 30)

# caller
agent = Assistant(function_list=[{'name': 'rate_limited', 'rate_limit_per_minute': 100,
                                   'timeout': 60}], llm=cfg)
```

逐项检查：
- 基类 `__init__` 只存 `self.cfg = cfg or {}`，**不解释字段语义**——所有 override 字段都在子类 `__init__` 自解析
- 不能改 `name` 的 cfg override —— `name` 是 class attribute + 装饰器赋值，**实例化后 immutable**
- `work_dir`（仅 `BaseToolWithFileAccess`）走 cfg `work_dir` 字段 override 默认 `~/.cache/qwen_agent_tools/<tool_name>/`

## 6. 完整检查清单

- [ ] class attribute `name` 与 `@register_tool(name=X)` 严格一致
- [ ] dict 形态 parameters 必须满足 OpenAI tool schema（type/properties/required 三字段齐全）
- [ ] 继承 `BaseToolWithFileAccess` 时要么调 `super().call(...)` 要么自己 fetch 远程文件
- [ ] 多模态 Tool 返回 `List[ContentItem]` 必须全 ContentItem，不要混 str
- [ ] 业务异常用普通 Exception / ValueError；基础设施异常用 `ToolServiceError`
- [ ] Tool 异常 message 会含 traceback 给 LLM——生产 Tool 必须 redact 敏感信息
- [ ] cfg override 字段在子类 `__init__` 调 `super().__init__(cfg)` 后自解析
- [ ] `description` 写正面具体：教 LLM 何时用，避免 negative instruction 在小模型上低效

完。

## 7. 关键源码索引

| 行号     | 内容                                |
|----------|-------------------------------------|
| 24       | `TOOL_REGISTRY = {}`                |
| 27-41    | `ToolServiceError`                  |
| 44-59    | `register_tool` 装饰器             |
| 109-191  | `BaseTool` 抽象基类                 |
| 140-162  | `_verify_json_format_args`          |
| 193-216  | `BaseToolWithFileAccess` + `call`   |

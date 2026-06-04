# 03 Tool 系统：从 BaseTool 到内置工具

## 1. 设计理念

Qwen-Agent 的 Tool 系统遵循「**注册即用**」原则：

1. 每个工具是一个类，继承 `BaseTool`
2. 用 `@register_tool('name')` 注册到全局 `TOOL_REGISTRY`
3. Agent 通过字符串名称引用工具，框架自动实例化

```python
# 工具定义
@register_tool('my_calculator')
class MyCalculator(BaseTool):
    name = 'my_calculator'
    description = '简单计算器'
    parameters = [{
        "name": "expression",
        "type": "string",
        "description": "数学表达式",
        "required": True
    }]

    def call(self, params: Union[str, dict], **kwargs) -> str:
        args = self._verify_json_format_args(params)
        return str(eval(args['expression']))

# 工具使用
agent = FnCallAgent(function_list=['my_calculator'], llm=llm_cfg)
```

---

## 2. BaseTool 抽象基类

### 2.1 类定义（tools/base.py:109）

```python
class BaseTool(ABC):
    name: str = ''           # 工具唯一标识
    description: str = ''    # 工具描述（给 LLM 看）
    parameters: Union[List[dict], dict] = []  # 参数 schema

    def __init__(self, cfg=None):
        ...

    @abstractmethod
    def call(self, params, **kwargs) -> Union[str, list, dict, List[ContentItem]]:
        """执行工具，返回结果"""
        ...

    def _verify_json_format_args(self, params, strict_json=False) -> dict:
        """验证并解析参数为 dict"""
        ...

    @property
    def function(self) -> dict:
        """返回 OpenAI 兼容的 function schema"""
        return {
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters,
        }
```

### 2.2 两种参数格式

BaseTool 支持两种参数定义方式：

| 格式 | `parameters` 类型 | 验证方式 | 示例 |
|------|-------------------|----------|------|
| **List 格式（遗留）** | `List[dict]` | 检查必填参数是否存在 | `[{"name": "x", "type": "string", "required": True}]` |
| **Dict 格式（推荐）** | `dict` (JSON Schema) | `jsonschema.validate` | `{"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}` |

`_verify_json_format_args()` 根据格式自动选择验证策略。

### 2.3 function 属性

`function` 属性返回 OpenAI 兼容的函数描述字典，这是 FnCall Agent 传递给 LLM 的工具列表来源：

```python
# Agent._run() 中
functions = [func.function for func in self.function_map.values()]
```

---

## 3. 注册机制

### 3.1 TOOL_REGISTRY（tools/base.py:24）

```python
TOOL_REGISTRY = {}  # 全局字典 {name: class}
```

### 3.2 @register_tool 装饰器（tools/base.py:44）

```python
@register_tool('code_interpreter')  # 注册名
class CodeInterpreter(BaseTool):
    name = 'code_interpreter'       # 必须与装饰器名一致
    ...
```

验证规则：
- 不允许重名注册（除非 `allow_overwrite=True`）
- `cls.name` 必须与装饰器的 `name` 一致

### 3.3 工具发现

Agent 初始化时通过字符串查找注册表：

```python
# agent.py 中的逻辑
for tool_name in function_list:
    if isinstance(tool_name, str):
        tool = TOOL_REGISTRY[tool_name](cfg=tool_cfg)
    elif isinstance(tool_name, dict):
        tool = TOOL_REGISTRY[tool_name['name']](cfg=tool_name)
    elif isinstance(tool_name, BaseTool):
        tool = tool_name
    self.function_map[tool.name] = tool
```

---

## 4. BaseToolWithFileAccess

`BaseToolWithFileAccess`（tools/base.py:193）扩展了 `BaseTool`，增加文件处理能力：

```python
class BaseToolWithFileAccess(BaseTool, ABC):
    @property
    def file_access(self) -> bool:
        return True  # 标记此工具需要文件

    def call(self, params, files=None, **kwargs) -> str:
        # 自动将远程文件下载到 work_dir
        # 然后调用子类实现
        ...
```

当 Agent 调用有 `file_access` 的工具时，会自动从消息历史中提取文件路径并传入。

---

## 5. 内置工具一览

| 工具名 | 类 | 文件 | 功能 | 文件访问 |
|--------|-----|------|------|----------|
| `code_interpreter` | `CodeInterpreter` | `tools/code_interpreter.py` | Docker 沙箱执行 Python | ✅ |
| `image_gen` | `ImageGen` | `tools/image_gen.py` | 调用模型生成图片 | ❌ |
| `web_search` | 搜索工具 | `tools/web_search.py` | 网页搜索 | ❌ |
| `retrieval` | `Retrieval` | `tools/retrieval.py` | RAG 检索 | ✅ |
| `doc_parser` | `DocParser` | `tools/doc_parser.py` | 文档解析+分块 | ✅ |
| `amap_poi_search` | POI 搜索 | `tools/amap_poi_search.py` | 高德地图 POI | ❌ |
| `amap_weather` | 天气查询 | `tools/amap_weather.py` | 高德地图天气 | ❌ |

---

## 6. 自定义工具实战

### 6.1 最简示例

```python
from qwen_agent.tools.base import BaseTool, register_tool

@register_tool('hello')
class HelloTool(BaseTool):
    name = 'hello'
    description = '打招呼工具'
    parameters = [{
        'name': 'name',
        'type': 'string',
        'description': '对方的名字',
        'required': True
    }]

    def call(self, params, **kwargs):
        args = self._verify_json_format_args(params)
        return f'你好，{args["name"]}！'
```

### 6.2 带文件访问的工具

```python
@register_tool('file_reader')
class FileReaderTool(BaseToolWithFileAccess):
    name = 'file_reader'
    description = '读取文件内容'
    parameters = {
        'type': 'object',
        'properties': {
            'filename': {'type': 'string', 'description': '文件名'}
        },
        'required': ['filename']
    }

    def call(self, params, files=None, **kwargs):
        args = self._verify_json_format_args(params)
        filename = args['filename']
        # files 包含 Agent 自动提取的文件路径列表
        for f in (files or []):
            if filename in f:
                with open(f, 'r') as fh:
                    return fh.read()
        return f'文件 {filename} 未找到'
```

### 6.3 返回多模态内容

```python
from qwen_agent.llm.schema import ContentItem

def call(self, params, **kwargs):
    return [ContentItem(image='https://example.com/img.png')]
```

---

## 7. 核心源码索引

| 文件 | 行号 | 关键内容 |
|------|------|----------|
| `tools/base.py` | 24 | `TOOL_REGISTRY` 全局注册表 |
| `tools/base.py` | 44 | `register_tool` 装饰器 |
| `tools/base.py` | 62 | `is_tool_schema` 验证函数 |
| `tools/base.py` | 109 | `BaseTool` 类定义 |
| `tools/base.py` | 126 | `call()` 抽象方法 |
| `tools/base.py` | 140 | `_verify_json_format_args` |
| `tools/base.py` | 164 | `function` 属性 |
| `tools/base.py` | 193 | `BaseToolWithFileAccess` |
| `tools/base.py` | 27 | `ToolServiceError` 异常 |
| `agent.py` | — | `function_map` 构建（从字符串到实例） |

# 自定义工具

## 1. 最简示例

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

## 2. 带文件访问的工具

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

## 3. 返回多模态内容

```python
from qwen_agent.llm.schema import ContentItem

def call(self, params, **kwargs):
    return [ContentItem(image='https://example.com/img.png')]
```

## 4. 使用 JSON Schema 参数（推荐）

```python
@register_tool('stock_query')
class StockQueryTool(BaseTool):
    name = 'stock_query'
    description = '查询股票信息'
    parameters = {
        'type': 'object',
        'properties': {
            'symbol': {
                'type': 'string',
                'description': '股票代码，如 AAPL, GOOGL'
            },
            'fields': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': '需要查询的字段，如 price, volume, market_cap'
            }
        },
        'required': ['symbol']
    }

    def call(self, params, **kwargs):
        args = self._verify_json_format_args(params)
        symbol = args['symbol']
        fields = args.get('fields', ['price'])
        # 调用实际 API ...
        return f'{symbol}: price=$175.50, volume=50M'
```

## 5. 工具配置传递

```python
# 通过 function_list 传递配置
agent = FnCallAgent(
    function_list=[{
        'name': 'my_tool',
        'max_results': 10,  # 自定义配置
    }],
    llm=llm_cfg,
)

# 在工具中读取配置
class MyTool(BaseTool):
    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.max_results = (cfg or {}).get('max_results', 5)
```

## 6. 注意事项

- `name` 属性必须与 `@register_tool` 的名称一致
- `description` 应清晰描述工具功能，这是 LLM 决定是否调用工具的依据
- `parameters` 应准确描述参数类型和必要性，LLM 据此生成调用参数
- `call()` 方法应处理参数解析错误，返回友好的错误信息
- 返回值应为字符串（或 `List[ContentItem]` 用于多模态）

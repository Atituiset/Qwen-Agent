# 03 Tool 系统：注册、调用、文件访问

> 这一系列讲"为什么这样设计、不那样会怎样"。本章只回答 BaseTool 抽象与自定义 Tool 的关键决策点。完整深度版见仓库 `docs/03_Tool_系统.md`。行号基于 `main` 分支。`tools/base.py` 指 `qwen_agent/tools/base.py`。

读完应能回答：

1. `@register_tool('x')` 装饰器 vs 直接 `TOOL_REGISTRY['x'] = MyTool`，差别在哪？
2. `parameters` 是 list 还是 dict？哪种适合我？
3. `_verify_json_format_args` 的宽松 JSON 什么时候必要？
4. `call` 返回 `List[ContentItem]` 是干什么的？
5. 何时该继承 `BaseToolWithFileAccess`？
6. `ToolServiceError` vs 普通 `Exception`：什么时候该 raise 哪个？

---

## 1. 关键定位

`tools/base.py:24-216` 提供：
- `TOOL_REGISTRY` 全局 dict 注册表（行 24）
- `register_tool(name, allow_overwrite=False)` 装饰器（行 44-59）
- `ToolServiceError` 基础设施级异常（行 27-41），可附加 `code` / `message` / `extra`
- `is_tool_schema(obj)` 校验 dict 形态的 `parameters` 是否 OpenAI 兼容（行 62-106）
- `BaseTool` 抽象基类，`call` 子类必须实现（行 109-191）
- `BaseToolWithFileAccess`：自动 fetch 远程文件到 work_dir（行 193-216）

所有 Tool 必须 `@register_tool(name=X)`，且类 attribute `name` 与装饰器参数严格一致，否则 raise `ValueError`。

---

## 2. 决策点速查（6 项核心，详细见扁平版 §1~§8）

| # | 决策点                              | 替代方案不选的理由                       | 详解    |
|---|--------------------------------------|------------------------------------------|---------|
| 1 | `register_tool` 默认不允许覆盖       | 静默覆盖会让多进程 / fork 注册相互吞      | 扁平版 §1 |
| 2 | `parameters` 同时支持 list 与 dict   | 历史向后兼容；list 形太宽松，dict 走 schema validation | 扁平版 §2 |
| 3 | `_verify_json_format_args` 默认走 json5 (`json_loads`) | 接 LLM 偶发不标准 JSON 输出（尾随逗号、单引号）  | 扁平版 §3 |
| 4 | `call` 可返 `List[ContentItem]` 多模态结果 | 强制 str 会把图像序列化为字符串化丢语义   | 扁平版 §4 |
| 5 | `BaseToolWithFileAccess` 自动 fetch 远程文件  | 各 Tool 重写 fetch+缓存+err 会不一致      | 扁平版 §5 |
| 6 | 异常分两类：`ToolServiceError` 透传 / 普通 `Exception` 回喂 LLM | 让基础设施级错中止，业务级 LLM 自愈     | 扁平版 §6 |

---

## 3. 注册机制速查

```python
from qwen_agent.tools import register_tool, BaseTool
from qwen_agent.llm.schema import ContentItem
from typing import Union, List

@register_tool('my_tool')
class MyTool(BaseTool):
    name = 'my_tool'                  # 必须与装饰器参数一致
    description = '在 ... 条件下用'
    parameters = {                    # 推荐 OpenAI schema 形态
        'type': 'object',
        'properties': {
            'input': {'type': 'string', 'description': '...'},
        },
        'required': ['input']
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        params = self._verify_json_format_args(params)  # 默认宽松 json5
        ...
        return 'OK'
```

`function` property 让 LLM 看到：`{name, description, parameters}`。`name_for_human` 与 `args_format` **不进 prompt**——只留给某些 Anthropic / OSS 兼容路径。

---

## 4. 调用关键的几行

```python
# tools/base.py:140-162  _verify_json_format_args
if isinstance(params, str):
    try:
        if strict_json:
            params_json: dict = json.loads(params)     # 严格
        else:
            params_json: dict = json_loads(params)    # 宽松 json5
    except json.decoder.JSONDecodeError:
        raise ValueError('Parameters must be formatted as a valid JSON!')
else:
    params_json: dict = params

if isinstance(self.parameters, list):
    for param in self.parameters:
        if 'required' in param and param['required']:
            if param['name'] not in params_json:
                raise ValueError('Parameters %s is required!' % param['name'])
elif isinstance(self.parameters, dict):
    jsonschema.validate(instance=params_json, schema=self.parameters)
return params_json

# tools/base.py:193-216 BaseToolWithFileAccess.call
def call(self, params, files: List[str] = None, **kwargs) -> str:
    if files:
        os.makedirs(self.work_dir, exist_ok=True)
        for file in files:
            try:
                save_url_to_local_work_dir(file, self.work_dir)
            except Exception:
                print_traceback()                      # ★ 静默吞 fetch 错
    # Then do something with the files: ...
```

---

## 5. 改这块代码前的检查清单

- [ ] 新 Tool 的 class attribute `name` 与 `@register_tool(name=X)` 严格一致
- [ ] 新 Tool 用 dict 形态 `parameters` 必须满足 OpenAI tool schema
- [ ] 自定义异常分类：业务 → `ValueError`/普通 Exception 让 LLM 自愈；基础设施 → `ToolServiceError` 透传
- [ ] 多模态 Tool 返回 `List[ContentItem]` 必须全 ContentItem，混 str 会落到 JSON 路径丢语义
- [ ] 远程文件 Tool 继承 `BaseToolWithFileAccess`，要么调 super 要么自己 fetch
- [ ] Tool 异常 traceback 字符串拼到 LLM 上下文 ——生产 Tool 必须 redact 敏感信息
- [ ] cfg override 字段 Tool 内部自己处理（基类只存 `self.cfg`）
- [ ] `description` 写得**正面具体**："在什么场景下用这个 Tool"——避免 negative 在小模型上低效
- [ ] LLM 输出可能 `json_loads` 触发宽松容错，严格 Tool 内部 argparse 自验

---

## 6. 子页继续

- 自定义 Tool 实战 + 常见错误：[03-tool-custom.md](./03-tool-custom.md)
- 完整 10 节深度版：仓库 `docs/03_Tool_系统.md`

## 7. 关键源码索引

| 行号      | 内容                                       |
|-----------|--------------------------------------------|
| 24        | `TOOL_REGISTRY = {}`                       |
| 27-41     | `ToolServiceError` 定义                     |
| 44-59     | `register_tool` 装饰器                     |
| 62-106    | `is_tool_schema` 校验 dict 形态 parameters  |
| 109-191   | `BaseTool` 抽象基类                        |
| 125-138   | `call` 抽象方法                            |
| 140-162   | `_verify_json_format_args`                 |
| 165-186   | `function` / `name_for_human` / `args_format` |
| 193-216   | `BaseToolWithFileAccess`                   |

完。

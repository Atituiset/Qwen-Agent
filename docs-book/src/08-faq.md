# FAQ

### Q1: 工具调用没有触发？

检查：
1. `function_list` 是否正确注册
2. `system_message` 是否引导 LLM 使用工具
3. 模型是否支持 FnCall（小模型可能不支持）
4. 尝试切换 `fncall_prompt_type` 为 `'qwen'`（Qwen 模型更适配）

### Q2: MCP 连接失败？

检查：
1. MCP Server 命令/URL 是否可访问
2. Node.js 是否安装（`npx` 命令需要）
3. 网络连接（SSE 模式需要 HTTP 访问）
4. 查看日志中的 subprocess 输出

### Q3: RAG 检索结果不相关？

优化：
1. 更换 `rag_keygen_strategy`（试试 `SplitQueryThenGenKeyword`）
2. 增大 `max_ref_token`
3. 调整 `parser_page_size`（更小的块更精确）
4. 检查文档格式是否被 DocParser 支持

### Q4: 消息过长被截断？

- 默认 `max_input_tokens = 58000`
- 截断策略优先保留 system message + 最后一轮
- 长函数结果会被最先截断（替换为 `'omit'`）
- 可通过 `generate_cfg` 调整 `max_input_tokens`

### Q5: Agent 循环不停止？

- `MAX_LLM_CALL_PER_RUN` 默认为 20，达到上限自动停止
- 检查工具返回是否让 LLM 满意（不满意会继续调用）
- 确保工具没有返回错误信息导致 LLM 重复尝试

### Q6: 如何调试 FnCall 模板？

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# 查看预处理后的消息
llm = get_chat_model(llm_cfg)
preprocessed = llm._preprocess_messages(messages, lang='zh', generate_cfg={}, functions=funcs)
for msg in preprocessed:
    print(f"[{msg.role}]: {msg.content[:200]}")
```

### Q7: 多 Agent 场景下 Agent 名称冲突？

- MultiAgentHub 要求所有 Agent 名称唯一
- 如果名称重复，会抛出 ValueError
- 确保 `name` 参数在所有 Agent 中唯一

### Q8: 如何自定义 Agent 编排？

继承 `Agent` 类并实现 `_run()` 方法：

```python
from qwen_agent import Agent

class MyAgent(Agent):
    def _run(self, messages, lang='en', **kwargs):
        # 自定义编排逻辑
        response = self._call_llm(messages)
        # 处理响应...
        yield [response]
```

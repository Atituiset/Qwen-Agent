# Qwen-Agent 框架深度解析

> 本系列文档面向 Agent 开发新人，从历史脉络到工程实践，渐进式拆解 Qwen-Agent 框架的方方面面。

## 推荐阅读路径

```
Agent 新人 ──→ 01 → 03 → 08 → 02 → 04 → 05 → 06 → 07
↑______________|
(实战后回看原理)
```

- **快速上手**：01 → 03 → 08（3 篇即可写出可用 Agent）
- **深度理解**：按顺序读完所有章节
- **按需查阅**：直接跳转到感兴趣的章节，每篇自成体系

## 源码导航速查

| 概念 | 核心源码位置 |
|------|-------------|
| Agent 基类 | `qwen_agent/agent.py` |
| FnCallAgent | `qwen_agent/agents/fncall_agent.py` |
| Assistant | `qwen_agent/agents/assistant.py` |
| ReActChat | `qwen_agent/agents/react_chat.py` |
| Router | `qwen_agent/agents/router.py` |
| GroupChat | `qwen_agent/agents/group_chat.py` |
| BaseTool | `qwen_agent/tools/base.py` |
| MCPManager | `qwen_agent/tools/mcp_manager.py` |
| Memory | `qwen_agent/memory/memory.py` |
| Retrieval | `qwen_agent/tools/retrieval.py` |
| BaseChatModel | `qwen_agent/llm/base.py` |
| FunctionCalling | `qwen_agent/llm/function_calling.py` |
| Nous FnCall Prompt | `qwen_agent/llm/fncall_prompts/nous_fncall_prompt.py` |
| Qwen FnCall Prompt | `qwen_agent/llm/fncall_prompts/qwen_fncall_prompt.py` |
| Message Schema | `qwen_agent/llm/schema.py` |

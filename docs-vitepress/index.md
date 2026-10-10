---
layout: home

hero:
  name: Qwen-Agent
  text: 框架深度解析
  tagline: 面向 Agent 开发新人——从历史脉络到工程实践，渐进式拆解 Qwen-Agent 框架的方方面面
  actions:
    - theme: brand
      text: 开始阅读
      link: /intro
    - theme: alt
      text: 框架全景
      link: /01-overview
    - theme: alt
      text: GitHub
      link: https://github.com/QwenLM/Qwen-Agent

features:
  - title: 导读与框架全景
    details: 推荐阅读路径、源码导航速查，以及 Qwen-Agent 的整体架构与核心概念。
    link: /intro
  - title: Function Call
    details: FnCall 的两种 Prompt 模板（Nous / Qwen 原生）、pre/post 处理与平行调用的实现细节。
    link: /02-fncall
  - title: Tool 系统与 MCP
    details: BaseTool 的注册与调用机制、自定义工具实战，以及 MCPManager 单例与 MCP 配置集成。
    link: /03-tool
  - title: Memory 与 RAG
    details: Memory 在 Agent 基类中的使用、关键词生成策略、分词与检索排序的原码级剖析。
    link: /05-memory
  - title: Agent 编排
    details: 多 Agent 编排模式——Router 路由与 GroupChat 群聊的设计与实现。
    link: /06-agent
  - title: LLM 层与实战
    details: LLM preprocess/postprocess、消息截断与重试机制，以及可直接上手的实战指南与 FAQ。
    link: /07-llm
---

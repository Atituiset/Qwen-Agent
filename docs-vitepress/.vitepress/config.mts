import { defineConfig } from 'vitepress'

const base = '/Qwen-Agent/'

// https://vitepress.dev/reference/site-config
export default defineConfig({
  lang: 'zh-CN',
  title: 'Qwen-Agent 框架深度解析',
  titleTemplate: '从历史脉络到工程实践',
  description:
    '面向 Agent 开发新人的 Qwen-Agent 框架深度解析：从历史脉络到工程实践，渐进式拆解 Function Call、Tool 系统、MCP、Memory 与 RAG、Agent 编排与 LLM 层。',

  base,
  lastUpdated: true,
  cleanUrls: true,
  srcExclude: ['README.md'],

  markdown: {
    // shiki 3.x 不再内置 cuda 语法, 用 cpp 高亮兜底
    languageAlias: { cuda: 'cpp' },
  },

  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}favicon.svg` }],
    ['meta', { name: 'theme-color', content: '#2e7d5b', media: '(prefers-color-scheme: light)' }],
    ['meta', { name: 'theme-color', content: '#18191c', media: '(prefers-color-scheme: dark)' }],
    ['meta', { property: 'og:title', content: 'Qwen-Agent 框架深度解析' }],
    [
      'meta',
      {
        property: 'og:description',
        content:
          '面向 Agent 开发新人的 Qwen-Agent 框架深度解析：从历史脉络到工程实践，渐进式拆解框架的方方面面。',
      },
    ],
    ['meta', { property: 'og:type', content: 'website' }],
    ['meta', { name: 'twitter:card', content: 'summary' }],
    // 学习笔记悬浮组件（localStorage），VitePress SPA 路由切换由 theme/index.ts 通知其重挂载
    ['script', { src: `${base}notes.js` }],
  ],

  themeConfig: {
    // https://vitepress.dev/reference/default-theme-config
    logo: '/favicon.svg',

    nav: [
      { text: '首页', link: '/' },
      { text: '导读', link: '/intro' },
      { text: '框架全景', link: '/01-overview' },
      { text: '实战指南', link: '/08-practice' },
    ],

    sidebar: [
      { text: '导读', link: '/intro' },
      { text: '01 框架全景', link: '/01-overview' },
      {
        text: '02 Function Call',
        link: '/02-fncall',
        collapsed: false,
        items: [
          { text: 'Nous FnCall 模板', link: '/02-fncall-nous' },
          { text: 'Qwen FnCall 模板', link: '/02-fncall-qwen' },
        ],
      },
      {
        text: '03 Tool 系统',
        link: '/03-tool',
        collapsed: false,
        items: [{ text: '自定义工具', link: '/03-tool-custom' }],
      },
      {
        text: '04 MCP',
        link: '/04-mcp',
        collapsed: false,
        items: [{ text: 'MCP 配置与集成', link: '/04-mcp-config' }],
      },
      {
        text: '05 Memory 与 RAG',
        link: '/05-memory',
        collapsed: false,
        items: [{ text: '关键词生成策略', link: '/05-memory-keygen' }],
      },
      {
        text: '06 Agent 编排',
        link: '/06-agent',
        collapsed: false,
        items: [{ text: 'Router 与 GroupChat', link: '/06-agent-multi' }],
      },
      {
        text: '07 LLM 层',
        link: '/07-llm',
        collapsed: false,
        items: [{ text: '消息截断与重试', link: '/07-llm-truncation' }],
      },
      {
        text: '08 实战指南',
        link: '/08-practice',
        collapsed: false,
        items: [{ text: 'FAQ', link: '/08-faq' }],
      },
      {
        text: '09 Memory 机制深度剖析',
        link: '/09-memory-deep-dive',
        collapsed: false,
        items: [
          { text: '关键词生成策略详解', link: '/09-memory-keygen' },
          { text: '分词与检索排序', link: '/09-memory-retrieval' },
        ],
      },
    ],

    outline: {
      level: [2, 3],
      label: '本页大纲',
    },

    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '无法找到相关结果',
            resetButtonTitle: '清除查询条件',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' },
          },
        },
      },
    },

    editLink: {
      pattern: 'https://github.com/Atituiset/Qwen-Agent/edit/main/docs-vitepress/:path',
      text: '在 GitHub 上编辑此页',
    },

    socialLinks: [{ icon: 'github', link: 'https://github.com/Atituiset/Qwen-Agent' }],

    docFooter: { prev: '上一页', next: '下一页' },

    lastUpdated: {
      text: '最后更新于',
      formatOptions: { dateStyle: 'short', timeStyle: 'short' },
    },

    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '菜单',
    darkModeSwitchLabel: '主题',
    lightModeSwitchTitle: '切换到浅色模式',
    darkModeSwitchTitle: '切换到深色模式',
    skipToContentLabel: '跳转到内容',

    footer: {
      message: '基于 Apache 2.0 许可发布',
      copyright: 'Copyright © 2026 Qwen-Agent 框架深度解析',
    },
  },
})

import { defineConfig } from 'vitepress'

const repo = 'https://github.com/kuangxing6367/zcbot-minecraftconsole'
const repoFile = (p: string) => `${repo}/blob/main/${p}`

export default defineConfig({
  lang: 'zh-CN',
  title: 'MinecraftConsole',
  description: '私有 TCP 帧链路驱动的 Minecraft Java 版远程控制台',
  base: '/',
  // 站点自身的说明文件不渲染成页面
  srcExclude: ['README.md'],
  // GitHub Pages 是纯静态托管，不做 rewrite，所以保持 xxx.html 路径
  cleanUrls: false,
  lastUpdated: true,
  appearance: 'dark',
  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: '/favicon.svg' }],
    ['meta', { name: 'theme-color', content: '#0b1017' }],
    ['meta', { property: 'og:title', content: 'MinecraftConsole 文档' }],
    ['meta', { property: 'og:description', content: '私有 TCP 帧链路 · mc_link 适配器 · 双向 RPC 协议规范' }],
    ['meta', { property: 'og:url', content: 'https://mc.zgric.top/' }],
  ],
  themeConfig: {
    nav: [
      { text: '指南', link: '/guide/overview' },
      { text: '协议规范', link: '/protocol/rpc' },
      { text: '部署', link: '/deploy/pages' },
      { text: 'GitHub', link: repo },
    ],
    sidebar: [
      {
        text: '开始',
        items: [
          { text: '概览', link: '/guide/overview' },
          { text: '组成与架构', link: '/guide/architecture' },
          { text: '快速开始', link: '/guide/getting-started' },
          { text: 'mc_link 适配器', link: '/guide/adapter' },
        ],
      },
      {
        text: '协议规范',
        items: [
          { text: '帧格式', link: '/protocol/frames' },
          { text: '双向 RPC / 跳包机制', link: '/protocol/rpc' },
          { text: '跨语言互操作验证', link: '/protocol/interop' },
        ],
      },
      {
        text: '部署与参考',
        items: [
          { text: '文档站部署', link: '/deploy/pages' },
          { text: '仓库文档索引', link: '/reference/index' },
          { text: '边界与变更', link: '/reference/boundary' },
        ],
      },
    ],
    outline: { level: [2, 3], label: '本页目录' },
    docFooter: { prev: '上一篇', next: '下一篇' },
    lastUpdated: { text: '最后更新于' },
    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '没有找到结果',
            resetButtonTitle: '清除查询',
            displayDetails: '显示详情',
            backButtonTitle: '返回',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' },
          },
        },
      },
    },
    socialLinks: [{ icon: 'github', link: repo }],
    footer: {
      message: 'ZGRIC · 静态文档 · 站点不含后端',
      copyright: `源代码与问题反馈见 ${repoFile('README.md')}`,
    },
    externalLinkIcon: true,
  },
})

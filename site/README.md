# MinecraftConsole 文档站

介绍 [MinecraftConsole](https://github.com/kuangxing6367/zcbot-minecraftconsole) 的静态文档站，**VitePress** 形态（`.vitepress/`）。域名 `mc.zgric.top`。纯静态、无后端、无外部 CDN 依赖。

## 本地

```bash
cd site
npm ci
npm run docs:dev       # http://127.0.0.1:5173
npm run docs:build     # -> site/.vitepress/dist
npm run docs:preview   # 预览构建产物
```

## 部署（GitHub Pages）

站点托管在 `kuangxing6367/zcbot-minecraftconsole` 的 GitHub Pages，自定义域名 `mc.zgric.top`。

1. `.github/workflows/pages.yml`：push 到 `main` 且 `site/**`（或该工作流）变更时自动构建并发布。
2. 仓库 Settings → Pages 的 Source 选 **GitHub Actions**。
3. `site/public/CNAME` 已写入 `mc.zgric.top`，构建时复制进 `site/.vitepress/dist`。
4. 域名服务商添加 `mc.zgric.top  CNAME  kuangxing6367.github.io.`；HTTPS 证书由 GitHub 签发。
5. `.vitepress/config.mts` 里 `base: '/'`，对应自定义域名根路径；GitHub Pages 不做 rewrite，保持 `cleanUrls: false`。

如需自托管，参考 `deploy/nginx.conf.example`：把 `site/.vitepress/dist` 交给 nginx，不要给文档站加指向控制台端口的反代。

## 内容结构

| 路径 | 内容 |
| --- | --- |
| `index.md` | 首页 hero 与特性 |
| `guide/` | 概览、组成与架构、快速开始、mc_link 适配器 |
| `protocol/` | 帧格式、**双向 RPC / 跳包机制正式规范**、跨语言互操作验证 |
| `deploy/` | Pages 部署说明 |
| `reference/` | 仓库文档索引、边界与变更 |
| `.vitepress/config.mts` | 导航、侧栏、搜索、页脚、head 元信息 |
| `.vitepress/theme/custom.css` | 暗色 + 玻璃质感的局部视觉微调 |

改文案直接编辑对应 Markdown。改域名需同步：`public/CNAME`、`.vitepress/config.mts` 的 `og:url`、`deploy/nginx.conf.example` 的 `server_name`。

## 文案事实依据

所有端口、版本、开关与协议字段以仓库内真实文件为准：`VERSION`、`core_plugins.yaml`、`core_plugins/mc_link/protocol.py`、`core_plugins/mc_link/rpc.py`、`mc-agent-java/pom.xml`、`mc-agent-java/src/main/resources/config.yml`、`docs/`。协议规范变更时必须同步更新源码注释与本文的 `protocol/` 章节。

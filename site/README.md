# MinecraftConsole 文档站

介绍 [MinecraftConsole](https://github.com/kuangxing6367/zcbot-minecraftconsole) 的静态文档站。
域名：`mc.zgric.top`。纯静态、无后端、无外部 CDN 依赖。

## 本地

```bash
cd site
npm ci
npm run dev      # http://127.0.0.1:5173
npm run build    # tsc --noEmit && vite build -> site/dist
npm run preview  # 预览构建产物
```

## 部署（GitHub Pages）

站点托管在 `kuangxing6367/zcbot-minecraftconsole` 的 GitHub Pages，自定义域名 `mc.zgric.top`。

1. `.github/workflows/pages.yml`：push 到 `main` 且 `site/**`（或该工作流）变更时自动构建并发布。
2. 仓库 Settings → Pages 的 Source 选 **GitHub Actions**；首次需启用 Pages。
3. `site/public/CNAME` 已写入 `mc.zgric.top`，构建时复制进 `site/dist`。
4. 域名服务商添加 `mc.zgric.top  CNAME  kuangxing6367.github.io.`；HTTPS 证书由 GitHub 签发。
5. 验证 `https://mc.zgric.top/`，DNS 未生效前以 Actions 工作流状态为准。

`vite.config.ts` 用 `base: './'` 相对资源路径，未绑自定义域名时 `https://kuangxing6367.github.io/zcbot-minecraftconsole/` 同样可用。

如需改回自托管，参考 `deploy/nginx.conf.example`：把构建产物交给 nginx，不要给文档站加指向控制台端口的反代。

## 内容维护

所有文案、链接、代码块集中在 `src/content.ts`；`src/App.tsx` 只负责结构与交互，
`src/styles.css` 负责视觉。改域名改 `content.ts` 里的 `site.domain` / `site.origin`，
以及 `index.html` 的 `og:url`、`deploy/nginx.conf.example` 的 `server_name`。

内容依据仓库内真实文件：`VERSION`、`README.md`、`core_plugins.yaml`、
`core_plugins/mc_link/protocol.py`、`mc-agent-java/pom.xml`、`docs/`。

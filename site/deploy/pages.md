# 文档站部署

本站是 **VitePress** 静态站点，由仓库内的 GitHub Actions 自动构建发布到 GitHub Pages，自定义域名 `mc.zgric.top`。纯静态、无后端、无外部 CDN 依赖。

| 项 | 值 |
| --- | --- |
| 站点框架 | VitePress |
| 源文件 | `site/*.md`、`site/.vitepress/` |
| 构建产物 | `site/.vitepress/dist` |
| 托管方式 | GitHub Pages · Source = GitHub Actions |
| 工作流 | `.github/workflows/pages.yml` |
| 自定义域名 | `mc.zgric.top`（`site/public/CNAME`） |

## 本地

```bash
cd site
npm ci
npm run docs:dev       # 本地预览，默认 http://127.0.0.1:5173
npm run docs:build     # 产物 site/.vitepress/dist
npm run docs:preview   # 预览构建产物
```

## 发布链路

1. push 到 `main` 且 `site/**` 或工作流文件变更时触发：`.github/workflows/pages.yml`。
2. Actions 里 `npm ci` → `npm run docs:build` → `actions/upload-pages-artifact` 上传 `site/.vitepress/dist` → `actions/deploy-pages`。
3. `site/public/CNAME` 会被 VitePress 复制到产物根目录，保证每次部署都保留自定义域名。
4. 也可在 Actions 页面手动 `workflow_dispatch`。

## 域名与 HTTPS

在域名服务商添加 CNAME 记录，仓库 Settings → Pages 里确认自定义域名；HTTPS 证书由 GitHub 签发。

```text
mc.zgric.top.    CNAME    kuangxing6367.github.io.
```

`base` 配为 `/`，对应自定义域名的根路径。GitHub Pages 是纯静态托管、不做 rewrite，所以**不要开 `cleanUrls`**，页面保持 `xxx.html` 路径。

## 验证顺序

1. 先看 Pages 工作流是否 success；
2. 再访问 `https://mc.zgric.top/`，确认首页、侧栏、搜索与静态资源都加载；
3. DNS 未生效时不要反复改配置，以工作流状态为准。

::: warning 不要把两件事混在一起
文档站（本页）与控制台 WebUI 是两个独立服务。Pages 只托管静态文件，不提供后端，也不会代理控制台端口；MC 侧链路仍是私有 TCP，与 HTTP 托管无关。
:::

## 自托管备选

仓库里保留 `deploy/nginx.conf.example` 作为参考：把 `site/.vitepress/dist` 交给 nginx 即可。改域名时同步修改 `site/public/CNAME`、`.vitepress/config.mts` 里的 og:url，以及 nginx 示例的 `server_name`。

::: info 用户部署偏好
本项目由你自己的服务器/域名托管；GitHub Pages 是用户明确要求的文档站托管方式，与控制台服务的自托管不是一回事。
:::

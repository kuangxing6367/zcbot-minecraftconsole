# 概览

控制器（Python）与 mc-agent（Java / Bukkit）通过一条轻量私有 TCP 链路通信：控制器负责「看与发」，agent 负责「在 Minecraft 服务器里真的执行」。本机可直连，异地经 FRP 的 **tcp** 隧道。

::: info 文档范围
本站只讲这个仓库本身。仓库 `docs/` 是框架级文档，见[仓库文档索引](/reference/index)。
:::

## 当前能力

| 能力 | 说明 |
| --- | --- |
| 状态总览 | 在线 / 离线、TPS、在线人数、最近心跳；心跳历史落库并绘制趋势图 |
| 命令台 | 下发 `list`、`give`、`say`、`op` 等命令，拿回真实执行输出 |
| 实时日志 | SSE 推送连接、命令、回执、离线告警，打开页面自动回放最近日志 |
| 文件管理 | 浏览、编辑、上传、下载、重命名、删除服务器文件；远端经 agent 操作 |
| 自动化工作流 | 触发 → 动作 → 条件分支 → 结束，网页可测试运行，QQ 关键词也能触发 |
| QQ 入口（可选） | 接上 OneBot 11 客户端后，群 / 私聊查状态、发命令 |

## 版本与底座

| 项 | 值 | 依据 |
| --- | --- | --- |
| 仓库版本 | `1.0.0-beta.0` | `VERSION` |
| 框架底座 | zcbot v1.20.0 | `framework/` |
| 服务器侧 | mc-agent 1.4.0 | `mc-agent-java/pom.xml`、`plugin.yml` |
| 协议适配器 | mc_link 1.1.0 | `core_plugins/mc_link/main.py` |
| 通信链路 | 私有 TCP 帧 · FRP tcp 隧道 | 默认端口 25599 |
| 文档站 | `mc.zgric.top` | GitHub Pages 自定义域名 |

## 上线前必须改掉默认值

::: warning 两处默认凭据
面板默认账号 `admin / admin123`；agent 与控制器共用默认 `secret`：`zcboot-mc-please-change-me`。

两端 secret 或端口不一致时，面板会一直显示离线，并在日志里出现 `bad-token`。改 secret 必须**两端同时改**。
:::

## 下一步

- [组成与架构](/guide/architecture) —— 看清两个组件和一条链路各自负责什么
- [快速开始](/guide/getting-started) —— 四步跑起来
- [双向 RPC / 跳包机制](/protocol/rpc) —— 新版本单连接双向主动消息的协议规范

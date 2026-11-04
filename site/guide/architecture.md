# 组成与架构

两个必需组件，一条私有链路。缺一不可。

```text
  浏览器 / QQ                 控制器 (Python)                MC 服务器 (Java)
  ┌──────────┐  WebUI/OneBot  ┌───────────────────┐  私有 TCP 帧  ┌──────────────┐
  │ 管理员入口 │ ────────────▶ │ 业务插件 + 框架层  │ ◀────────────▶ │ mc-agent     │
  └──────────┘                │ mc_link 适配器     │  FRP tcp 隧道  │ 执行 / 回传   │
                              └───────────────────┘                └──────────────┘
```

连接方向由 **MC 服务器（Java agent）出站**建立，Python 侧只监听。这条 TCP 是全双工长连接，两端随时都能写帧；因此不需要在服务器上新开任何入站端口。

## 三个部分

| 部分 | 位置 | 职责 |
| --- | --- | --- |
| 控制器业务插件 | `plugins/minecraftconsole/` | 发命令、收结果、跑工作流、存日志，提供 WebUI 与 SSE 推送 |
| mc-agent | `mc-agent-java/` | Bukkit 插件，执行 EXEC / QUERY / FS 三类指令并回传输出；兼容 MC 1.12+ / Java 8 |
| mc_link 适配器 | `core_plugins/mc_link/` | 新版本把 TCP 帧链路从业务插件里抽出来，注册为独立 `ProtocolAdapter` |

框架层已同步到 zcbot v1.20.0：支持多适配器并存、按 `source` 单独订阅。`mc_link` 以 `bot_name=mc_link` 作为事件来源，把帧转成 `notice.mc_heartbeat` / `notice.mc_output` / `notice.mc_command` / `notice.mc_log` 广播，业务插件单独订阅，不会串到 OneBot 或 Web 接入端。

::: info 为什么 MC 侧链路不走 HTTP
HTTP 属于 Web 类型，FRP 的 http 类型隧道存在风控问题。MC 侧链路全程使用私有 TCP 帧，没有 Host、URL、Header。Web 控制台 API 只是管理员浏览器访问通道，不经过 FRP，与这条链路互不相干。
:::

## 端口速查

| 端口 | 用途 |
| --- | --- |
| 25599 | MC 私有 TCP 帧链路（agent → 控制器） |
| 25598 | 控制台 API（业务插件，直连 / FRP 转发） |
| 8080 | 框架 Web 管理后台（`config.yaml → web.port`），根路由被业务插件接管为 MC 控制台壳页 |
| 8090 | 新版框架 webui 核心插件（`core_plugins.yaml → webui.port`），独立服务 |
| 6830 | 可选，OneBot 反向 WS |

::: warning 8080 没有被 8090 取代
框架升级后新增了 8090 的 webui 插件，但原来的 8080 面板仍在，且业务插件继续接管它的根路由。把 8090 当成「新面板地址」会连到另一个服务上。
:::

::: warning 文档站不是控制台
本站（Pages 静态托管）与控制台 WebUI 是两个独立服务。Pages 只托管静态文件，不提供后端，也不会代理控制台端口。
:::

## 链路上的两个方向

旧版语义是「Python 主动下发命令、agent 回传结果」。新版本在此基础上允许两端都主动发消息：

- agent 主动发起 RPC 请求（例如查询 pass 闸门状态）
- 控制器主动推变更 Notification，并在心跳里回带 `epoch` 书签

冲突的根源不是「要不要多开端口」，而是**同一条连接上两边都能主动发包，包却没有身份、方向和关联**。解法见[双向 RPC / 跳包机制](/protocol/rpc)。

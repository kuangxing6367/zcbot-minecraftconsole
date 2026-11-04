---
layout: home
hero:
  name: MinecraftConsole
  text: 一条私有 TCP 链路，管住远端的服务器
  tagline: 控制器（Python）与 mc-agent（Java / Bukkit）通过轻量私有帧协议通信。可本机直连，也可经 FRP 的 tcp 隧道跨网管理。
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/getting-started
    - theme: alt
      text: 阅读协议规范
      link: /protocol/rpc
features:
  - title: 私有 TCP，不是 HTTP
    details: MC 服务器与控制器之间没有 Host、URL、Header，链路上报走私有帧与 FRP tcp 隧道，绕开 http 类型隧道的风控问题。
    link: /guide/architecture
  - title: 独立协议适配器
    details: 帧链路已从业务插件抽成 core_plugins/mc_link，注册为独立 ProtocolAdapter，按 source 单独订阅事件，不与 OneBot / Web 接入端串线。
    link: /guide/adapter
  - title: 单连接双向主动消息
    details: 新版本 v1 在同一条全双工长连接上做双向 RPC：能力协商、按方向奇偶 id、epoch 书签对账，不新增端口。
    link: /protocol/rpc
  - title: 跨语言逐字节对齐
    details: Python 与 Java 各有一套 FrameCodec，历史 64B 帧头字节级冻结；扩展只使用保留区，旧端全 0 忽略。
    link: /protocol/frames
  - title: 帧级认证与防重放
    details: 每帧带 HMAC 令牌、秒级时间窗、单调序号；v1 的保留区与 RPC 正文另有独立签名。
    link: /protocol/frames
  - title: 静态文档站
    details: 本站在 GitHub Pages 上以自定义域名 mc.zgric.top 提供，纯静态、无后端、无外部 CDN 依赖。
    link: /deploy/pages
---

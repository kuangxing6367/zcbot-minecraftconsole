# 边界与变更

用之前先知道这几条。

| # | 边界 | 说明 |
| --- | --- | --- |
| 01 | 链路无内置 TLS | 私有 TCP 协议设计上跑在 FRP 私有隧道里，本身不提供 TLS；隧道与网络边界由部署方负责 |
| 02 | HTTP 的位置 | HTTP 只用于管理员浏览器访问 WebUI 与文档站，不用于 MC 服务器到控制器之间的链路上报 |
| 03 | 两个 Web 端口 | `config.yaml → web.port: 8080` 是被业务插件接管的面板；`core_plugins.yaml → webui.port: 8090` 是新增的 webui 核心插件。两者并存，不是替代关系 |
| 04 | 适配器默认关闭 | `mc_link` 需在 `core_plugins.yaml` 显式开启并填入与 agent 一致的 secret，否则走内置监听 |
| 05 | RPC 默认关闭 | `mc-agent/config.yml` 的 `rpc-enabled: false`，关闭时帧字节与 mc-agent 1.3.0 完全一致 |
| 06 | 规划中的下一版本 | 白名单 / 验证码 / 自动与手动审批（zcore）尚未实现；当前只有 fail-closed 占位接口 |
| 07 | 文档站不是控制台 | Pages 只托管静态文件，不含后端、不代理控制台端口 |

## 变更记录（相对旧版）

| 变更 | 影响 | 兼容性 |
| --- | --- | --- |
| 框架同步到 zcbot v1.20.0 | 多适配器并存、按 source 订阅；新增 webui 核心插件（8090） | 原 8080 面板与业务插件根路由接管保持不变 |
| 抽出 `mc_link` 独立适配器 | 链路层可由业务插件委托；事件以 `bot_name=mc_link` 广播 | 默认关闭，关闭时行为不变 |
| v1 帧扩展 | 保留区带能力位 / epoch / 签名；新增 `0x05` / `0x06`；`0x04` 纳入变长切分 | 基础 64B 帧头字节不变；v0 端忽略保留区 |
| 心跳 BOOKMARK | 控制器对 v1 心跳回一帧只带 epoch 的心跳 | v0 端不期待、不解析，不受影响 |
| mc-agent 1.4.0 | Java 侧新增 FrameCodec v1、RpcHub、MiniJson；jar 仍不入库 | `rpc-enabled: false` 时帧字节与 1.3.0 一致 |
| 文档站改为 VitePress | `site/.vitepress/` 形态，构建产物改为 `site/.vitepress/dist` | 域名 `mc.zgric.top` 不变 |

## 已知需要注意的点

- **时钟**：帧 `ts` 是秒级时间戳，两端偏差超过 3s 会全部丢帧。
- **secret 一致性**：两端 secret 不一致表现为「一直离线 + `bad-token`」，不是连不上。
- **jar 交付**：`mvn clean package` 产出 `target/mc-agent.jar`，走 CI artifact / Release，不复制进仓库。
- **`port: 0`**：适配器把它当「随机端口」，是测试用的合法值，不会被兜底成 25599。
- **UDP 心跳专线**：FRP UDP 心跳与 TCP BOOKMARK 是两条独立路径，不要互相替代。

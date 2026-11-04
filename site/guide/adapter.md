# mc_link 适配器

链路层从业务里独立出来。它注册为框架里的一个独立 `ProtocolAdapter`，**默认关闭**；关闭时业务插件仍用内置 TCP 监听，旧部署行为不变。

## 启用

```yaml
# core_plugins.yaml
  mc_link:
    enabled: false            # 改成 true
    host: 0.0.0.0
    port: 25599
    secret: ''                # 留空则回退 MC_SECRET 环境变量，再回退默认值
    bot_name: mc_link
```

`secret` 必须与 mc-agent 的 `config.yml` 一致。`port: 0` 是合法值，表示让内核随机分配端口（互操作测试用），不会被兜底成 25599。

## 事实速查

| 项 | 值 |
| --- | --- |
| 服务名 | `mc_link`（专用，不抢 `protocol_adapter` / `api_caller`） |
| 事件来源 | `bot_name = mc_link` |
| 广播事件 | `notice.mc_heartbeat` / `mc_output` / `mc_command` / `mc_log` |
| 链路形态 | MC agent → FRP tcp → mc_link:25599 → dispatch_event → 业务插件 |
| 隧道类型 | tcp（非 http） |

## 开关的两种状态

**启用时**：业务插件把命令下发、状态查询、回执关联委托给适配器，并跳过内置 TCP 监听，避免端口抢占。

**关闭时**：业务插件自动退回内置 TCP 监听，无需改任何配置。

::: info 为什么要独立成适配器
新版框架支持多个协议适配器并存，且业务插件可以按 `source` 单独订阅。把 MC 私有 TCP 链路和 OneBot / QQ 官方 / Web 接入端分开后，链路升级不必改业务插件，事件也不会串到别的接入端。
:::

## v1 能力

适配器当前版本 `1.1.0`，在历史帧协议之上增加：

- 能力协商（`cap` 位图：`CAP_RPC` / `CAP_PASS` / `CAP_PUSH`）
- 心跳 BOOKMARK / ACK：控制器回一帧只带权威 `epoch` 的心跳
- `0x05 REQ` / `0x06 RESP` 双向 RPC，JSON-RPC 2.0 envelope
- `zcore.sync` 增量对账，`zcore.pass.changed` 主动推 Notification
- `zcore.pass.query` 默认 fail-closed：未接入真实签发者时返回 `pending`，绝不默认放行

规范与边界见[双向 RPC / 跳包机制](/protocol/rpc)。

::: warning 尚未接入的部分
pass 闸门目前只有适配器的 fail-closed 占位实现，白名单 / 验证码 / 审批签发者（zcore whitelist）属于规划中的新版本，仓库里尚未实现。业务插件可用 `register_method()` 注入真实实现。
:::

## 验证

`tests/test_mc_link.py` 覆盖历史帧字节一致性、TCP 往返、回执关联、坏 token / 过期帧丢弃、多适配器注册，以及 v1 扩展签名、能力闸门、未知帧整帧跳过、RPC 幂等与 fail-closed。跨语言逐字节验证见[互操作验证](/protocol/interop)。

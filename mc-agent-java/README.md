# mc-agent (Java 服务器侧)

[minecraftconsole](../plugins/minecraftconsole/README.md) 的 **Minecraft 服务器侧 agent**（Bukkit/Spigot/Paper 插件）。
它连到 ZCBOT 侧的 minecraftconsole 控制器，周期上报心跳、接收远程命令并执行。

兼容 **MC 1.12+ / Java 8**（面向老旧服务器）。

## 作用

```
[MC 服务器] mc-agent  ──64B 心跳(ts+token+tps+玩家)──►  minecraftconsole 控制器
             ◄────────384B 命令帧(如 give @a command_block)──┘
             └─► 主线程 Bukkit.dispatchCommand(consoleSender, cmd) 执行
```

## 构建

需要 **JDK 8+** 与 Maven：

```bash
mvn clean package
# 产物：target/mc-agent.jar
```

## 安装与配置

1. 把 `mc-agent.jar` 放入服务器 `plugins/`，重启服务器。
2. 编辑 `plugins/mc-agent/config.yml`：

```yaml
host: 127.0.0.1        # 控制器地址（经 FRP 转发到内网，或直连内网地址）
port: 25599            # 必须与 minecraftconsole 的 tcp_port 一致
secret: "zcboot-mc-please-change-me"   # 必须与 minecraftconsole 的 secret 一致
interval-ms: 1000      # 心跳周期
reconnect-ms: 3000     # 断线重连间隔

# ── FRP 心跳（专用 UDP，国内低延迟）──
# 心跳不再走 TCP 专线，而是把 64B 心跳以 UDP 发给下面 URL 返回的节点清单（每节点 host:port）。
# 路径：agent → frp 公共 UDP 入口 → 139:4094 → 82:25600 控制器 UDP 入口。
# 节点清单每小时自动拉一次（格式固定 {"nodes":[{"host":"...","port":12345}]}）。
frp-enabled: true
frp-nodes-url: "https://api.zgric.top/frp_nodes.json"
frp-beat-ms: 2500     # 心跳周期（毫秒），轮询单节点发送，总速率=60000/该值 包/分钟。
                      # 控制器按「收到包的源 IP」每 60s 限 30 包：若 139→82 转发没保留 frp 节点源 IP，
                      # 这个值必须 ≥2000 才不超限；默认 2500 留余量。
```

> `secret` 两端必须一致，否则心跳/命令会被判 `bad-token` 丢弃。

### FRP 心跳说明

- 默认开启（`frp-enabled: true`）：agent 启动即异步拉一次节点清单，之后**每 1 小时**刷新；
  心跳线程按 `frp-beat-ms` **轮询**向清单里一个节点发 64B UDP 心跳，下一个周期换下一个节点
  （某个节点挂了会自动跳过，仍保活）。
- 为什么轮询单节点而不是全发：控制器按收到包的**源 IP** 每 60s 限 30 包；若 139 把 4094 转发到
  82:25600 时没保留 frp 节点源 IP（所有包都算 139 一个 IP），多发就会互相挤掉。轮询单节点使总速率
  恒定 ≤30/分钟，无论转发是否保留源 IP 都安全。
- 即使 UDP 心跳被限，TCP 控制通道每 1s 仍发心跳保活，服务器**不会因此离线**（在线判定只看
  `last_beat`，与走哪条连接无关）。
- 关掉 FRP 心跳：`frp-enabled: false` 退回旧逻辑（TCP 专线到 `beat-port`）。

## 安全

与控制器端一致，无 TLS（走 FRP 私有隧道）：
- HMAC-SHA256 Token（固定对称密钥）
- 时间戳 3s 窗口：超窗丢弃（防重放 / DDoS）
- 单调序号 `ts <= lastTs`：拦截窗口内重放
- Token 比较用 `MessageDigest.isEqual`（恒定时间）

## 帧格式（与 Python 端完全一致，大端序）

| 帧 | 长度 | 布局 |
|----|------|------|
| 心跳 | 64B | `type=0x01(1) ts(4) token(16) tps(4) players(4) 填充(35)` |
| 命令 | 384B | 前 64B 复用帧头`type=0x02` + `len(4)` + 命令正文(≤316B) + 填充 |

线程模型：TPS/玩家数在主线程（Bukkit 调度器）采样写 volatile，网络线程只读；命令执行回主线程 `dispatchCommand`，保证线程安全。

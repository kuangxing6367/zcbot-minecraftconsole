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
```

> `secret` 两端必须一致，否则心跳/命令会被判 `bad-token` 丢弃。

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

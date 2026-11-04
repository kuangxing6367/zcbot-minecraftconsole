# 帧格式

MC agent 与控制器之间的私有 TCP 帧协议。大端序，设计为跑在 FRP 私有隧道里；协议本身不提供 TLS。

::: warning 三方契约
帧格式由三处共同实现：Java `mc-agent-java/.../FrameCodec.java`、Python `core_plugins/mc_link/protocol.py`，以及旧业务插件里的内置编解码。改动基础帧头会同时打断 Java agent，必须同步修改并做逐字节验证。
:::

## 基础帧头（64B，字节级冻结）

```text
偏移   长度   字段      说明
0      1B     type      帧类型
1      4B     ts        秒级时间戳（uint32 BE）
5      16B    token     HMAC-SHA256(secret, ts4BE)[:16]
21     4B     tps       float，上报的 TPS
25     4B     players   uint32，在线人数
29     4B     plen      正文长度（心跳为 0，上限 16MB）
33     4B     seq       单调序号（按方向独立）
37     27B    保留区     旧实现全 0
64…    变长   body      UTF-8 指令 / 输出，或 v1 的 RPC 载荷
```

| 项 | 规则 |
| --- | --- |
| 心跳帧 | 固定 64B，无载荷 |
| 令牌 | `HMAC-SHA256(secret, big-endian ts)[:16]` |
| 时间窗 | 与本地时间差超过 3s 丢弃 |
| 防重放 | 常数时间比对 + 单调序号 |
| 离线判定 | 6s 收不到心跳 |
| 默认端口 | 25599 |

## 帧类型

| 值 | 名称 | 方向 | 载荷 | 说明 |
| --- | --- | --- | --- | --- |
| `0x01` | HEARTBEAT | agent → 控制器（v1 支持反向 ACK） | 无 | 固定 64B |
| `0x02` | COMMAND | 控制器 → agent | UTF-8 指令 | 历史 |
| `0x03` | OUTPUT | agent → 控制器 | UTF-8 输出 | 历史回执 |
| `0x04` | LOG | agent → 控制器 | UTF-8 日志 | Java 侧早已定义，v1 起纳入变长帧切分 |
| `0x05` | REQ | 双向 | `sig(16B) + JSON` | v1 RPC 请求 / Notification |
| `0x06` | RESP | 双向 | `sig(16B) + JSON` | v1 RPC 响应 |

`0x01` 是固定帧；`0x02`–`0x06` 按 `plen` 切分。未知帧类型**按 `plen` 整帧跳过**，不逐字节扫描污染字节流（借鉴 RFC 9113 §4.1 的扩展帧处理思路）。

## v1 保留区布局

v1 只占用 37–63 的保留区，基础帧头其余字段一个字节都不改。旧端把保留区全 0 写入并忽略读取，因此 v0 与 v1 可以混用。

```text
偏移     长度   字段       说明
37-38   2B     magic      b"ZC"，识别 v1 扩展
39      1B     ver        = 1
40      1B     flags      bit0 = CAP 有效 / bit1 = EPOCH 有效
41-44   4B     cap        能力位图
45-48   4B     epoch      单调版本戳（心跳书签，只带版本不带数据）
49-56   8B     ext_sig    HMAC-SHA256(secret, ts4BE || f[37:49])[:8]
57-63   7B     保留        全 0
```

能力位：

| 位 | 名称 | 含义 |
| --- | --- | --- |
| `0x01` | `CAP_RPC` | 支持 REQ / RESP |
| `0x02` | `CAP_PASS` | 支持 pass 闸门查询 |
| `0x04` | `CAP_PUSH` | 接受控制器主动推 Notification |

## 两级签名

基础 `token` 只覆盖 `ts`，因为旧端要按原逻辑校验它。v1 因此另加两级独立签名：

```text
ext_sig = HMAC-SHA256(secret, ts4BE || f[37:49])[:8]           # 保护 cap / epoch
rpc_sig = HMAC-SHA256(secret, ts4BE || f[37:49] || json)[:16]  # 保护 RPC 正文
```

`rpc_sig` 直接覆盖**原始 JSON 字节**，不做 JSON 规范化，避免两端因键序或空白产生歧义。RPC 帧的 `body = rpc_sig + json_bytes`。

## 兼容规则

| 场景 | 行为 |
| --- | --- |
| `magic != "ZC"` | 视为 v0：跳过扩展校验，只跑历史 token / 时间窗 / 序号逻辑，行为与 1.3.0 完全一致 |
| v1 帧但 `ext_sig` 不符 | 整帧丢弃（防止伪造 cap / epoch） |
| v1 帧但 `rpc_sig` 不符 | 整帧丢弃 |
| 对端 `cap` 不含 `CAP_RPC` | 本端不得发 REQ / RESP，能力协商前一律 fail-closed |
| 收到未知 type | 按 `plen` 丢弃整帧，连接继续 |

::: info 定位说明
本节描述的是**本工程的私有二进制帧**，不是任何公开协议的实现。RFC 9113 / RSocket 等只作为设计参照，帧长、字段、签名与端口都是本项目自定义，不可与那些标准的数值横向比较。
:::

下一步：[双向 RPC / 跳包机制](/protocol/rpc)。

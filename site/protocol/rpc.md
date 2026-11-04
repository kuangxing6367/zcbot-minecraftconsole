# 双向 RPC / 跳包机制规范

::: info 定位说明
本文是 **minecraftconsole v1 私有链路的正式协议规范**，描述对象是「MC agent（Java）↔ 控制器 mc_link（Python）」**同一条已存在的 TCP 全双工长连接**上的双向主动消息机制，与 [帧格式](/protocol/frames) 是同一套协议的消息层与字节层。

文中引用的 JSON-RPC 2.0、RFC 9113、RSocket、Kubernetes watch 只是**设计参照**，本实现不是它们中的任何一个，帧长、id 规则、错误码数值、超时与窗口均为本项目自定义，**不可与那些标准的数值横向比较**。
:::

## 1. 问题定义

旧版语义是「Python 主动下发命令、agent 回传结果」。新版本需要反过来：由 MC 服务器端主动向 Python 侧发起请求（例如查询玩家 pass 闸门状态），同时控制器也要能主动推变更。

约束：

- 服务器侧**不能新开入站端口**，也不接受为 RPC 再建一条连接；
- 链路**不能走 HTTP**（FRP http 类型隧道有风控问题）；
- 两端都可能在同一时刻主动写帧，读循环里收到的下一个包**未必是自己发出去的那笔的响应**。

先核对一个容易搞错的前提：连接是 **Java agent 出站**建立到 Python 控制器（`MCAgent` 里 `new Socket().connect(...)`），Python 只监听 25599。这条 TCP 本身是全双工的，Python 随时可以写帧 —— 所以**根本不需要新端口**。真正缺的是：包的身份、方向、请求/响应关联，以及断线重连后的状态补齐。

::: tip 「跳包」这个说法
口语里的「跳包」在本规范里被拆成两件事：**心跳 BOOKMARK**（只带版本戳的对账节拍）和 **独立 RPC 帧**（业务数据）。心跳不承担数据通道，因此不会被大结果挤掉保活，也不会破坏固定 64B 心跳帧。
:::

## 2. 方案取舍

| 方案 | 做法 | 新端口 | 代价 |
| --- | --- | --- | --- |
| A 纯反向拉 | Java 发查询，Python 挂起等结果 | 0 | 与命令下发争用同一读循环；仍要解决 id 关联；pass 未定会把请求挂住 |
| B 心跳捎带数据 | `0x01` 带请求、ACK 带结果 | 0 | 破坏固定 64B 心跳与 UDP 心跳专线；延迟至少 1 个心跳周期；大结果挤掉保活 |
| **C 心跳带版本戳 + 独立 RPC 帧** | 心跳只带 `epoch`，变了才拉；业务走 `0x05/0x06` | 0 | 需三端同步帧类型 + 能力协商 |

**选 C**。理由：把「对账」与「传数据」解耦 —— 控制器一签发 pass，直接在已存在的连接上推事件；Java 侧重启或断连后，靠心跳里的 `epoch` 发现自己落后，再发一次增量拉取。代价是协议复杂度上升：新增两个帧类型、两级签名、能力协商和一套双端状态机。

## 3. 设计依据与对应关系

| 参照 | 借用的机制 | 在本实现中的形态 |
| --- | --- | --- |
| JSON-RPC 2.0 §2/§4/§4.1/§5.1/§8 | 双端可同时为 Client/Server；`id` 关联；Notification 无 id 且 `MUST NOT` 回复；`-32768..-32000` 保留错误区；`rpc.` 前缀保留 | JSON envelope；`rpc.capabilities` 为保留方法 |
| RFC 9113 §4.1 / §5.1.1 | 未知帧类型忽略；Stream ID 按方向奇偶、单调递增、`0` 保留给连接级 | 请求 id 奇偶方向隔离；未知 type 按 `plen` 整帧跳过 |
| RSocket SETUP / KEEPALIVE / RESUME | 能力协商；KEEPALIVE 带 `Last Received Position`；断线后增量恢复 | `cap` 位图；心跳 BOOKMARK 带 `epoch`；`zcore.sync` 增量对账 |
| Kubernetes watch / BOOKMARK / 410 Gone | `resourceVersion` + bookmark；历史窗口不足时回退全量 relist | `epoch` 书签；`-32010` 要求 `since=0` 全量重拉 |

## 4. 规范正文

### 4.1 连接与角色

- 一条 TCP 连接 = 一个 RPC 会话；连接建立方（Java agent）是 `client`，接受方（Python mc_link）是 `server`。
- 角色只用于 **id 奇偶分配**，不限制谁可以发起请求：两端都能发 REQ，也都要能收 RESP。
- 会话状态（能力、已知 epoch、待处理请求、结果缓存、收发序号）绑定在连接上；**断连即清空**。

### 4.2 能力协商

v1 能力通过 64B 帧头保留区的 `magic = b"ZC"`、`ver = 1`、`flags`、`cap` 声明，并由 `ext_sig` 保护。

- 收到带 `magic` 的首帧才认为对端支持 v1；没有 `magic` 的对端一律按 v0 处理。
- 能力协商完成前，任何 RPC 调用都 **fail-closed**，不发出 REQ 帧。
- 对端 `cap` 不含 `CAP_RPC` 时不得发 REQ / RESP；不含 `CAP_PUSH` 时不推 Notification。
- `rpc.capabilities` 方法返回 `{ver, role, methods, cap}`，供运行时探测。

### 4.3 心跳 BOOKMARK / ACK

历史实现里心跳是 **Java → Python 单向**，Python 并不回 ACK（Java 注释里声称存在，代码里没有）。v1 补上这一帧：

1. agent 周期性发 `0x01` 心跳，`cap` 声明自身能力，`epoch` 带自己**已知**的版本戳；
2. 控制器收到 v1 且 `cap & CAP_RPC` 的心跳后，回一帧**同类型反方向**的 `0x01`，只带权威 `epoch`，不带业务数据；
3. agent 比较两端 `epoch`：本地落后则触发一次 `zcore.sync` 增量拉取。

心跳帧长仍是 64B。旧 v0 agent 不解析保留区，也不期待回帧，行为不变。

### 4.4 RPC envelope

`0x05 REQ` / `0x06 RESP` 的载荷为 `rpc_sig(16B) + UTF-8 JSON`。JSON 遵循 JSON-RPC 2.0 形状：

```json
// request
{"jsonrpc":"2.0","method":"zcore.pass.query","params":{"player":"Steve"},"id":1}
// notification（无 id）
{"jsonrpc":"2.0","method":"zcore.pass.changed","params":{"epoch":7}}
// response
{"jsonrpc":"2.0","result":{"state":"pending","reason":"unavailable"},"id":1}
// error
{"jsonrpc":"2.0","error":{"code":-32010,"message":"epoch too old"},"id":3}
```

判定顺序：有 `method` + 有 `id` → request；有 `method` + 无 `id` → notification；无 `method` + 有 `id` + 有 `result`/`error` → response；其余 invalid，回 `-32600`。

### 4.5 id 与方向

| 规则 | 内容 |
| --- | --- |
| 奇偶 | `client`（连接发起方 / Java）用**奇数**，`server`（Python）用**偶数** |
| `0` | 保留给连接级消息，任何一端都不分配 |
| 递增 | 单调递增、不复用；`owns(id)` 用来判断响应归属 |
| 帧 `seq` | 与 RPC `id` 是两回事：`seq` 按方向各自单调，用于帧级防重放；`id` 用于请求/响应关联 |

### 4.6 Notification

Notification 没有 `id`，接收方 **MUST NOT 回复**；方法处理器抛错也不回错误帧。控制器在 `cap & CAP_PUSH` 的 peer 上推 `zcore.pass.changed`。

### 4.7 错误码

| 码 | 含义 |
| --- | --- |
| `-32700` | JSON 解析失败 |
| `-32600` | Invalid Request |
| `-32601` | Method not found |
| `-32602` | Invalid params |
| `-32603` | Internal error |
| `-32010` | epoch 过旧：历史窗口不足，清空本地状态并 `since=0` 全量重拉（等价 410 Gone） |
| `-32011` | pass 闸门不可判定（超时 / 连接关闭 / RPC 不可用） |

### 4.8 幂等

同一连接上，同一 `id` 的重复 request 直接返回缓存结果，不重复执行处理器。缓存为 LRU，上限 256 条、TTL 60s。目的是让网络重试不会造成重复签发。

### 4.9 epoch 增量对账

- 控制器维护单调 `epoch` 与有限长度变更日志（窗口 1024 条）。
- 业务侧签发变更后调用 `bump_epoch(item)`：`epoch += 1`、记录变更、向 `CAP_PUSH` peer 推 Notification。
- agent 发现权威 `epoch` 领先时，发 `zcore.sync` 带 `since = knownEpoch`；返回 `{epoch, since, full, items}`。
- `since = 0` 表示全量重拉；`since` 落后于历史窗口起点时返回 `-32010`，agent 清空本地状态后重发 `since=0`。
- 为避免心跳抖动引发风暴，Java 侧对同步请求做 300ms 节流。

### 4.10 fail-closed 闸门

`zcore.pass.query` 属于 `FAIL_CLOSED_METHODS`。以下任一情况都返回 `{"state":"pending","reason":"unavailable"}`，**绝不默认放行**：

- 方法未注册、处理器抛异常、返回值不是对象；
- 能力尚未协商完成、连接关闭、调用超时；
- 真实签发者（zcore whitelist）尚未接入。

这是新版本白名单系统的安全底线：闸门查不到答案时，玩家应停在「等待」而不是被放行。

## 5. 时序

```text
Java agent (client, 奇数 id)                Python mc_link (server, 偶数 id)
        |                                            |
        |--- 0x01 heartbeat  cap=CAP_ALL, epoch=N ---->|
        |<-- 0x01 BOOKMARK   cap=CAP_ALL, epoch=M -----|   M = 权威版本
        |                                            |
        |   M > N ? --- 0x05 REQ zcore.sync {since:N}->|
        |<-- 0x06 RESP {epoch:M, items:[...]} ---------|
        |                                            |
        |--- 0x05 REQ  id=1  zcore.pass.query -------->|
        |<-- 0x06 RESP id=1  {state:"pending"} --------|
        |                                            |
        |<-- 0x05 NOTIFY     zcore.pass.changed --------|   签发后主动推（无回复）
        |                                            |
  断连重连：会话状态清空 → 重新能力协商 → BOOKMARK → 按需增量 / 全量重拉
```

## 6. 兼容性与开关

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `core_plugins.yaml → mc_link.enabled` | `false` | 是否启用独立适配器；关闭时业务插件走内置 TCP 监听 |
| `mc-agent config.yml → rpc-enabled` | `false` | 是否声明 v1 能力并启用 RPC |

`rpc-enabled: false` 时，Java 发出的心跳用历史打包函数，保留区全 0，**所有帧字节与 mc-agent 1.3.0 完全一致**，可与旧控制器混用。启用后复用同一条连接，不新增端口、不走 HTTP。

三条兼容硬规则：

1. 基础 64B 帧头任何字段都不改；扩展只写保留区。
2. 旧端忽略保留区；新端见到 `magic` 才按 v1 解析。
3. 未知帧类型按 `plen` 整帧跳过，不允许逐字节扫描。

## 7. 安全边界与代价

| 项 | 说明 |
| --- | --- |
| 认证 | 每帧 `token` + 保留区 `ext_sig` + RPC 正文 `rpc_sig`，三层都基于共享 secret |
| 防重放 | 3s 时间窗 + 按方向单调的帧 `seq` |
| 不可抵赖 / 加密 | **不提供**。没有 TLS、没有证书、没有非对称签名；隧道与网络边界由部署方负责 |
| 资源上限 | 帧载荷 16MB；变更窗口 1024 条；结果缓存 256 条 / 60s |
| 复杂度代价 | 三端需同步帧类型与能力位；老端永远看不懂新帧，只能靠兼容规则兜住 |
| 时钟依赖 | `ts` 用秒级时间戳并要求两端时钟偏差在 3s 内，这是历史协议既有约束 |

## 8. 尚未落地（明确边界）

- **第三端**：白名单 / 验证码 / 自动与手动审批的 pass 签发者（zcore whitelist）尚未实现。当前只有适配器侧的 `zcore.pass.query` fail-closed 占位与 `register_method()` 注入口。
- **Release 交付**：jar 由 CI artifact 产出（`target/mc-agent.jar`），自动发布到 GitHub Release 的工作流尚未配置。
- **默认关闭**：`rpc-enabled` 与 `mc_link.enabled` 都为 `false`，要实际启用需部署配置与业务插件接入。
- **UDP 心跳专线**：FRP UDP 心跳保持原有实现，与本文的 TCP BOOKMARK 是两条独立路径，不可混为一谈。

## 9. 验证

| 层 | 位置 | 覆盖 |
| --- | --- | --- |
| Python 单元 | `tests/test_mc_link.py` | 历史帧字节一致、v1 扩展签名、能力闸门、未知帧跳过、RPC 幂等 / fail-closed / 410、真实 TCP 往返 |
| Java 构建 | `mc-agent-java/pom.xml` | `mvn clean package` 产出 `target/mc-agent.jar` |
| 跨语言互操作 | 会话工作区 `audit/InteropSmoke.java` + `audit/interop_server.py`（尚未入库） | Python golden frames 与 Java 逐字节比对、MiniJson 往返、fail-closed、BOOKMARK、live RESP id 关联 |

详见[跨语言互操作验证](/protocol/interop)。

---

**版本**：协议扩展版本 `EXT_VER = 1`。规范变更必须同步更新 `protocol.py` / `FrameCodec.java` 注释、本文与互操作 golden frames。

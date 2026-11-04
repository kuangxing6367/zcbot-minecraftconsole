# 跨语言互操作验证

帧协议由 Python 与 Java 两套独立实现共同承担，只靠各自的单元测试不足以证明「逐字节一致」。这里用一组不依赖 Bukkit 的 golden frames 做跨语言比对，再起一个真实 TCP 适配器跑通整条链路。

::: info 定位说明
本文记录的是**本仓库的验证手段与验证项**，不是通用测试框架说明。golden frames 由当前 Python 实现生成，Java 侧比对的是同一份字节；任何一端改了帧布局，这组比对必须同步更新，否则验证失去意义。
:::

## 三个层次

| 层 | 做什么 | 为什么需要 |
| --- | --- | --- |
| 静态字节比对 | Python 生成 v0 heartbeat、v1 heartbeat、REQ、RESP 的 golden 字节，Java `FrameCodec` 生成同样条件的帧并逐字节比较 | 单测各自通过 ≠ 两端拼出来的字节相同；大端序、签名切片、保留区偏移最容易在这里错 |
| 语义比对 | `MiniJson` 的 object / array / number / string / boolean / null 往返；能力协商前的 fail-closed | Java 8 / Bukkit 1.12 环境零依赖 JSON，不能靠 Gson，必须自己验证 |
| 真实 TCP | 启动真实 `McLinkAdapter`，Java 发 v1 heartbeat → 收 BOOKMARK → 发 `rpc.capabilities` REQ → 收 RESP → 发 `zcore.sync` 全量重拉 | 证明读循环、帧切分、id 关联、对账节拍在同一条连接上真的能跑通 |

## 关键事实核对

- 心跳是 **Java → Python 单向**；Python 侧原本没有 ACK 实现，v1 才补上 BOOKMARK 回帧。
- 因此 TCP 段的读序列会多一帧：测试必须**按 `plen` 切帧并按 type 挑选**，不能一上来 `read()` 就当成 RESP。
- 适配器 `port: 0` 是合法的「随机端口」，用于互操作测试；不能用 `or` 把它兜底成默认 25599。
- 未知帧类型按 `plen` 整帧跳过，避免把未知载荷误切进后续帧。

## 相关文件

| 文件 | 作用 |
| --- | --- |
| `core_plugins/mc_link/protocol.py` | Python 帧编解码、扩展区与两级签名 |
| `core_plugins/mc_link/rpc.py` | JSON-RPC 语义层、id 分配、结果缓存 |
| `mc-agent-java/src/main/java/com/zgric/mcagent/FrameCodec.java` | Java 帧编解码，与 Python 逐字节对齐 |
| `mc-agent-java/src/main/java/com/zgric/mcagent/MiniJson.java` | Java 8 零依赖 JSON |
| `mc-agent-java/src/main/java/com/zgric/mcagent/RpcHub.java` | Java 双向 RPC 状态机 |
| `tests/test_mc_link.py` | Python 侧单元与 TCP 往返测试 |

## 复现方式

::: info 脚本位置
`interop_server.py` 与 `InteropSmoke.java` 是协议验证脚本，当前放在**会话工作区** `audit/` 目录，尚未纳入仓库。若协议后续还要演进，应迁入仓库 `tests/interop/` 以便长期复用。下面命令里的 `A` 指该目录。
:::

```bash
# 1. Python 单元测试
cd /e/工程/minecraftconsole && python tests/test_mc_link.py

# 2. Java 打包（jar 不入库）
cd mc-agent-java && mvn -B -ntp clean package -DskipTests

# 3. 编译 Java smoke（只需 target/classes，不需要 Bukkit）
cd /e/工程/minecraftconsole
javac -encoding UTF-8 -cp "mc-agent-java/target/classes" -d "$A/classes" "$A/InteropSmoke.java"

# 4. 起真实适配器（后台），它会写 golden.txt / port.txt 并保持监听
rm -f "$A/port.txt" "$A/golden.txt"
python "$A/interop_server.py" &

# 5. 另一个终端：等 port.txt 出现后跑 Java smoke
cd "$A" && java -cp "E:/工程/minecraftconsole/mc-agent-java/target/classes;classes" InteropSmoke "$A"
```

::: warning 顺序
`interop_server.py` 会在监听后写出 `port.txt` 并保持 180 秒；Java smoke 必须在它还活着的时候连上去。服务提前退出会表现为 `Connection refused`，而不是协议错误。
:::

## 本次实测结果

| 项 | 结果 |
| --- | --- |
| Python 单元 `tests/test_mc_link.py` | 23 tests · `OK` |
| Java `mvn clean package` | `BUILD SUCCESS`，产出 `target/mc-agent.jar` |
| VitePress `npm run docs:build` | `build complete`，产物含 `CNAME` |
| 跨语言 `InteropSmoke` | `SUMMARY passed=21 failed=0` |

21 项覆盖：v0 心跳字节与 legacy 识别、v1 心跳字节、扩展 roundtrip / 签名、篡改 epoch 拒绝、REQ / RESP 帧字节与签名、REQ JSON 提取、篡改 RESP 拒绝、MiniJson 解析与往返、能力协商前 fail-closed、BOOKMARK 的 v1 判定 / 签名 / 携带服务端 epoch、live RESP 签名与 `id` 关联、live 方法表、`zcore.sync` 全量重拉。

若任一端改动了帧布局，先更新 golden frames 再谈兼容。

::: tip 判据
静态比对必须逐字节相等；TCP 段必须看到 BOOKMARK、RESP 的 `id` 与请求一致、`zcore.sync` 返回 `full=true`。只看到「连上了」不算通过。
:::

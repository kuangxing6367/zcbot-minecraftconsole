# zcore 白名单闸门（第三端）

v1 双向 RPC 只为「闸门」提供通道，不提供答案。答案来自这一端：`plugins/minecraftconsole/zcore_pass.py`。

::: info 定位说明
这是**业务侧的签发者**，不是协议层。协议只规定 `zcore.pass.query` 的 envelope 与 fail-closed 语义（见[双向 RPC](/protocol/rpc)）；本页讲的是往这个注入口里塞什么、状态怎么存、变更怎么触发对账。
:::

## 它在链路里的位置

```text
Java agent ──0x05 REQ zcore.pass.query──▶ mc_link 适配器
                                              │ 未注册时：fail-closed pending
                                              │ 已注册时：转给签发者
                                              ▼
                                     zcore_pass.ZcorePassSigner
                                              │
                                              ▼
                                  data/zcore_pass.json（闸门状态）
```

- `mc_link` 启动时给 `zcore.pass.query` 注册的是**占位**处理器，固定返回 `{"state":"pending","reason":"unavailable"}`。
- 业务插件 `start()` 里 `attach(link)` 会调 `link.register_method("zcore.pass.query", ...)` **覆盖**同名方法（`RpcHub.register_method` 是直接赋值），此后查询走真实数据。
- 未启用 `mc_link` 时不 attach，签发者只在本地存状态，不参与对账。

## 状态模型

存储是单个 JSON 文件 `data/zcore_pass.json`（`ctx.get_data_dir()` 下，`data/` 已在 `.gitignore`）：

```json
{
  "default_action": "pending",
  "passes": {
    "Steve": {
      "player": "Steve",
      "state": "approved",
      "code": null,
      "issued_by": "42",
      "issued_at": 1762000000,
      "expires_at": 1762003600,
      "note": "chat-command"
    }
  }
}
```

| 字段 | 含义 |
| --- | --- |
| `state` | `approved` / `pending` / `rejected` / `expired` |
| `code` | 验证码（如有），随查询一并返回 |
| `expires_at` | 到期时间戳；`null` 表示不过期 |
| `default_action` | 查不到记录时的默认动作：`pending`（停在等待）或 `deny`（直接 rejected） |

**没有 `allow` 这个默认值**。`default_action` 只接受 `pending` 与 `deny`——闸门在设计上不存在「默认放行」。

## 查询语义

| 情况 | 返回 |
| --- | --- |
| 无记录 + `default_action=pending` | `{"state":"pending","reason":"no-record"}` |
| 无记录 + `default_action=deny` | `{"state":"rejected","reason":"no-record"}` |
| 记录存在且未过期 | 记录的 `state`，`reason=ok` |
| 记录存在但已过期 | `{"state":"expired","reason":"expired"}` |
| `player` 为空 | `{"state":"pending","reason":"missing-player"}` |

## 聊天命令

管理员（`require_admin=True`）：

```text
/白名单                     列出全部记录
/白名单 <玩家>              查询单个玩家
/白名单 审批 <玩家> [小时]   签发 approved；省略小时=不过期
/白名单 拒绝 <玩家>         改为 rejected（吊销）
/白名单 删除 <玩家>         删除记录（回落到 default_action）
```

命令 handler 跑在框架线程池，签发是协程，因此通过 `run_coroutine_threadsafe` 派发回主循环执行——这与 `on_loaded` 里修过的 loop 绑定问题是同一类约束。

## 对账如何被触发

```text
/白名单 审批 Steve
   └─ signer.issue()  → store.upsert()      持久化（原子写：tmp + os.replace）
                      → link.bump_epoch()   epoch += 1，记入变更窗口
                                            → 向 CAP_PUSH peer 推 zcore.pass.changed
                                            → agent 下次心跳看到 epoch 领先
                                            → REQ zcore.sync {since:已知}
                                            → 增量补齐或 -32010 全量重拉
```

`bump_epoch` 失败**不会**让签发失败：本地 store 已落盘，对账水位落后时 agent 会通过 `zcore.sync` 补上。这是有意的取舍——宁可对账延后，也不要签发结果不确定。

变更窗口上限 1024 条，超出后旧水位会触发 `-32010`，agent 走全量重拉。这是[协议规范 §4.9](/protocol/rpc) 里定义的行为。

## 未实现的部分

- **验证码下发**：`code` 字段能存能返回，但生成、投递、校验属于业务编排，本模块不做。
- **审批卡片 / 自动审批规则**：目前只有命令这一条入口。
- **多来源账号体系**：`player` 就是字符串 key，没有和 QQ↔MC 绑定表 `mc_whitelist` 打通。想接的话在 `handle_query` 里查绑定表即可，不动协议。

## 测试

```bash
python tests/test_zcore_pass.py     # 15 项：fail-closed、签发/吊销/删除、bump 载荷
python tests/interop/run_interop.py # 22 项：含真实 TCP 上的 pass 查询命中与未命中
```

跨语言那 22 项里的最后两项，验证的正是「Java 侧发起 `zcore.pass.query` → 真实签发者回答 `approved`」与「查无此人 → `pending`」。

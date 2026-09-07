# zcbot-minecraftconsole ⛏

> 基于 [ZCBOT](https://github.com/kuangxing6367/zcbot)（OneBot 11 QQ 机器人框架）做的 **Minecraft 服务器管理**：Web 面板 1Panel 风格，服管靠"可视化工作流"拼，QQ 群里一句话就能让机器人执行。

不是框架、不是插件市场，就是一个**自己用/自己玩**的 MC 服配套管理端。

---

## 它能干嘛

- **网页管理**（`http://localhost:8080`，登录 admin/admin123）
  - 概览：在线状态 / TPS / 玩家数，TPS 与在线人数的历史曲线
  - MC 控制台：状态卡、快捷命令、命令输入框、实时日志
  - 文件管理：直接操作 MC 服务器目录——上传/下载/编辑（自动识别 UTF-8/GBK，二进制禁编辑）/重命名/删除/新建文件夹
  - 工作流：可视化编排"触发词 → 条件 → 动作 → 结束"
  - 框架后台：内嵌 ZCBOT 原生管理（群组/用户/权限/接口令牌）
- **QQ 群里管服**
  - `/绑定玩家 <游戏ID>` 把 QQ 和 MC 玩家绑一起（按群隔离）
  - 工作流按**触发词**自动执行，动作节点下发任意 MC 命令，`{player}` 自动替换成绑定的玩家名
  - 多个工作流可用同一条消息/同一前缀同时触发
- **桥接**：框架 ↔ MC 服务器通过 `mc-agent`（打进服的 Spigot 插件）双向通信，命令执行/查询/文件操作都走它

## 项目怎么构成

```
framework/                  ← vendored 的 ZCBOT 框架本体（版本与 zcbot 仓库 main 一致）
plugins/minecraftconsole/   ← 本项目的灵魂：MC 控制器 + 工作流引擎 + 玩家绑定 + 文件服务 + 前端面板
  ├─ main.py                MC 控制器：TCP 桥接(25599) / 控制台 HTTP(25598) / QQ 命令 / 权限门控
  ├─ workflow.py            工作流引擎 + /api/mc/* 路由（workflow/auth/fs/lfs）
  └─ web/                   前端产物：panel/(1Panel 风格) console.html workflow.html
mc-agent-java/              MC 服务端插件源码（编译出 mc-agent.jar 丢进服里）
mc-server/                  （不入库）本机 MC 服务器运行现场
webui/mc-panel/             面板前端源码工程（Vue3 + Element Plus + ECharts），构建到 web/panel
docs/                       框架官方开发文档（从 zcbot 同步，仅作参考）
```

> 注意分工：**通用能力改进先进 zcbot 框架仓库**（main 分支发版），再同步回这里的 `framework/`。这里只叠 MC 相关的业务。

## 快速开始

前置：Python 3.11+、装 `requirements.txt`；MC 服务器（1.12.2 用 JDK8）；一个跑 OneBot 的 QQ 小号。

```bash
pip install -r requirements.txt
python main.py          # 起框架：Web 8080 / 桥接 25599 / 控制台 25598 / OneBot 6830
```

MC 服接入：
1. 编译 `mc-agent-java`（或取构建好的 jar），放进服 `plugins/`；
2. 改 `mc-agent` 的 `config.yml`，`secret` 要和控制器侧一致（默认 `zcboot-mc-please-change-me`，**上线务必改**）；
3. 起服，日志出现 `已连上控制器` 即通；控制器默认把 6 秒无心跳判离线。

然后浏览器开 `http://localhost:8080`，用框架种子管理员 `admin / admin123` 登录（**首次登录后请改密码**）。

## 群里常用命令

| 命令 | 作用 | 权限 |
|---|---|---|
| `/绑定玩家 <ID>` | 当前 QQ 绑定游戏名（可 `[QQ] <ID>` 代绑） | 管理员可代绑 |
| `/解绑玩家` | 解除绑定 | 本人 |
| `/我的玩家` | 查看自己绑定的玩家 | 本人 |
| `/mcstatus` | 查询服状态 (TPS/玩家/在线) | 管理员 |
| `/mcon reload` | 重新加载 `workflows.json`（外部改了即时生效） | 管理员 |

工作流执行按权限节点 `zcbot.wf.<工作流id>` 控制：默认组带 `zcbot.wf.*` 通配（默认全开），要限制某条在框架后台「权限管理」里收紧即可。

## 端口速查

| 端口 | 用途 |
|---|---|
| 8080 | Web 面板 + API（根路径直接进 MC 面板） |
| 25598 | MC 控制台 HTTP（状态/命令/心跳 SSE） |
| 25599 | TCP 桥接（mc-agent 反向连入） |
| 25565 | Minecraft 服务器本体 |
| 6830 | OneBot 反向 WS（QQ 接入） |

## 改前端

面板源码在 `webui/mc-panel/`，改完构建：

```bash
cd webui
node node_modules/vite/bin/vite.js build --config mc-panel/vite.config.js
# 产物自动落到 plugins/minecraftconsole/web/panel/
```

## License

MIT / Apache-2.0 双许可（随 ZCBOT 框架）。

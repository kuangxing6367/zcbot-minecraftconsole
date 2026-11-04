# 仓库文档索引

本站只讲本项目；仓库 `docs/` 是框架级文档，两者不重复。以下链接指向仓库 `main` 分支对应文件。

| 文档 | 内容 |
| --- | --- |
| [README.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/README.md) | 完整功能说明、网页面板导览、配置与排障 |
| [docs/INDEX.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/INDEX.md) | 仓库文档总索引 |
| [docs/architecture.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/architecture.md) | 框架架构说明 |
| [docs/getting-started.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/getting-started.md) | 框架侧上手指南 |
| [docs/configuration.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/configuration.md) | 配置项说明 |
| [docs/API.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/API.md) | 接口文档入口 |
| [docs/api/event.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/api/event.md) | 事件对象与订阅方式 |
| [docs/api/protocol_adapter.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/api/protocol_adapter.md) | 协议适配器契约 |
| [docs/api/services.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/api/services.md) | 服务注册表 |
| [docs/KNOWN_ISSUES.md](https://github.com/kuangxing6367/zcbot-minecraftconsole/blob/main/docs/KNOWN_ISSUES.md) | 已知问题 |

## 代码入口

| 路径 | 说明 |
| --- | --- |
| `core_plugins/mc_link/protocol.py` | 私有 TCP 帧编解码与 v1 扩展 |
| `core_plugins/mc_link/rpc.py` | JSON-RPC 语义层 |
| `core_plugins/mc_link/main.py` | 独立适配器主程序 |
| `plugins/minecraftconsole/main.py` | 业务插件、WebUI、命令与工作流 |
| `mc-agent-java/src/main/java/com/zgric/mcagent/` | Java 侧 FrameCodec / RpcHub / MiniJson / MCAgent |
| `tests/test_mc_link.py` | 适配器单元测试 |
| `.github/workflows/pages.yml` | 文档站 Pages 发布 |

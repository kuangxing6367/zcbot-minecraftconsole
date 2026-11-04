# 快速开始

四步把控制台跑起来。以下命令与配置项均取自仓库当前代码。

## 1. 启动控制器

Python 3.10+。首次启动自动生成 `config.yaml` 与 `data/` 数据目录；缺依赖时启动也会自动补装。

```bash
pip install -r requirements.txt
python main.py                    # Linux 也可 bash start.sh
```

登录面板：框架升级后 WebUI 默认端口为 **8090**（`core_plugins.yaml` 默认值）。默认账号 `admin / admin123`，上线前请改密码。

```text
http://127.0.0.1:8090
```

## 2. 构建 mc-agent

JDK 8+ / Maven。产物为 `target/mc-agent.jar`，放进 MC 服务器的 `plugins/` 后重启一次生成配置。

```bash
cd mc-agent-java
mvn clean package
```

::: tip 交付形态
jar 是**打包发布**产物，不入库：源码在 `mc-agent-java/src/main/java`，构建产物通过 CI artifact 或 Release 交付。不要手动把 jar 复制进仓库。
:::

## 3. 对接两端

编辑 MC 服务器上生成的 `plugins/mc-agent/config.yml`，让 `host` / `port` / `secret` 与控制器一致。

```yaml
host: 127.0.0.1        # 控制器地址；异地填 FRP 转发后的地址
port: 25599            # 控制器 mc_link / 内置监听端口
secret: "zcboot-mc-please-change-me"   # 与控制器一致，务必改掉
interval-ms: 1000      # 心跳周期（毫秒）
reconnect-ms: 3000     # 断线重连间隔（毫秒）
```

两端一致后，面板出现「agent 接入」并周期性刷新 TPS 与人数。若一直离线，先查日志里是不是 `bad-token`。

## 4. 可选：启用独立适配器与 v1 RPC

`core_plugins.yaml` 默认把 `mc_link` 关掉，此时业务插件使用内置 TCP 监听，行为与旧版一致。开启方式见[mc_link 适配器](/guide/adapter)。

Java 侧 `config.yml` 的 `rpc-enabled: false` 是另一个独立开关；开启后复用同一条连接做双向 RPC，不新增端口、不走 HTTP。协议细节见[跳包机制](/protocol/rpc)。

::: warning 上线前
- 改掉面板默认口令与两端默认 secret
- MC 侧链路本身不提供 TLS，隧道与网络边界由部署方负责
- 走 FRP 时用 **tcp** 类型隧道，不要用 http 类型
:::

## 可选：QQ 入口

启动 OneBot 11 客户端（NapCat / Lagrange / go-cqhttp），反向 WebSocket 连到 `ws://127.0.0.1:6830`，Access Token 与 `config.yaml → onebot.access_token` 一致。不需要 QQ 入口可跳过。

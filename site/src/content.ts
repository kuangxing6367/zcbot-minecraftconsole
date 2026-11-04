// 全站文案、链接、代码块集中在这里。组件只负责结构，不写死文字。

export const site = {
  domain: 'mc.zgric.top',
  origin: 'https://mc.zgric.top',
  repo: 'https://github.com/kuangxing6367/zcbot-minecraftconsole',
  name: 'MinecraftConsole',
  version: 'v1.0.0-beta.0',
  framework: 'zcbot v1.20.0',
  agent: 'mc-agent 1.3.0',
  tagline: '在网页与 QQ 上，远程管住 Minecraft Java 版服务器',
  summary:
    '控制器（Python）与 mc-agent（Java / Bukkit）通过一条轻量私有 TCP 链路通信。可本机直连，也可经 FRP 的 tcp 隧道跨网控制异地服务器。',
}

export const repoFile = (path: string) => `${site.repo}/blob/main/${path}`

export type Block =
  | { kind: 'p'; text: string }
  | { kind: 'note'; tone: 'info' | 'warn'; title: string; text: string }
  | { kind: 'code'; label: string; lang: string; code: string }
  | { kind: 'cards'; items: { tag: string; title: string; desc: string }[] }
  | { kind: 'facts'; items: { k: string; v: string }[] }
  | { kind: 'table'; head: string[]; rows: string[][] }
  | { kind: 'steps'; items: { title: string; text: string; code?: string; lang?: string }[] }
  | { kind: 'flow'; nodes: { title: string; sub: string }[]; edges: string[] }
  | { kind: 'links'; items: { label: string; href: string; desc: string }[] }

export type Section = {
  id: string
  label: string
  kicker: string
  title: string
  lead?: string
  blocks: Block[]
}

const lines = (...xs: string[]) => xs.join('\n')

export const sections: Section[] = [
  {
    id: 'overview',
    label: '概览',
    kicker: '00 / OVERVIEW',
    title: '一个控制台，管住远端的服务器',
    lead: site.summary,
    blocks: [
      {
        kind: 'facts',
        items: [
          { k: '项目版本', v: site.version },
          { k: '框架底座', v: site.framework },
          { k: '服务器侧', v: site.agent },
          { k: '通信链路', v: '私有 TCP 帧协议 · FRP tcp 隧道' },
          { k: '文档站域名', v: site.domain },
        ],
      },
      {
        kind: 'cards',
        items: [
          { tag: '01', title: '状态总览', desc: '实时显示在线 / 离线、TPS、在线人数与最近心跳；心跳历史落库并绘制趋势图。' },
          { tag: '02', title: '命令台', desc: '像在服务器控制台一样下发 list、give、say、op 等命令，拿到真实执行输出。' },
          { tag: '03', title: '实时日志', desc: 'SSE 推送连接、命令、回执、离线告警等事件，打开页面自动回放最近日志。' },
          { tag: '04', title: '文件管理', desc: '浏览、编辑、上传、下载、重命名、删除服务器文件；远端经 agent 操作，本机直接操作。' },
          { tag: '05', title: '自动化工作流', desc: '拖拽编排 触发 → 动作 → 条件分支 → 结束，网页可测试运行，QQ 关键词也能触发。' },
          { tag: '06', title: 'QQ 入口（可选）', desc: '接上 OneBot 11 客户端后，群 / 私聊查状态、发命令；QQ 号与游戏 ID 按群绑定。' },
        ],
      },
      {
        kind: 'note',
        tone: 'warn',
        title: '上线前必须改掉两处默认值',
        text: '面板默认账号 admin / admin123；agent 与控制器共用默认 secret zcboot-mc-please-change-me。两端 secret 或端口不一致时，面板会一直显示离线并在日志里出现 bad-token。',
      },
    ],
  },
  {
    id: 'architecture',
    label: '组成与架构',
    kicker: '01 / ARCHITECTURE',
    title: '两个必需组件，一条私有链路',
    lead: '控制器负责「看与发」，mc-agent 负责「在服务器里真的执行」。缺一不可。',
    blocks: [
      {
        kind: 'flow',
        nodes: [
          { title: '浏览器 / QQ', sub: '管理员操作入口' },
          { title: '控制器', sub: 'Python · 本仓库' },
          { title: 'mc-agent', sub: 'Java · 装进 MC 服务器' },
        ],
        edges: ['WebUI / OneBot', '私有 TCP 帧 · 可经 FRP tcp 隧道'],
      },
      {
        kind: 'cards',
        items: [
          { tag: 'A', title: '控制器', desc: 'plugins/minecraftconsole 业务插件 + 框架层。发命令、收结果、跑工作流、存日志，并提供 WebUI 与 SSE 推送。' },
          { tag: 'B', title: 'mc-agent', desc: 'mc-agent-java，Bukkit 插件，兼容 MC 1.12+ / Java 8。连上控制器，执行 EXEC / QUERY / FS 三类指令并回传输出。' },
          { tag: 'C', title: 'mc_link 适配器', desc: '新版本把 TCP 帧链路从业务插件里抽出来，注册为独立 ProtocolAdapter；默认关闭，关闭时业务插件仍用内置监听，行为与旧版一致。' },
        ],
      },
      {
        kind: 'p',
        text: `框架层已同步到 ${site.framework}：支持多适配器并存、按 source 单独订阅。mc_link 以 bot_name=mc_link 作为事件来源，把帧转成 notice.mc_heartbeat / notice.mc_output / notice.mc_command 广播，业务插件单独订阅，不会串到 OneBot 或 Web 接入端。`,
      },
      {
        kind: 'note',
        tone: 'info',
        title: '为什么 MC 侧链路不走 HTTP',
        text: 'HTTP 属于 Web 类型，FRP 的 http 类型隧道存在风控问题。MC 侧链路全程使用私有 TCP 帧，没有 Host、URL、Header；Web 控制台 API 只是管理员浏览器访问通道，不经过 FRP，与这条链路互不相干。',
      },
    ],
  },
  {
    id: 'protocol',
    label: '通信协议',
    kicker: '02 / PROTOCOL',
    title: '64 字节定长帧头 + 变长正文',
    lead: '大端序，设计为跑在 FRP 私有隧道里，协议本身不提供 TLS。',
    blocks: [
      {
        kind: 'table',
        head: ['偏移', '长度', '字段', '说明'],
        rows: [
          ['0', '1B', 'type', '0x01 心跳 / 0x02 命令 / 0x03 回执'],
          ['1', '4B', 'ts', '秒级时间戳'],
          ['5', '16B', 'token', 'HMAC-SHA256(secret, ts) 前 16 字节'],
          ['21', '4B', 'tps', '上报的 TPS'],
          ['25', '4B', 'players', '在线人数'],
          ['29', '4B', 'plen', '正文长度，心跳为 0，上限 16MB'],
          ['33', '4B', 'seq', '命令 / 回执单调序号'],
          ['64…', '变长', 'body', 'UTF-8 指令或输出'],
        ],
      },
      {
        kind: 'facts',
        items: [
          { k: '心跳帧', v: '64B 固定帧头，无载荷' },
          { k: '令牌', v: 'HMAC-SHA256(secret, big-endian ts)[:16]' },
          { k: '时间窗', v: '与本地时间差超过 3s 丢弃' },
          { k: '防重放', v: '常数时间比对 + 单调序号' },
          { k: '离线判定', v: '6s 收不到心跳' },
          { k: '默认端口', v: '25599' },
        ],
      },
      {
        kind: 'note',
        tone: 'warn',
        title: '帧格式是三方契约',
        text: 'mc-agent-java、旧业务插件里的 FrameCodec、core_plugins/mc_link/protocol.py 三方逐字节一致。改动帧格式会同时打断 Java agent，必须三方同步修改。',
      },
    ],
  },
  {
    id: 'start',
    label: '快速开始',
    kicker: '03 / GETTING STARTED',
    title: '四步把控制台跑起来',
    blocks: [
      {
        kind: 'steps',
        items: [
          {
            title: '启动控制器（Python 3.10+）',
            text: '首次启动自动生成 config.yaml 与 data/ 数据目录；缺依赖时启动也会自动补装。',
            lang: 'bash',
            code: lines('pip install -r requirements.txt', 'python main.py                    # Linux 也可 bash start.sh'),
          },
          {
            title: '登录面板',
            text: '框架升级后 WebUI 默认端口为 8090（core_plugins.yaml 默认值）。旧版 README 写的 8080 已不适用。默认账号 admin / admin123，上线前请改密码。',
            lang: 'text',
            code: 'http://127.0.0.1:8090',
          },
          {
            title: '构建 mc-agent（JDK 8+ / Maven）',
            text: '产物为 target/mc-agent.jar，放进 MC 服务器 plugins/ 后重启一次生成配置。',
            lang: 'bash',
            code: lines('cd mc-agent-java', 'mvn clean package'),
          },
          {
            title: '对接两端',
            text: '编辑 plugins/mc-agent/config.yml，把 host / port / secret 与控制器对齐；两端一致后，面板出现「agent 接入」并周期性刷新 TPS 与人数。',
            lang: 'yaml',
            code: lines(
              'host: 127.0.0.1                       # 控制器地址；异地填 FRP 转发后的地址',
              'port: 25599                           # 与控制器 tcp_port 一致',
              'secret: "zcboot-mc-please-change-me"  # 与控制器一致，务必改掉默认值',
              'interval-ms: 1000                     # 心跳周期',
              'reconnect-ms: 3000                    # 断线重连间隔',
              'fs:',
              '  enabled: true',
              '  read-only: false',
              '  allow-absolute-paths: false',
              '  max-read-bytes: 262144',
            ),
          },
        ],
      },
      {
        kind: 'note',
        tone: 'info',
        title: '可选：QQ 入口',
        text: '启动 OneBot 11 客户端（NapCat / Lagrange / go-cqhttp），反向 WebSocket 连到 ws://127.0.0.1:6830，Access Token 与 config.yaml → onebot.access_token 一致。不需要 QQ 入口可跳过。',
      },
    ],
  },
  {
    id: 'adapter',
    label: 'mc_link 适配器',
    kicker: '04 / ADAPTER',
    title: '链路层从业务里独立出来',
    lead: '默认关闭。开启后由独立 ProtocolAdapter 接管 TCP 帧链路，业务插件只订阅事件。',
    blocks: [
      {
        kind: 'code',
        label: 'core_plugins.yaml',
        lang: 'yaml',
        code: lines(
          'mc_link:',
          '  enabled: true            # 默认 false',
          '  host: 0.0.0.0',
          '  port: 25599',
          "  secret: '换成与 agent 一致的密钥'",
          '  bot_name: mc_link',
        ),
      },
      {
        kind: 'facts',
        items: [
          { k: '服务名', v: 'mc_link（专用，不抢 protocol_adapter / api_caller）' },
          { k: '事件来源', v: 'bot_name = mc_link' },
          { k: '广播事件', v: 'notice.mc_heartbeat / mc_output / mc_command' },
          { k: '链路形态', v: 'MC agent → FRP tcp → mc_link:25599 → dispatch_event → 业务插件' },
          { k: '隧道类型', v: 'tcp（非 http）' },
        ],
      },
      {
        kind: 'cards',
        items: [
          { tag: 'ON', title: '启用时', desc: '业务插件把命令下发、状态查询、回执关联委托给适配器，并跳过内置 TCP 监听，避免端口抢占。' },
          { tag: 'OFF', title: '关闭时', desc: '业务插件自动退回内置 TCP 监听，旧部署行为完全不变，无需改任何配置。' },
        ],
      },
      {
        kind: 'note',
        tone: 'info',
        title: '验证状态',
        text: 'tests/test_mc_link.py 覆盖帧字节一致性、TCP 往返、回执关联、坏 token / 过期帧丢弃与多适配器注册；双模式启动会分别监听 25599（TCP）、25598（业务 WebUI HTTP）、8090（框架 WebUI）。',
      },
    ],
  },
  {
    id: 'deploy',
    label: '文档站部署',
    kicker: '05 / DEPLOY',
    title: `托管到 GitHub Pages · ${site.domain}`,
    lead: '本站是纯静态 Vite 站点，由仓库内的 Actions 自动构建并发布到 GitHub Pages，只承载文档，不含后端。',
    blocks: [
      {
        kind: 'facts',
        items: [
          { k: '托管方式', v: 'GitHub Pages · Source = GitHub Actions' },
          { k: '工作流', v: '.github/workflows/pages.yml' },
          { k: '构建目录', v: 'site/ → site/dist' },
          { k: '自定义域名', v: `${site.domain}（site/public/CNAME）` },
        ],
      },
      {
        kind: 'steps',
        items: [
          {
            title: '推送即发布',
            text: 'push 到 main 且 site/** 或工作流文件变更时自动触发：npm ci → npm run build → 上传 site/dist → deploy-pages。也可在 Actions 页面手动 workflow_dispatch。',
            lang: 'bash',
            code: lines('cd site', 'npm ci', 'npm run build        # 本地预览构建产物'),
          },
          {
            title: '绑定自定义域名',
            text: 'site/public/CNAME 已写入域名，构建时会复制进 dist。在域名服务商添加 CNAME 记录，并在仓库 Settings → Pages 里确认自定义域名；HTTPS 证书由 GitHub 签发。',
            lang: 'dns',
            code: `${site.domain}.    CNAME    kuangxing6367.github.io.`,
          },
          {
            title: '验证',
            text: `DNS 生效后访问 ${site.origin}/；仓库 Pages 面板与 Actions 的 Pages 工作流会显示发布状态。DNS 未生效前先看工作流是否 success，不要急着改配置。`,
            lang: 'text',
            code: `${site.origin}/`,
          },
        ],
      },
      {
        kind: 'note',
        tone: 'warn',
        title: '不要把两件事混在一起',
        text: '文档站（本页）与控制台 WebUI 是两个独立服务。Pages 只托管静态文件，不提供后端，也不会代理控制台端口；MC 侧链路仍是私有 TCP，与 HTTP 托管无关。若以后要自托管，仓库里保留 deploy/nginx.conf.example 作为参考配置。',
      },
    ],
  },
  {
    id: 'docs',
    label: '文档索引',
    kicker: '06 / INDEX',
    title: '仓库里还有什么',
    lead: '仓库 docs/ 是框架与项目级文档；本站只讲本项目，不重复整套框架手册。',
    blocks: [
      {
        kind: 'links',
        items: [
          { label: 'README.md', href: repoFile('README.md'), desc: '完整功能说明、网页面板导览、配置与排障' },
          { label: 'docs/INDEX.md', href: repoFile('docs/INDEX.md'), desc: '仓库文档总索引' },
          { label: 'docs/architecture.md', href: repoFile('docs/architecture.md'), desc: '框架架构说明' },
          { label: 'docs/getting-started.md', href: repoFile('docs/getting-started.md'), desc: '框架侧上手指南' },
          { label: 'docs/configuration.md', href: repoFile('docs/configuration.md'), desc: '配置项说明' },
          { label: 'docs/API.md', href: repoFile('docs/API.md'), desc: '接口文档入口' },
          { label: 'docs/api/event.md', href: repoFile('docs/api/event.md'), desc: '事件对象与订阅方式' },
          { label: 'docs/api/protocol_adapter.md', href: repoFile('docs/api/protocol_adapter.md'), desc: '协议适配器契约' },
          { label: 'docs/api/services.md', href: repoFile('docs/api/services.md'), desc: '服务注册表' },
          { label: 'docs/KNOWN_ISSUES.md', href: repoFile('docs/KNOWN_ISSUES.md'), desc: '已知问题' },
        ],
      },
    ],
  },
  {
    id: 'boundary',
    label: '边界与变更',
    kicker: '07 / NOTES',
    title: '用之前先知道这几条',
    blocks: [
      {
        kind: 'cards',
        items: [
          { tag: '01', title: '链路无内置 TLS', desc: '私有 TCP 协议设计上跑在 FRP 私有隧道里，本身不提供 TLS；隧道与网络边界由部署方负责。' },
          { tag: '02', title: 'HTTP 的位置', desc: 'HTTP 只用于管理员浏览器访问 WebUI 与文档站，不用于 MC 服务器到控制器之间的链路上报。' },
          { tag: '03', title: '端口变更', desc: '框架升级后 WebUI 默认 8090；业务面板 HTTP 仍在 25598，MC 帧链路仍是 25599。' },
          { tag: '04', title: '适配器默认关闭', desc: 'mc_link 需要在 core_plugins.yaml 显式开启并填入与 agent 一致的 secret，否则走内置监听。' },
          { tag: '05', title: '规划中的下一版本', desc: '白名单 / 验证码 / 自动与手动审批（zcore）属于规划中的新版本，当前仓库尚未实现，本页描述的是已落地能力。' },
        ],
      },
    ],
  },
]

export const footer = {
  note: 'ZGRIC · 自托管部署 · 站点内容为静态文档',
  links: [
    { label: 'GitHub 仓库', href: site.repo },
    { label: '问题反馈', href: `${site.repo}/issues` },
  ],
}

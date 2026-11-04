package com.zgric.mcagent;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.OfflinePlayer;
import org.bukkit.World;
import org.bukkit.command.CommandSender;
import org.bukkit.configuration.file.FileConfiguration;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Level;

/**
 * mc-agent —— minecraftconsole 服务器侧插件。
 *
 * 职责：
 *  - 通过 FRP 连到 minecraftconsole 控制器 TCP 端口（控制通道：命令 / 回执）；
 *  - 心跳走专用 FRP UDP 节点（国内低延迟）：每 frp-beat-ms 轮询向节点清单里一个节点发 64B 心跳；
 *  - 接收变长命令帧(EXEC/QUERY/FS 指令文本)，校验后：
 *      EXEC        在服务器控制台执行 MC 命令
 *      QUERY|...   查询玩家/世界/方块/附近实体（主线程 Bukkit API）
 *      FS|...      服务器文件读写查看
 *  - 执行结果以变长回执帧(0x03)回传：执行后【立即启动独立线程】发送，无 384 长度限制。
 *
 * 线程模型：
 *  - TPS/玩家数采样在主线程(volatile 写)；网络 IO 在独立线程；
 *  - EXEC/QUERY 回主线程执行 Bukkit API；FS 用异步任务；
 *  - 回执由独立新线程发送，与心跳写 socket 用同一把锁防帧交错。
 */
public final class MCAgent extends JavaPlugin {

    private FrameCodec codec;
    private RpcHub rpc;                  // v1 双向 RPC / 跳包状态机（零 Bukkit 依赖）
    private boolean rpcEnabled = false;
    private volatile boolean running = false;
    private Thread netThread;
    private Thread beatThread;

    // 回执序号 + 写锁（回执彼此之间共用，防帧交错）。
    // 心跳迁到专线后不再参与这把锁 —— 大回执（最大 256KB）不会再把它挡在后面。
    private final AtomicInteger outSeq = new AtomicInteger(1);
    private final Object writeLock = new Object();
    // 由网络线程（控制通道）持有并写，回执线程经锁写
    private volatile OutputStream netOut;

    // 心跳专线：独立一条 TCP 只发 64B 心跳，与控制通道互不干扰。
    // beatOut==null / beatDedicated==false 时心跳自动退回控制通道（老控制器或专线不通时的降级）。
    private volatile OutputStream beatOut;
    private volatile boolean beatDedicated = false;

    // 控制台日志转发：有界队列 + 独立发送线程。
    // 日志是服务器线程产出的，绝不能让它等 socket —— offer 进队列（满就丢），
    // 发送线程按批合并成一条帧发出去，几十行日志也只有一个帧头开销。
    private final java.util.concurrent.BlockingQueue<String> logQueue =
            new java.util.concurrent.LinkedBlockingQueue<String>(500);
    private final AtomicInteger logSeq = new AtomicInteger(1);
    private Thread logThread;
    private static final int LOG_BATCH = 40;        // 一条帧最多合并多少行
    private static final int LOG_LINE_MAX = 500;    // 单行截断长度（防一条超长日志吃满帧）

    private volatile float currentTps = 20.0f;
    private volatile int currentPlayers = 0;

    private final long[] tickDiffs = new long[20];
    private long lastTick = -1;
    private int tickIdx = 0;

    private String host;
    private int port;
    private int beatPort;      // 心跳专线端口；<=0 表示不用专线（心跳全走控制通道）
    private long intervalMs;

    /** 控制通道活性探测间隔：即便心跳专线通着，也按这个节奏在控制通道补一拍心跳。
     *  它不为报数，只为让「这条通道还活着」有信号，并换来控制器的一拍 ACK ——
     *  有了 ACK，本端 read 才有数据流，「静默判死」才成立。 */
    private static final long CTRL_PROBE_MS = 15000;

    /** 控制通道静默判死阈值：连续这么久一个字节都没读到，判定链路已断（典型是 TCP 半开）。 */
    private static final long CTRL_IDLE_MS = 45000;
    private long reconnectMs;

    // ── FRP 心跳（专用 UDP，国内低延迟）──
    // 心跳不走控制通道的 TCP，也不直连控制器本身；改为把 64B 心跳以 UDP 发给 frp_nodes.json
    // 里的节点（agent → frp 公共 UDP 入口 → 139:4094 → 82:25600 控制器 UDP 入口）。
    // 节点清单每小时从 frp-nodes-url 拉一次；frpNodes 为空时心跳线程只空转、绝不报错崩线程。
    private boolean frpEnabled = true;
    private String frpNodesUrl = "https://api.zgric.top/frp_nodes.json";
    private long frpBeatMs = 2500;                  // 心跳周期（毫秒）。轮询单节点发送，总速率 = 60000/frpBeatMs；
                                                     // 务必 ≤30000，否则触发控制器每 IP 30 包/分钟限速
    private volatile java.util.List<InetSocketAddress> frpNodes =
            new java.util.ArrayList<InetSocketAddress>();
    private int frpRotate = 0;                       // 轮询下标：每个周期只发一个节点，下个周期换下一个
    private volatile long lastFrpHint = 0;          // 最近一次成功发出 frp 心跳的时间（调试用）

    // FS 配置
    private boolean fsEnabled = true;
    private boolean fsReadOnly = false;
    private boolean fsAllowAbsolute = false;
    private long fsMaxReadBytes = 256 * 1024;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        FileConfiguration c = getConfig();
        host = c.getString("host", "127.0.0.1");
        port = c.getInt("port", 25599);
        beatPort = c.getInt("beat-port", 25600);
        intervalMs = c.getLong("interval-ms", 1000);
        reconnectMs = c.getLong("reconnect-ms", 3000);
        frpEnabled = c.getBoolean("frp-enabled", true);
        frpNodesUrl = c.getString("frp-nodes-url", "https://api.zgric.top/frp_nodes.json");
        long fb = c.getLong("frp-beat-ms", 2000);
        frpBeatMs = fb < 1000 ? 1000 : fb;          // 兜底：太猛会触发控制器每 IP 30 包/分钟限速
        String secret = c.getString("secret", "zcboot-mc-please-change-me");
        fsEnabled = c.getBoolean("fs.enabled", true);
        fsReadOnly = c.getBoolean("fs.read-only", false);
        fsAllowAbsolute = c.getBoolean("fs.allow-absolute-paths", false);
        fsMaxReadBytes = c.getLong("fs.max-read-bytes", 256 * 1024);

        codec = new FrameCodec(secret.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        rpcEnabled = c.getBoolean("rpc-enabled", false);
        rpc = new RpcHub(codec, new RpcHub.Sender() {
            @Override public void send(byte[] frame) throws Exception {
                OutputStream target = netOut;
                if (target == null) throw new IllegalStateException("control channel not connected");
                synchronized (writeLock) {
                    target.write(frame);
                    target.flush();
                }
            }
        }, rpcEnabled);

        Bukkit.getScheduler().runTaskTimer(this, this::onTick, 0L, 1L);
        Bukkit.getScheduler().runTaskTimer(this, this::refreshHeartbeatData, 20L, 20L);

        running = true;
        netThread = new Thread(this::runLoop, "mc-agent-net");
        netThread.setDaemon(true);
        netThread.start();

        if (c.getBoolean("console-log", true)) {
            attachLogForwarder();          // 服务器控制台日志实时转发（可配置关掉）
        }

        if (frpEnabled) {
            // 立即异步拉一次节点清单（不阻塞启动），之后每小时刷新一次
            Bukkit.getScheduler().runTaskAsynchronously(this, this::refreshFrpNodes);
            Bukkit.getScheduler().runTaskTimerAsynchronously(
                    this, this::refreshFrpNodes, 20L * 60 * 60, 20L * 60 * 60);
            beatThread = new Thread(this::runFrpHubLoop, "mc-agent-frp-beat");
            beatThread.setDaemon(true);
            beatThread.start();
            getLogger().log(Level.INFO, "已启动，控制通道 " + host + ":" + port
                    + "，FRP 心跳(UDP)走 " + frpNodesUrl + " 的节点清单");
        } else if (beatPort > 0) {
            beatThread = new Thread(this::runBeatLoop, "mc-agent-beat");
            beatThread.setDaemon(true);
            beatThread.start();
            getLogger().log(Level.INFO, "已启动，控制通道 " + host + ":" + port
                    + "，心跳专线(TCP) " + host + ":" + beatPort);
        } else {
            getLogger().log(Level.INFO, "已启动，连接 " + host + ":" + port + "（心跳与控制通道共用）");
        }
    }

    @Override
    public void onDisable() {
        running = false;
        if (rpc != null) rpc.shutdown();
        if (netThread != null) {
            netThread.interrupt();
            netThread = null;
        }
        if (beatThread != null) {
            beatThread.interrupt();
            beatThread = null;
        }
        if (logThread != null) {
            logThread.interrupt();
            logThread = null;
        }
    }

    // ================= 控制台日志：跟随服务器自己的 logs/latest.log =================
    /**
     * 为什么不挂 JUL Handler：这台 Paper 1.12.2 的日志实际走 log4j，
     * 往根日志器上加 Handler 一条都收不到（实测）。改跟日志文件——
     * 与日志框架无关，堆栈行、第三方插件的输出一视同仁。
     */
    private void attachLogForwarder() {
        logThread = new Thread(this::runLogLoop, "mc-agent-log");
        logThread.setDaemon(true);
        logThread.start();
    }

    /** 单行过滤 + 入队。队列满即丢：宁可少几行日志，也不拖慢服务器。 */
    private void enqueueLine(String raw) {
        if (raw == null) {
            return;
        }
        String line = raw.replace("\r", "").trim();
        if (line.isEmpty() || line.contains("[mc-agent]")) {
            return;                       // agent 自己的行不回传，免得自激
        }
        if (line.length() > LOG_LINE_MAX) {
            line = line.substring(0, LOG_LINE_MAX) + "…";
        }
        logQueue.offer(line);
    }

    private void runLogLoop() {
        long offset = -1;                 // -1：还没定位；首轮只取尾部预热
        String carry = "";                // 上次读到的半行（字节被切断的情况）
        while (running) {
            try {
                Path f = serverRoot().resolve("logs").resolve("latest.log");
                if (!Files.exists(f)) {
                    sleepQuietly(1000);
                    continue;
                }
                long size = Files.size(f);
                if (offset < 0) {
                    offset = Math.max(0, size - 64 * 1024);   // 预热：最多回看 64KB
                } else if (size < offset) {
                    offset = 0;                   // 日志被轮转/清空
                }
                if (size > offset) {
                    int want = (int) Math.min(size - offset, 256 * 1024);
                    byte[] buf = new byte[want];
                    try (RandomAccessFile raf = new RandomAccessFile(f.toFile(), "r")) {
                        raf.seek(offset);
                        raf.readFully(buf);
                    }
                    offset += buf.length;
                    String chunk = carry + new String(buf, java.nio.charset.StandardCharsets.UTF_8);
                    int cut = chunk.lastIndexOf('\n');
                    if (cut < 0) {
                        carry = chunk;            // 整块都没有换行：继续攒
                    } else {
                        carry = chunk.substring(cut + 1);
                        String body = chunk.substring(0, cut);
                        String[] lines = body.split("\n");
                        int n = 0;
                        for (String l : lines) {
                            enqueueLine(l);
                            if (++n >= LOG_BATCH * 5) {       // 单轮上限，防积压
                                break;
                            }
                        }
                    }
                }
                flushLogQueue();
                sleepQuietly(500);
            } catch (Throwable ignored) {
                sleepQuietly(1000);           // 文件被占用/删掉的瞬时异常，下一轮再来
            }
        }
    }

    /** 攒到的一批日志合并成一条帧发出去（几十行只花一个帧头）。 */
    private void flushLogQueue() {
        StringBuilder sb = new StringBuilder();
        int n = 0;
        String more;
        while (n < LOG_BATCH && (more = logQueue.poll()) != null) {
            if (n > 0) {
                sb.append('\n');
            }
            sb.append(more);
            n++;
        }
        if (n > 0) {
            sendLog(sb.toString());
        }
    }

    private void sendLog(String text) {
        OutputStream out = netOut;
        if (out == null) {
            return;                       // 控制通道没连上，日志先不推
        }
        synchronized (writeLock) {
            try {
                out.write(codec.packLog((int) (System.currentTimeMillis() / 1000),
                        logSeq.getAndIncrement(), currentTps, currentPlayers, text));
                out.flush();
            } catch (Exception ignored) {
                // 这里不能 getLogger().log(...)：那会再进队列绕回来
            }
        }
    }

    private void onTick() {
        long now = System.currentTimeMillis();
        if (lastTick >= 0) {
            tickDiffs[tickIdx] = now - lastTick;
            tickIdx = (tickIdx + 1) % tickDiffs.length;
        }
        lastTick = now;
    }

    private void refreshHeartbeatData() {
        try {
            currentPlayers = Bukkit.getOnlinePlayers().size();
        } catch (Throwable ignore) {
        }
        double sum = 0;
        int n = Math.min(tickIdx, tickDiffs.length);
        for (int i = 0; i < n; i++) sum += tickDiffs[i];
        currentTps = (n > 0 && sum > 0) ? (float) Math.min(20.0, 1000.0 / (sum / n)) : 20.0f;
    }

    /** 服务器根目录 = 运行 -jar 的工作目录。 */
    private Path serverRoot() {
        return Paths.get(System.getProperty("user.dir")).toAbsolutePath().normalize();
    }

    // ================= 网络线程：连接 / 心跳 / 收帧 =================
    private void runLoop() {
        while (running) {
            try {
                connectAndServe();
            } catch (InterruptedException ie) {
                return;
            } catch (Exception e) {
                getLogger().log(Level.WARNING, "连接异常: " + e.getMessage());
            }
            sleepQuietly(reconnectMs);
        }
    }

    /** 心跳专线：一条只发 64B 心跳的连接，不收任何东西，也不参与控制通道的写锁。 */
    private void runBeatLoop() {
        while (running) {
            try {
                try (Socket socket = new Socket()) {
                    socket.connect(new InetSocketAddress(host, beatPort), 5000);
                    socket.setTcpNoDelay(true);
                    OutputStream out = socket.getOutputStream();
                    beatOut = out;
                    beatDedicated = true;
                    getLogger().log(Level.INFO, "心跳专线已连上 " + host + ":" + beatPort);

                    while (running && !Thread.currentThread().isInterrupted()) {
                        long now = System.currentTimeMillis();
                        // 专线独占，无需加锁：这条连接上没有别的写者
                        out.write(codec.packHeartbeat((int) (now / 1000), currentTps, currentPlayers));
                        out.flush();
                        sleepQuietly(intervalMs);
                    }
                }
            } catch (Exception e) {
                // 中断由循环条件的 running / isInterrupted 收口，这里只处理连接与 IO 异常
                if (running) {
                    getLogger().log(Level.WARNING, "心跳专线异常，本次心跳退回控制通道: " + e.getMessage());
                }
            } finally {
                beatOut = null;
                beatDedicated = false;     // 交还给控制线程，避免两边都不发心跳
            }
            sleepQuietly(reconnectMs);
        }
    }

    // ================= FRP 心跳：UDP 发往 frp 公共节点 =================
    /**
     * FRP 心跳专线：把 64B 心跳以 UDP 发给 frp_nodes.json 里的节点（国内低延迟，轮询单节点）。
     * 不走控制通道的 TCP，也不连控制器本身 —— 控制器在 82 的 25600 UDP 入口收到这些包。
     * 节点清单由 refreshFrpNodes 每小时拉一次（frpNodes 为空时只空转，不报错）。
     *
     * 频率口径（很重要，否则自己把自己限掉）：控制器按「收到包的源 IP」每 60s 限 30 包。
     * 82 实际看到的源 IP 取决于 139 怎么把 4094 转发到 82:25600 —— 若 139 用自身 socket
     * 转发，所有包都算 139 一个 IP，多个服务器会共享这 30 包/分钟。因此这里【每个周期只发一个
     * 节点、下一个周期轮到下一个】，总速率恒为 60000/frpBeatMs：默认 2500ms ⇒ 24 包/分钟，
     * 无论 139 是否保留 frp 节点源 IP 都不会超 30。某节点挂了，下个周期自动换下一个，仍保活。
     * （即使 UDP 被限，TCP 控制通道每 1s 也发心跳保活，服务器不会因此离线。）
     */
    private void runFrpHubLoop() {
        while (running) {
            try (DatagramSocket ds = new DatagramSocket()) {
                ds.setSoTimeout(0);
                while (running && !Thread.currentThread().isInterrupted()) {
                    java.util.List<InetSocketAddress> nodes = frpNodes;
                    long now = System.currentTimeMillis();
                    byte[] beat = codec.packHeartbeat((int) (now / 1000), currentTps, currentPlayers);
                    if (nodes != null && !nodes.isEmpty()) {
                        int idx = frpRotate % nodes.size();      // 轮询：本周期只打一个节点
                        frpRotate++;
                        try {
                            ds.send(new DatagramPacket(beat, beat.length, nodes.get(idx)));
                            lastFrpHint = now;
                        } catch (Exception ignored) {
                            // 该节点解析/不可达：忽略，下个周期换下一个
                        }
                    }
                    sleepQuietly(frpBeatMs);
                }
            } catch (Exception e) {
                if (running) {
                    getLogger().log(Level.WARNING, "FRP 心跳套接字异常，稍后重建: " + e.getMessage());
                }
            }
            sleepQuietly(reconnectMs);
        }
    }

    /** 拉取并解析 frp 节点清单；失败或为空则保留旧列表（不回退成空）。 */
    private void refreshFrpNodes() {
        try {
            URL u = new URL(frpNodesUrl);
            HttpURLConnection conn = (HttpURLConnection) u.openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setRequestProperty("User-Agent", "mc-agent/1.3.0");
            conn.setRequestProperty("Accept", "application/json");
            try (java.io.InputStream in = conn.getInputStream()) {
                byte[] buf = new byte[65536];
                int n, total = 0;
                java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
                while ((n = in.read(buf)) > 0) {
                    bos.write(buf, 0, n);
                    total += n;
                    if (total > 65536) break;       // 清单不可能这么大，防爆内存
                }
                java.util.List<InetSocketAddress> nodes = parseFrpNodes(
                        new String(bos.toByteArray(), java.nio.charset.StandardCharsets.UTF_8));
                if (!nodes.isEmpty()) {
                    frpNodes = nodes;
                    getLogger().log(Level.INFO, "已拉取 FRP 心跳节点 " + nodes.size() + " 个");
                } else {
                    getLogger().log(Level.WARNING, "frp_nodes.json 未解析出节点，沿用旧清单");
                }
            } finally {
                conn.disconnect();
            }
        } catch (Exception e) {
            getLogger().log(Level.WARNING,
                    "拉取 FRP 节点清单失败（沿用旧清单）: " + e.getMessage());
        }
    }

    /** 手撸解析固定格式 {nodes:[{host,port}...]}，不依赖任何 JSON 库（Java8 无内置）。
     *  只认 nodes 数组内的 host/port 配对，顺序对应即可。 */
    private java.util.List<InetSocketAddress> parseFrpNodes(String json) {
        java.util.List<InetSocketAddress> out = new java.util.ArrayList<InetSocketAddress>();
        if (json == null) return out;
        int i = json.indexOf("\"nodes\"");
        if (i < 0) return out;
        int arr = json.indexOf('[', i);
        if (arr < 0) return out;
        int depth = 0, end = -1;
        for (int j = arr; j < json.length(); j++) {
            char c = json.charAt(j);
            if (c == '[') depth++;
            else if (c == ']') { depth--; if (depth == 0) { end = j; break; } }
        }
        if (end < 0) return out;
        String body = json.substring(arr + 1, end);
        Pattern pHost = Pattern.compile("\"host\"\\s*:\\s*\"([^\"]+)\"");
        Pattern pPort = Pattern.compile("\"port\"\\s*:\\s*(\\d+)");
        Matcher mh = pHost.matcher(body);
        while (mh.find()) {
            String host = mh.group(1);
            Matcher mp = pPort.matcher(body);
            String port = null;
            while (mp.find()) {
                if (mp.start() > mh.start()) { port = mp.group(1); break; }
            }
            if (port != null) {
                try {
                    out.add(new InetSocketAddress(host, Integer.parseInt(port)));
                } catch (Exception ignored) {
                }
            }
        }
        return out;
    }

    /** 控制通道心跳。v1 开启时带能力位图 + 本地水位；关闭时字节与旧版逐字节一致。
     *  UDP 心跳专线不使用本方法 —— 那条链路保持 v0，避免影响既有上报与限速。 */
    private byte[] heartbeatFrame(int ts) {
        if (!rpcEnabled) {
            return codec.packHeartbeat(ts, currentTps, currentPlayers);
        }
        return codec.packHeartbeatV1(ts, currentTps, currentPlayers,
                FrameCodec.CAP_ALL, rpc == null ? 0 : rpc.getKnownEpoch());
    }

    private void connectAndServe() throws Exception {
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress(host, port), 5000);
            getLogger().log(Level.INFO, "已连上控制器 " + host + ":" + port);

            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();
            netOut = out;

            ByteArrayOutputStream acc = new ByteArrayOutputStream();
            long lastBeat = 0;
            long lastRead = System.currentTimeMillis();
            // 收到过控制器的 ACK 才启用静默判死：老控制器不会回 ACK，这条通道本来就一直
            // 静默，无门控地判死会让它每 CTRL_IDLE_MS 断一次、反复重连（比原问题更糟）。
            boolean ackSeen = false;

            // 连上就立刻发一帧（hello）。
            // 为什么必须：控制器靠「第一帧里的 token」把这条链路认到某台服务器名下。
            // 心跳走专线时这条通道平时一个字都不出，于是服务器明明连着（TCP 在、心跳也在），
            // 控制器却不知道它是谁 —— 下发指令只会回「MC 离线」，点了没反应。
            // 这一帧不为心跳，只为认领身份。
            try {
                synchronized (writeLock) {
                    out.write(heartbeatFrame((int) (System.currentTimeMillis() / 1000)));
                    out.flush();
                }
                lastBeat = System.currentTimeMillis();
            } catch (Exception ignored) {
                // 发不出去就当没发生：下面主循环会在 1s 后按常规路径重试
            }

            while (running && !Thread.currentThread().isInterrupted()) {
                long now = System.currentTimeMillis();
                // 心跳：专线通着时由专线线程按 interval-ms 发，这里不参与（避免被大回执挤到
                // 防重放窗口外）；专线断了/还没连上时退回本通道，老控制器和降级期都不会离线。
                // 专线正常时控制通道也每 CTRL_PROBE_MS 补一拍 —— 只为探测链路活性（见下）。
                long beatEvery = beatDedicated ? CTRL_PROBE_MS : intervalMs;
                if (now - lastBeat >= beatEvery) {
                    synchronized (writeLock) {
                        out.write(heartbeatFrame((int) (now / 1000)));
                        out.flush();
                    }
                    lastBeat = now;
                }
                // 链路静默判死：对端进程没了、本机却收不到 FIN（TCP 半开）时，read 只会一直
                // 超时，本线程永远不知道要重连 —— 表现就是「卡片在线、心跳正常，点什么都超时」。
                // 靠控制器对控制通道心跳回的 ACK 做判据：连续 CTRL_IDLE_MS 没读到任何字节就重连。
                if (ackSeen && now - lastRead > CTRL_IDLE_MS) {
                    getLogger().log(Level.WARNING, "控制通道静默 " + (CTRL_IDLE_MS / 1000)
                            + "s 无任何回应，判定链路已断，重连");
                    return;
                }
                socket.setSoTimeout((int) Math.min(intervalMs, 1000));
                int n;
                byte[] chunk = new byte[8192];
                try {
                    n = in.read(chunk);
                } catch (java.net.SocketTimeoutException ste) {
                    n = 0;
                }
                if (n < 0) {
                    getLogger().log(Level.INFO, "连接被关闭");
                    return;
                }
                if (n == 0) continue;
                lastRead = System.currentTimeMillis();
                ackSeen = true;
                acc.write(chunk, 0, n);

                // 变长帧切分：先判 type，命令/回执再从帧头 plen 定总长
                byte[] data = acc.toByteArray();
                acc.reset();
                int pos = 0;
                boolean residue = false;
                while (true) {
                    if (data.length - pos < 1) { residue = true; break; }
                    int ft = data[pos] & 0xFF;
                    int total;
                    if (ft == FrameCodec.HEARTBEAT_TYPE) {
                        total = FrameCodec.HEADER_LEN;
                    } else {
                        boolean known = FrameCodec.isVariableType(ft);
                        if (data.length - pos < FrameCodec.HEADER_LEN) { residue = true; break; }
                        int plen = ((data[pos + FrameCodec.PLEN_OFF] & 0xFF) << 24)
                                | ((data[pos + FrameCodec.PLEN_OFF + 1] & 0xFF) << 16)
                                | ((data[pos + FrameCodec.PLEN_OFF + 2] & 0xFF) << 8)
                                | (data[pos + FrameCodec.PLEN_OFF + 3] & 0xFF);
                        if (plen < 0 || plen > FrameCodec.MAX_PAYLOAD) { pos++; continue; } // 流失步
                        total = FrameCodec.HEADER_LEN + plen;
                        if (!known) {
                            // RFC 9113 §4.1：未知帧类型 MUST 忽略并丢弃。
                            // 按 plen 整帧跳过，避免逐字节重同步把正文误判为帧头。
                            if (data.length - pos < total) { residue = true; break; }
                            pos += total;
                            continue;
                        }
                    }
                    if (data.length - pos < total) { residue = true; break; }
                    byte[] frame = Arrays.copyOfRange(data, pos, pos + total);
                    pos += total;
                    handleFrame(frame);
                }
                if (residue && pos < data.length) {
                    acc.write(data, pos, data.length - pos);
                }
            }
        } finally {
            netOut = null;
            if (rpc != null) rpc.onDisconnected();
        }
    }

    private int cmdSeq = 0;   // 已接受命令帧的序号
    private int cmdTs = 0;    // 已接受命令帧的时间戳（与 seq 组成二元单调，重启/重连不漂移）

    private void handleFrame(byte[] frame) {
        FrameCodec.Parsed p = FrameCodec.parse(frame);
        if (!codec.verifyExtSig(frame)) {
            getLogger().log(Level.WARNING, "丢弃帧(bad-ext-sig)");
            return;
        }
        if (p.type == FrameCodec.HEARTBEAT_TYPE) {
            // 控制器回的 BOOKMARK / ACK：只带权威 epoch，不带业务数据。
            FrameCodec.Ext ext = FrameCodec.parseExt(frame);
            if (ext.v1 && rpc != null) {
                rpc.setPeerState(p.tps, p.players);
                rpc.onHello(ext);
            }
            return;
        }
        if ((p.type == FrameCodec.REQ_TYPE || p.type == FrameCodec.RESP_TYPE) && rpc != null) {
            rpc.onFrame(frame);
            return;
        }
        if (p.type != FrameCodec.COMMAND_TYPE || p.text.isEmpty()) return;
        String reason = verifyCmd(p.ts, p.token, p.seq);
        if (reason != null) {
            getLogger().log(Level.WARNING, "丢弃命令帧(" + reason + "): " + p.text);
            return;
        }
        cmdSeq = p.seq;
        cmdTs = p.ts;
        handleInstruction(p.text, p.seq);
    }

    /** 命令帧二元单调判重：ts 更大放行；ts 相同则要求 seq 更大。防止重启后 seq 从 1 重计被误判重放。 */
    private String verifyCmd(int ts, byte[] token, int seq) {
        if (!codec.checkToken(ts, token)) {
            return (Math.abs(System.currentTimeMillis() - ts * 1000L) > FrameCodec.REPLAY_WINDOW_MS)
                    ? "stale-timestamp" : "bad-token";
        }
        if (ts < cmdTs || (ts == cmdTs && seq <= cmdSeq)) {
            return "replay-seq";
        }
        return null;
    }

    // ================= 指令分发：EXEC / QUERY / FS =================
    private void handleInstruction(final String raw, final int cmdSeq) {
        final String s = raw.trim();
        final String up = s.toUpperCase(Locale.ROOT);
        if (up.startsWith("QUERY|")) {
            execOnMain("QUERY", cmdSeq, () -> queryDispatch(s));
        } else if (up.startsWith("FS|")) {
            if (!fsEnabled) { sendOutput("FS 已禁用", cmdSeq); return; }
            Bukkit.getScheduler().runTaskAsynchronously(this, () -> sendOutput(fsDispatch(s), cmdSeq));
        } else {
            execAsync(s, cmdSeq);   // EXEC：控制台执行 + 读日志捕获输出
        }
    }

    /** 主线程执行（Bukkit API 安全），结果进回执。 */
    private void execOnMain(String tag, int cmdSeq, java.util.concurrent.Callable<String> job) {
        Bukkit.getScheduler().runTask(this, () -> {
            String out;
            try {
                out = job.call();
            } catch (Throwable e) {
                out = "ERROR: " + e.getClass().getSimpleName() + ": " + e.getMessage();
            }
            sendOutput(out, cmdSeq);
        });
    }

    // ================= EXEC：控制台执行 + 日志捕获（兼容 vanilla 命令） =================
    /** 控制台 sender 执行（vanilla/插件命令都支持），再从 logs/latest.log 读取新增行作为输出。 */
    private void execAsync(final String cmd, final int cmdSeq) {
        final Path log = serverRoot().resolve("logs/latest.log");
        final long startLen = getLogLen(log);
        Bukkit.getScheduler().runTask(this, () -> {
            try {
                Bukkit.dispatchCommand(Bukkit.getConsoleSender(), cmd);
            } catch (Throwable e) {
                sendOutput("EXEC 异常: " + e.getMessage(), cmdSeq);
                return;
            }
            Bukkit.getScheduler().runTaskLaterAsynchronously(this, () -> {
                String added = readNewLogLines(log, startLen, 80);
                sendOutput(added.isEmpty() ? "(命令已执行，无输出)" : added, cmdSeq);
            }, 6L); // ≈300ms
        });
    }

    private long getLogLen(Path log) {
        try {
            return Files.exists(log) ? Files.size(log) : -1;
        } catch (Exception e) {
            return -1;
        }
    }

    private String readNewLogLines(Path log, long startLen, int maxLines) {
        if (startLen < 0 || !Files.exists(log)) return "";
        try (RandomAccessFile raf = new RandomAccessFile(log.toFile(), "r")) {
            if (startLen > raf.length()) startLen = raf.length();
            raf.seek(startLen);
            List<String> out = new ArrayList<>();
            String line;
            while (out.size() < maxLines && (line = raf.readLine()) != null) {
                String t = stripLogPrefix(line);
                if (t.isEmpty()) continue;
                // 去掉命令自身回显行（可选）
                out.add(t);
            }
            return String.join("\n", out);
        } catch (Exception e) {
            return "";
        }
    }

    private String stripLogPrefix(String raw) {
        // 形如 "[18:09:09 INFO]: 内容" 或 "[18:09:09 INFO]: [插件] 内容"
        String t = raw.trim();
        int idx = t.indexOf(']');
        if (idx >= 0 && idx + 1 < t.length()) {
            String rest = t.substring(idx + 1).trim();
            if (rest.startsWith(":")) rest = rest.substring(1).trim();
            t = rest;
        }
        return t;
    }

    // ================= QUERY =================
    private String queryDispatch(String s) {
        String body = s.substring("QUERY|".length());
        String[] t = body.split("\\|", -1);
        if (t.length == 0) return "QUERY 用法: QUERY|PLAYERLIST|PLAYER|OFFLINE|WORLDLIST|WORLD|BLOCK|NEAR";
        String sub = t[0].trim().toUpperCase(Locale.ROOT);
        switch (sub) {
            case "PLAYERLIST": return queryPlayerList();
            case "PLAYER":     return t.length >= 2 ? queryPlayer(t[1].trim()) : "用法: QUERY|PLAYER|<name>";
            case "OFFLINE":    return t.length >= 2 ? queryOffline(t[1].trim()) : "用法: QUERY|OFFLINE|<name>";
            case "WORLDLIST":  return queryWorldList();
            case "WORLD":      return t.length >= 2 ? queryWorld(t[1].trim()) : "用法: QUERY|WORLD|<name>";
            case "BLOCK":
                if (t.length < 6) return "用法: QUERY|BLOCK|<world>|<x>|<y>|<z>";
                return queryBlock(t[1].trim(), t[2].trim(), t[3].trim(), t[4].trim());
            case "NEAR": {
                String radius = t.length >= 6 ? t[5].trim() : "8";
                if (t.length < 5) return "用法: QUERY|NEAR|<world>|<x>|<y>|<z>[|<radius>]";
                return queryNear(t[1].trim(), t[2].trim(), t[3].trim(), t[4].trim(), radius);
            }
            default: return "未知 QUERY 子类型: " + sub;
        }
    }

    private String queryPlayerList() {
        List<String> names = new ArrayList<>();
        for (Player p : Bukkit.getOnlinePlayers()) names.add(p.getName());
        return "在线玩家(" + names.size() + "): " + (names.isEmpty() ? "无" : String.join(", ", names));
    }

    private String queryPlayer(String name) {
        Player p = Bukkit.getPlayerExact(name);
        if (p == null) {
            for (Player x : Bukkit.getOnlinePlayers())
                if (x.getName().toLowerCase(Locale.ROOT).startsWith(name.toLowerCase(Locale.ROOT))) { p = x; break; }
        }
        if (p == null) return "未找到在线玩家: " + name;
        Location l = p.getLocation();
        return String.format("玩家 %s | UUID=%s | 等级=%d | 血量=%.1f | 食物=%d | GM=%s | 地址=%s\n世界=%s 坐标=%.1f,%.1f,%.1f",
                p.getName(), p.getUniqueId(), p.getLevel(), p.getHealth(), p.getFoodLevel(),
                p.getGameMode(), p.getAddress() != null ? p.getAddress().getHostString() : "-",
                l.getWorld().getName(), l.getX(), l.getY(), l.getZ());
    }

    private String queryOffline(String name) {
        OfflinePlayer o = Bukkit.getOfflinePlayer(name);
        return String.format("玩家 %s 离线数据: 已玩过=%s, 封禁=%s, 最后上线=%s",
                name, o.hasPlayedBefore(), o.isBanned(),
                o.getLastPlayed() > 0
                        ? new SimpleDateFormat("yyyy-MM-dd HH:mm").format(new Date(o.getLastPlayed()))
                        : "从未");
    }

    private String queryWorldList() {
        List<String> ws = new ArrayList<>();
        for (World w : Bukkit.getWorlds()) ws.add(w.getName() + "(" + w.getPlayers().size() + ")");
        return "世界列表(" + ws.size() + "): " + String.join(", ", ws);
    }

    private String queryWorld(String name) {
        World w = Bukkit.getWorld(name);
        if (w == null) return "未找到世界: " + name;
        return String.format("世界 %s | 类型=%s | 环境=%s | 难度=%s | 玩家=%d | 生物=%d | 种子=%d\n生成=%d,%d 生成范围=%d",
                w.getName(), w.getWorldType(), w.getEnvironment(), w.getDifficulty(),
                w.getPlayers().size(), w.getEntities().size(), w.getSeed(),
                w.getSpawnLocation().getBlockX(), w.getSpawnLocation().getBlockZ(), w.getSpawnLocation().getBlockY());
    }

    private String queryBlock(String wn, String xs, String ys, String zs) {
        try {
            World w = Bukkit.getWorld(wn);
            if (w == null) return "未找到世界: " + wn;
            int x = Integer.parseInt(xs), y = Integer.parseInt(ys), z = Integer.parseInt(zs);
            org.bukkit.block.Block b = w.getBlockAt(x, y, z);
            return String.format("方块 %s @ (%d,%d,%d): %s", wn, x, y, z, b.getType());
        } catch (NumberFormatException e) {
            return "坐标必须是整数";
        }
    }

    private String queryNear(String wn, String xs, String ys, String zs, String rs) {
        try {
            World w = Bukkit.getWorld(wn);
            if (w == null) return "未找到世界: " + wn;
            int x = Integer.parseInt(xs), y = Integer.parseInt(ys), z = Integer.parseInt(zs);
            int r = Integer.parseInt(rs);
            Location loc = new Location(w, x + 0.5, y + 0.5, z + 0.5);
            java.util.Collection<Entity> es = w.getNearbyEntities(loc, r, r, r);
            List<String> lines = new ArrayList<>();
            int cnt = 0;
            for (Entity e : es) {
                if (cnt >= 30) break;
                Location el = e.getLocation();
                lines.add(String.format("  %s %s @ %.0f,%.0f,%.0f", e.getType(),
                        e instanceof Player ? ((Player) e).getName() : e.getUniqueId().toString().substring(0, 8),
                        el.getX(), el.getY(), el.getZ()));
                cnt++;
            }
            if (es.size() > 30) lines.add("  ... 共 " + es.size() + " 个实体，仅显示前 30");
            return "半径 " + r + " 内实体(" + es.size() + "):\n" + String.join("\n", lines);
        } catch (NumberFormatException e) {
            return "坐标/半径必须是整数";
        }
    }

    // ================= FS =================
    private Path resolve(String pathB64) {
        String rel = new String(Base64.getDecoder().decode(pathB64), java.nio.charset.StandardCharsets.UTF_8);
        Path root = serverRoot();
        Path p;
        if (rel.startsWith("/") || rel.matches("^[A-Za-z]:.*")) {
            if (!fsAllowAbsolute) throw new IllegalArgumentException("禁止绝对路径");
            p = Paths.get(rel);
        } else {
            p = root.resolve(rel);
        }
        p = p.toAbsolutePath().normalize();
        if (!p.startsWith(root)) throw new IllegalArgumentException("路径越界: " + rel);
        return p;
    }

    private String b64(String s) {
        if (s == null) return "";
        return Base64.getEncoder().encodeToString(s.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private String fsDispatch(String s) {
        try {
        String body = s.substring("FS|".length());
        // limit=-1：保留尾随空字段。用默认的 split，`FS|LIST|`（根目录）会被拆成
        // 只有 ["LIST"]，参数个数判断直接失败——根目录永远列不出来。
        String[] t = body.split("\\|", -1);
            if (t.length < 1) return "FS 用法: FS|READ|LIST|EXISTS|STAT|WRITE|APPEND|REPLACE|SETLINE"
                    + "|DELETE|MOVE|COPY|MKDIR|TOUCH|DOWNLOAD";
            String op = t[0].trim().toUpperCase(Locale.ROOT);
            switch (op) {
                case "READ": {
                    require(t, 2, "FS|READ|<pathB64>[|<maxBytes>]");
                    long mb = t.length >= 4 && !t[3].trim().isEmpty() ? Long.parseLong(t[3].trim()) : fsMaxReadBytes;
                    mb = Math.min(mb, fsMaxReadBytes * 4);
                    Path f = resolve(t[1].trim());
                    if (!Files.exists(f)) return "文件不存在: " + f;
                    if (Files.size(f) > mb) return "文件过大(" + Files.size(f) + "B)，上限 " + mb + "B (FS|READ + maxBytes)";
                    return "----- " + f + " -----\n" + new String(Files.readAllBytes(f), java.nio.charset.StandardCharsets.UTF_8);
                }
                case "LIST": {
                    require(t, 2, "FS|LIST|<pathB64>");
                    Path d = resolve(t[1].trim());
                    if (!Files.isDirectory(d)) return "不是目录: " + d;
                    List<String> lines = new ArrayList<>();
                    Files.list(d).sorted().forEach(p -> {
                        try {
                            long sz = Files.size(p);
                            String m = Files.getLastModifiedTime(p).toMillis() > 0
                                    ? new SimpleDateFormat("MM-dd HH:mm").format(new Date(Files.getLastModifiedTime(p).toMillis())) : "-";
                            lines.add(String.format("%s\t%d\t%s\t%s", Files.isDirectory(p) ? "D" : "F",
                                    sz, m, p.getFileName()));
                        } catch (Exception ignored) {
                        }
                    });
                    return "目录 " + d + " (" + lines.size() + " 项):\n" + String.join("\n", lines);
                }
                case "EXISTS": {
                    require(t, 2, "FS|EXISTS|<pathB64>");
                    Path f = resolve(t[1].trim());
                    return (Files.exists(f) ? "存在" : "不存在") + ": " + f;
                }
                case "WRITE": case "APPEND": {
                    require(t, 3, "FS|" + op + "|<pathB64>|<dataB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path f = resolve(t[1].trim());
                    byte[] data = Base64.getDecoder().decode(t[2].trim());
                    if (op.equals("WRITE")) Files.write(f, data);
                    else {
                        java.nio.charset.Charset c = java.nio.charset.StandardCharsets.UTF_8;
                    String tcontent = new String(data, c);
                    if (!Files.exists(f)) Files.createFile(f);
                    Files.write(f, (new String(Files.readAllBytes(f), c) + tcontent).getBytes(c));
                    }
                    return op + " OK: " + f + " (" + data.length + "B)";
                }
                case "REPLACE": {
                    require(t, 5, "FS|REPLACE|<pathB64>|<searchB64>|<replaceB64>|<scope>");
                    if (fsReadOnly) return "FS 只读";
                    Path f = resolve(t[1].trim());
                    String sc = new String(Base64.getDecoder().decode(t[2].trim()), java.nio.charset.StandardCharsets.UTF_8);
                    String rc = new String(Base64.getDecoder().decode(t[3].trim()), java.nio.charset.StandardCharsets.UTF_8);
                    boolean all = t[4].trim().equalsIgnoreCase("all");
                    String content = new String(Files.readAllBytes(f), java.nio.charset.StandardCharsets.UTF_8);
                    String out = all ? content.replace(sc, rc)
                            : content.replaceFirst(java.util.regex.Pattern.quote(sc), java.util.regex.Matcher.quoteReplacement(rc));
                    Files.write(f, out.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                    return "REPLACE OK (" + (all ? "all" : "first") + ")";
                }
                case "SETLINE": {
                    require(t, 4, "FS|SETLINE|<pathB64>|<lineNo>|<contentB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path f = resolve(t[1].trim());
                    int no = Integer.parseInt(t[2].trim());
                    String replace = new String(Base64.getDecoder().decode(t[3].trim()), java.nio.charset.StandardCharsets.UTF_8);
                    List<String> lines = new ArrayList<>(Files.readAllLines(f, java.nio.charset.StandardCharsets.UTF_8));
                    if (no >= 1 && no <= lines.size()) lines.set(no - 1, replace);
                    else return "行号越界(1-" + lines.size() + "): " + no;
                    Files.write(f, lines, java.nio.charset.StandardCharsets.UTF_8);
                    return "SETLINE OK 第" + no + "行";
                }
                /* ---- 下面这组是「文件管理器要用到的其余动作」：删/移/拷/建/看大小/下载分块 ---- */
                case "DELETE": {
                    require(t, 2, "FS|DELETE|<pathB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path f = resolve(t[1].trim());
                    if (!Files.exists(f)) return "不存在: " + f;
                    // 服根目录是整台服务器的家底，误删等于把服务器拆了，不给这条路
                    if (f.equals(serverRoot())) return "拒绝删除服务器根目录";
                    int n = deleteTree(f);
                    return "DELETE OK: " + f.getFileName() + "（" + n + " 项）";
                }
                case "MOVE": case "RENAME": {
                    require(t, 3, "FS|MOVE|<srcB64>|<dstB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path src = resolve(t[1].trim());
                    Path dst = resolve(t[2].trim());
                    if (!Files.exists(src)) return "不存在: " + src;
                    if (dst.startsWith(src)) return "不能移动到自身的子目录";
                    if (dst.getParent() != null) Files.createDirectories(dst.getParent());
                    Files.move(src, dst, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                    return "MOVE OK: " + src.getFileName() + " → " + dst.getFileName();
                }
                case "COPY": {
                    require(t, 3, "FS|COPY|<srcB64>|<dstB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path src = resolve(t[1].trim());
                    Path dst = resolve(t[2].trim());
                    if (!Files.exists(src)) return "不存在: " + src;
                    if (dst.startsWith(src)) return "不能复制到自身的子目录";
                    if (dst.getParent() != null) Files.createDirectories(dst.getParent());
                    int n = copyTree(src, dst);
                    return "COPY OK: " + src.getFileName() + " → " + dst.getFileName() + "（" + n + " 项）";
                }
                case "MKDIR": {
                    require(t, 2, "FS|MKDIR|<pathB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path d = resolve(t[1].trim());
                    Files.createDirectories(d);
                    return "MKDIR OK: " + d.getFileName();
                }
                case "TOUCH": {
                    require(t, 2, "FS|TOUCH|<pathB64>");
                    if (fsReadOnly) return "FS 只读";
                    Path f = resolve(t[1].trim());
                    if (f.getParent() != null) Files.createDirectories(f.getParent());
                    if (!Files.exists(f)) Files.createFile(f);
                    return "TOUCH OK: " + f.getFileName();
                }
                case "STAT": {
                    // 单文件/目录的元信息：网页端下载前先问一句「多大」，再决定分几块拉
                    require(t, 2, "FS|STAT|<pathB64>");
                    Path f = resolve(t[1].trim());
                    if (!Files.exists(f)) return "不存在: " + f;
                    boolean d = Files.isDirectory(f);
                    long sz = d ? 0 : Files.size(f);
                    String m = d ? "-" : new SimpleDateFormat("yyyy-MM-dd HH:mm:ss")
                            .format(new Date(Files.getLastModifiedTime(f).toMillis()));
                    return "STAT\t" + (d ? "D" : "F") + "\t" + sz + "\t" + m;
                }
                case "DOWNLOAD": {
                    // 分块读，二进制安全：正文走 base64，前端逐块取回拼成原文件。
                    // 不复用 READ 的原因：READ 按 UTF-8 解码，zip/jar/图片会直接读坏。
                    require(t, 3, "FS|DOWNLOAD|<pathB64>|<offset>[|<len>]");
                    Path f = resolve(t[1].trim());
                    if (!Files.exists(f)) return "文件不存在: " + f;
                    if (Files.isDirectory(f)) return "这是目录，不能下载";
                    long total = Files.size(f);
                    long off = Long.parseLong(t[2].trim());
                    if (off < 0 || off > total) off = 0;
                    long len = t.length >= 4 && !t[3].trim().isEmpty()
                            ? Long.parseLong(t[3].trim()) : fsMaxReadBytes;
                    len = Math.max(1, Math.min(len, fsMaxReadBytes));
                    byte[] buf = new byte[(int) Math.min(len, total - off)];
                    try (RandomAccessFile raf = new RandomAccessFile(f.toFile(), "r")) {
                        raf.seek(off);
                        raf.readFully(buf);
                    }
                    return "----- " + f + " -----\nsize=" + total + " offset=" + off
                            + " len=" + buf.length + "\n"
                            + Base64.getEncoder().encodeToString(buf);
                }
                default: return "未知 FS 操作: " + op;
            }
        } catch (IllegalArgumentException e) {
            // e.getMessage() 是内部用法串（FS|LIST|<pathB64> 之类），不适合直接给用户看
            return "文件操作参数不完整";
        } catch (Exception e) {
            return "文件操作失败：" + e.getMessage();
        }
    }

    private static void require(String[] t, int n, String usage) {
        if (t.length < n) throw new IllegalArgumentException(usage);
    }

    /** 递归删除，返回动过的条目数（目录本身算一项）。先收子项再收自己，深度不限。 */
    private int deleteTree(Path p) throws Exception {
        if (!Files.isDirectory(p)) {
            Files.deleteIfExists(p);
            return 1;
        }
        int n = 1;
        java.util.List<Path> kids = new ArrayList<>();
        try (java.util.stream.Stream<Path> st = Files.list(p)) {
            st.forEach(kids::add);
        }
        for (Path kid : kids) {
            n += deleteTree(kid);
        }
        Files.deleteIfExists(p);
        return n;
    }

    /** 递归复制，返回复制条目数。目标是文件时按覆盖算，免得「复制过去发现没生效」。 */
    private int copyTree(Path src, Path dst) throws Exception {
        if (!Files.isDirectory(src)) {
            Files.copy(src, dst, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            return 1;
        }
        Files.createDirectories(dst);
        int n = 1;
        java.util.List<Path> kids = new ArrayList<>();
        try (java.util.stream.Stream<Path> st = Files.list(src)) {
            st.forEach(kids::add);
        }
        for (Path kid : kids) {
            n += copyTree(kid, dst.resolve(kid.getFileName().toString()));
        }
        return n;
    }

    // ================= 回执：立即新线程发送（变长，无长度限制） =================
    /** 回执帧 seq 字段回显所响应的命令序号，供控制器把回执关联到待决命令（await 结果）。 */
    private void sendOutput(String text, int cmdSeq) {
        final String t = (text == null || text.isEmpty()) ? "(ok)" : text;
        final OutputStream out = netOut;
        if (out == null) return;
        new Thread(() -> {
            synchronized (writeLock) {
                try {
                    out.write(codec.packOutput((int) (System.currentTimeMillis() / 1000),
                            cmdSeq, currentTps, currentPlayers, t));
                    out.flush();
                } catch (Exception e) {
                    getLogger().log(Level.WARNING, "回执发送失败: " + e.getMessage());
                }
            }
        }, "mc-agent-reply").start();
    }

    private void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }
}
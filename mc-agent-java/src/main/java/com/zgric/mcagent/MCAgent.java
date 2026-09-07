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
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
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
 *  - 通过 FRP 连到 minecraftconsole 控制器 TCP 端口；周期上报 64B 心跳；
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
    private volatile boolean running = false;
    private Thread netThread;

    // 回执序号 + 写锁（网络线程心跳 与 回执新线程 共用，防帧交错）
    private final AtomicInteger outSeq = new AtomicInteger(1);
    private final Object writeLock = new Object();
    // 由网络线程持有并写，回执线程经锁写
    private volatile OutputStream netOut;

    private volatile float currentTps = 20.0f;
    private volatile int currentPlayers = 0;

    private final long[] tickDiffs = new long[20];
    private long lastTick = -1;
    private int tickIdx = 0;

    private String host;
    private int port;
    private long intervalMs;
    private long reconnectMs;

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
        intervalMs = c.getLong("interval-ms", 1000);
        reconnectMs = c.getLong("reconnect-ms", 3000);
        String secret = c.getString("secret", "zcboot-mc-please-change-me");
        fsEnabled = c.getBoolean("fs.enabled", true);
        fsReadOnly = c.getBoolean("fs.read-only", false);
        fsAllowAbsolute = c.getBoolean("fs.allow-absolute-paths", false);
        fsMaxReadBytes = c.getLong("fs.max-read-bytes", 256 * 1024);

        codec = new FrameCodec(secret.getBytes(java.nio.charset.StandardCharsets.UTF_8));

        Bukkit.getScheduler().runTaskTimer(this, this::onTick, 0L, 1L);
        Bukkit.getScheduler().runTaskTimer(this, this::refreshHeartbeatData, 20L, 20L);

        running = true;
        netThread = new Thread(this::runLoop, "mc-agent-net");
        netThread.setDaemon(true);
        netThread.start();
        getLogger().log(Level.INFO, "已启动，连接 " + host + ":" + port);
    }

    @Override
    public void onDisable() {
        running = false;
        if (netThread != null) {
            netThread.interrupt();
            netThread = null;
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

    private void connectAndServe() throws Exception {
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress(host, port), 5000);
            getLogger().log(Level.INFO, "已连上控制器 " + host + ":" + port);

            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();
            netOut = out;

            ByteArrayOutputStream acc = new ByteArrayOutputStream();
            long lastBeat = 0;

            while (running && !Thread.currentThread().isInterrupted()) {
                long now = System.currentTimeMillis();
                // 心跳（与回执线程共用写锁）
                if (now - lastBeat >= intervalMs) {
                    synchronized (writeLock) {
                        out.write(codec.packHeartbeat((int) (now / 1000), currentTps, currentPlayers));
                        out.flush();
                    }
                    lastBeat = now;
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
                    } else if (ft == FrameCodec.COMMAND_TYPE || ft == FrameCodec.OUTPUT_TYPE) {
                        if (data.length - pos < FrameCodec.HEADER_LEN) { residue = true; break; }
                        int plen = ((data[pos + FrameCodec.PLEN_OFF] & 0xFF) << 24)
                                | ((data[pos + FrameCodec.PLEN_OFF + 1] & 0xFF) << 16)
                                | ((data[pos + FrameCodec.PLEN_OFF + 2] & 0xFF) << 8)
                                | (data[pos + FrameCodec.PLEN_OFF + 3] & 0xFF);
                        if (plen < 0 || plen > FrameCodec.MAX_PAYLOAD) { pos++; continue; } // 流失步
                        total = FrameCodec.HEADER_LEN + plen;
                    } else {
                        pos++; continue; // 重新同步
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
        }
    }

    private int cmdSeq = 0;   // 已接受命令帧的序号
    private int cmdTs = 0;    // 已接受命令帧的时间戳（与 seq 组成二元单调，重启/重连不漂移）

    private void handleFrame(byte[] frame) {
        FrameCodec.Parsed p = FrameCodec.parse(frame);
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
        String[] t = body.split("\\|");
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
            String[] t = body.split("\\|");
            if (t.length < 1) return "FS 用法: FS|READ|LIST|EXISTS|WRITE|APPEND|REPLACE|SETLINE";
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
                default: return "未知 FS 操作: " + op;
            }
        } catch (IllegalArgumentException e) {
            return "FS 参数错误: " + e.getMessage();
        } catch (Exception e) {
            return "FS 失败: " + e.getClass().getSimpleName() + ": " + e.getMessage();
        }
    }

    private static void require(String[] t, int n, String usage) {
        if (t.length < n) throw new IllegalArgumentException(usage);
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
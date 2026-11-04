package com.zgric.mcagent;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 单条控制连接上的双向 RPC 状态机（Java 侧 = TCP 连接发起方，用奇数 id）。
 *
 * 零 Bukkit 依赖：协议层可脱离服务端单独编译与验证。
 *
 * 重要：本类只做「异步 + 回调」，不提供同步阻塞的 call()。
 * 控制通道的读循环是单线程；若在网络读线程里发 REQ 然后等 RESP，
 * 就没人去读那个 RESP —— 这正是「两边都主动发包」最容易踩的互锁。
 *
 * 标准映射：
 *   JSON-RPC 2.0 §4/§5  id 关联、Notification 不回复、错误码区间
 *   RFC 9113  §5.1.1    连接发起方奇数 id，0 保留
 *   RSocket   KEEPALIVE 心跳里的 position 用于断线增量重传
 *   Kubernetes watch    resourceVersion + 410 Gone 时全量重拉
 */
public final class RpcHub {

    public interface Sender {
        void send(byte[] frame) throws Exception;
    }

    public interface Callback {
        void onResult(boolean ok, Object result, Map<String, Object> error);
    }

    public interface Handler {
        Object handle(Map<String, Object> params);
    }

    public static final int METHOD_NOT_FOUND = -32601;
    public static final int INTERNAL_ERROR = -32603;
    public static final int EPOCH_TOO_OLD = -32010;
    public static final int PASS_UNAVAILABLE = -32011;

    private static final long DEFAULT_TIMEOUT_MS = 5000;
    private static final int MAX_PENDING = 256;

    private final FrameCodec codec;
    private final Sender sender;
    private final boolean enabled;
    private final AtomicInteger nextId = new AtomicInteger(1);   // client 角色：1,3,5...
    private final AtomicInteger txSeq = new AtomicInteger(0);
    private final AtomicInteger rxSeq = new AtomicInteger(0);
    private final ConcurrentHashMap<Integer, Pending> pending =
            new ConcurrentHashMap<Integer, Pending>();
    private final ConcurrentHashMap<String, Handler> methods =
            new ConcurrentHashMap<String, Handler>();
    private final ScheduledExecutorService timer;

    private volatile boolean peerV1 = false;
    private volatile int peerCap = 0;
    private volatile int knownEpoch = 0;
    private volatile float tps = 20.0f;
    private volatile int players = 0;
    private volatile long lastSyncAt = 0;
    private volatile boolean fullResyncSent = false;

    public RpcHub(final FrameCodec codec, Sender sender, boolean enabled) {
        this.codec = codec;
        this.sender = sender;
        this.enabled = enabled;
        this.timer = Executors.newSingleThreadScheduledExecutor(new ThreadFactory() {
            @Override public Thread newThread(Runnable r) {
                Thread t = new Thread(r, "mc-agent-rpc-timeout");
                t.setDaemon(true);
                return t;
            }
        });
        registerMethod("rpc.capabilities", new Handler() {
            @Override public Object handle(Map<String, Object> params) {
                Map<String, Object> out = new LinkedHashMap<String, Object>();
                out.put("ver", 1);
                out.put("role", "client");
                out.put("cap", FrameCodec.CAP_ALL);
                out.put("methods", new ArrayList<String>(methods.keySet()));
                return out;
            }
        });
    }

    public boolean isEnabled() { return enabled; }
    public int getKnownEpoch() { return knownEpoch; }
    public boolean isPeerV1() { return peerV1; }

    public void registerMethod(String name, Handler handler) {
        if (name.startsWith("rpc.") && !"rpc.capabilities".equals(name)) {
            throw new IllegalArgumentException("rpc. prefix is reserved (JSON-RPC 2.0 §8)");
        }
        methods.put(name, handler);
    }

    public void setPeerState(float tps, int players) {
        this.tps = tps;
        this.players = players;
    }

    /** 能力协商：收到 v1 帧（magic 有效）即认为对端支持 RPC。 */
    public void onHello(FrameCodec.Ext ext) {
        if (!ext.v1) return;
        peerV1 = true;
        peerCap = ext.cap;
        if ((ext.flags & FrameCodec.FLAG_EPOCH) != 0) onEpoch(ext.epoch);
    }

    /**
     * 心跳书签：只带版本号，不带业务数据。
     * 发现服务端 epoch 领先就发一次增量拉取；窗口不足（-32010）时改全量重拉。
     */
    public void onEpoch(int serverEpoch) {
        if (serverEpoch <= knownEpoch) return;
        // 注意：这里不能直接把 knownEpoch 设成 serverEpoch，否则增量拉取的
        // since 就是最新水位，什么都拉不到。先按旧水位拉增量，成功后由响应推进。
        if (!enabled || !peerV1) {
            knownEpoch = serverEpoch;            // 未启用 RPC 时只跟水位，不发请求
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastSyncAt < 300) return;      // 同批次变化合并成一次拉取
        lastSyncAt = now;
        fullResyncSent = false;
        requestSync(knownEpoch);
    }

    private void requestSync(int since) {
        Map<String, Object> params = new LinkedHashMap<String, Object>();
        params.put("since", since);
        request("zcore.sync", params, DEFAULT_TIMEOUT_MS, new Callback() {
            @Override public void onResult(boolean ok, Object result,
                                           Map<String, Object> error) {
                if (!ok && error != null && intValue(error.get("code")) == EPOCH_TOO_OLD
                        && !fullResyncSent) {
                    fullResyncSent = true;
                    clearLocalState();
                    requestSync(0);              // 等价 Kubernetes 410 Gone 回退
                    return;
                }
                if (ok && result instanceof Map) {
                    Map<?, ?> r = (Map<?, ?>) result;
                    if (Boolean.TRUE.equals(r.get("full"))) clearLocalState();
                    onSyncResult(r.get("items"));
                    int serverEpoch = intValue(r.get("epoch"));
                    if (serverEpoch > knownEpoch) knownEpoch = serverEpoch;
                }
            }
        });
    }

    /** 业务层覆写：应用增量变更 / 全量快照。默认只记录数量。 */
    protected void onSyncResult(Object items) {
        if (items instanceof List) {
            // 由 zcore whitelist 插件继承或注册 zcore.sync handler 覆盖
        }
    }

    protected void clearLocalState() {
        knownEpoch = 0;
    }

    // ------------------------------------------------------------ 发起调用
    public void request(String method, Map<String, Object> params, Callback cb) {
        request(method, params, DEFAULT_TIMEOUT_MS, cb);
    }

    public void request(String method, Map<String, Object> params,
                        long timeoutMs, final Callback cb) {
        if (!enabled || !peerV1) {
            failClosed(cb, "capability-not-negotiated");
            return;
        }
        if (pending.size() >= MAX_PENDING) {
            failClosed(cb, "too-many-pending");
            return;
        }
        final int id = nextId.getAndAdd(2);
        Map<String, Object> env = new LinkedHashMap<String, Object>();
        env.put("jsonrpc", "2.0");
        env.put("method", method);
        env.put("params", params == null ? new LinkedHashMap<String, Object>() : params);
        env.put("id", id);
        pending.put(id, new Pending(method, cb));
        try {
            send(FrameCodec.REQ_TYPE, env);
        } catch (Exception e) {
            pending.remove(id);
            failClosed(cb, "send-failed:" + e.getMessage());
            return;
        }
        timer.schedule(new Runnable() {
            @Override public void run() {
                Pending p = pending.remove(id);
                if (p != null) failClosed(p.callback, "timeout");
            }
        }, timeoutMs, TimeUnit.MILLISECONDS);
    }

    /** Notification：无 id，按 JSON-RPC 2.0 §4.1 不期待任何回复。 */
    public void notify(String method, Map<String, Object> params) {
        if (!enabled || !peerV1) return;
        Map<String, Object> env = new LinkedHashMap<String, Object>();
        env.put("jsonrpc", "2.0");
        env.put("method", method);
        env.put("params", params == null ? new LinkedHashMap<String, Object>() : params);
        try {
            send(FrameCodec.REQ_TYPE, env);
        } catch (Exception ignored) {
        }
    }

    private void send(int type, Map<String, Object> env) throws Exception {
        byte[] json = MiniJson.stringify(env).getBytes(StandardCharsets.UTF_8);
        int ts = (int) (System.currentTimeMillis() / 1000);
        int seq = txSeq.incrementAndGet();
        byte[] frame = codec.packRpc(ts, seq, tps, players, type, json,
                FrameCodec.CAP_ALL, knownEpoch);
        sender.send(frame);
    }

    // ------------------------------------------------------------ 收包处理
    /** @return true 表示该帧已由 RPC 层消费（调用方不要再按旧类型分发）。 */
    public boolean onFrame(byte[] frame) {
        int type = frame[0] & 0xFF;
        if (type != FrameCodec.REQ_TYPE && type != FrameCodec.RESP_TYPE) return false;
        if (!codec.verifyExtSig(frame) || !codec.verifyRpcSig(frame)) return true;
        int seq = ByteBufferHelper.getInt(frame, FrameCodec.SEQ_OFF);
        int last = rxSeq.get();
        if (seq <= last || !rxSeq.compareAndSet(last, seq)) return true;  // 重放
        onHello(FrameCodec.parseExt(frame));
        byte[] json = codec.rpcJson(frame);
        if (json == null) return true;
        Map<String, Object> env;
        try {
            env = MiniJson.parseObject(new String(json, StandardCharsets.UTF_8));
        } catch (Exception e) {
            return true;
        }
        if (!"2.0".equals(env.get("jsonrpc"))) return true;
        boolean hasMethod = env.get("method") instanceof String;
        boolean hasId = env.containsKey("id");
        if (hasMethod) {
            if (hasId) handleRequest(env);
            else handleNotification(env);
        } else if (hasId) {
            handleResponse(env);
        }
        return true;
    }

    private void handleRequest(final Map<String, Object> env) {
        String method = String.valueOf(env.get("method"));
        Object id = env.get("id");
        Handler h = methods.get(method);
        Map<String, Object> reply = new LinkedHashMap<String, Object>();
        reply.put("jsonrpc", "2.0");
        reply.put("id", id);
        if (h == null) {
            reply.put("error", error(METHOD_NOT_FOUND, "Method not found"));
        } else {
            try {
                Object params = env.get("params");
                @SuppressWarnings("unchecked")
                Map<String, Object> p = params instanceof Map
                        ? (Map<String, Object>) params : new LinkedHashMap<String, Object>();
                reply.put("result", h.handle(p));
            } catch (Exception e) {
                reply.put("error", error(INTERNAL_ERROR, "Internal error"));
            }
        }
        try {
            send(FrameCodec.RESP_TYPE, reply);
        } catch (Exception ignored) {
        }
    }

    private void handleNotification(Map<String, Object> env) {
        // 服务端推来的 pass 变更：epoch 由下一次心跳/帧确认，这里不单独回包
        Object params = env.get("params");
        if (params instanceof Map) {
            Object ep = ((Map<?, ?>) params).get("epoch");
            int v = intValue(ep);
            if (v > knownEpoch) knownEpoch = v;
        }
    }

    @SuppressWarnings("unchecked")
    private void handleResponse(Map<String, Object> env) {
        Object idObj = env.get("id");
        int id = intValue(idObj);
        Pending p = pending.remove(id);
        if (p == null || p.callback == null) return;
        if (env.containsKey("error")) {
            Object err = env.get("error");
            p.callback.onResult(false, null,
                    err instanceof Map ? (Map<String, Object>) err
                            : error(PASS_UNAVAILABLE, "rpc error"));
        } else {
            p.callback.onResult(true, env.get("result"), null);
        }
    }

    private Map<String, Object> error(int code, String message) {
        Map<String, Object> e = new LinkedHashMap<String, Object>();
        e.put("code", code);
        e.put("message", message);
        return e;
    }

    private static void failClosed(Callback cb, String reason) {
        if (cb == null) return;
        Map<String, Object> pendingState = new LinkedHashMap<String, Object>();
        pendingState.put("state", "pending");
        pendingState.put("reason", reason);
        cb.onResult(false, pendingState, null);
    }

    private static int intValue(Object v) {
        if (v instanceof Number) return ((Number) v).intValue();
        if (v instanceof String) {
            try { return Integer.parseInt((String) v); } catch (Exception ignored) { return 0; }
        }
        return 0;
    }

    public void onDisconnected() {
        peerV1 = false;
        peerCap = 0;
        rxSeq.set(0);
        txSeq.set(0);
        knownEpoch = 0;
        for (Pending p : pending.values()) failClosed(p.callback, "connection-closed");
        pending.clear();
    }

    public void shutdown() {
        timer.shutdownNow();
    }

    private static final class Pending {
        final String method;
        final Callback callback;

        Pending(String method, Callback callback) {
            this.method = method;
            this.callback = callback;
        }
    }

    /** 避免引入 java.nio 之外的依赖，直接读大端 int。 */
    private static final class ByteBufferHelper {
        static int getInt(byte[] b, int off) {
            return ((b[off] & 0xFF) << 24) | ((b[off + 1] & 0xFF) << 16)
                    | ((b[off + 2] & 0xFF) << 8) | (b[off + 3] & 0xFF);
        }
    }
}

import com.zgric.mcagent.FrameCodec;
import com.zgric.mcagent.MiniJson;
import com.zgric.mcagent.RpcHub;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 跨语言互操作 smoke：不依赖 Bukkit，直接用 target/classes 跑。
 *
 * <p>前半段是静态比对（Python 生成的 golden 帧 vs Java 的 FrameCodec），
 * 后半段连真实 Python mc_link 适配器做 RPC 往返，其中 zcore.pass.query
 * 验证的是第三端签发者（不是 fail-closed 占位）。
 *
 * <p>用法：java -cp "mc-agent-java/target/classes:..." InteropSmoke <interop-dir>
 * interop-dir 需含 golden.txt 与 port.txt，由 interop_server.py 写出。
 */
public class InteropSmoke {

    static final byte[] SECRET = "interop-test-secret".getBytes(StandardCharsets.UTF_8);
    static final String APPROVED_PLAYER = "InteropApproved";
    static final String UNKNOWN_PLAYER = "InteropUnknown";

    private static int passed = 0;
    private static int failed = 0;

    private static void check(boolean ok, String name, String detail) {
        if (ok) { passed++; System.out.println("PASS " + name); }
        else { failed++; System.out.println("FAIL " + name + " :: " + detail); }
    }

    private static byte[] unhex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    private static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x & 0xFF));
        return sb.toString();
    }

    public static void main(String[] args) throws Exception {
        String dir = args[0];
        FrameCodec codec = new FrameCodec(SECRET);
        int ts = 1700000000;

        Map<String, String> golden = new LinkedHashMap<String, String>();
        for (String line : Files.readAllLines(Paths.get(dir, "golden.txt"), StandardCharsets.UTF_8)) {
            int i = line.indexOf(':');
            if (i > 0) golden.put(line.substring(0, i), line.substring(i + 1));
        }

        // ---- 1) 逐字节比对 Python 生成的 golden 帧 ----
        byte[] v0 = unhex(golden.get("v0_heartbeat"));
        check(Arrays.equals(v0, codec.packHeartbeat(ts, 19.8f, 3)),
                "v0 heartbeat bytes", hex(codec.packHeartbeat(ts, 19.8f, 3)));
        check(!FrameCodec.parseExt(v0).v1, "v0 detected as legacy", "magic mismatch");

        byte[] v1 = unhex(golden.get("v1_heartbeat"));
        check(Arrays.equals(v1, codec.packHeartbeatV1(ts, 19.8f, 3, FrameCodec.CAP_ALL, 42)),
                "v1 heartbeat bytes", hex(codec.packHeartbeatV1(ts, 19.8f, 3, FrameCodec.CAP_ALL, 42)));
        FrameCodec.Ext ext = FrameCodec.parseExt(v1);
        check(ext.v1 && ext.epoch == 42 && ext.cap == FrameCodec.CAP_ALL,
                "v1 ext roundtrip", "epoch=" + ext.epoch + " cap=" + ext.cap);
        check(codec.verifyExtSig(v1), "v1 ext signature", "bad sig");
        byte[] tampered = v1.clone();
        tampered[FrameCodec.EXT_OFF + 8] ^= 1;
        check(!codec.verifyExtSig(tampered), "tampered epoch rejected", "not rejected");

        byte[] req = unhex(golden.get("req"));
        String reqJson = golden.get("req_json");
        byte[] rebuilt = codec.packRpc(ts, 7, 19.8f, 3, FrameCodec.REQ_TYPE,
                reqJson.getBytes(StandardCharsets.UTF_8), FrameCodec.CAP_ALL, 42);
        check(Arrays.equals(req, rebuilt), "REQ frame bytes", hex(rebuilt));
        check(codec.verifyRpcSig(req), "REQ signature", "bad sig");
        check(reqJson.equals(new String(codec.rpcJson(req), StandardCharsets.UTF_8)),
                "REQ json extraction", "mismatch");

        byte[] resp = unhex(golden.get("resp"));
        check(codec.verifyRpcSig(resp), "RESP signature", "bad sig");
        byte[] badResp = resp.clone();
        badResp[FrameCodec.HEADER_LEN + FrameCodec.RPC_SIG_LEN + 3] ^= 1;
        check(!codec.verifyRpcSig(badResp), "tampered RESP rejected", "not rejected");

        // ---- 2) MiniJson 往返 ----
        Map<String, Object> obj = MiniJson.parseObject(reqJson);
        check("zcore.sync".equals(obj.get("method")) && "2.0".equals(obj.get("jsonrpc")),
                "MiniJson parse", String.valueOf(obj));
        Map<String, Object> nested = new LinkedHashMap<String, Object>();
        nested.put("s", "中文 \"引号\"\n");
        nested.put("n", 42);
        nested.put("b", true);
        nested.put("nil", null);
        nested.put("arr", Arrays.asList(1, 2, 3));
        String encoded = MiniJson.stringify(nested);
        Map<String, Object> back = MiniJson.parseObject(encoded);
        check("中文 \"引号\"\n".equals(back.get("s")) && back.containsKey("nil")
                        && ((List<?>) back.get("arr")).size() == 3,
                "MiniJson stringify/parse", encoded);

        // ---- 3) 能力协商前 fail-closed ----
        final AtomicReference<String> failState = new AtomicReference<String>("");
        RpcHub hub = new RpcHub(codec, new RpcHub.Sender() {
            @Override public void send(byte[] frame) { }
        }, true);
        hub.request("zcore.pass.query", new LinkedHashMap<String, Object>(),
                new RpcHub.Callback() {
                    @Override public void onResult(boolean ok, Object result,
                                                  Map<String, Object> error) {
                        if (result instanceof Map) {
                            failState.set(String.valueOf(((Map<?, ?>) result).get("state")));
                        }
                    }
                });
        check("pending".equals(failState.get()), "fail-closed before hello", failState.get());
        hub.shutdown();

        // ---- 4) 真实 TCP：与 Python mc_link 适配器互通 ----
        int port = Integer.parseInt(new String(
                Files.readAllBytes(Paths.get(dir, "port.txt")), StandardCharsets.UTF_8).trim());
        Socket sock = new Socket("127.0.0.1", port);
        sock.setSoTimeout(6000);
        InputStream in = sock.getInputStream();
        OutputStream out = sock.getOutputStream();
        int liveTs = (int) (System.currentTimeMillis() / 1000);

        // v1 心跳声明能力 → 服务端回 BOOKMARK
        out.write(codec.packHeartbeatV1(liveTs, 20.0f, 2, FrameCodec.CAP_ALL, 0));
        out.flush();
        byte[] bookmark = readFrame(in, FrameCodec.HEARTBEAT_TYPE);
        FrameCodec.Ext be = FrameCodec.parseExt(bookmark);
        check(be.v1, "BOOKMARK is v1", "not v1");
        check(codec.verifyExtSig(bookmark), "BOOKMARK signature", "bad sig");
        System.out.println("INFO bookmark epoch=" + be.epoch + " cap=" + be.cap);
        check(be.epoch >= 1, "BOOKMARK carries server epoch", "epoch=" + be.epoch);

        // rpc.capabilities
        Map<String, Object> res = call(codec, in, out, 1, "rpc.capabilities",
                new LinkedHashMap<String, Object>(), be.epoch);
        check(((Number) res.get("id")).intValue() == 1, "live RESP id correlation",
                String.valueOf(res.get("id")));
        Object result = res.get("result");
        boolean hasSync = result instanceof Map
                && ((Map<?, ?>) result).get("methods") instanceof List
                && ((List<?>) ((Map<?, ?>) result).get("methods")).contains("zcore.sync");
        check(hasSync, "live RESP method table", String.valueOf(result));

        // zcore.sync 全量对账
        Map<String, Object> syncParams = new LinkedHashMap<String, Object>();
        syncParams.put("since", 0);
        Map<String, Object> sync = call(codec, in, out, 3, "zcore.sync",
                syncParams, be.epoch);
        Object sr = sync.get("result");
        check(sr instanceof Map && Boolean.TRUE.equals(((Map<?, ?>) sr).get("full")),
                "live zcore.sync full resync", String.valueOf(sync));

        // ---- 5) 第三端：zcore.pass.query 走真实签发者 ----
        Map<String, Object> approvedParams = new LinkedHashMap<String, Object>();
        approvedParams.put("player", APPROVED_PLAYER);
        Map<String, Object> approved = call(codec, in, out, 5, "zcore.pass.query",
                approvedParams, be.epoch);
        Object ar = approved.get("result");
        check(ar instanceof Map && "approved".equals(((Map<?, ?>) ar).get("state")),
                "live pass query approved", String.valueOf(ar));

        Map<String, Object> unknownParams = new LinkedHashMap<String, Object>();
        unknownParams.put("player", UNKNOWN_PLAYER);
        Map<String, Object> unknown = call(codec, in, out, 7, "zcore.pass.query",
                unknownParams, be.epoch);
        Object ur = unknown.get("result");
        check(ur instanceof Map && "pending".equals(((Map<?, ?>) ur).get("state")),
                "live pass query fail-closed", String.valueOf(ur));

        sock.close();
        System.out.println("SUMMARY passed=" + passed + " failed=" + failed);
        if (failed > 0) System.exit(1);
    }

    /** 发一个 REQ 并读回同 id 的 RESP。 */
    private static Map<String, Object> call(FrameCodec codec, InputStream in,
                                            OutputStream out, int id, String method,
                                            Map<String, Object> params, int epoch)
            throws Exception {
        Map<String, Object> env = new LinkedHashMap<String, Object>();
        env.put("jsonrpc", "2.0");
        env.put("method", method);
        env.put("params", params);
        env.put("id", id);
        int ts = (int) (System.currentTimeMillis() / 1000);
        out.write(codec.packRpc(ts, id, 20.0f, 2, FrameCodec.REQ_TYPE,
                MiniJson.stringify(env).getBytes(StandardCharsets.UTF_8),
                FrameCodec.CAP_ALL, epoch));
        out.flush();
        byte[] frame = readFrame(in, FrameCodec.RESP_TYPE);
        if (!codec.verifyRpcSig(frame)) {
            throw new IllegalStateException("RESP signature invalid for " + method);
        }
        return MiniJson.parseObject(new String(codec.rpcJson(frame), StandardCharsets.UTF_8));
    }

    /**
     * 按帧类型读一整帧；遇到其它类型（BOOKMARK、Notification）整帧跳过，
     * 与 Python 侧 _read_type 的语义一致。对齐 RFC 9113 §4.1「未知帧忽略」。
     */
    private static byte[] readFrame(InputStream in, int expectedType) throws Exception {
        long deadline = System.currentTimeMillis() + 6000;
        while (System.currentTimeMillis() < deadline) {
            byte[] header;
            try {
                header = readExactly(in, FrameCodec.HEADER_LEN);
            } catch (SocketTimeoutException te) {
                break;
            }
            int type = header[0] & 0xFF;
            int plen = ((header[FrameCodec.PLEN_OFF] & 0xFF) << 24)
                    | ((header[FrameCodec.PLEN_OFF + 1] & 0xFF) << 16)
                    | ((header[FrameCodec.PLEN_OFF + 2] & 0xFF) << 8)
                    | (header[FrameCodec.PLEN_OFF + 3] & 0xFF);
            byte[] payload = plen > 0 ? readExactly(in, plen) : new byte[0];
            if (type == expectedType) {
                ByteArrayOutputStream all = new ByteArrayOutputStream();
                all.write(header);
                all.write(payload);
                return all.toByteArray();
            }
            System.out.println("INFO skip frame type=0x" + Integer.toHexString(type));
        }
        throw new IllegalStateException("timeout waiting for frame type 0x"
                + Integer.toHexString(expectedType));
    }

    private static byte[] readExactly(InputStream in, int n) throws Exception {
        byte[] buf = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(buf, off, n - off);
            if (r < 0) throw new IllegalStateException("connection closed mid-frame");
            off += r;
        }
        return buf;
    }
}

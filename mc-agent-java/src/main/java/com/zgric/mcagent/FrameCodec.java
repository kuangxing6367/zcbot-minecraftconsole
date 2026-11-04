package com.zgric.mcagent;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Arrays;

/**
 * 与 minecraftconsole (Python) 端一致的可变长二进制帧编解码 + 安全校验。
 *
 * 基础帧头（0-63）为历史格式，字节级不可改：
 *   offset 0     : type (1B)  0x01 心跳 / 0x02 命令 / 0x03 回执 / 0x04 日志
 *   offset 1-4   : ts (4B)   时间戳（秒）
 *   offset 5-20  : token (16B) HMAC-SHA256(secret, ts4BE)[:16]
 *   offset 21-24 : tps (4B float)
 *   offset 25-28 : players (4B)
 *   offset 29-32 : plen (4B)  载荷字节数（心跳为 0）
 *   offset 33-36 : seq (4B)   单调序号（按方向独立）
 *   offset 37-63 : 保留区（旧实现全 0）
 *   之后         : 载荷（变长）
 *
 * v1 扩展占用保留区前 20B；旧端全 0 时视为 v0，行为逐字节不变：
 *   37-38 magic b"ZC" / 39 ver / 40 flags / 41-44 cap / 45-48 epoch
 *   49-56 ext_sig(8B) = HMAC-SHA256(secret, ts4BE || f[37:49])[:8]
 *   57-63 保留 7B
 * 新增帧类型 0x05 REQ / 0x06 RESP，载荷 = sig(16B) + UTF-8 JSON envelope。
 *
 * 帧头 token 仍只覆盖 ts（旧端要校验），所以保留区与正文各自独立签名。
 * 标准依据见 site/protocol/rpc.md（RFC 9113 / JSON-RPC 2.0 / RSocket / Kubernetes watch）。
 */
public final class FrameCodec {

    public static final int HEARTBEAT_TYPE = 0x01;
    public static final int COMMAND_TYPE  = 0x02;
    public static final int OUTPUT_TYPE   = 0x03;
    public static final int LOG_TYPE      = 0x04;
    public static final int REQ_TYPE      = 0x05;
    public static final int RESP_TYPE     = 0x06;

    public static final int HEARTBEAT_LEN = 64;
    public static final int HEADER_LEN    = 64;
    public static final int PLEN_OFF      = 29;
    public static final int SEQ_OFF       = 33;
    public static final int MAX_PAYLOAD   = 16 * 1024 * 1024;
    public static final long REPLAY_WINDOW_MS = 3000;

    // ---- v1 扩展 ----
    public static final int EXT_OFF        = 37;
    public static final int EXT_FIELDS_LEN = 12;   // magic..epoch
    public static final int EXT_SIG_LEN    = 8;
    public static final int RPC_SIG_LEN    = 16;
    public static final byte[] MAGIC       = new byte[] { 'Z', 'C' };
    public static final int EXT_VER        = 1;
    public static final int FLAG_CAP       = 0x01;
    public static final int FLAG_EPOCH     = 0x02;
    public static final int CAP_RPC        = 0x01;
    public static final int CAP_PASS       = 0x02;
    public static final int CAP_PUSH       = 0x04;
    public static final int CAP_ALL        = CAP_RPC | CAP_PASS | CAP_PUSH;

    private final byte[] secret;
    private final Mac mac;

    public FrameCodec(byte[] secret) {
        this.secret = secret.clone();
        try {
            this.mac = Mac.getInstance("HmacSHA256");
        } catch (Exception e) {
            throw new IllegalStateException("HmacSHA256 不可用", e);
        }
    }

    /** 历史 token：HMAC(secret, ts4BE)[:16]。不可改，旧端与新端都靠它校验。 */
    private byte[] token(int ts) {
        try {
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            byte[] h = mac.doFinal(ByteBuffer.allocate(4).putInt(ts).array());
            return Arrays.copyOf(h, 16);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** 局部 Mac 实例，避免与 token() 共享状态在多线程下交错。 */
    private byte[] hmac(byte[]... parts) {
        try {
            Mac m = Mac.getInstance("HmacSHA256");
            m.init(new SecretKeySpec(secret, "HmacSHA256"));
            for (byte[] part : parts) m.update(part);
            return m.doFinal();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private byte[] tsBytes(int ts) {
        return ByteBuffer.allocate(4).putInt(ts).array();
    }

    private byte[] extSig(int ts, byte[] ext) {
        return Arrays.copyOf(hmac(tsBytes(ts), ext), EXT_SIG_LEN);
    }

    public byte[] rpcSig(int ts, byte[] ext, byte[] json) {
        return Arrays.copyOf(hmac(tsBytes(ts), ext, json), RPC_SIG_LEN);
    }

    public static byte[] makeExt(int cap, int epoch) {
        byte[] e = new byte[EXT_FIELDS_LEN];
        if (cap == 0) return e;                  // v0：全 0
        e[0] = MAGIC[0];
        e[1] = MAGIC[1];
        e[2] = (byte) EXT_VER;
        e[3] = (byte) (FLAG_CAP | FLAG_EPOCH);
        System.arraycopy(ByteBuffer.allocate(4).putInt(cap).array(), 0, e, 4, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(epoch).array(), 0, e, 8, 4);
        return e;
    }

    private void applyExt(byte[] f, int ts, int cap, int epoch) {
        byte[] ext = makeExt(cap, epoch);
        System.arraycopy(ext, 0, f, EXT_OFF, EXT_FIELDS_LEN);
        if (cap != 0) {
            System.arraycopy(extSig(ts, ext), 0, f,
                    EXT_OFF + EXT_FIELDS_LEN, EXT_SIG_LEN);
        }
    }

    private void fillHeader(byte[] f, int type, int ts, float tps, int players) {
        f[0] = (byte) type;
        System.arraycopy(tsBytes(ts), 0, f, 1, 4);
        System.arraycopy(token(ts), 0, f, 5, 16);
        System.arraycopy(ByteBuffer.allocate(4).putFloat(tps).array(), 0, f, 21, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(players).array(), 0, f, 25, 4);
        // plen / seq 由调用方覆盖
    }

    // ================= 历史接口（字节不变） =================

    /** 心跳帧：固定 64B，无载荷。v0 路径保留区全 0。 */
    public byte[] packHeartbeat(int ts, float tps, int players) {
        byte[] f = new byte[HEARTBEAT_LEN];
        fillHeader(f, HEARTBEAT_TYPE, ts, tps, players);
        return f;
    }

    /** v1 心跳：带能力位图与 epoch 书签（RSocket KEEPALIVE / K8s BOOKMARK 语义）。 */
    public byte[] packHeartbeatV1(int ts, float tps, int players, int cap, int epoch) {
        byte[] f = new byte[HEARTBEAT_LEN];
        fillHeader(f, HEARTBEAT_TYPE, ts, tps, players);
        applyExt(f, ts, cap, epoch);
        return f;
    }

    public byte[] packCommand(int ts, float tps, int players, String cmd) {
        byte[] body = cmd.getBytes(StandardCharsets.UTF_8);
        byte[] f = new byte[HEADER_LEN + body.length];
        fillHeader(f, COMMAND_TYPE, ts, tps, players);
        System.arraycopy(ByteBuffer.allocate(4).putInt(body.length).array(), 0, f, PLEN_OFF, 4);
        System.arraycopy(body, 0, f, HEADER_LEN, body.length);
        return f;
    }

    public byte[] packOutput(int ts, int seq, float tps, int players, String text) {
        byte[] body = text.getBytes(StandardCharsets.UTF_8);
        byte[] f = new byte[HEADER_LEN + body.length];
        fillHeader(f, OUTPUT_TYPE, ts, tps, players);
        System.arraycopy(ByteBuffer.allocate(4).putInt(body.length).array(), 0, f, PLEN_OFF, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(seq).array(), 0, f, SEQ_OFF, 4);
        System.arraycopy(body, 0, f, HEADER_LEN, body.length);
        return f;
    }

    public byte[] packLog(int ts, int seq, float tps, int players, String text) {
        byte[] body = text.getBytes(StandardCharsets.UTF_8);
        byte[] f = new byte[HEADER_LEN + body.length];
        fillHeader(f, LOG_TYPE, ts, tps, players);
        System.arraycopy(ByteBuffer.allocate(4).putInt(body.length).array(), 0, f, PLEN_OFF, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(seq).array(), 0, f, SEQ_OFF, 4);
        System.arraycopy(body, 0, f, HEADER_LEN, body.length);
        return f;
    }

    // ================= v1 新增 =================

    /** RPC 帧：64B 头 + sig(16B) + JSON 正文。type 为 REQ 或 RESP，双向对等。 */
    public byte[] packRpc(int ts, int seq, float tps, int players, int type,
                          byte[] json, int cap, int epoch) {
        byte[] header = new byte[HEADER_LEN];
        fillHeader(header, type, ts, tps, players);
        applyExt(header, ts, cap, epoch);
        byte[] ext = Arrays.copyOfRange(header, EXT_OFF, EXT_OFF + EXT_FIELDS_LEN);
        byte[] sig = rpcSig(ts, ext, json);
        byte[] f = new byte[HEADER_LEN + RPC_SIG_LEN + json.length];
        System.arraycopy(header, 0, f, 0, HEADER_LEN);
        System.arraycopy(ByteBuffer.allocate(4).putInt(RPC_SIG_LEN + json.length).array(),
                0, f, PLEN_OFF, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(seq).array(), 0, f, SEQ_OFF, 4);
        System.arraycopy(sig, 0, f, HEADER_LEN, RPC_SIG_LEN);
        System.arraycopy(json, 0, f, HEADER_LEN + RPC_SIG_LEN, json.length);
        return f;
    }

    /** 是否为变长帧（载荷长度由 plen 决定）。未知类型也按此规则整帧跳过。 */
    public static boolean isVariableType(int type) {
        return type == COMMAND_TYPE || type == OUTPUT_TYPE || type == LOG_TYPE
                || type == REQ_TYPE || type == RESP_TYPE;
    }

    public static Ext parseExt(byte[] frame) {
        Ext e = new Ext();
        e.raw = Arrays.copyOfRange(frame, EXT_OFF, EXT_OFF + EXT_FIELDS_LEN);
        e.v1 = e.raw[0] == MAGIC[0] && e.raw[1] == MAGIC[1];
        if (!e.v1) return e;
        e.ver = e.raw[2] & 0xFF;
        e.flags = e.raw[3] & 0xFF;
        e.cap = ByteBuffer.wrap(e.raw, 4, 4).getInt();
        e.epoch = ByteBuffer.wrap(e.raw, 8, 4).getInt();
        return e;
    }

    /** 校验保留区签名；v0 帧返回 true（放行，行为不变）。 */
    public boolean verifyExtSig(byte[] frame) {
        Ext e = parseExt(frame);
        if (!e.v1) return true;
        int ts = ByteBuffer.wrap(frame, 1, 4).getInt();
        byte[] actual = Arrays.copyOfRange(frame, EXT_OFF + EXT_FIELDS_LEN,
                EXT_OFF + EXT_FIELDS_LEN + EXT_SIG_LEN);
        return MessageDigest.isEqual(actual, extSig(ts, e.raw));
    }

    public byte[] rpcJson(byte[] frame) {
        int plen = ByteBuffer.wrap(frame, PLEN_OFF, 4).getInt();
        if (plen < RPC_SIG_LEN) return null;
        return Arrays.copyOfRange(frame, HEADER_LEN + RPC_SIG_LEN, HEADER_LEN + plen);
    }

    public boolean verifyRpcSig(byte[] frame) {
        int type = frame[0] & 0xFF;
        if (type != REQ_TYPE && type != RESP_TYPE) return false;
        int plen = ByteBuffer.wrap(frame, PLEN_OFF, 4).getInt();
        if (plen < RPC_SIG_LEN) return false;
        byte[] sig = Arrays.copyOfRange(frame, HEADER_LEN, HEADER_LEN + RPC_SIG_LEN);
        byte[] json = Arrays.copyOfRange(frame, HEADER_LEN + RPC_SIG_LEN, HEADER_LEN + plen);
        int ts = ByteBuffer.wrap(frame, 1, 4).getInt();
        byte[] ext = Arrays.copyOfRange(frame, EXT_OFF, EXT_OFF + EXT_FIELDS_LEN);
        return MessageDigest.isEqual(sig, rpcSig(ts, ext, json));
    }

    public static Parsed parse(byte[] frame) {
        Parsed p = new Parsed();
        p.type = frame[0] & 0xFF;
        p.ts = ByteBuffer.wrap(frame, 1, 4).getInt();
        p.token = Arrays.copyOfRange(frame, 5, 21);
        p.tps = ByteBuffer.wrap(frame, 21, 4).getFloat();
        p.players = ByteBuffer.wrap(frame, 25, 4).getInt();
        p.plen = ByteBuffer.wrap(frame, PLEN_OFF, 4).getInt();
        p.seq = ByteBuffer.wrap(frame, SEQ_OFF, 4).getInt();
        if (frame.length > HEADER_LEN && p.plen > 0) {
            int n = Math.min(p.plen, frame.length - HEADER_LEN);
            p.text = new String(frame, HEADER_LEN, n, StandardCharsets.UTF_8);
        }
        return p;
    }

    // ================= 历史校验接口 =================

    public String verify(int ts, byte[] token, int lastTs) {
        if (Math.abs(System.currentTimeMillis() - ts * 1000L) > REPLAY_WINDOW_MS) {
            return "stale-timestamp";
        }
        byte[] expect;
        try {
            expect = token(ts);
        } catch (Exception e) {
            return "bad-token";
        }
        if (!MessageDigest.isEqual(token, expect)) return "bad-token";
        if (ts <= lastTs) return "replay-seq";
        return null;
    }

    public String verifySeq(int ts, byte[] token, int seq, int lastSeq) {
        if (Math.abs(System.currentTimeMillis() - ts * 1000L) > REPLAY_WINDOW_MS) {
            return "stale-timestamp";
        }
        byte[] expect;
        try {
            expect = token(ts);
        } catch (Exception e) {
            return "bad-token";
        }
        if (!MessageDigest.isEqual(token, expect)) return "bad-token";
        if (seq <= lastSeq) return "replay-seq";
        return null;
    }

    public boolean checkToken(int ts, byte[] token) {
        if (Math.abs(System.currentTimeMillis() - ts * 1000L) > REPLAY_WINDOW_MS) return false;
        byte[] expect;
        try {
            expect = token(ts);
        } catch (Exception e) {
            return false;
        }
        return MessageDigest.isEqual(token, expect);
    }

    /** 一帧的解析结果。 */
    public static class Parsed {
        public int type;
        public int ts;
        public int players;
        public int plen;
        public int seq;
        public byte[] token;
        public float tps;
        public String text = "";
    }

    /** v1 保留区解析结果。 */
    public static class Ext {
        public boolean v1;
        public int ver;
        public int flags;
        public int cap;
        public int epoch;
        public byte[] raw;
    }
}

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
 * 帧格式（大端序，所有帧共用 64B 基础头，载荷变长、无 384 上限）：
 *   offset 0     : type (1B)  0x01 心跳 / 0x02 命令(EXEC/QUERY/FS 指令文本) / 0x03 回执
 *   offset 1-4   : ts (4B)   时间戳
 *   offset 5-20  : token (16B) HMAC-SHA256(secret, ts4) 前16字节
 *   offset 21-24 : tps (4B float)
 *   offset 25-28 : players (4B)
 *   offset 29-32 : plen (4B)  载荷字节数（心跳为 0）
 *   offset 33-36 : seq (4B)   回执帧独立单调序号（防重放）
 *   之后         : 载荷（命令/回执文本，变长）
 *
 * 安全（无 TLS，走 FRP 私有隧道）：
 *   - 固定对称密钥 + HMAC Token + 时间戳 3s 窗口
 *   - 命令/心跳：单调时间戳 ts 判重放
 *   - 回执：独立 seq 单调判重放（回执与心跳同秒时 ts 不递增）
 */
public final class FrameCodec {

    public static final int HEARTBEAT_TYPE = 0x01;
    public static final int COMMAND_TYPE  = 0x02;
    public static final int OUTPUT_TYPE   = 0x03;
    public static final int HEARTBEAT_LEN = 64;
    public static final int HEADER_LEN    = 64;
    public static final int PLEN_OFF      = 29;
    public static final int SEQ_OFF       = 33;
    public static final int MAX_PAYLOAD   = 16 * 1024 * 1024;   // 16MB 载荷上限
    public static final long REPLAY_WINDOW_MS = 3000;

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

    private byte[] token(int ts) {
        try {
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            byte[] h = mac.doFinal(ByteBuffer.allocate(4).putInt(ts).array());
            return Arrays.copyOf(h, 16);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private void fillHeader(byte[] f, int type, int ts, float tps, int players) {
        f[0] = (byte) type;
        System.arraycopy(ByteBuffer.allocate(4).putInt(ts).array(), 0, f, 1, 4);
        System.arraycopy(token(ts), 0, f, 5, 16);
        System.arraycopy(ByteBuffer.allocate(4).putFloat(tps).array(), 0, f, 21, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(players).array(), 0, f, 25, 4);
        // plen/seq 在调用方按载荷覆盖
    }

    /** 心跳帧：固定 64B，无载荷。 */
    public byte[] packHeartbeat(int ts, float tps, int players) {
        byte[] f = new byte[HEARTBEAT_LEN];
        fillHeader(f, HEARTBEAT_TYPE, ts, tps, players);
        return f;
    }

    /** 命令帧 = 64B 头 + 变长指令文本（无 384 上限）。 */
    public byte[] packCommand(int ts, float tps, int players, String cmd) {
        byte[] body = cmd.getBytes(StandardCharsets.UTF_8);
        byte[] f = new byte[HEADER_LEN + body.length];
        fillHeader(f, COMMAND_TYPE, ts, tps, players);
        System.arraycopy(ByteBuffer.allocate(4).putInt(body.length).array(), 0, f, PLEN_OFF, 4);
        System.arraycopy(body, 0, f, HEADER_LEN, body.length);
        return f;
    }

    /** 回执帧 = 64B 头 + 变长输出文本（可远超 384B）。 */
    public byte[] packOutput(int ts, int seq, float tps, int players, String text) {
        byte[] body = text.getBytes(StandardCharsets.UTF_8);
        byte[] f = new byte[HEADER_LEN + body.length];
        fillHeader(f, OUTPUT_TYPE, ts, tps, players);
        System.arraycopy(ByteBuffer.allocate(4).putInt(body.length).array(), 0, f, PLEN_OFF, 4);
        System.arraycopy(ByteBuffer.allocate(4).putInt(seq).array(), 0, f, SEQ_OFF, 4);
        System.arraycopy(body, 0, f, HEADER_LEN, body.length);
        return f;
    }

    /** 解析一帧。 */
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

    /**
     * 校验命令帧：时间戳窗口 + Token + 单调序号。
     * @return null 表示通过；否则为丢弃原因。
     */
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
        if (!MessageDigest.isEqual(token, expect)) {
            return "bad-token";
        }
        if (ts <= lastTs) {
            return "replay-seq";
        }
        return null;
    }

    /**
     * 校验命令帧/回执帧：时间戳窗口 + Token + 独立单调序号 seq。
     * 命令/回执同秒并发时 ts 相同，须用 seq 单调判重放，避免误丢弃。
     * @return null 表示通过；否则为丢弃原因。
     */
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
        if (!MessageDigest.isEqual(token, expect)) {
            return "bad-token";
        }
        if (seq <= lastSeq) {
            return "replay-seq";
        }
        return null;
    }

    /** 仅校验时间戳窗口 + Token（不判序号），供调用方自行实现单调判重。 */
    public boolean checkToken(int ts, byte[] token) {
        if (Math.abs(System.currentTimeMillis() - ts * 1000L) > REPLAY_WINDOW_MS) {
            return false;
        }
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
}
# -*- coding: utf-8 -*-
"""mc_link 私有 TCP 帧协议（与 Java agent 对接）。

基础帧头（0-63）为历史格式，字节级不可改：
  offset 0     : type (1B)
  offset 1-4   : ts (4B, uint32 BE)
  offset 5-20  : token (16B) = HMAC-SHA256(secret, ts4BE)[:16]
  offset 21-24 : tps (4B float)
  offset 25-28 : players (4B)
  offset 29-32 : plen (4B)  载荷字节数
  offset 33-36 : seq (4B)   单调序号（按方向独立）
  offset 37-63 : 保留区（旧实现全 0）
  offset 64..  : 载荷

v1 扩展占用保留区前 20B，旧端全 0 时视为 v0，行为与旧版逐字节一致：
  offset 37-38 : magic (2B) = b"ZC"
  offset 39    : ver (1B)   = 1
  offset 40    : flags (1B) bit0=CAP 有效 / bit1=EPOCH 有效
  offset 41-44 : cap (4B)   能力位图
  offset 45-48 : epoch (4B) 单调版本戳（心跳书签，只带版本不带数据）
  offset 49-56 : ext_sig (8B) = HMAC-SHA256(secret, ts4BE || f[37:49])[:8]
  offset 57-63 : 保留 7B，全 0

帧类型：
  0x01 心跳（固定 64B，无载荷）
  0x02 命令 / 0x03 回执 / 0x04 控制台日志（历史）
  0x05 REQ  / 0x06 RESP（v1 新增，双向对等）

RPC 帧载荷 = sig(16B) + UTF-8 JSON envelope：
  sig = HMAC-SHA256(secret, ts4BE || f[37:49] || json_bytes)[:16]
  帧头 token 仍只覆盖 ts（旧端要校验），故保留区与正文各需独立签名。

设计依据（详见 site/protocol/rpc.md）：
  RFC 9113 §4.1/§5.1.1   未知帧类型忽略、按方向奇偶分配 id
  JSON-RPC 2.0 §4/§5     id 关联、Notification 不回复、错误码区间
  RSocket KEEPALIVE/SETUP 心跳带 position、能力协商、断线增量重传
  Kubernetes watch        resourceVersion + BOOKMARK + 410 Gone 全量回退

这条链路走 frp 的 tcp 隧道，不使用 HTTP：没有 Host、URL、Header，
也就不会触发 http 类型隧道的风控。
"""
import hashlib
import hmac
import struct
import time

HEARTBEAT_TYPE = 0x01
COMMAND_TYPE = 0x02
OUTPUT_TYPE = 0x03
LOG_TYPE = 0x04
REQ_TYPE = 0x05
RESP_TYPE = 0x06

HEARTBEAT_LEN = 64
HEADER_LEN = 64
PLEN_OFF = 29
SEQ_OFF = 33
MAX_PAYLOAD = 16 * 1024 * 1024
REPLAY_WINDOW = 3.0
OFFLINE_THRESHOLD = 6.0
TCP_BUF_SIZE = 8192
LOOP_INTERVAL = 1.0

DEFAULT_TCP_PORT = 25599

# ---- v1 保留区布局 ----
EXT_OFF = 37
MAGIC = b"ZC"
EXT_VER = 1
FLAG_CAP = 0x01
FLAG_EPOCH = 0x02
CAP_RPC = 0x01        # 支持 REQ/RESP
CAP_PASS = 0x02       # 支持 pass 闸门查询
CAP_PUSH = 0x04       # 支持服务端主动推 Notification
CAP_ALL = CAP_RPC | CAP_PASS | CAP_PUSH
EXT_SIG_LEN = 8
RPC_SIG_LEN = 16
EXT_FIELDS_LEN = 12   # magic..epoch (37-48)

# 未知帧类型的载荷上限：按 plen 整帧跳过，避免逐字节扫描污染流（RFC 9113 §4.1）
VARIABLE_TYPES = (COMMAND_TYPE, OUTPUT_TYPE, LOG_TYPE, REQ_TYPE, RESP_TYPE)


def _token(secret: bytes, ts: int) -> bytes:
    return hmac.new(secret, struct.pack(">I", ts), hashlib.sha256).digest()[:16]


def _ext_sig(secret: bytes, ts: int, ext: bytes) -> bytes:
    """保留区签名：ts + 扩展字段，防止 cap/epoch 被篡改。"""
    return hmac.new(secret, struct.pack(">I", ts) + ext, hashlib.sha256).digest()[:EXT_SIG_LEN]


def rpc_sig(secret: bytes, ts: int, ext: bytes, json_bytes: bytes) -> bytes:
    """RPC 正文签名：ts + 扩展字段 + 原始 JSON 字节（不做 JSON 规范化，两端无歧义）。"""
    return hmac.new(secret, struct.pack(">I", ts) + ext + json_bytes,
                    hashlib.sha256).digest()[:RPC_SIG_LEN]


def make_ext(cap: int = 0, epoch: int = 0, flags: int = FLAG_CAP | FLAG_EPOCH) -> bytes:
    """构造 12B 扩展字段；cap=0/flags=0 时返回全 0，等价于旧端。"""
    if not cap and not flags:
        return b"\x00" * EXT_FIELDS_LEN
    return MAGIC + bytes([EXT_VER, flags]) + struct.pack(">II", cap, epoch)


def parse_ext(frame: bytes):
    """解析保留区。返回 (is_v1, ver, flags, cap, epoch, ext_sig_ok)。

    ext_sig_ok 只有在 is_v1 且提供 secret 时才有意义；由 FrameCodec.verify_ext 校验。
    """
    ext = bytes(frame[EXT_OFF:EXT_OFF + EXT_FIELDS_LEN])
    if ext[:2] != MAGIC:
        return False, 0, 0, 0, 0, ext
    ver, flags = ext[2], ext[3]
    cap, epoch = struct.unpack(">II", ext[4:12])
    return True, ver, flags, cap, epoch, ext


class FrameCodec:
    """固定长度二进制帧编解码 + 校验。纯函数，无状态。"""

    # ---- 历史接口（字节不变） ----
    @staticmethod
    def _token(secret: bytes, ts: int) -> bytes:
        return _token(secret, ts)

    @staticmethod
    def _fill_header(frame: bytearray, ftype: int, ts: int, tps: float,
                     players: int, secret: bytes) -> None:
        frame[0] = ftype
        struct.pack_into(">I", frame, 1, ts)
        frame[5:21] = _token(secret, ts)
        struct.pack_into(">f", frame, 21, tps)
        struct.pack_into(">I", frame, 25, players)
        struct.pack_into(">I", frame, PLEN_OFF, 0)
        struct.pack_into(">I", frame, SEQ_OFF, 0)

    @classmethod
    def _apply_ext(cls, frame: bytearray, ts: int, secret: bytes,
                   cap: int, epoch: int, flags: int) -> None:
        ext = make_ext(cap, epoch, flags)
        frame[EXT_OFF:EXT_OFF + EXT_FIELDS_LEN] = ext
        if ext[:2] == MAGIC:
            frame[EXT_OFF + EXT_FIELDS_LEN:EXT_OFF + EXT_FIELDS_LEN + EXT_SIG_LEN] = \
                _ext_sig(secret, ts, ext)

    @classmethod
    def pack_heartbeat(cls, secret: bytes, ts: int, tps: float, players: int,
                       cap: int = 0, epoch: int = 0, flags: int = 0) -> bytes:
        f = bytearray(HEARTBEAT_LEN)
        cls._fill_header(f, HEARTBEAT_TYPE, ts, tps, players, secret)
        if cap or flags:
            cls._apply_ext(f, ts, secret, cap, epoch, flags or (FLAG_CAP | FLAG_EPOCH))
        return bytes(f)

    @classmethod
    def pack_command(cls, secret: bytes, ts: int, seq: int, tps: float,
                     players: int, cmd: str) -> bytes:
        body = cmd.encode("utf-8")
        f = bytearray(HEADER_LEN) + bytearray(body)
        cls._fill_header(f, COMMAND_TYPE, ts, tps, players, secret)
        struct.pack_into(">I", f, PLEN_OFF, len(body))
        struct.pack_into(">I", f, SEQ_OFF, seq)
        return bytes(f)

    @classmethod
    def pack_output(cls, secret: bytes, ts: int, seq: int, tps: float,
                    players: int, text: str) -> bytes:
        body = text.encode("utf-8")
        f = bytearray(HEADER_LEN) + bytearray(body)
        cls._fill_header(f, OUTPUT_TYPE, ts, tps, players, secret)
        struct.pack_into(">I", f, PLEN_OFF, len(body))
        struct.pack_into(">I", f, SEQ_OFF, seq)
        return bytes(f)

    # ---- v1 新增 ----
    @classmethod
    def pack_rpc(cls, secret: bytes, ts: int, seq: int, tps: float, players: int,
                 ftype: int, json_bytes: bytes, cap: int = CAP_ALL,
                 epoch: int = 0) -> bytes:
        f = bytearray(HEADER_LEN)
        cls._fill_header(f, ftype, ts, tps, players, secret)
        cls._apply_ext(f, ts, secret, cap, epoch, FLAG_CAP | FLAG_EPOCH)
        ext = bytes(f[EXT_OFF:EXT_OFF + EXT_FIELDS_LEN])
        sig = rpc_sig(secret, ts, ext, json_bytes)
        payload = sig + json_bytes
        struct.pack_into(">I", f, PLEN_OFF, len(payload))
        struct.pack_into(">I", f, SEQ_OFF, seq)
        return bytes(f) + payload

    @staticmethod
    def parse(frame: bytes):
        ftype = frame[0]
        ts = struct.unpack_from(">I", frame, 1)[0]
        tok = frame[5:21]
        tps = struct.unpack_from(">f", frame, 21)[0]
        players = struct.unpack_from(">I", frame, 25)[0]
        plen = struct.unpack_from(">I", frame, PLEN_OFF)[0]
        seq = struct.unpack_from(">I", frame, SEQ_OFF)[0]
        text = ""
        if len(frame) > HEADER_LEN and plen > 0:
            text = bytes(frame[HEADER_LEN:HEADER_LEN + plen]).decode("utf-8", "replace")
        return ftype, ts, tok, tps, players, plen, seq, text

    @staticmethod
    def rpc_payload(frame: bytes):
        """拆出 (sig, json_bytes)；非 RPC 帧返回 (None, None)。"""
        if frame[0] not in (REQ_TYPE, RESP_TYPE):
            return None, None
        plen = struct.unpack_from(">I", frame, PLEN_OFF)[0]
        body = bytes(frame[HEADER_LEN:HEADER_LEN + plen])
        if len(body) < RPC_SIG_LEN:
            return None, None
        return body[:RPC_SIG_LEN], body[RPC_SIG_LEN:]

    @classmethod
    def verify_ext(cls, secret: bytes, frame: bytes) -> bool:
        """校验 v1 保留区签名；v0 帧返回 True（放行，行为不变）。"""
        is_v1, _ver, _flags, _cap, _epoch, ext = parse_ext(frame)
        if not is_v1:
            return True
        ts = struct.unpack_from(">I", frame, 1)[0]
        return hmac.compare_digest(
            bytes(frame[EXT_OFF + EXT_FIELDS_LEN:EXT_OFF + EXT_FIELDS_LEN + EXT_SIG_LEN]),
            _ext_sig(secret, ts, ext))

    @classmethod
    def verify_rpc(cls, secret: bytes, frame: bytes) -> bool:
        sig, body = cls.rpc_payload(frame)
        if sig is None:
            return False
        ts = struct.unpack_from(">I", frame, 1)[0]
        ext = bytes(frame[EXT_OFF:EXT_OFF + EXT_FIELDS_LEN])
        return hmac.compare_digest(sig, rpc_sig(secret, ts, ext, body))

    # ---- 历史校验接口 ----
    @classmethod
    def verify(cls, secret: bytes, ts: int, tok: bytes, last_ts: int):
        if abs(time.time() - ts) > REPLAY_WINDOW:
            return False, "stale-timestamp"
        if not hmac.compare_digest(tok, _token(secret, ts)):
            return False, "bad-token"
        if ts <= last_ts:
            return False, "replay-seq"
        return True, ""

    @classmethod
    def verify_output(cls, secret: bytes, ts: int, tok: bytes, seq: int, last_seq: int):
        if abs(time.time() - ts) > REPLAY_WINDOW:
            return False, "stale-timestamp"
        if not hmac.compare_digest(tok, _token(secret, ts)):
            return False, "bad-token"
        if seq <= last_seq:
            return False, "replay-seq"
        return True, ""


class Peer:
    """单个远端 agent 连接。"""

    __slots__ = ("addr", "writer", "last_ts", "last_beat", "tps", "players",
                 "offline", "receipt_seq", "cmd_seq", "cap", "ver", "known_epoch",
                 "rpc_tx_seq", "rpc_rx_seq")

    def __init__(self, writer):
        self.addr = writer.get_extra_info("peername")
        self.writer = writer
        self.last_ts = 0
        self.last_beat = time.time()
        self.tps = 0.0
        self.players = 0
        self.offline = False
        self.receipt_seq = 0
        self.cmd_seq = 0
        # v1 能力协商：收到带 magic 的首帧才认为对端支持 RPC
        self.cap = 0
        self.ver = 0
        self.known_epoch = 0
        # 按方向独立单调序号（RFC 9113 §5.1.1 思路：方向隔离，互不抢占）
        self.rpc_tx_seq = 0
        self.rpc_rx_seq = 0

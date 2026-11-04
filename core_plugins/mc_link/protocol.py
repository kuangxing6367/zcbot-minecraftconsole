# -*- coding: utf-8 -*-
"""mc_link 私有 TCP 帧协议（与 Java agent 对接，帧格式不可改）。

帧格式与 plugins/minecraftconsole/main.py 中的 FrameCodec 完全一致：
  - 64B 固定帧头：type(1) + ts(4) + token(16) + tps(4) + players(4) + plen(4) + seq(4)
  - 心跳帧 = 64B 无载荷；命令帧/回执帧 = 64B 头 + 变长 UTF-8 正文
  - token = HMAC-SHA256(secret, big-endian ts)[:16]
  - 校验：3s 时间窗 + 常数时间 token 比对 + 单调序号防重放

这条链路走 frp 的 tcp 隧道，不使用 HTTP：没有 Host、URL、Header，
也就不会触发 http 类型隧道的风控。改动帧格式会同时打断 Java agent，
必须与 mc-agent-java 及旧插件的 FrameCodec 三方同步。
"""
import hashlib
import hmac
import struct
import time

HEARTBEAT_TYPE = 0x01
COMMAND_TYPE = 0x02
OUTPUT_TYPE = 0x03
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


class FrameCodec:
    """固定长度二进制帧编解码 + 校验。纯函数，无状态。"""

    @staticmethod
    def _token(secret: bytes, ts: int) -> bytes:
        return hmac.new(secret, struct.pack(">I", ts), hashlib.sha256).digest()[:16]

    @staticmethod
    def _fill_header(frame: bytearray, ftype: int, ts: int, tps: float,
                     players: int, secret: bytes) -> None:
        frame[0] = ftype
        struct.pack_into(">I", frame, 1, ts)
        frame[5:21] = FrameCodec._token(secret, ts)
        struct.pack_into(">f", frame, 21, tps)
        struct.pack_into(">I", frame, 25, players)
        struct.pack_into(">I", frame, PLEN_OFF, 0)
        struct.pack_into(">I", frame, SEQ_OFF, 0)

    @classmethod
    def pack_heartbeat(cls, secret: bytes, ts: int, tps: float, players: int) -> bytes:
        f = bytearray(HEARTBEAT_LEN)
        cls._fill_header(f, HEARTBEAT_TYPE, ts, tps, players, secret)
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

    @classmethod
    def verify(cls, secret: bytes, ts: int, tok: bytes, last_ts: int):
        if abs(time.time() - ts) > REPLAY_WINDOW:
            return False, "stale-timestamp"
        if not hmac.compare_digest(tok, cls._token(secret, ts)):
            return False, "bad-token"
        if ts <= last_ts:
            return False, "replay-seq"
        return True, ""

    @classmethod
    def verify_output(cls, secret: bytes, ts: int, tok: bytes, seq: int, last_seq: int):
        if abs(time.time() - ts) > REPLAY_WINDOW:
            return False, "stale-timestamp"
        if not hmac.compare_digest(tok, cls._token(secret, ts)):
            return False, "bad-token"
        if seq <= last_seq:
            return False, "replay-seq"
        return True, ""


class Peer:
    """单个远端 agent 连接。"""

    __slots__ = ("addr", "writer", "last_ts", "last_beat", "tps", "players",
                 "offline", "receipt_seq", "cmd_seq")

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

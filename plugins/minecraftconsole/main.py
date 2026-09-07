# -*- coding: utf-8 -*-
"""
minecraftconsole —— ZCBOT 插件：通过 FRP 穿透远程管理 Minecraft 服务器 (Java 版)

架构一句话：本插件 = 控制器端。
  * 远端(MC 服务器侧) 的 agent 主动连入本插件监听的 TCP 端口，周期性推送 64B 心跳。
  * 6 秒未收到心跳 => 判定 MC 服务器离线（容忍网络抖动/丢包）。
  * 管理员的 WebUI 操作 => 统一映射为 user_id=0（系统用户）=> 权限直接判 ID =>
    审计入库(复用 ctx.audit_log / 自建 mc_commands_log 表) =>
    以 384B 命令帧原样转发给 agent，由 agent 通过 RCON/控制台 stdin 执行。

协议要点（极简、无 TLS、固定长度帧，走 FRP 隧道省字节）：
  * 心跳帧 = 64 字节；命令帧 = 384 字节（前 64 字节复用心跳帧头，后 320 字节承载 MC 命令）
  * 安全模型 = 固定对称密钥 + 4B 时间戳(3s 窗口) + HMAC Token + 单调序号防重放
    - 时间戳超出 3s 窗口直接丢弃（防 DDoS 消耗算力）
    - 单调序号(ts<=上次)拦截窗口内重放
  * 并发：8 个并发请求下 CPU 允许 >60%，但所有缓冲均有界，内存峰值卡在阈值内

零阻塞加载：register() 只注册 WebUI 与命令；异步 TCP/HTTP 服务在 on_loaded() 里
通过 create_task 启动，不影响框架启动路径。不修改任何底层框架代码。
"""

import asyncio
import hashlib
import hmac
import json
import os
import struct
import time
import threading
from collections import deque

# 工作流引擎模块：框架会把插件目录加进 sys.path 并预注册短名，绝对导入即可
import workflow

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

__plugin_meta__ = {
    "name": "minecraftconsole",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "通过 FRP 穿透远程管理 Minecraft 服务器：极简私有 TCP 协议 + WebUI 控制台",
    "priority": 100,
}

# ------------------------------------------------------------------ 协议常量
HEARTBEAT_TYPE = 0x01        # 心跳帧首字节
COMMAND_TYPE = 0x02          # 命令帧首字节
OUTPUT_TYPE = 0x03           # 命令回执帧：agent→插件，回传 MC 命令执行输出
HEARTBEAT_LEN = 64           # 心跳帧固定 64B（无载荷）
HEADER_LEN = 64              # 帧头长度：所有帧复用 64B 基础头
PLEN_OFF = 29                # 帧头内载荷长度字段偏移（4B 大端）
SEQ_OFF = 33                 # 帧头内回执单调序号偏移（4B 大端）
MAX_PAYLOAD = 16 * 1024 * 1024   # 变长帧载荷上限（防 DoS 无限占用内存），可按需调大

REPLAY_WINDOW = 3.0          # 时间戳 3 秒内有效，超窗直接丢弃（防重放/DDoS）
OFFLINE_THRESHOLD = 6.0      # 6 秒无心跳 => 判定离线（容忍网络抖动/丢包）
TCP_BUF_SIZE = 8192          # 单次 read 上限
LOOP_INTERVAL = 1.0          # 看门狗巡检间隔（秒）

DEFAULT_TCP_PORT = 25599     # MC agent 协议端口
DEFAULT_API_PORT = 25598     # WebUI 控制台 API 端口（直连 / FRP 转发）
DEFAULT_LOG_RING = 300       # 内存日志环形缓冲上限（低频数据，允许 Swap）


# ------------------------------------------------------------------ 帧编解码
class FrameCodec:
    """固定长度二进制帧编解码 + 校验。纯函数，无状态，适合热路径复用预分配缓冲。"""

    @staticmethod
    def _token(secret: bytes, ts: int) -> bytes:
        """基于固定对称密钥 + 时间戳的 16 字节 HMAC Token。"""
        return hmac.new(secret, struct.pack(">I", ts), hashlib.sha256).digest()[:16]

    @staticmethod
    def _fill_header(frame: bytearray, ftype: int, ts: int, tps: float, players: int, secret: bytes) -> None:
        """填充 64B 帧头：type(1)+ts(4)+token(16)+tps(4)+players(4)+plen(4)+seq(4)。"""
        frame[0] = ftype
        struct.pack_into(">I", frame, 1, ts)
        frame[5:21] = FrameCodec._token(secret, ts)
        struct.pack_into(">f", frame, 21, tps)
        struct.pack_into(">I", frame, 25, players)
        struct.pack_into(">I", frame, PLEN_OFF, 0)   # 载荷长度，pack_* 内覆盖
        struct.pack_into(">I", frame, SEQ_OFF, 0)    # 回执序号，pack_output 内覆盖

    @classmethod
    def pack_heartbeat(cls, secret: bytes, ts: int, tps: float, players: int) -> bytes:
        f = bytearray(HEARTBEAT_LEN)
        cls._fill_header(f, HEARTBEAT_TYPE, ts, tps, players, secret)
        return bytes(f)

    @classmethod
    def pack_command(cls, secret: bytes, ts: int, seq: int, tps: float, players: int, cmd: str) -> bytes:
        """命令帧 = 64B 帧头 + 变长命令正文。seq 为命令独立单调序号（防同秒并发重放）。"""
        body = cmd.encode("utf-8")
        f = bytearray(HEADER_LEN) + bytearray(body)
        cls._fill_header(f, COMMAND_TYPE, ts, tps, players, secret)
        struct.pack_into(">I", f, PLEN_OFF, len(body))
        struct.pack_into(">I", f, SEQ_OFF, seq)
        return bytes(f)

    @classmethod
    def pack_output(cls, secret: bytes, ts: int, seq: int, tps: float, players: int, text: str) -> bytes:
        """回执帧 = 64B 帧头 + 变长输出文本（可远超 384B，如查看文件内容）。"""
        body = text.encode("utf-8")
        f = bytearray(HEADER_LEN) + bytearray(body)
        cls._fill_header(f, OUTPUT_TYPE, ts, tps, players, secret)
        struct.pack_into(">I", f, PLEN_OFF, len(body))
        struct.pack_into(">I", f, SEQ_OFF, seq)
        return bytes(f)

    @staticmethod
    def parse(frame: bytes):
        """解析一帧，返回 (ftype, ts, token, tps, players, plen, seq, text)。
        text 为命令/回执文本；心跳 plen=0、text 为空。"""
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
        """时间戳窗口 + Token + 单调序号三重校验。返回 (ok, reason)。"""
        if abs(time.time() - ts) > REPLAY_WINDOW:
            return False, "stale-timestamp"   # 过期/重放包：直接丢弃，不消耗解析算力
        if not hmac.compare_digest(tok, cls._token(secret, ts)):
            return False, "bad-token"
        if ts <= last_ts:
            return False, "replay-seq"        # 单调序号：窗口内重放也被拦
        return True, ""

    @classmethod
    def verify_output(cls, secret: bytes, ts: int, tok: bytes, seq: int, last_seq: int):
        """回执帧校验：时间戳窗口 + Token + 独立单调序号。
        回执与心跳同秒时 ts 不递增，故用 seq 单调判重放（避免同秒回执被丢弃）。"""
        if abs(time.time() - ts) > REPLAY_WINDOW:
            return False, "stale-timestamp"
        if not hmac.compare_digest(tok, cls._token(secret, ts)):
            return False, "bad-token"
        if seq <= last_seq:
            return False, "replay-seq"        # 回执独立单调序号，窗口内重放被拦
        return True, ""


# ------------------------------------------------------------------ 连接对端
class _Peer:
    """单个远端 agent 连接。__slots__ 压缩内存占用（嵌入式级优化）。"""
    __slots__ = ("addr", "writer", "last_ts", "last_beat", "tps", "players", "offline", "receipt_seq", "cmd_seq")

    def __init__(self, writer):
        self.addr = writer.get_extra_info("peername")
        self.writer = writer
        self.last_ts = 0
        self.last_beat = time.time()
        self.tps = 0.0
        self.players = 0
        self.offline = False
        self.receipt_seq = 0      # 命令回执帧独立单调序号（防重放）
        self.cmd_seq = 0          # 下发命令帧独立单调序号（防同秒并发重放）


class MCConsole:
    """插件主体：TCP 协议服务 + WebUI 控制台 API + 看门狗 + 审计。"""

    def __init__(self, ctx):
        self.ctx = ctx

        # 防御性读取：配置可能未初始化(返回 None)，一律回退默认字面量
        def _cfg(key, default):
            v = ctx.get_config(key, default)
            return default if v is None else v

        # 固定对称密钥：优先取配置，否则环境变量，否则默认（生产务必更换）
        self.secret = str(_cfg("secret", os.environ.get("MC_SECRET", "zcboot-mc-please-change-me"))).encode("utf-8")
        self.tcp_host = str(_cfg("tcp_host", "0.0.0.0"))
        self.tcp_port = int(_cfg("tcp_port", DEFAULT_TCP_PORT))
        self.api_host = str(_cfg("api_host", "0.0.0.0"))
        self.api_port = int(_cfg("api_port", DEFAULT_API_PORT))
        self.web_token = str(_cfg("web_token", "")).strip()   # 可选的 WebUI API 令牌
        # 允许执行命令的 user_id：0=WebUI 系统用户；超管由 ctx.is_superuser 判定
        self.extra_admin_ids = {int(x) for x in str(_cfg("admin_ids", "")).split(",") if x.strip().isdigit()}

        self._peers = {}                 # addr -> _Peer
        self._pending = {}               # cmd_seq -> asyncio.Future（回执同步关联）
        self._status = {"online": False, "tps": 0.0, "players": 0, "last_beat": None}
        self._log_ring = deque(maxlen=int(_cfg("log_ring", DEFAULT_LOG_RING)))
        self._watchers = set()           # SSE 订阅者（asyncio.Queue）
        self._loop = None
        self._started = False
        # Vue 工作流引擎：register() 中实例化并赋值（供 run_command / 消息钩子使用）
        self.workflow_engine = None

    # ================================================================ 生命周期
    async def start(self):
        if self._started:
            return
        self._started = True
        self._loop = asyncio.get_running_loop()
        await self._init_db()
        await self._init_workflow_perms()

        # 1) MC agent 协议 TCP 服务
        tcp_server = await asyncio.start_server(
            self._on_peer_connected, self.tcp_host, self.tcp_port, limit=TCP_BUF_SIZE)
        self.ctx.log(f"MC TCP 协议监听 {self.tcp_host}:{self.tcp_port}")

        # 2) WebUI 控制台 HTTP 服务（自带 CORS，供框架内嵌 iframe / 直连）
        api_server = await asyncio.start_server(
            self._on_http, self.api_host, self.api_port, limit=TCP_BUF_SIZE)
        self.ctx.log(f"MC WebUI 控制台 {self.api_host}:{self.api_port}")

        # 3) 看门狗
        self._loop.create_task(self._watchdog_loop())
        self._push_log("[sys] minecraftconsole 已启动，等待 MC agent 连接...")

    async def stop(self):
        self._started = False
        loop = self._loop
        if loop is None:
            return
        # 可能在非事件循环线程调用（内存看门狗卸载线程），线程安全关停
        for coro in (self._close_all_peers(),):
            if loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                try:
                    fut.result(timeout=2)
                except Exception:
                    pass
            else:
                await coro

    async def _close_all_peers(self):
        for p in list(self._peers.values()):
            try:
                p.writer.close()
            except Exception:
                pass
        self._peers.clear()

    # ================================================================ 数据库与审计
    async def _init_db(self):
        """利用 ctx.create_table() 自动建表（内部做 SQLite/MySQL 方言翻译）。"""
        try:
            ctx = self.ctx
            # create_table 接收完整 DDL 字符串，框架自动适配 SQLite/MySQL
            ctx.create_table(
                "CREATE TABLE IF NOT EXISTS mc_commands_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "user_id INTEGER NOT NULL DEFAULT 0,"
                "command TEXT NOT NULL,"
                "result TEXT DEFAULT '',"
                "created_at INTEGER NOT NULL"
                ") "
            )
            # 心跳数据表：记录 TPS 和玩家数，环形缓冲 1000 条
            ctx.create_table(
                "CREATE TABLE IF NOT EXISTS mc_heartbeat_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "tps REAL NOT NULL DEFAULT 0,"
                "players INTEGER NOT NULL DEFAULT 0,"
                "created_at INTEGER NOT NULL"
                ") "
            )
            # QQ ↔ MC 玩家绑定表（按群隔离）：同一群里一个 QQ 绑定一个 MC 玩家
            ctx.create_table(
                "CREATE TABLE IF NOT EXISTS mc_whitelist ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "group_id VARCHAR(32) NOT NULL DEFAULT '0',"
                "qq INTEGER NOT NULL,"
                "player VARCHAR(64) NOT NULL,"
                "bound_by INTEGER NOT NULL DEFAULT 0,"
                "created_at INTEGER NOT NULL,"
                "UNIQUE(group_id, qq)"
                ") "
            )
        except Exception as e:
            self.ctx.log(f"建表失败: {e}")

    async def _init_workflow_perms(self):
        """工作流权限默认开放：确保默认权限组 default 携带 zcbot.wf.* 通配节点。

        这样新建工作流默认对全员开放；如需收紧某个工作流，可在后台「权限管理」
        移除该通配、或为用户/组设置显式节点（如 zcbot.wf.<wf_id>，含否决值 0）。
        聊天触发工作流时按 (group/bot/msgtype) 上下文做 has_perm 判定。"""
        try:
            ctx = self.ctx
            row = await ctx.db_query_one_async(
                "SELECT name FROM perm_groups WHERE name='default'")
            if not row:
                await ctx.db_execute_async(
                    "INSERT INTO perm_groups "
                    "(name, display_name, weight, is_default, created_at) "
                    "VALUES ('default','默认组',0,1,%s)", (int(time.time()),))
            node = await ctx.db_query_one_async(
                "SELECT id FROM perm_group_nodes "
                "WHERE group_name='default' AND node='zcbot.wf.*'")
            if not node:
                await ctx.db_execute_async(
                    "INSERT INTO perm_group_nodes "
                    "(group_name, node, value, created_at) "
                    "VALUES ('default','zcbot.wf.*',1,%s)", (int(time.time()),))
            self.ctx.log("[minecraftconsole] 工作流默认权限(zcbot.wf.*)已就绪")
        except Exception as e:
            self.ctx.log(f"初始化工作流权限失败: {e}")

    async def _audit_command(self, user_id: int, command: str, ok: bool):
        """审计：复用 ctx.audit_log(→audit_logs，admin_id 恒为 0) + 写入 mc_commands_log。"""
        try:
            self.ctx.audit_log(
                action="mc_command",
                target_type="minecraft",
                target_name="server",
                detail={"user_id": user_id, "command": command},
                result="success" if ok else "failure",
            )
        except Exception as e:
            self.ctx.log(f"audit_log 失败: {e}")
        try:
            await self.ctx.db_execute_async(
                "INSERT INTO mc_commands_log (user_id, command, created_at) VALUES (%s, %s, %s)",
                (user_id, command, int(time.time())),
            )
        except Exception as e:
            self.ctx.log(f"写 mc_commands_log 失败: {e}")

    async def _record_heartbeat(self, tps: float, players: int):
        """记录心跳数据到数据库，保持最近1000条。"""
        try:
            await self.ctx.db_execute_async(
                "INSERT INTO mc_heartbeat_log (tps, players, created_at) VALUES (%s, %s, %s)",
                (round(tps, 2), players, int(time.time())),
            )
            # 删除超过1000条的旧数据
            await self.ctx.db_execute_async(
                "DELETE FROM mc_heartbeat_log WHERE id NOT IN ("
                "SELECT id FROM mc_heartbeat_log ORDER BY id DESC LIMIT 1000"
                ")"
            )
        except Exception as e:
            self.ctx.log(f"写 mc_heartbeat_log 失败: {e}")

    # ================================================================ TCP 协议服务
    async def _on_peer_connected(self, reader, writer):
        peer = _Peer(writer)
        self._peers[peer.addr] = peer
        self._push_log(f"[conn] agent {peer.addr} 接入")

        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(TCP_BUF_SIZE)
                if not chunk:                       # 对端关闭
                    break
                buf.extend(chunk)
                # ---------- 粘包 / 拆包处理：变长帧，按帧头 plen 切分 ----------
                while len(buf) >= 1:
                    ftype = buf[0]
                    if ftype == HEARTBEAT_TYPE:
                        total = HEADER_LEN
                    elif ftype in (COMMAND_TYPE, OUTPUT_TYPE):
                        if len(buf) < HEADER_LEN:
                            break                    # 帧头未到齐
                        plen = struct.unpack_from(">I", buf, PLEN_OFF)[0]
                        if plen > MAX_PAYLOAD:       # 防超大载荷占用内存
                            self._push_log(f"[drop] 载荷超限 {plen}B")
                            del buf[0]
                            continue
                        total = HEADER_LEN + plen     # 变长帧总长
                    else:                            # 流失步：跳过 1 字节重新同步
                        del buf[0]
                        continue
                    if len(buf) < total:
                        break                        # 拆包：数据未到齐，等待更多
                    frame = bytes(buf[:total])       # 粘包：取一帧
                    del buf[:total]
                    await self._dispatch(peer, frame)
        finally:
            self._peers.pop(peer.addr, None)
            try:
                writer.close()
            except Exception:
                pass
            self._push_log(f"[conn] agent {peer.addr} 离开")

    async def _dispatch(self, peer: _Peer, frame: bytes):
        """校验并处理单帧。低开销路径，不分配多余对象。"""
        ftype, ts, tok, tps, players, plen, seq, text = FrameCodec.parse(frame)

        # 命令回执帧：按 seq 关联到待决命令的 Future（工作流引擎 await 真实输出）。
        # 回执 seq = 对应命令帧的 cmd_seq；回执可能乱序，故只做窗口+token 校验，
        # 不用单调序号（单调由下发侧 cmd_seq 保证，回执重放由 ts 窗口拦截）。
        if ftype == OUTPUT_TYPE:
            if abs(time.time() - ts) > REPLAY_WINDOW:
                self._push_log(f"[drop] {peer.addr} output:stale-timestamp")
                return
            if not hmac.compare_digest(tok, FrameCodec._token(self.secret, ts)):
                self._push_log(f"[drop] {peer.addr} output:bad-token")
                return
            for line in text.splitlines() or [text]:
                self._push_log(f"[mc-out] {line}")
            fut = self._pending.pop(seq, None)
            if fut is not None and not fut.done():
                fut.set_result(text)
            await self.ctx.aemit("mc.agent_output", {"addr": str(peer.addr), "command": text})
            return

        # 心跳帧：时间戳窗口 + token + ts 单调（每秒不同，足够防重放）
        ok, reason = FrameCodec.verify(self.secret, ts, tok, peer.last_ts)
        if not ok:
            self._push_log(f"[drop] {peer.addr} {reason}")
            return
        peer.last_ts = ts
        peer.last_beat = time.time()
        peer.offline = False
        peer.tps = tps
        peer.players = players
        self._status.update({"online": True, "tps": round(tps, 2), "players": players, "last_beat": int(peer.last_beat)})
        
        # 记录心跳数据到数据库
        if ftype == HEARTBEAT_TYPE:
            await self._record_heartbeat(tps, players)

        # 命令帧：用独立 seq 单调（同秒并发命令 ts 相同，seq 保证不误判重放）
        if ftype == COMMAND_TYPE and text:
            cok, creason = FrameCodec.verify_output(self.secret, ts, tok, seq, peer.cmd_seq)
            if not cok:
                self._push_log(f"[drop] {peer.addr} cmd:{creason}")
                return
            peer.cmd_seq = seq
            peer.last_beat = time.time()
            self._push_log(f"[MC] {text}")
            await self.ctx.aemit("mc.agent_output", {"addr": str(peer.addr), "command": text})

    # ================================================================ 命令下发
    async def run_command(self, command: str, user_id: int = 0) -> dict:
        """执行 MC 命令。权限直接判 ID：user_id==0(WebUI 系统) / 超管 / 配置的 admin_ids。

        工作流接管：在真正下发前先尝试 workflow_engine.match(command)，
        命中启用的工作流则走工作流引擎（代替默认 EXEC/QUERY/FS 分发），
        返回 {"ok": True, "workflow": name}，不破坏原有 WebUI run_command 语义。
        """
        command = (command or "").strip().lstrip("/")
        if not command:
            return {"ok": False, "error": "empty-command"}
        # 工作流是自动化规则而非用户级命令，故放在权限判定之前（内网工具，见 workflow.py）
        engine = self.workflow_engine
        if engine is not None:
            wfs = engine.match_all(command)
            if wfs:
                names, results = [], []
                for wf in wfs:
                    name = wf.get("name") or wf.get("id", "workflow")
                    final = await engine.run(wf, command, self.run_instruction)
                    names.append(name)
                    results.append(final)
                    self._push_log(f"[wf] 工作流 [{name}] 接管指令，结果: {final}")
                combined = "\n".join(filter(None, results))
                return {"ok": True, "workflow": names[0] if len(names) == 1 else names,
                        "workflow_count": len(names), "result": combined}
        if not self._authorized(user_id):
            return {"ok": False, "error": "permission-denied"}
        if user_id != 0 and not self.ctx.is_superuser(user_id) and user_id not in self.extra_admin_ids:
            return {"ok": False, "error": "permission-denied"}

        target = next((p for p in self._peers.values() if not p.offline), None)
        if target is None:
            await self._audit_command(user_id, command, False)
            return {"ok": False, "error": "mc-offline"}

        try:
            target.cmd_seq += 1   # 自增序号；agent 端以 (ts,seq) 二元单调判重，重启后 ts 更大即放行
            frame = FrameCodec.pack_command(self.secret, int(time.time()), target.cmd_seq,
                                            target.tps, target.players, command)
            target.writer.write(frame)
            await target.writer.drain()
            await self._audit_command(user_id, command, True)
            self._push_log(f"[admin:{user_id}] /{command}")
            return {"ok": True}
        except Exception as e:
            await self._audit_command(user_id, command, False)
            return {"ok": False, "error": f"send-failed:{e}"}

    def _authorized(self, user_id: int) -> bool:
        return user_id == 0 or self.ctx.is_superuser(user_id) or user_id in self.extra_admin_ids

    async def run_instruction(self, instruction: str, timeout: float = 8.0) -> str:
        """工作流引擎的 send_fn：下发指令帧并【同步等待该指令的回执】，返回真实输出。

        instruction 支持 EXEC|... / QUERY|... / FS|... / 普通命令，均原样作为
        命令帧正文下发（前缀语义由远端 agent 解释，本插件不解析）。
        通过 cmd_seq 登记 pending Future，_dispatch 收到关联回执时 complete，
        从而让工作流的 condition 节点能基于真实执行输出做分支。
        """
        instruction = (instruction or "").strip()
        if not instruction:
            return ""
        target = next((p for p in self._peers.values() if not p.offline), None)
        if target is None:
            self._push_log(f"[wf] MC 离线，指令未下发: {instruction}")
            return f"[mc-offline] {instruction}"
        target.cmd_seq += 1   # 自增序号；agent 端以 (ts,seq) 二元单调判重，重启后 ts 更大即放行
        seq = target.cmd_seq
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[seq] = fut
        try:
            frame = FrameCodec.pack_command(self.secret, int(time.time()), seq,
                                            target.tps, target.players, instruction)
            target.writer.write(frame)
            await target.writer.drain()
            self._push_log(f"[wf-send] {instruction}")
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except asyncio.TimeoutError:
                self._push_log(f"[wf] 指令超时无回执: {instruction}")
                return f"[timeout] {instruction}"
        except Exception as e:
            self._push_log(f"[wf-send-err] {instruction} → {e}")
            return f"[send-failed] {instruction} ({e})"
        finally:
            self._pending.pop(seq, None)

    # ================================================================ WebUI 控制台 HTTP API
    async def _on_http(self, reader, writer):
        """极简 asyncio HTTP 服务：GET /、GET /api/status、POST /api/cmd、GET /api/stream(SSE)。"""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line:
                writer.close()
                return
            parts = request_line.decode("latin-1", "replace").strip().split()
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0], parts[1]
            headers, body = {}, b""
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1", "replace").partition(":")
                headers[k.strip().lower()] = v.strip()
            if "content-length" in headers:
                body = await reader.readexactly(int(headers["content-length"]))

            route = path.split("?", 1)[0]

            # CORS 预检：iframe 内嵌(跨源)时浏览器会先发 OPTIONS，必须放行且不做鉴权。
            # 否则 POST /api/cmd 被拦截，前端表现为「请求失败」。
            if method == "OPTIONS":
                head = ("HTTP/1.1 204 No Content\r\n"
                        + self._cors_headers()
                        + "Access-Control-Max-Age: 600\r\n"
                        + "Access-Control-Allow-Headers: Content-Type, X-MC-Token\r\n\r\n")
                writer.write(head.encode("latin-1"))
                await writer.drain()
                return

            # 安全：WebUI API 令牌校验（可选；空 token 则不校验）
            if self.web_token and not self._check_token(headers, path):
                await self._http_json(writer, {"ok": False, "error": "unauthorized"}, 401)
                return

            if method == "GET" and route == "/":
                await self._http_file(writer, "index.html")
            elif method == "GET" and route == "/api/status":
                await self._http_json(writer, {"ok": True, "data": self._status})
            elif method == "POST" and route == "/api/cmd":
                data = {}
                try:
                    data = json.loads(body.decode("utf-8", "replace"))
                except Exception:
                    pass
                # WebUI 请求统一映射为 user_id=0（系统用户）
                result = await self.run_command(str(data.get("command", "")), user_id=0)
                await self._http_json(writer, result)
            elif method == "GET" and route == "/api/stream":
                await self._http_sse(writer)
            elif method == "GET" and route == "/api/heartbeat":
                await self._http_heartbeat_history(writer)
            else:
                await self._http_json(writer, {"ok": False, "error": "not-found"}, 404)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _check_token(self, headers: dict, path: str) -> bool:
        """令牌校验：支持 X-MC-Token 头或 ?token= 查询参数（hmac 恒定时间比较）。"""
        q_token = ""
        if "?" in path:
            for kv in path.split("?", 1)[1].split("&"):
                k, _, v = kv.partition("=")
                if k == "token":
                    q_token = v
        tok = headers.get("x-mc-token", "") or q_token
        return hmac.compare_digest(tok, self.web_token)

    def _cors_headers(self) -> str:
        return ("Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: GET,POST,OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type,X-MC-Token\r\n")

    async def _http_json(self, writer, obj: dict, status: int = 200):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        head = (f"HTTP/1.1 {status} OK\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                + self._cors_headers() +
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + payload)
        await writer.drain()

    async def _http_file(self, writer, filename: str):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        fpath = os.path.join(web_dir, filename)
        if not os.path.isfile(fpath):
            await self._http_json(writer, {"ok": False, "error": "file-missing"}, 404)
            return
        with open(fpath, "rb") as f:
            data = f.read()
        ctype = "text/html; charset=utf-8" if filename.endswith(".html") else "application/octet-stream"
        head = (f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
                + self._cors_headers() +
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + data)
        await writer.drain()

    async def _http_heartbeat_history(self, writer):
        """返回最近1000条心跳历史记录（TPS、玩家数、时间戳）。"""
        try:
            rows = await self.ctx.db_query_async(
                "SELECT tps, players, created_at FROM mc_heartbeat_log ORDER BY id DESC LIMIT 1000"
            )
            data = [{
                "tps": float(r["tps"] or 0),
                "players": int(r["players"] or 0),
                "time": int(r["created_at"] or 0),
            } for r in reversed(rows)]
            await self._http_json(writer, {"ok": True, "data": data})
        except Exception as e:
            self.ctx.log(f"查询心跳历史失败: {e}")
            await self._http_json(writer, {"ok": False, "error": "query-failed"}, 500)

    async def _http_sse(self, writer):
        """SSE 实时日志流：先回放最近日志，再逐行推送新日志。"""
        head = ("HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n" + self._cors_headers() + "\r\n")
        writer.write(head.encode("latin-1"))
        await writer.drain()
        q = asyncio.Queue(maxsize=64)
        self._watchers.add(q)
        try:
            with threading.Lock():
                history = list(self._log_ring)[-100:]
            for line in history:
                writer.write(f"data: {line}\n\n".encode("utf-8"))
            await writer.drain()
            while True:
                line = await q.get()
                writer.write(f"data: {line}\n\n".encode("utf-8"))
                await writer.drain()
        except Exception:
            pass
        finally:
            self._watchers.discard(q)

    # ================================================================ 看门狗 / 日志
    async def _watchdog_loop(self):
        """低频巡检：3s 无心跳 => 离线；附加内存观测（进程 RSS），超限告警。"""
        proc = psutil.Process() if psutil else None
        limit_mb = getattr(self.ctx, "_framework", None) and \
            getattr(self.ctx._framework, "_memory_limit_mb", 120) or 120
        while True:
            try:
                await asyncio.sleep(LOOP_INTERVAL)
                changed = False
                for p in list(self._peers.values()):
                    if time.time() - p.last_beat > OFFLINE_THRESHOLD and not p.offline:
                        p.offline = True
                        changed = True
                if changed or (not self._peers and self._status.get("online")):
                    self._status.update({"online": False})
                    self._push_log("[warn] 3s 未收到心跳，判定 MC 服务器离线")
                # 内存观测：超过看门狗阈值时告警（不卸载自己，仅提示）
                if proc is not None:
                    rss = proc.memory_info().rss / 1024 / 1024
                    if rss > limit_mb:
                        self.ctx.log(f"[内存] RSS {rss:.1f}MB 超阈值 {limit_mb}MB")
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _push_log(self, line: str):
        stamp = time.strftime("%H:%M:%S")
        with threading.Lock():
            self._log_ring.append(f"[{stamp}] {line}")
            snap = self._log_ring[-1]
        for q in list(self._watchers):
            try:
                if not q.full():
                    q.put_nowait(snap)
            except Exception:
                self._watchers.discard(q)

    # ================================================================ 消息命令（可选）
    def on_chat_command(self, event, match):
        """聊天命令 /mcstatus：查询并回复。

        同步实现：框架以 to_thread 在子线程调用 sync handler（无事件循环），
        故用 ctx.send_msg（框架同步桥接到主循环，线程安全）而非 asend_msg。
        """
        # 兼容框架的 Event 对象（属性访问）与 dict 两种来源
        def _field(name, default):
            if hasattr(event, name):
                return getattr(event, name, default)
            return event.get(name, default) if isinstance(event, dict) else default

        if self._status.get("online"):
            msg = (f"MC 在线 | TPS={self._status['tps']} | 玩家={self._status['players']}"
                   f" | 最近心跳 {time.strftime('%H:%M:%S', time.localtime(self._status['last_beat']))}")
        else:
            msg = "MC 服务器离线（3s 无心跳）"
        try:
            self.ctx.send_msg(user_id=_field("user_id", 0),
                              group_id=_field("group_id", None), message=msg)
        except Exception:
            pass

    def on_mcon_reload(self, event, match):
        """/mcon reload —— 重读磁盘 workflows.json（同步 handler，线程安全回复）。"""
        def _field(name, default):
            if hasattr(event, name):
                return getattr(event, name, default)
            return event.get(name, default) if isinstance(event, dict) else default

        engine = self.workflow_engine
        if engine is None:
            reply = "工作流引擎未就绪，无法重载"
        else:
            try:
                engine.load()
                n = len(engine.all())
                reply = f"✅ 已重新加载 {n} 个工作流（来自 {engine.path}）"
            except Exception as e:
                self.ctx.log(f"/mcon reload 失败: {e}", level="error")
                reply = f"❌ 重载失败: {e}"
        try:
            self.ctx.send_msg(user_id=_field("user_id", 0),
                              group_id=_field("group_id", None), message=reply)
        except Exception:
            pass

    # ================================================================ 暴露给 WebUI / 其它
    def status(self) -> dict:
        return dict(self._status)


# ------------------------------------------------------------------ ZCBOT 插件入口
_console = None   # 模块级引用，供 on_unload 使用


def _mcstatus_cmd(event, match):
    """模块级命令处理器（框架按函数名在模块上查找 handler，必须是模块级函数）。"""
    if _console is not None:
        _console.on_chat_command(event, match)


def _mcon_reload_cmd(event, match):
    """/mcon reload —— 重新从磁盘加载工作流（外部改动即时生效，无需重启）。"""
    if _console is not None:
        _console.on_mcon_reload(event, match)


def register(ctx):
    """框架要求的入口：注册 WebUI 页面 + 聊天命令 + 工作流引擎。零阻塞。"""
    global _console
    _console = MCConsole(ctx)
    ctx._mc_console = _console  # 暴露给其它插件 / WebUI 扩展调用

    # 注入 WebUI 管理面板（静态资源走 plugins/minecraftconsole/web/）
    ctx.webui(title="MC控制台", entry="index.html", icon="🎮", order=10)
    # Vue 工作流编排面板入口（接管后根路由 /minecraftconsole/ 即此页）
    ctx.webui(title="MC工作流", entry="workflow.html", icon="🔀", order=20)

    # 工作流引擎：实例化（加载 workflows.json）+ 注册 /api/mc/workflow 配置 API
    engine = workflow.WorkflowEngine(ctx)
    _console.workflow_engine = engine
    ctx._mc_workflow = engine  # 暴露给其它插件 / WebUI 扩展调用
    engine.register_routes()

    # 登录授权（/api/mc/auth/*）与文件管理（/api/mc/fs）端点，复用框架 admin_users 表
    workflow.register_auth_routes(ctx)
    workflow.register_fs_routes(ctx)
    # 本地文件服务（上传/下载/编辑/新建/重命名/删除，直接操作 mc-server 目录）
    workflow.register_local_fs_routes(ctx)

    # 接管 Web 前端（用户选定：8080 打开默认进 MC 控制台壳页）。
    # 框架管理界面未被藏起：override 只改根路由，框架 SPA 仍可通过 /index.html 直达，
    # 壳页「框架后台」即 iframe /index.html。若要回到「8080=框架面板」，注释掉下一行即可。
    workflow.takeover(ctx)

    # 原始消息钩子：聊天或其它入口收到的指令文本先过工作流匹配；
    # 命中启用的工作流则接管消息（返回 True 让框架跳过后续处理），走引擎执行。
    async def _wf_raw_handler(raw_event, bot_name):
        text = _extract_message_text(raw_event)
        if not text:
            return None
        # 一条消息可同时命中多个启用工作流 → 全部执行（多流程图并发）
        wfs = engine.match_all(text)
        if not wfs:
            return None

        # 取出用户 / 群 / 消息类型，构造权限上下文
        uid = _ev_field(raw_event, "user_id", 0)
        gid = _ev_field(raw_event, "group_id", None)
        msgtype = _ev_field(raw_event, "message_type", "group") or "group"
        context = {"msgtype": str(msgtype)}
        if gid:
            context["group"] = str(gid)
        if bot_name:
            context["bot"] = str(bot_name)

        def _name(wf):
            return wf.get("name") or wf.get("id", "workflow")

        async def _deliver(wf, final):
            """按单个工作流的「发送结果」设置投递 final；未开启则回触发人/所在群。"""
            if not final:
                return
            send_cfg = wf.get("send") or {}
            if send_cfg.get("enabled"):
                to = str(send_cfg.get("to") or "trigger")
                tid = str(send_cfg.get("target_id") or "").strip()
                if to == "qq" and tid.isdigit():
                    await _console.ctx.asend_msg(user_id=int(tid), message=final)
                    return
                if to == "group" and tid.isdigit():
                    await _console.ctx.asend_msg(group_id=int(tid), message=final)
                    return
            await _console.ctx.asend_msg(user_id=uid, group_id=gid, message=final)

        executed = []      # (wf, final)
        warnings = []      # 权限/绑定被拦的说明
        for wf in wfs:
            wf_id = str(wf.get("id", _name(wf)))
            # 权限判定：zcbot.wf.<wf_id>；默认组带通配全放行，超管恒放行
            node = "zcbot.wf." + wf_id
            if uid and not _console.ctx.is_superuser(uid) \
                    and not _console.ctx.has_perm(uid, node, context=context):
                warnings.append(f"「{_name(wf)}」需要权限节点 {node}，已跳过")
                continue
            player = _lookup_player(_console.ctx, gid, uid) if uid else None
            if _wf_needs_player(text, wf) and not player:
                warnings.append(f"「{_name(wf)}」需要先 /绑定玩家 才能执行")
                continue
            try:
                variables = {"player": player} if player else {}
                final = await engine.run(wf, text, _console.run_instruction, variables=variables)
            except Exception as e:
                _console._push_log(f"[wf] 工作流 [{_name(wf)}] 执行异常: {e}")
                continue
            _console._push_log(f"[wf] 聊天触发工作流 [{_name(wf)}]: {final}")
            if final:
                executed.append((wf, final))

        if executed:
            if len(executed) == 1 and not warnings:
                # 单个命中：完全按其「发送结果」设置投递
                await _deliver(executed[0][0], executed[0][1])
            else:
                # 多个命中：结果合并回触发人；各工作流若开了"指定 QQ/群"则再各自投递一份
                parts = []
                for wf, final in executed:
                    if len(executed) > 1:
                        parts.append(f"【{_name(wf)}】\n{final}")
                    else:
                        parts.append(final)
                await _console.ctx.asend_msg(user_id=uid, group_id=gid,
                                             message="\n\n".join(parts))
                for wf, final in executed:
                    send_cfg = wf.get("send") or {}
                    if send_cfg.get("enabled"):
                        to = str(send_cfg.get("to") or "trigger")
                        tid = str(send_cfg.get("target_id") or "").strip()
                        if to == "qq" and tid.isdigit():
                            await _console.ctx.asend_msg(user_id=int(tid), message=final)
                        elif to == "group" and tid.isdigit():
                            await _console.ctx.asend_msg(group_id=int(tid), message=final)

        if warnings:
            await _console.ctx.asend_msg(user_id=uid, group_id=gid,
                                         message="部分工作流未执行：\n" + "\n".join(warnings))
        return True  # 已接管该消息，框架不再走命令匹配/关键词兜底

    ctx.on_raw_message(_wf_raw_handler)

    # 可选聊天命令：查询服务器状态（require_admin 走框架权限中间件）
    ctx.command(
        r"^/?mc(status|state)\b", _mcstatus_cmd,
        priority=40, require_admin=True, description="查询 Minecraft 服务器状态 (TPS/玩家/在线)",
        alias="/mcstatus",
    )
    # 重载工作流：外部直接改了 workflows.json 后执行 /mcon reload 即时生效（管理员）
    ctx.command(
        r"^/?mcon\s+reload\b", _mcon_reload_cmd,
        priority=40, require_admin=True, description="重新加载工作流（从磁盘，无需重启）",
        alias="/mcon reload",
    )
    # QQ↔MC 玩家绑定（按群隔离），供工作流 {player} 注入使用
    ctx.command(r"^/?绑定玩家\s*(.+?)\s*$", _bind_cmd, priority=40,
                description="绑定你的 QQ 与 MC 玩家ID（按群隔离）；超管可代绑：/绑定玩家 <QQ> <ID>")
    ctx.command(r"^/?解绑玩家\s*$", _unbind_cmd, priority=40,
                description="解除本群的 MC 玩家绑定")
    ctx.command(r"^/?我的玩家\s*$", _myplayers_cmd, priority=40,
                description="查看本群绑定的 MC 玩家")
    ctx.log("[minecraftconsole] register 完成（WebUI + /mcstatus + 玩家绑定 + 工作流引擎已注册）")


def _extract_message_text(raw_event) -> str:
    """从 OneBot 11 原始事件中提取纯文本（message 可能是字符串或消息段数组）。"""
    try:
        msg = raw_event.get("message", "") if isinstance(raw_event, dict) else ""
        if isinstance(msg, str):
            return msg.strip()
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
            return "".join(parts).strip()
    except Exception:
        return ""
    return ""


# ------------------------------------------------------------------ 玩家绑定 / 工作流权限辅助
def _ev_field(event, name, default=None):
    """从事件对象或原始事件 dict 取字段（兼容框架 Event 与 OneBot 原始 dict）。"""
    if isinstance(event, dict):
        if name in event:
            return event[name]
        if name == "user_id":
            sender = event.get("sender") or {}
            return sender.get("user_id", default)
        return default
    return getattr(event, name, default)


def _lookup_player(ctx, group_id, user_id):
    """查 QQ↔MC 玩家绑定（按群隔离）；无则返回 None。"""
    gkey = str(group_id or 0)
    try:
        row = ctx.db_query_one(
            "SELECT player FROM mc_whitelist WHERE group_id=%s AND qq=%s",
            (gkey, int(user_id)))
        return row["player"] if row else None
    except Exception:
        return None


def _wf_needs_player(text: str, wf: dict) -> bool:
    """工作流是否需要 {player}：出现在触发文本或任意 action 节点的命令中。"""
    if "{player}" in (text or ""):
        return True
    for n in wf.get("nodes", []):
        if n.get("type") == workflow.NODE_ACTION:
            if "{player}" in str((n.get("config") or {}).get("command") or ""):
                return True
    return False


def _safe_reply(ctx, event, msg):
    """同步回复（命令 handler 在框架线程池执行，可用同步桥）。"""
    uid = _ev_field(event, "user_id", 0)
    gid = _ev_field(event, "group_id", None)
    try:
        ctx.send_msg(user_id=uid, group_id=gid, message=msg)
    except Exception:
        pass


def _do_bind(ctx, group_key: str, qq: int, player: str, by: int):
    """写入/覆盖一条 QQ↔MC 绑定（先删后插，方言安全）。"""
    try:
        ctx.db_execute("DELETE FROM mc_whitelist WHERE group_id=%s AND qq=%s",
                       (group_key, int(qq)))
        ctx.db_execute(
            "INSERT INTO mc_whitelist (group_id, qq, player, bound_by, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (group_key, int(qq), player, int(by), int(time.time())))
        return True
    except Exception as e:
        ctx.log(f"[mc-bind] 绑定失败: {e}")
        return False


def _bind_cmd(event, match):
    """/绑定玩家 <MC游戏ID>：把当前 QQ 与某 MC 玩家绑定（按群隔离）。
    超管可代他人绑定：/绑定玩家 <目标QQ> <MC游戏ID>。"""
    if _console is None:
        return
    ctx = _console.ctx
    uid = _ev_field(event, "user_id", 0)
    gid = _ev_field(event, "group_id", None)
    gkey = str(gid or 0)
    arg = (match.group(1) if match and match.lastindex else "").strip()
    if not arg:
        _safe_reply(ctx, event, "用法：/绑定玩家 <你的MC游戏ID>")
        return
    parts = arg.split()
    target_qq, player = uid, arg
    # 超管代他人绑定：/绑定玩家 <QQ> <游戏ID>
    if len(parts) >= 2 and ctx.is_superuser(uid):
        try:
            target_qq = int(parts[0])
            player = " ".join(parts[1:]).strip()
        except ValueError:
            target_qq, player = uid, arg
    if not player:
        _safe_reply(ctx, event, "玩家名不能为空")
        return
    if _do_bind(ctx, gkey, int(target_qq), player, int(uid)):
        _safe_reply(ctx, event,
                    f"已绑定：QQ {target_qq} ↔ MC玩家「{player}」（群 {gkey}）")
    else:
        _safe_reply(ctx, event, "绑定失败，请稍后再试")


def _unbind_cmd(event, match):
    """/解绑玩家：解除当前 QQ 在本群的 MC 玩家绑定。"""
    if _console is None:
        return
    ctx = _console.ctx
    uid = _ev_field(event, "user_id", 0)
    gid = _ev_field(event, "group_id", None)
    gkey = str(gid or 0)
    try:
        ctx.db_execute("DELETE FROM mc_whitelist WHERE group_id=%s AND qq=%s",
                       (gkey, int(uid)))
        _safe_reply(ctx, event, f"已解除本群(QQ {uid})的 MC 玩家绑定")
    except Exception as e:
        ctx.log(f"[mc-bind] 解绑失败: {e}")
        _safe_reply(ctx, event, "解绑失败，请稍后再试")


def _myplayers_cmd(event, match):
    """/我的玩家：查看当前 QQ 在本群绑定的 MC 玩家。"""
    if _console is None:
        return
    ctx = _console.ctx
    uid = _ev_field(event, "user_id", 0)
    gid = _ev_field(event, "group_id", None)
    gkey = str(gid or 0)
    player = _lookup_player(ctx, gid, uid)
    if player:
        _safe_reply(ctx, event, f"你在本群({gkey})绑定的 MC 玩家：{player}")
    else:
        _safe_reply(ctx, event,
                    "你在本群尚未绑定 MC 玩家，发送 /绑定玩家 <游戏ID> 进行绑定")


def on_loaded(ctx):
    """框架在 register 后调用一次（主事件循环内）：异步启动 TCP/HTTP 服务。"""
    console = getattr(ctx, "_mc_console", None)
    if console is None:
        return
    try:
        asyncio.get_running_loop().create_task(console.start())
    except RuntimeError as e:
        ctx.log(f"[minecraftconsole] on_loaded 无法获取事件循环（将延迟启动）: {e}")


def on_unload():
    """框架卸载钩子（可能在后台线程调用），线程安全关停服务。"""
    global _console
    if _console is not None:
        try:
            import asyncio
            loop = _console._loop
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(_console.stop(), loop)
            else:
                asyncio.run(_console.stop())
        except Exception:
            pass
        _console = None

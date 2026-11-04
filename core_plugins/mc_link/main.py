# -*- coding: utf-8 -*-
"""mc_link —— Minecraft agent 独立协议适配器（非 HTTP）。

把 MC agent 的私有 TCP 帧链路从业务插件里抽出来，作为一个独立的
ProtocolAdapter 注册进框架。多适配器并存时，本端以 bot_name="mc_link"
作为事件来源，业务插件用 ctx.on("notice.mc_*") 单独订阅，不会串到
OneBot / Web 接入端。

链路形态：
    MC agent --frp tcp 隧道--> mc_link:25599 --dispatch_event--> 业务插件
全程无 HTTP（无 Host / URL / Header），因此不走 frp 的 http 类型隧道，
规避其风控。Web 侧控制台 API 仍由 plugins/minecraftconsole 自己提供，
那是玩家/管理员浏览器访问的通道，不经过 frp，与本链路互不相干。

帧格式与 mc-agent-java、旧插件 FrameCodec 三方一致，见 protocol.py。

默认关闭：core_plugins.yaml 里 mc_link.enabled 置 true 才启动。
关闭时业务插件自动退回内置 TCP 监听，行为与旧版完全相同。
"""
import asyncio
import importlib.util
import logging
import os
import time
from collections import deque

from framework.messaging.protocol import ProtocolAdapter

logger = logging.getLogger("zcbot")


def _load_frames():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protocol.py")
    spec = importlib.util.spec_from_file_location("mc_link_protocol", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load_frames()

__plugin_meta__ = {
    "name": "mc_link",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "MC agent 私有 TCP 帧协议独立适配器（非 HTTP，走 frp tcp 隧道）",
    "priority": 10,
    "official": True,
    "process": "core",
}


class McLinkAdapter(ProtocolAdapter):
    """MC agent 链路：监听 TCP、验帧、下发命令、把状态广播成框架事件。"""

    adapter_id = "mc_link"

    def __init__(self, framework, cfg=None):
        cfg = cfg or {}
        self.framework = framework
        self.bot_name = str(cfg.get("bot_name") or "mc_link")
        self.host = str(cfg.get("host") or "0.0.0.0")
        self.port = int(cfg.get("port") or P.DEFAULT_TCP_PORT)
        self.secret = str(cfg.get("secret") or os.environ.get("MC_SECRET")
                          or "zcboot-mc-please-change-me").encode("utf-8")

        self._peers = {}
        self._pending = {}
        self._status = {"online": False, "tps": 0.0, "players": 0, "last_beat": None}
        self._log_ring = deque(maxlen=300)
        self._server = None
        self._watchdog = None
        self._started = False

    # ---------------------------------------------------------------- 契约
    def get_connection_info(self) -> dict:
        return {
            "id": "mc_link",
            "name": "MC agent 私有 TCP 链路",
            "config_section": "mc_link",
            "fields": [
                {"key": "host", "label": "监听地址", "type": "string"},
                {"key": "port", "label": "监听端口", "type": "number"},
                {"key": "secret", "label": "帧签名密钥", "type": "password"},
            ],
            "restart_keys": ["host", "port", "secret"],
            "endpoint_hint": f"tcp://{self.host}:{self.port}",
            "guide": "MC agent 主动连入此 TCP 端口，走 frp tcp 隧道；不要配成 http 类型。",
        }

    def get_connected_bots(self) -> list:
        return [self.bot_name]

    async def handle_event(self, raw_event: dict, bot_name: str) -> dict:
        return raw_event

    async def call_api(self, action: str, bot: str = None, **params) -> dict:
        """通用动作入口：send_command / status，供 ctx.api() 调用。"""
        if action == "send_command":
            return await self.send_command(params.get("command", ""))
        if action == "status":
            return self.status()
        return {"ok": False, "error": "unsupported-action"}

    def start(self):
        """契约要求同步启动；派发回框架主事件循环。"""
        loop = getattr(self.framework, "loop", None)
        if loop is None:
            return
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(self.start_async(), loop)
        else:
            loop.create_task(self.start_async())

    async def start_async(self):
        if self._started:
            return self._server
        self._started = True
        self._server = await asyncio.start_server(
            self._on_peer_connected, self.host, self.port, limit=P.TCP_BUF_SIZE)
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="mc-link-watchdog")
        self._log(f"[sys] mc_link 监听 tcp://{self.host}:{self.port}")
        return self._server

    async def stop(self):
        self._started = False
        if self._watchdog:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        for peer in list(self._peers.values()):
            try:
                peer.writer.close()
            except Exception:
                pass
        self._peers.clear()
        self._status["online"] = False

    # ---------------------------------------------------------------- 收帧
    async def _on_peer_connected(self, reader, writer):
        peer = P.Peer(writer)
        self._peers[peer.addr] = peer
        self._log(f"[conn] agent {peer.addr} 接入")
        buf = bytearray()
        try:
            while True:
                chunk = await reader.read(P.TCP_BUF_SIZE)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 1:
                    ftype = buf[0]
                    if ftype == P.HEARTBEAT_TYPE:
                        total = P.HEADER_LEN
                    elif ftype in (P.COMMAND_TYPE, P.OUTPUT_TYPE):
                        if len(buf) < P.HEADER_LEN:
                            break
                        plen = int.from_bytes(buf[P.PLEN_OFF:P.PLEN_OFF + 4], "big")
                        if plen > P.MAX_PAYLOAD:
                            self._log(f"[drop] 载荷超限 {plen}B")
                            del buf[0]
                            continue
                        total = P.HEADER_LEN + plen
                    else:
                        del buf[0]
                        continue
                    if len(buf) < total:
                        break
                    frame = bytes(buf[:total])
                    del buf[:total]
                    await self._dispatch(peer, frame)
        finally:
            self._peers.pop(peer.addr, None)
            try:
                writer.close()
            except Exception:
                pass
            self._log(f"[conn] agent {peer.addr} 离开")

    async def _dispatch(self, peer, frame: bytes):
        ftype, ts, tok, tps, players, plen, seq, text = P.FrameCodec.parse(frame)

        if ftype == P.OUTPUT_TYPE:
            if abs(time.time() - ts) > P.REPLAY_WINDOW:
                self._log(f"[drop] {peer.addr} output:stale-timestamp")
                return
            if not P.FrameCodec.verify_output(self.secret, ts, tok, seq, 0)[0]:
                self._log(f"[drop] {peer.addr} output:bad-token")
                return
            fut = self._pending.pop(seq, None)
            if fut is not None and not fut.done():
                fut.set_result(text)
            await self._emit_notice("mc_output", addr=str(peer.addr), text=text, seq=seq)
            return

        ok, reason = P.FrameCodec.verify(self.secret, ts, tok, peer.last_ts)
        if not ok:
            self._log(f"[drop] {peer.addr} {reason}")
            return
        peer.last_ts = ts
        peer.last_beat = time.time()
        peer.offline = False
        peer.tps, peer.players = tps, players
        self._status.update({"online": True, "tps": round(tps, 2), "players": players,
                             "last_beat": int(peer.last_beat)})
        if ftype == P.HEARTBEAT_TYPE:
            await self._emit_notice("mc_heartbeat", addr=str(peer.addr),
                                    tps=tps, players=players)
        elif ftype == P.COMMAND_TYPE and text:
            cok, creason = P.FrameCodec.verify_output(self.secret, ts, tok, seq, peer.cmd_seq)
            if not cok:
                self._log(f"[drop] {peer.addr} cmd:{creason}")
                return
            peer.cmd_seq = seq
            await self._emit_notice("mc_command", addr=str(peer.addr), text=text, seq=seq)

    async def _emit_notice(self, kind: str, **fields):
        """转成框架 notice 事件；bot_name=mc_link 让插件按来源单独订阅。"""
        event = {
            "type": "notice", "post_type": "notice", "notice_type": kind,
            "bot_name": self.bot_name, "adapter": self.adapter_id,
            "self_id": 0, "user_id": 0, "group_id": None, "message": "",
            "time": int(time.time()),
        }
        event.update(fields)
        try:
            await self.framework.dispatch_event(event)
        except Exception as e:
            logger.warning(f"mc_link 事件投递失败: {e}")
        bus = getattr(self.framework, "event_bus", None)
        if bus is not None and kind == "mc_output":
            try:
                await bus.aemit("mc.agent_output", {"command": fields.get("text", "")})
            except Exception:
                pass

    # ---------------------------------------------------------------- 下发
    async def send_command(self, command: str) -> dict:
        command = (command or "").strip().lstrip("/")
        if not command:
            return {"ok": False, "error": "empty-command"}
        target = next((p for p in self._peers.values() if not p.offline), None)
        if target is None:
            return {"ok": False, "error": "mc-offline"}
        try:
            target.cmd_seq += 1
            frame = P.FrameCodec.pack_command(
                self.secret, int(time.time()), target.cmd_seq,
                target.tps, target.players, command)
            target.writer.write(frame)
            await target.writer.drain()
            self._log(f"[cmd] /{command}")
            return {"ok": True, "seq": target.cmd_seq}
        except Exception as e:
            return {"ok": False, "error": f"send-failed:{e}"}

    async def send_instruction(self, instruction: str, timeout: float = 8.0) -> str:
        """下发指令并等待同 seq 的回执帧（工作流引擎用）。"""
        target = next((p for p in self._peers.values() if not p.offline), None)
        if target is None:
            return ""
        target.cmd_seq += 1
        seq = target.cmd_seq
        fut = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        try:
            frame = P.FrameCodec.pack_command(
                self.secret, int(time.time()), seq, target.tps, target.players, instruction)
            target.writer.write(frame)
            await target.writer.drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return ""
        except Exception:
            return ""
        finally:
            self._pending.pop(seq, None)

    def status(self) -> dict:
        return dict(self._status, peers=len(self._peers), adapter=self.adapter_id)

    async def _watchdog_loop(self):
        while True:
            await asyncio.sleep(P.LOOP_INTERVAL)
            now = time.time()
            offline = True
            for peer in self._peers.values():
                if now - peer.last_beat <= P.OFFLINE_THRESHOLD:
                    peer.offline = False
                    offline = False
                else:
                    peer.offline = True
            if self._peers and offline:
                self._status["online"] = False

    def _log(self, line: str):
        self._log_ring.append(line)
        logger.debug(line)


_adapter_instance = None


def register(ctx):
    """注册为独立适配器服务（默认关闭）。"""
    global _adapter_instance
    fw = ctx._framework
    cfg = fw.config.get("mc_link", {}) or {}
    if cfg.get("enabled") is not True:
        fw.services.register("mc_link", None)
        ctx.log("mc_link 独立适配器已禁用，业务插件沿用内置 TCP 监听")
        return
    _adapter_instance = McLinkAdapter(fw, cfg)
    # 专用服务名：不覆盖 protocol_adapter / api_caller，多适配器并存
    fw.services.register("mc_link", _adapter_instance)
    _adapter_instance.start()
    ctx.log(f"mc_link 独立适配器已启动: tcp://{_adapter_instance.host}:{_adapter_instance.port}")


def unregister():
    global _adapter_instance
    inst, _adapter_instance = _adapter_instance, None
    if inst is not None:
        loop = getattr(inst.framework, "loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(inst.stop(), loop)
        try:
            inst.framework.services.remove("mc_link")
        except Exception:
            pass

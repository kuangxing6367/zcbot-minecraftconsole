# -*- coding: utf-8 -*-
"""mc_link v1 RPC 层：JSON-RPC 2.0 envelope + 按方向奇偶 id + 幂等对账。

与 protocol.py 的分工：本文件只处理「消息语义」，不碰帧字节。
帧的打包/校验/签名全部在 protocol.py，Java 侧对应 FrameCodec + MiniJson + RpcHub。

标准依据：
  JSON-RPC 2.0 §2   双端可同时充当 Client 与 Server
             §4     id 用于关联 Request / Response
             §4.1   Notification 无 id，服务端 MUST NOT 回复
             §5.1   -32768..-32000 为预定义错误保留区
             §8     rpc. 前缀方法名保留给系统扩展
  RFC 9113 §5.1.1  连接发起方用奇数 id，接受方用偶数；0 保留给连接级消息
  RSocket §Stream IDs 同上，且 id 单调递增、不可复用
"""
import asyncio
import inspect
import json
import logging
import time
from collections import OrderedDict

logger = logging.getLogger("zcbot")

JSONRPC = "2.0"

# JSON-RPC 2.0 预定义错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# -32000..-32099 为实现自定义 server error
EPOCH_TOO_OLD = -32010      # 等价 Kubernetes 410 Gone：历史窗口不足，需全量重拉
PASS_UNAVAILABLE = -32011   # pass 闸门不可判定

# 闸门方法：任何异常都必须 fail-closed，绝不默认放行
FAIL_CLOSED_METHODS = frozenset({"zcore.pass.query"})
PASS_PENDING = {"state": "pending", "reason": "unavailable"}

CACHE_LIMIT = 256
CACHE_TTL = 60.0


class RpcFailure(Exception):
    """方法处理器主动返回的 JSON-RPC 错误（区别于未捕获异常）。"""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def make_request(method: str, params, rpc_id: int) -> dict:
    return {"jsonrpc": JSONRPC, "method": method, "params": params, "id": rpc_id}


def make_notification(method: str, params) -> dict:
    return {"jsonrpc": JSONRPC, "method": method, "params": params}


def make_result(rpc_id: int, result) -> dict:
    return {"jsonrpc": JSONRPC, "result": result, "id": rpc_id}


def make_error(rpc_id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC, "error": err, "id": rpc_id}


def encode(env: dict) -> bytes:
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode(raw: bytes):
    try:
        env = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, PARSE_ERROR
    if not isinstance(env, dict) or env.get("jsonrpc") != JSONRPC:
        return None, INVALID_REQUEST
    return env, 0


def kind(env: dict) -> str:
    """request / notification / response / invalid"""
    has_method = isinstance(env.get("method"), str)
    has_id = "id" in env
    if has_method:
        return "request" if has_id else "notification"
    if has_id and ("result" in env or "error" in env):
        return "response"
    return "invalid"


class IdAllocator:
    """按 TCP 连接方向分配请求 id：发起方奇数、接受方偶数。

    id=0 保留给连接级消息，任何一端都不分配。单调递增，不复用。
    """

    def __init__(self, role: str):
        if role not in ("client", "server"):
            raise ValueError("role must be client or server")
        self._next = 1 if role == "client" else 2
        self.role = role

    def next(self) -> int:
        value = self._next
        self._next += 2
        return value

    def owns(self, rpc_id) -> bool:
        """该 id 是否由本端发出（用于判定响应归属）。"""
        if not isinstance(rpc_id, int) or isinstance(rpc_id, bool) or rpc_id <= 0:
            return False   # 0 保留给连接级消息，不属于任何一端
        return (rpc_id % 2 == 1) if self.role == "client" else (rpc_id % 2 == 0)


class ResultCache:
    """同 id 重发返回同一结果（幂等），避免网络重试造成重复签发。"""

    def __init__(self, limit: int = CACHE_LIMIT, ttl: float = CACHE_TTL):
        self._items = OrderedDict()
        self.limit = limit
        self.ttl = ttl

    def get(self, rpc_id):
        hit = self._items.get(rpc_id)
        if hit is None:
            return None
        ts, value = hit
        if time.time() - ts > self.ttl:
            self._items.pop(rpc_id, None)
            return None
        self._items.move_to_end(rpc_id)
        return value

    def put(self, rpc_id, value):
        self._items[rpc_id] = (time.time(), value)
        self._items.move_to_end(rpc_id)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def clear(self):
        self._items.clear()


class RpcHub:
    """单条连接上的双向 RPC 状态机。

    send_frame(peer, ftype, env_dict) 由适配器提供（负责打包 + 写 socket）。
    on_event(kind, env) 可选回调，用于把 RPC 事件转给框架 event_bus。
    """

    REQ_TYPE = 0x05
    RESP_TYPE = 0x06

    def __init__(self, role: str, send_frame, on_event=None):
        self.role = role
        self.ids = IdAllocator(role)
        self.cache = ResultCache()
        self.methods = {}
        self._pending = {}
        self._send_frame = send_frame
        self._on_event = on_event
        self.register_method("rpc.capabilities", self._capabilities)

    # ------------------------------------------------------------ 方法表
    def register_method(self, name: str, handler):
        if name.startswith("rpc.") and name != "rpc.capabilities":
            raise ValueError("rpc. prefix is reserved (JSON-RPC 2.0 §8)")
        self.methods[name] = handler

    def unregister_method(self, name: str):
        self.methods.pop(name, None)

    def _capabilities(self, params, peer=None):
        return {"ver": 1, "role": self.role,
                "methods": sorted(k for k in self.methods if not k.startswith("rpc.")),
                "cap": 0x01 | 0x02 | 0x04}

    # ------------------------------------------------------------ 发起调用
    async def call(self, peer, method: str, params=None, timeout: float = 5.0):
        rpc_id = self.ids.next()
        env = make_request(method, params if params is not None else {}, rpc_id)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = fut
        try:
            await self._send_frame(peer, self.REQ_TYPE, env)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": {"code": PASS_UNAVAILABLE,
                                           "message": "rpc timeout"}}
        except Exception as e:
            return {"ok": False, "error": {"code": PASS_UNAVAILABLE,
                                           "message": f"rpc unavailable: {e}"}}
        finally:
            self._pending.pop(rpc_id, None)

    async def notify(self, peer, method: str, params=None):
        env = make_notification(method, params if params is not None else {})
        try:
            await self._send_frame(peer, self.REQ_TYPE, env)
        except Exception as e:
            logger.debug(f"mc_link notify skipped: {e}")

    # ------------------------------------------------------------ 收包处理
    async def on_envelope(self, peer, env: dict):
        """处理一帧 RPC envelope；必要时通过 send_frame 回复。"""
        k = kind(env)
        if k == "invalid":
            await self._reply(peer, make_error(None, INVALID_REQUEST, "Invalid Request"))
            return
        if k == "notification":
            await self._dispatch_method(peer, env.get("method"), env.get("params"), None)
            self._fire("mc_rpc_notification", env)
            return
        if k == "response":
            rpc_id = env.get("id")
            fut = self._pending.get(rpc_id)
            if fut is not None and not fut.done():
                if "error" in env:
                    fut.set_result({"ok": False, "error": env["error"]})
                else:
                    fut.set_result({"ok": True, "result": env.get("result")})
                self._fire("mc_rpc_response", env)
            return
        # request：先查幂等缓存
        rpc_id = env.get("id")
        cached = self.cache.get(rpc_id)
        if cached is not None:
            await self._reply(peer, cached)
            return
        response = await self._dispatch_method(peer, env.get("method"),
                                               env.get("params"), rpc_id)
        if response is not None:
            self.cache.put(rpc_id, response)
            await self._reply(peer, response)

    async def _reply(self, peer, env: dict):
        await self._send_frame(peer, self.RESP_TYPE, env)

    async def _dispatch_method(self, peer, method, params, rpc_id):
        handler = self.methods.get(method)
        if handler is None:
            if rpc_id is None:      # Notification 不回复（JSON-RPC §4.1）
                return None
            # 闸门方法即使未注册也必须 fail-closed：不能让「未实现」被误读成放行
            if method in FAIL_CLOSED_METHODS:
                return make_result(rpc_id, dict(PASS_PENDING))
            return make_error(rpc_id, METHOD_NOT_FOUND, "Method not found")
        try:
            # handler 可声明 (params) 或 (params, peer)；也允许 async
            try:
                takes_peer = "peer" in inspect.signature(handler).parameters
            except (TypeError, ValueError):
                takes_peer = False
            args = (params if params is not None else {},)
            result = handler(*args, peer=peer) if takes_peer else handler(*args)
            if asyncio.iscoroutine(result):
                result = await result
        except RpcFailure as e:
            if rpc_id is None:
                return None
            return make_error(rpc_id, e.code, e.message, e.data)
        except Exception as e:
            logger.warning(f"mc_link rpc method {method} failed: {e}")
            if method in FAIL_CLOSED_METHODS:
                result = PASS_PENDING
            else:
                if rpc_id is None:
                    return None
                return make_error(rpc_id, INTERNAL_ERROR, "Internal error")
        if method in FAIL_CLOSED_METHODS and not isinstance(result, dict):
            result = PASS_PENDING
        if rpc_id is None:
            return None
        return make_result(rpc_id, result)

    def _fire(self, name, env):
        if self._on_event is None:
            return
        try:
            self._on_event(name, env)
        except Exception:
            pass

    def on_peer_closed(self, peer=None):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result({"ok": False,
                                "error": {"code": PASS_UNAVAILABLE,
                                          "message": "connection closed"}})
        self._pending.clear()
        self.cache.clear()

# -*- coding: utf-8 -*-
"""
zcore 白名单 / pass 签发者（v1 私有链路的「第三端」）

定位
----
minecraftconsole v1 私有 TCP 链路 RPC 的**真实签发者**。

mc_link 适配器侧只提供 `zcore.pass.query` 的 fail-closed 占位与
`register_method()` 注入口；本模块是注入进去的真实实现：
  * 维护 pass 存储（玩家闸门状态）；
  * 回答 `zcore.pass.query`，覆盖适配器默认的 pending 占位；
  * 签发 / 审批 / 吊销变更时通过 `mc_link.bump_epoch(item)` 推进对账 epoch，
    触发控制器向 Java agent 主动推 `zcore.pass.changed` Notification，
    并由 `zcore.sync` 提供增量对账。

数据模型（参考实现；字段语义以 zcore 设计稿为准）
------------------------------------------------
pass 记录 = {
    "player":    str,            # MC 玩家标识（UUID 或名称，按部署约定）
    "state":     "approved" | "pending" | "rejected" | "expired",
    "code":      str | None,     # 验证码（如有）
    "issued_by": str,            # 签发 / 审批人
    "issued_at": int,            # epoch 秒
    "expires_at": int | None,    # epoch 秒；None 表示不过期
    "note":      str,
}

安全底线（与协议规范 rpc.md §4.10 一致）
--------------------------------------
查不到记录时默认 fail-closed：返回 `pending`（停在等待），绝不默认放行；
配置 `default_action="deny"` 时返回 `rejected`。记录过期一律返回 `expired`。
"""

import json
import os
import threading
import time


VALID_STATES = ("approved", "pending", "rejected", "expired")


class PassStore:
    """JSON 文件存储的 pass 记录库，带进程内锁与原子写。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data = {"passes": {}, "default_action": "pending"}
        self._load()

    # ---- 持久化 ----------------------------------------------------------
    def _load(self):
        try:
            with self._lock:
                if os.path.isfile(self.path):
                    with open(self.path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        self._data = raw
                        self._data.setdefault("passes", {})
                        self._data.setdefault("default_action", "pending")
        except Exception:
            # 损坏文件不致命：以空库启动，后续写会覆盖
            self._data = {"passes": {}, "default_action": "pending"}

    def _save(self):
        tmp = self.path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    # ---- 查询 ------------------------------------------------------------
    def query(self, player: str, default_action: str = None) -> dict:
        """回答闸门查询。命中按记录 state；过期返回 expired；无记录 fail-closed。"""
        if not player:
            return {"state": "pending", "reason": "missing-player",
                    "player": player, "expires_at": None, "code": None}
        with self._lock:
            rec = self._data["passes"].get(player)
            now = int(time.time())
            if rec is None:
                da = default_action or self._data.get("default_action", "pending")
                state = "rejected" if da == "deny" else "pending"
                return {"state": state, "reason": "no-record",
                        "player": player, "expires_at": None, "code": None}
            exp = rec.get("expires_at")
            if exp and exp <= now:
                return {"state": "expired", "reason": "expired",
                        "player": player, "expires_at": exp, "code": rec.get("code")}
            return {"state": rec.get("state", "pending"), "reason": "ok",
                    "player": player, "expires_at": exp, "code": rec.get("code")}

    # ---- 变更 ------------------------------------------------------------
    def upsert(self, player: str, state: str, code=None, issued_by="system",
               expires_at=None, note="") -> dict:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state}")
        with self._lock:
            rec = self._data["passes"].get(player, {"player": player})
            rec.update({
                "player": player,
                "state": state,
                "code": code,
                "issued_by": issued_by,
                "issued_at": int(time.time()),
                "expires_at": expires_at,
                "note": note,
            })
            self._data["passes"][player] = rec
            self._save()
            return dict(rec)

    def revoke(self, player: str, issued_by="system") -> dict:
        return self.upsert(player, "rejected", code=None,
                           issued_by=issued_by, expires_at=None,
                           note="revoked")

    def delete(self, player: str) -> bool:
        with self._lock:
            if player in self._data["passes"]:
                del self._data["passes"][player]
                self._save()
                return True
            return False

    def list_all(self) -> list:
        with self._lock:
            return [dict(v) for v in self._data["passes"].values()]

    def set_default_action(self, action: str):
        if action not in ("pending", "deny"):
            raise ValueError("default_action must be 'pending' or 'deny'")
        with self._lock:
            self._data["default_action"] = action
            self._save()


class ZcorePassSigner:
    """
    zcore whitelist 的真实签发者：把 store 接到 mc_link 的 RPC 注入口，
    变更时推进对账 epoch。

    用法：
        signer = ZcorePassSigner(store, default_action="pending")
        signer.attach(link)          # link = services.get("mc_link")
        await signer.issue("Steve", "approved", ttl=3600)
    """

    def __init__(self, store: PassStore, default_action: str = None, link=None):
        self.store = store
        if default_action:
            self.store.set_default_action(default_action)
        self.link = link

    def attach(self, link):
        """把真实 zcore.pass.query 注入 mc_link，覆盖默认 fail-closed 占位。"""
        self.link = link
        if link is not None and hasattr(link, "register_method"):
            link.register_method("zcore.pass.query", self.handle_query)

    def handle_query(self, params, peer=None) -> dict:
        """RPC 方法处理器：注册进 mc_link 后由 RpcHub 派发。"""
        player = (params or {}).get("player")
        return self.store.query(player)

    async def issue(self, player: str, state: str, code=None, issued_by="system",
                    ttl: int = None, note="") -> dict:
        """签发 / 审批一条 pass，并推进对账 epoch。"""
        expires_at = (int(time.time()) + int(ttl)) if ttl else None
        rec = self.store.upsert(player, state, code=code, issued_by=issued_by,
                                expires_at=expires_at, note=note)
        await self._bump("upsert", player, state, code, issued_by)
        return rec

    async def revoke(self, player: str, issued_by="system") -> dict:
        rec = self.store.revoke(player, issued_by=issued_by)
        await self._bump("revoke", player, "rejected", None, issued_by)
        return rec

    async def delete(self, player: str) -> bool:
        ok = self.store.delete(player)
        if ok:
            await self._bump("delete", player, "removed", None, "system")
        return ok

    async def _bump(self, op, player, state, code, issued_by):
        if self.link is None or not hasattr(self.link, "bump_epoch"):
            return
        try:
            await self.link.bump_epoch({
                "op": op, "player": player, "state": state,
                "code": code, "issued_by": issued_by, "ts": int(time.time()),
            })
        except Exception:
            # 对账推进失败不应让签发失败；记录由本地 store 保证持久
            pass

    def list_passes(self) -> list:
        return self.store.list_all()

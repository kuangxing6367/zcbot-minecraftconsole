# -*- coding: utf-8 -*-
"""zcore pass 签发者（v1 链路第三端）验证（脚本式：python tests/test_zcore_pass.py）。

覆盖：
  1. 无记录 fail-closed：默认 pending，配置 deny 时 rejected
  2. 签发 approved / 吊销 / 删除 / 过期的查询语义
  3. 非法 state 拒绝
  4. ZcorePassSigner.attach 覆盖 mc_link 的 zcore.pass.query 占位
  5. 签发 / 吊销 / 删除 推进对账 epoch（bump_epoch 被调用且载荷正确）
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "plugins", "minecraftconsole"))

import zcore_pass  # noqa: E402


class FakeLink:
    """最小 mc_link 替身：记录注册的方法与 bump_epoch 载荷。"""

    def __init__(self):
        self.methods = {}
        self.bumps = []

    def register_method(self, name, handler):
        self.methods[name] = handler

    async def bump_epoch(self, item):
        self.bumps.append(item)
        return len(self.bumps)


class TestPassStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "zcore_pass.json")
        self.store = zcore_pass.PassStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_player_is_fail_closed_pending(self):
        info = self.store.query("Steve")
        self.assertEqual(info["state"], "pending")
        self.assertEqual(info["reason"], "no-record")

    def test_default_action_deny_yields_rejected(self):
        self.store.set_default_action("deny")
        info = self.store.query("Steve")
        self.assertEqual(info["state"], "rejected")
        self.assertEqual(info["reason"], "no-record")

    def test_invalid_default_action_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_default_action("allow")

    def test_approve_then_query(self):
        self.store.upsert("Steve", "approved", issued_by="42")
        info = self.store.query("Steve")
        self.assertEqual(info["state"], "approved")
        self.assertEqual(info["reason"], "ok")

    def test_expired_record_reports_expired(self):
        self.store.upsert("Steve", "approved", expires_at=int(time.time()) - 1)
        info = self.store.query("Steve")
        self.assertEqual(info["state"], "expired")
        self.assertEqual(info["reason"], "expired")

    def test_revoke_overrides_approval(self):
        self.store.upsert("Steve", "approved")
        self.store.revoke("Steve")
        self.assertEqual(self.store.query("Steve")["state"], "rejected")

    def test_delete_returns_to_fail_closed(self):
        self.store.upsert("Steve", "approved")
        self.assertTrue(self.store.delete("Steve"))
        self.assertFalse(self.store.delete("Steve"))
        self.assertEqual(self.store.query("Steve")["state"], "pending")

    def test_invalid_state_rejected(self):
        with self.assertRaises(ValueError):
            self.store.upsert("Steve", "allow")

    def test_missing_player_is_pending(self):
        info = self.store.query("")
        self.assertEqual(info["state"], "pending")
        self.assertEqual(info["reason"], "missing-player")

    def test_persistence_roundtrip(self):
        self.store.upsert("Steve", "approved", code="1234")
        again = zcore_pass.PassStore(self.path)
        self.assertEqual(again.query("Steve")["state"], "approved")
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertIn("Steve", raw["passes"])


class TestSigner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = zcore_pass.PassStore(os.path.join(self._tmp.name, "p.json"))
        self.link = FakeLink()
        self.signer = zcore_pass.ZcorePassSigner(self.store)
        self.signer.attach(self.link)

    def tearDown(self):
        self._tmp.cleanup()

    def test_attach_overrides_fail_closed_placeholder(self):
        self.assertIn("zcore.pass.query", self.link.methods)
        result = self.link.methods["zcore.pass.query"]({"player": "Steve"})
        self.assertEqual(result["state"], "pending")

    def test_issue_bumps_epoch_and_answers_approved(self):
        rec = asyncio.run(
            self.signer.issue("Steve", "approved", ttl=3600, issued_by="42"))
        self.assertEqual(rec["state"], "approved")
        self.assertEqual(len(self.link.bumps), 1)
        bump = self.link.bumps[0]
        self.assertEqual(bump["op"], "upsert")
        self.assertEqual(bump["player"], "Steve")
        self.assertEqual(bump["state"], "approved")
        info = self.link.methods["zcore.pass.query"]({"player": "Steve"})
        self.assertEqual(info["state"], "approved")

    def test_revoke_and_delete_bump(self):
        asyncio.run(self.signer.issue("Steve", "approved"))
        asyncio.run(self.signer.revoke("Steve"))
        asyncio.run(self.signer.delete("Steve"))
        self.assertEqual([b["op"] for b in self.link.bumps],
                         ["upsert", "revoke", "delete"])
        self.assertEqual(self.link.methods["zcore.pass.query"]({"player": "Steve"})["state"],
                         "pending")

    def test_bump_failure_does_not_break_issue(self):
        class Broken(FakeLink):
            async def bump_epoch(self, item):
                raise RuntimeError("link down")

        signer = zcore_pass.ZcorePassSigner(self.store)
        signer.attach(Broken())
        rec = asyncio.run(
            signer.issue("Alex", "approved"))
        self.assertEqual(rec["state"], "approved")
        self.assertEqual(self.store.query("Alex")["state"], "approved")

    def test_no_link_does_not_crash(self):
        signer = zcore_pass.ZcorePassSigner(self.store)
        rec = asyncio.run(
            signer.issue("Bob", "approved"))
        self.assertEqual(rec["state"], "approved")


if __name__ == "__main__":
    unittest.main(verbosity=2)

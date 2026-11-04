# -*- coding: utf-8 -*-
"""mc_link 独立适配器验证（脚本式：python tests/test_mc_link.py）。

覆盖：
  1. 新适配器帧格式与旧插件 FrameCodec 逐字节一致（不改协议）
  2. TCP 收帧 → notice.mc_* 事件（bot_name=mc_link，可单独订阅）
  3. 命令下发、回执关联（send_instruction 等到同 seq 输出）
  4. 坏 token / 过期时间戳被丢弃
  5. 多适配器注册：protocol_adapters() / adapter_for_source()
"""
import asyncio
import hashlib
import hmac
import importlib.util
import os
import struct
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "plugins", "minecraftconsole"))

from framework.messaging.protocol import ServiceRegistry  # noqa: E402

SECRET = b"unit-test-secret"
MC_LINK_MAIN = os.path.join(ROOT, "core_plugins", "mc_link", "main.py")
OLD_MAIN = os.path.join(ROOT, "plugins", "minecraftconsole", "main.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ml = load("mc_link_main_under_test", MC_LINK_MAIN)
P = ml.P
old = load("mc_console_main_under_test", OLD_MAIN)
OldCodec = old.FrameCodec


class FakeBus:
    def __init__(self):
        self.emitted = []

    async def aemit(self, name, payload=None, **kw):
        self.emitted.append((name, payload))
        return False


class FakeServices:
    def __init__(self):
        self.data = {}

    def get(self, name, default=None):
        return self.data.get(name, default)

    def register(self, name, service):
        self.data[name] = service


class FakeFramework:
    def __init__(self):
        self.loop = LOOP
        self.config = {}
        self.services = FakeServices()
        self.event_bus = FakeBus()
        self.events = []

    async def dispatch_event(self, event, wait=False):
        self.events.append(event)


def now():
    return int(time.time())


class TestFrameCompat(unittest.TestCase):
    def test_bytes_identical(self):
        ts = now()
        cases = [
            ("heartbeat", lambda c: c.pack_heartbeat(SECRET, ts, 19.8, 3)),
            ("command", lambda c: c.pack_command(SECRET, ts, 7, 19.8, 3, "EXEC|list")),
            ("output", lambda c: c.pack_output(SECRET, ts, 7, 19.8, 3, "hello 世界")),
        ]
        for name, pack in cases:
            a, b = pack(P.FrameCodec), pack(OldCodec)
            self.assertEqual(a, b, f"{name} 帧字节不一致")
            self.assertEqual(P.FrameCodec.parse(a), OldCodec.parse(b), f"{name} 解析不一致")

    def test_verify_agreement(self):
        ts = now()
        tok = P.FrameCodec._token(SECRET, ts)
        self.assertEqual(P.FrameCodec.verify(SECRET, ts, tok, 0),
                         OldCodec.verify(SECRET, ts, tok, 0))
        bad = b"\x00" * 16
        self.assertEqual(P.FrameCodec.verify(SECRET, ts, bad, 0)[1], "bad-token")
        self.assertEqual(OldCodec.verify(SECRET, ts, bad, 0)[1], "bad-token")
        self.assertEqual(P.FrameCodec.verify(SECRET, ts - 100, tok, 0)[1], "stale-timestamp")


class TestAdapterLink(unittest.TestCase):
    def setUp(self):
        self.fw = FakeFramework()
        self.adapter = ml.McLinkAdapter(self.fw, {"host": "127.0.0.1", "port": 0,
                                                  "secret": SECRET.decode()})
        self.server = LOOP.run_until_complete(self.adapter.start_async())
        self.port = self.server.sockets[0].getsockname()[1]

    def tearDown(self):
        LOOP.run_until_complete(self.adapter.stop())

    def _test(self, coro):
        return LOOP.run_until_complete(coro)

    async def _connect(self):
        return await asyncio.open_connection("127.0.0.1", self.port)

    async def _wait_event(self, kind, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for ev in self.fw.events:
                if ev.get("notice_type") == kind:
                    return ev
            await asyncio.sleep(0.02)
        self.fail(f"未收到 notice.{kind}")

    def test_heartbeat_notice_and_source(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 4))
            await writer.drain()
            ev = await self._wait_event("mc_heartbeat")
            self.assertEqual(ev["bot_name"], "mc_link")
            self.assertEqual(ev["adapter"], "mc_link")
            self.assertEqual(ev["players"], 4)
            self.assertTrue(self.adapter.status()["online"])
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_command_frame_and_notice(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 1))
            await writer.drain()
            await self._wait_event("mc_heartbeat")
            ts = now() + 1
            writer.write(P.FrameCodec.pack_command(SECRET, ts, 2, 20.0, 1, "say hi"))
            await writer.drain()
            ev = await self._wait_event("mc_command")
            self.assertEqual(ev["text"], "say hi")
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_send_command_writes_frame(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 1))
            await writer.drain()
            await self._wait_event("mc_heartbeat")
            res = await self.adapter.send_command("list")
            self.assertTrue(res["ok"], res)
            raw = await asyncio.wait_for(reader.read(8192), 2)
            ftype, ts, tok, tps, players, plen, seq, text = P.FrameCodec.parse(raw)
            self.assertEqual(ftype, P.COMMAND_TYPE)
            self.assertEqual(text, "list")
            self.assertEqual(seq, res["seq"])
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_instruction_waits_for_receipt(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 1))
            await writer.drain()
            await self._wait_event("mc_heartbeat")
            task = asyncio.create_task(
                self.adapter.send_instruction("EXEC|list", timeout=3))
            raw = await asyncio.wait_for(reader.read(8192), 2)
            seq = P.FrameCodec.parse(raw)[6]
            writer.write(P.FrameCodec.pack_output(SECRET, now(), seq, 20.0, 1, "3 players"))
            await writer.drain()
            out = await task
            self.assertEqual(out, "3 players")
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_bad_token_dropped(self):
        async def scenario():
            reader, writer = await self._connect()
            frame = bytearray(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 1))
            frame[5:21] = b"\x00" * 16
            writer.write(bytes(frame))
            await writer.drain()
            await asyncio.sleep(0.25)
            self.assertFalse(any(e.get("notice_type") == "mc_heartbeat"
                                 for e in self.fw.events))
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_stale_timestamp_dropped(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now() - 100, 20.0, 1))
            await writer.drain()
            await asyncio.sleep(0.25)
            self.assertFalse(any(e.get("notice_type") == "mc_heartbeat"
                                 for e in self.fw.events))
            writer.close()
            await writer.wait_closed()
        self._test(scenario())


class TestMultiAdapter(unittest.TestCase):
    def test_registry_sees_mc_link_by_source(self):
        fw = FakeFramework()
        adapter = ml.McLinkAdapter(fw, {"secret": SECRET.decode()})
        reg = ServiceRegistry()
        reg.register("mc_link", adapter)
        self.assertIn("mc_link", reg.protocol_adapters())
        self.assertIs(reg.adapter_for_source("mc_link"), adapter)
        self.assertEqual(adapter.get_connected_bots(), ["mc_link"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

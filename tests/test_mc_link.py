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


R = ml.R
from collections import deque  # noqa: E402


class TestV1Extension(unittest.TestCase):
    """v1 保留区：老端字节不变、新端签名可验。"""

    def test_v0_bytes_and_detect(self):
        ts = now()
        a = P.FrameCodec.pack_heartbeat(SECRET, ts, 20.0, 3)
        b = OldCodec.pack_heartbeat(SECRET, ts, 20.0, 3)
        self.assertEqual(a, b)
        self.assertEqual(P.FrameCodec.parse(a), OldCodec.parse(b))
        is_v1, ver, flags, cap, epoch, _ext = P.parse_ext(a)
        self.assertFalse(is_v1)
        self.assertTrue(P.FrameCodec.verify_ext(SECRET, a))
        self.assertEqual(bytes(a[P.EXT_OFF:]), b"\x00" * 27)

    def test_v1_ext_roundtrip(self):
        ts = now()
        f = P.FrameCodec.pack_heartbeat(SECRET, ts, 20.0, 3,
                                        cap=P.CAP_ALL, epoch=42)
        self.assertEqual(len(f), 64)
        is_v1, ver, flags, cap, epoch, _ext = P.parse_ext(f)
        self.assertTrue(is_v1)
        self.assertEqual(ver, P.EXT_VER)
        self.assertEqual(cap, P.CAP_ALL)
        self.assertEqual(epoch, 42)
        self.assertTrue(flags & P.FLAG_EPOCH)
        self.assertTrue(P.FrameCodec.verify_ext(SECRET, f))

    def test_ext_tamper_rejected(self):
        f = bytearray(P.FrameCodec.pack_heartbeat(
            SECRET, now(), 20.0, 1, cap=P.CAP_ALL, epoch=7))
        f[P.EXT_OFF + 8] ^= 0x01          # 改 epoch
        self.assertFalse(P.FrameCodec.verify_ext(SECRET, bytes(f)))

    def test_rpc_frame_signature_and_layout(self):
        env = R.make_request("zcore.sync", {"since": 0}, 1)
        body = R.encode(env)
        f = P.FrameCodec.pack_rpc(SECRET, now(), 3, 20.0, 1,
                                  P.REQ_TYPE, body, epoch=9)
        plen = struct.unpack_from(">I", f, P.PLEN_OFF)[0]
        self.assertEqual(plen, P.RPC_SIG_LEN + len(body))
        self.assertEqual(len(f), P.HEADER_LEN + plen)
        sig, parsed_body = P.FrameCodec.rpc_payload(f)
        self.assertEqual(len(sig), P.RPC_SIG_LEN)
        self.assertEqual(parsed_body, body)
        self.assertTrue(P.FrameCodec.verify_rpc(SECRET, f))
        self.assertTrue(P.FrameCodec.verify_ext(SECRET, f))
        bad = bytearray(f)
        bad[P.HEADER_LEN + P.RPC_SIG_LEN + 2] ^= 0x01   # 篡改 JSON 正文
        self.assertFalse(P.FrameCodec.verify_rpc(SECRET, bytes(bad)))


class TestJsonRpcLayer(unittest.TestCase):
    def test_id_allocator_parity(self):
        c, s = R.IdAllocator("client"), R.IdAllocator("server")
        self.assertEqual([c.next() for _ in range(4)], [1, 3, 5, 7])
        self.assertEqual([s.next() for _ in range(4)], [2, 4, 6, 8])
        self.assertTrue(c.owns(7))
        self.assertFalse(c.owns(8))
        self.assertTrue(s.owns(8))
        self.assertFalse(s.owns(0))
        with self.assertRaises(ValueError):
            R.IdAllocator("peer")

    def test_envelope_kinds(self):
        self.assertEqual(R.kind(R.make_request("m", {}, 1)), "request")
        self.assertEqual(R.kind(R.make_notification("m", {})), "notification")
        self.assertEqual(R.kind(R.make_result(2, {"ok": True})), "response")
        self.assertEqual(R.kind({"jsonrpc": "2.0"}), "invalid")
        self.assertIsNone(R.decode(b"not json")[0])
        self.assertEqual(R.decode(b'{"x":1}')[1], R.INVALID_REQUEST)

    def test_notification_must_not_reply(self):
        sent = []

        async def send(peer, ftype, env):
            sent.append((ftype, env))

        hub = R.RpcHub("server", send)
        LOOP.run_until_complete(hub.on_envelope(
            None, R.make_notification("zcore.pass.changed", {"epoch": 1})))
        self.assertEqual(sent, [])       # JSON-RPC 2.0 §4.1

    def test_method_not_found_and_idempotent_cache(self):
        sent = []

        async def send(peer, ftype, env):
            sent.append(env)

        hub = R.RpcHub("server", send)
        req = R.make_request("not.exist", {}, 3)
        LOOP.run_until_complete(hub.on_envelope(None, req))
        self.assertEqual(sent[0]["error"]["code"], R.METHOD_NOT_FOUND)

        hub.register_method("echo", lambda params: params)
        req2 = R.make_request("echo", {"v": 1}, 5)
        LOOP.run_until_complete(hub.on_envelope(None, req2))
        LOOP.run_until_complete(hub.on_envelope(None, req2))   # 重发同 id
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1], sent[2])                     # 幂等返回同一结果
        self.assertEqual(sent[1]["result"], {"v": 1})

    def test_fail_closed_pass(self):
        sent = []

        async def send(peer, ftype, env):
            sent.append(env)

        hub = R.RpcHub("server", send)
        LOOP.run_until_complete(hub.on_envelope(
            None, R.make_request("zcore.pass.query", {"player": "Steve"}, 1)))
        self.assertEqual(sent[0]["result"], R.PASS_PENDING)

        def broken(params):
            raise RuntimeError("db down")

        hub.register_method("zcore.pass.query", broken)
        LOOP.run_until_complete(hub.on_envelope(
            None, R.make_request("zcore.pass.query", {"player": "Alex"}, 3)))
        self.assertEqual(sent[1]["result"], R.PASS_PENDING)   # 异常也绝不放行

    def test_sync_incremental_and_410(self):
        adapter = ml.McLinkAdapter(FakeFramework(), {"secret": SECRET.decode()})
        LOOP.run_until_complete(adapter.bump_epoch({"player": "A"}))
        LOOP.run_until_complete(adapter.bump_epoch({"player": "B"}))
        res = adapter._rpc_sync({"since": 1})
        self.assertEqual(res["epoch"], 2)
        self.assertEqual([x["player"] for x in res["items"]], ["B"])
        self.assertFalse(res["full"])
        full = adapter._rpc_sync({"since": 0})
        self.assertTrue(full["full"])
        self.assertEqual(len(full["items"]), 2)

        # 模拟历史窗口被裁剪：等价 Kubernetes 410 Gone，要求对端全量重拉
        adapter._epoch = 100
        adapter._changes = deque([(95, {"player": "C"})], maxlen=1024)
        with self.assertRaises(R.RpcFailure) as ctx:
            adapter._rpc_sync({"since": 1})
        self.assertEqual(ctx.exception.code, R.EPOCH_TOO_OLD)


class TestV1Link(unittest.TestCase):
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

    async def _read_type(self, reader, ftype, timeout=2.0):
        """按帧切分读取，跳过 BOOKMARK 等其它类型，直到拿到目标帧。"""
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = await asyncio.wait_for(
                reader.read(8192), max(0.1, deadline - time.time()))
            if not chunk:
                break
            buf.extend(chunk)
            while buf:
                t = buf[0]
                if t == P.HEARTBEAT_TYPE:
                    total = P.HEADER_LEN
                else:
                    if len(buf) < P.HEADER_LEN:
                        break
                    plen = int.from_bytes(buf[P.PLEN_OFF:P.PLEN_OFF + 4], "big")
                    total = P.HEADER_LEN + plen
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                del buf[:total]
                if t == ftype:
                    return frame
        self.fail(f"未收到 0x{ftype:02x} 帧")

    def test_capability_hello_and_rpc_roundtrip(self):
        async def scenario():
            reader, writer = await self._connect()
            # v1 首帧心跳：声明 cap（RSocket SETUP / WAMP HELLO 语义）
            writer.write(P.FrameCodec.pack_heartbeat(
                SECRET, now(), 20.0, 2, cap=P.CAP_ALL, epoch=0))
            await writer.drain()
            await asyncio.sleep(0.15)
            peer = next(iter(self.adapter._peers.values()))
            self.assertEqual(peer.cap, P.CAP_ALL)

            env = R.make_request("rpc.capabilities", {}, 1)   # Java 侧奇数 id
            writer.write(P.FrameCodec.pack_rpc(
                SECRET, now(), 1, 20.0, 2, P.REQ_TYPE, R.encode(env), epoch=0))
            await writer.drain()
            raw = await self._read_type(reader, P.RESP_TYPE)
            self.assertEqual(raw[0], P.RESP_TYPE)
            self.assertTrue(P.FrameCodec.verify_rpc(SECRET, raw))
            _sig, body = P.FrameCodec.rpc_payload(raw)
            answer, err = R.decode(body)
            self.assertEqual(err, 0)
            self.assertEqual(answer["id"], 1)
            self.assertIn("zcore.sync", answer["result"]["methods"])
            self.assertEqual(answer["result"]["role"], "server")
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_capability_gate_when_peer_is_v0(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 1))
            await writer.drain()
            await asyncio.sleep(0.15)
            res = await self.adapter.call_rpc("zcore.sync", {"since": 0})
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"]["code"], R.PASS_UNAVAILABLE)
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_unknown_frame_type_skipped_as_whole(self):
        async def scenario():
            reader, writer = await self._connect()
            unknown = bytearray(P.HEADER_LEN + 5)
            unknown[0] = 0x7F
            struct.pack_into(">I", unknown, P.PLEN_OFF, 5)
            unknown[P.HEADER_LEN:] = b"hello"
            writer.write(bytes(unknown))
            writer.write(P.FrameCodec.pack_heartbeat(SECRET, now(), 20.0, 6))
            await writer.drain()
            deadline = time.time() + 2
            while time.time() < deadline:
                if any(e.get("notice_type") == "mc_heartbeat" for e in self.fw.events):
                    break
                await asyncio.sleep(0.02)
            evs = [e for e in self.fw.events if e.get("notice_type") == "mc_heartbeat"]
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["players"], 6)
            writer.close()
            await writer.wait_closed()
        self._test(scenario())

    def test_rpc_replay_seq_dropped(self):
        async def scenario():
            reader, writer = await self._connect()
            writer.write(P.FrameCodec.pack_heartbeat(
                SECRET, now(), 20.0, 1, cap=P.CAP_ALL, epoch=0))
            await writer.drain()
            await asyncio.sleep(0.12)
            env = R.make_request("rpc.capabilities", {}, 1)
            frame = P.FrameCodec.pack_rpc(
                SECRET, now(), 5, 20.0, 1, P.REQ_TYPE, R.encode(env), epoch=0)
            writer.write(frame)
            await writer.drain()
            await asyncio.wait_for(reader.read(8192), 2)
            writer.write(frame)          # 同 seq 重放
            await writer.drain()
            await asyncio.sleep(0.2)
            peer = next(iter(self.adapter._peers.values()))
            self.assertEqual(peer.rpc_rx_seq, 5)
            writer.close()
            await writer.wait_closed()
        self._test(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)

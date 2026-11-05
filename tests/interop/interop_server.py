# -*- coding: utf-8 -*-
"""跨语言互操作验证的服务端：起真实 mc_link 适配器，生成 golden 帧并把端口写盘。

Java 侧的 `InteropSmoke` 会在本进程存活期内连上来做逐字节比对与真实 RPC 往返。
第三端（zcore pass 签发者）也在这里挂上去，用来验证 `zcore.pass.query`
走的是真实签发者而不是 fail-closed 占位。

用法：
    python tests/interop/interop_server.py [--timeout 180]
"""
import argparse
import asyncio
import importlib.util
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = os.path.join(HERE, ".tmp")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "plugins", "minecraftconsole"))

SECRET = b"interop-test-secret"
APPROVED_PLAYER = "InteropApproved"
UNKNOWN_PLAYER = "InteropUnknown"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ml = load("mc_link_main", os.path.join(ROOT, "core_plugins", "mc_link", "main.py"))
P, R = ml.P, ml.R
zcore_pass = load("zcore_pass", os.path.join(ROOT, "plugins", "minecraftconsole",
                                             "zcore_pass.py"))


class FakeBus:
    async def aemit(self, *a, **kw):
        return False


class FakeServices:
    def __init__(self):
        self.data = {}

    def get(self, name, default=None):
        return self.data.get(name, default)

    def register(self, name, service):
        self.data[name] = service


class FakeFramework:
    def __init__(self, loop):
        self.loop = loop
        self.config = {}
        self.services = FakeServices()
        self.event_bus = FakeBus()
        self.events = []

    async def dispatch_event(self, event, wait=False):
        self.events.append(event)


def hexs(b):
    return b.hex()


def gen_golden():
    """生成 v0 / v1 / REQ / RESP 的确定性字节，供 Java 侧逐字节比对。"""
    ts = 1700000000
    lines = []
    lines.append("v0_heartbeat:" + hexs(P.FrameCodec.pack_heartbeat(SECRET, ts, 19.8, 3)))
    lines.append("v1_heartbeat:" + hexs(P.FrameCodec.pack_heartbeat(
        SECRET, ts, 19.8, 3, cap=P.CAP_ALL, epoch=42)))
    env = R.make_request("zcore.sync", {"since": 0}, 1)
    lines.append("req:" + hexs(P.FrameCodec.pack_rpc(
        SECRET, ts, 7, 19.8, 3, P.REQ_TYPE, R.encode(env), epoch=42)))
    lines.append("resp:" + hexs(P.FrameCodec.pack_rpc(
        SECRET, ts, 8, 19.8, 3, P.RESP_TYPE,
        R.encode(R.make_result(1, {"epoch": 42, "items": []})), epoch=42)))
    lines.append("req_json:" + R.encode(env).decode("utf-8"))
    with open(os.path.join(TMP, "golden.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("golden written:", len(lines), "lines", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    for stale in ("port.txt", "golden.txt"):
        p = os.path.join(TMP, stale)
        if os.path.exists(p):
            os.remove(p)

    gen_golden()
    loop = asyncio.get_running_loop()
    fw = FakeFramework(loop)
    adapter = ml.McLinkAdapter(fw, {"host": "127.0.0.1", "port": 0,
                                    "secret": SECRET.decode()})
    server = await adapter.start_async()
    port = server.sockets[0].getsockname()[1]

    # 第三端：挂真实签发者，覆盖 mc_link 的 fail-closed 占位
    store = zcore_pass.PassStore(os.path.join(TMP, "zcore_pass.json"))
    signer = zcore_pass.ZcorePassSigner(store)
    signer.attach(adapter)
    await signer.issue(APPROVED_PLAYER, "approved", issued_by="interop", ttl=3600)

    with open(os.path.join(TMP, "port.txt"), "w", encoding="utf-8") as f:
        f.write(str(port))
    print("server listening", port, "epoch", adapter._epoch, flush=True)
    try:
        await asyncio.sleep(args.timeout)
    finally:
        await adapter.stop()
        print("server stopped", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

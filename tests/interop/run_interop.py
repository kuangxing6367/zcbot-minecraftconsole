# -*- coding: utf-8 -*-
"""跨语言互操作验证的一键入口。

流程：
  1. 确保 Java agent 已编译（target/classes 不存在时调 mvn package）
  2. 后台起真实 mc_link 适配器（interop_server.py），等它写出 port.txt
  3. javac 编译 InteropSmoke.java，再用同一个 JDK 跑
  4. 结束服务端进程，透传 Java 侧退出码

用法：
    python tests/interop/run_interop.py
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TMP = os.path.join(HERE, ".tmp")
CLASSES = os.path.join(HERE, "classes")
JAVA_ROOT = os.path.join(ROOT, "mc-agent-java")
JAVA_CLASSES = os.path.join(JAVA_ROOT, "target", "classes")


def which(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"找不到 {name}，请先安装并加入 PATH")
    return path


def ensure_java_build():
    if os.path.isfile(os.path.join(JAVA_CLASSES, "com", "zgric", "mcagent", "FrameCodec.class")):
        return
    print("[interop] target/classes 缺失，执行 mvn package ...", flush=True)
    proc = subprocess.run([which("mvn"), "-B", "-ntp", "package", "-DskipTests"],
                          cwd=JAVA_ROOT)
    if proc.returncode != 0:
        raise SystemExit("mvn package 失败")


def wait_port(timeout=60.0):
    path = os.path.join(TMP, "port.txt")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(0.2)
    return False


def main():
    ensure_java_build()
    os.makedirs(CLASSES, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    server = subprocess.Popen([sys.executable, os.path.join(HERE, "interop_server.py"),
                               "--timeout", "150"], cwd=ROOT, env=env)
    try:
        if not wait_port():
            raise SystemExit("服务端未在超时内写出 port.txt")
        print("[interop] 服务端就绪", flush=True)

        sep = ";" if os.name == "nt" else ":"
        subprocess.run([which("javac"), "-encoding", "UTF-8", "-cp", JAVA_CLASSES,
                        "-d", CLASSES, os.path.join(HERE, "InteropSmoke.java")],
                       check=True)
        cp = JAVA_CLASSES + sep + CLASSES
        rc = subprocess.run([which("java"), "-Dfile.encoding=UTF-8", "-cp", cp,
                             "InteropSmoke", "."], cwd=TMP, env=env).returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    return rc


if __name__ == "__main__":
    sys.exit(main())

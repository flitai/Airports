"""守护进程入口：python -m sdrd [--source ...] [--port N] [--parent-pipe]

平时由 serve.py --sdr 拉起；也可以手动启动做开发，例如不插接收机：
    python -m sdrd --source synth:adsb:40.08,116.58
"""
import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import threading

from aiohttp import web

from .config import Config
from .engine import Engine
from .server import Server

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser(prog="sdrd", description="Airports 本地接收机守护进程")
    # 环境变量 SDR_SOURCE 是给「由 serve.py 拉起」的场合换采样源用的，比如无硬件验收
    ap.add_argument("--source", default=os.environ.get("SDR_SOURCE", "rtl"),
                    help="rtl | file:<cu8 路径> | synth:adsb[:纬,经]")
    ap.add_argument("--port", type=int, default=0, help="监听端口，0 为自动挑选")
    ap.add_argument("--runtime-dir", default=os.path.join(ROOT, ".runtime"))
    ap.add_argument("--allow-origin", action="append", default=[],
                    help="允许连接的页面来源，可重复；不给则放行任意本机来源")
    ap.add_argument("--parent-pipe", action="store_true",
                    help="标准输入一关闭就退出，用来跟随父进程的生死")
    args = ap.parse_args()

    os.makedirs(args.runtime_dir, exist_ok=True)
    # 接收机只有一台，守护进程也只能有一个。用文件锁而不是「看 pid 文件」：
    # 进程无论怎么死，内核都会替它放锁，不会留下骗人的旧 pid。
    lock = open(os.path.join(args.runtime_dir, "sdrd.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"ready": False, "error": "已有一个 sdrd 在运行"}), flush=True)
        return 2

    cfg = Config(os.path.join(args.runtime_dir, "sdr.json"))
    server = Server(None, args.allow_origin)
    engine = Engine(cfg, args.source, args.runtime_dir, notify=server.notify)
    server.engine = engine
    info_path = os.path.join(args.runtime_dir, "sdrd.json")

    async def run():
        loop = asyncio.get_running_loop()
        server.loop = loop
        done = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, done.set)
        if args.parent_pipe:
            # 父进程被 kill -9 时来不及给我们发信号，但它那头的管道会随之关闭。
            # 读到文件结束就是「父进程没了」，这条路不依赖任何信号能否送达。
            def watch():
                try:
                    while sys.stdin.buffer.read(4096):
                        pass
                except OSError:
                    pass
                loop.call_soon_threadsafe(done.set)
            threading.Thread(target=watch, name="lifeline", daemon=True).start()

        # shutdown_timeout：aiohttp 收尾时默认等在途连接 60 秒，见下面 close_clients 的说明
        runner = web.AppRunner(server.app, access_log=None, shutdown_timeout=1.0)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", args.port)   # 只听本机，绝不对局域网开放
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        engine.start()
        with open(info_path, "w") as f:                      # 手动启动时 serve.py 靠它找到我们
            json.dump({"port": port, "pid": os.getpid()}, f)
        print(json.dumps({"ready": True, "port": port, "pid": os.getpid()}), flush=True)
        await done.wait()
        # 先把页面连接主动断掉再收尾。WebSocket 是长连接，页面不会自己断；
        # 不这么做的话 aiohttp 会一直等它，我们就迟迟走不到关设备那一步——
        # 实测页面开着时退出要拖到被父进程强杀，Bias-T 都来不及关。
        await server.close_clients()
        await runner.cleanup()

    try:
        asyncio.run(run())
    finally:
        engine.stop()                                        # 关设备、关 Bias-T、收掉 readsb
        try:
            os.remove(info_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""HTTP 与 WebSocket 服务。

页面通过一条 WebSocket 拿到全部状态并下发命令。服务端只推完整快照、不推增量：
每秒一份飞机列表也就几 KB，换来的是页面不必维护「我漏了哪条增量」这种状态，
断线重连后第一份快照就是全部真相。

安全：这个端口能操纵一台无线电接收机，还知道接收机位置，不能让随便哪个网页
连上来。浏览器发起 WebSocket 时必带 Origin 且脚本无法伪造，所以查 Origin 就够
挡住别的网站；再查 Host 是为了挡 DNS 重绑定（恶意域名解析到 127.0.0.1）。
"""
import asyncio
import collections
import json

from aiohttp import WSMsgType, web

from . import PROTO, __version__

# 发送优先级：音频永远最先（单独一条队列，见 Client），其余按这张表，数值小的先发。
# 将来的频谱排在最后。
PRIORITY = {"status": 1, "channels": 2, "aircraft": 3, "outline": 4}
AUDIO_BACKLOG = 25                       # 每个页面最多积压 25 帧 = 500 毫秒，再多就丢最旧的
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _host_ok(host):
    """去掉端口后是不是本机名。IPv6 字面量带方括号，要先按 ] 截。"""
    host = host or ""
    name = host[:host.index("]") + 1] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
    return name in LOCAL_HOSTS


def origin_allowed(origin, allowed):
    """allowed 为空表示「任意本机来源」（手动启动守护进程做开发时用）。

    没有 Origin 头的是非浏览器客户端（测试脚本、curl），放行：要防的是浏览器里
    别的网站，而浏览器一定会带这个头。"null" 来源（file:// 页面、沙箱 iframe）
    一律拒绝，它无法和恶意页面区分开。
    """
    if origin is None:
        return True
    if origin == "null":
        return False
    if allowed:
        return origin in allowed
    rest = origin.split("://", 1)[-1]
    return origin.startswith("http://") and _host_ok(rest)


class Client:
    """一个页面连接。快照每类只留最新一份，按优先级发；音频另有一条有上限的队列。

    标签页被挂起时浏览器不再读数据。如果给每个连接配一个无界队列，守护进程的
    内存会被一个睡着的标签页慢慢撑大；「每类只留最新」和「音频最多 500 毫秒」
    让积压有上界。音频满了丢最旧的：迟到半秒的话音不如不要，留着只会越积越晚。
    """
    def __init__(self, ws):
        self.ws = ws
        self.pending = {}
        self.audio = collections.deque(maxlen=AUDIO_BACKLOG)
        self.wants_audio = False         # 这个页面开了声音没有
        self.wake = asyncio.Event()

    def offer(self, kind, payload):
        self.pending[kind] = payload
        self.wake.set()

    def offer_audio(self, frame):
        self.audio.append(frame)
        self.wake.set()

    async def pump(self):
        while not self.ws.closed:
            await self.wake.wait()
            self.wake.clear()
            while self.audio or self.pending:
                if self.audio:
                    await self.ws.send_bytes(self.audio.popleft())
                    continue
                kind = min(self.pending, key=lambda k: PRIORITY.get(k, 9))
                await self.ws.send_str(json.dumps(self.pending.pop(kind), ensure_ascii=False,
                                                  separators=(",", ":")))


class Server:
    def __init__(self, engine, allowed_origins=()):
        self.engine = engine
        self.allowed = set(allowed_origins)
        self.clients = set()
        self.loop = None
        self.app = web.Application()
        self.app.add_routes([web.get("/ws", self.ws_handler), web.get("/health", self.health)])

    # 引擎线程调用：把「这几类快照更新了」转交给事件循环
    def notify(self, kinds):
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._fanout, kinds)

    def _fanout(self, kinds):
        for kind in kinds:
            payload = self.engine.snapshot(kind)
            for c in self.clients:
                c.offer(kind, payload)

    # sdr-dsp 线程调用：一帧音频。只发给开了声音的页面
    def audio(self, frame):
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._audio_fanout, frame)

    def _audio_fanout(self, frame):
        for c in self.clients:
            if c.wants_audio:
                c.offer_audio(frame)

    def _count_listeners(self):
        # 引擎靠这个数决定「没人听了，回去收 ADS-B」
        self.engine.listeners = sum(1 for c in self.clients if c.wants_audio)

    def _guard(self, request):
        if not _host_ok(request.headers.get("Host")):
            raise web.HTTPForbidden(text="bad host")
        if not origin_allowed(request.headers.get("Origin"), self.allowed):
            raise web.HTTPForbidden(text="origin not allowed")

    async def close_clients(self):
        for c in list(self.clients):
            try:
                await asyncio.wait_for(c.ws.close(code=1001, message=b"shutdown"), 1.0)
            except Exception:                    # 对端已经没了也无所谓，目的只是别等它
                pass

    async def health(self, request):
        self._guard(request)
        return web.json_response({"ok": True, "version": __version__, "proto": PROTO,
                                  "status": self.engine.snapshot("status")})

    async def ws_handler(self, request):
        self._guard(request)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=65536)
        await ws.prepare(request)
        client = Client(ws)
        await ws.send_str(json.dumps({"t": "hello", "proto": PROTO, "version": __version__,
                                      "modes": ["off", "adsb", "air"]}))
        for kind in ("status", "outline", "aircraft", "channels"):   # 连上就给全量，不用等下一拍
            client.offer(kind, self.engine.snapshot(kind))
        self.clients.add(client)
        pump = asyncio.ensure_future(client.pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    op = json.loads(msg.data)
                    if not isinstance(op, dict):
                        raise ValueError
                except ValueError:
                    continue
                if op.get("op") == "audio":                  # 开关声音是这条连接自己的事，不进引擎
                    client.wants_audio = bool(op.get("on"))
                    if not client.wants_audio:
                        client.audio.clear()
                    self._count_listeners()
                    await ws.send_str(json.dumps({"t": "ack", "id": op.get("id"), "ok": True}))
                    continue
                result = await asyncio.wrap_future(self.engine.command(op))
                await ws.send_str(json.dumps(dict(result, t="ack", id=op.get("id")),
                                             ensure_ascii=False))
        finally:
            self.clients.discard(client)
            self._count_listeners()
            pump.cancel()
        return ws

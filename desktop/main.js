// Airports · 桌面外壳
// SPDX-License-Identifier: GPL-3.0-only
//
// 这个外壳刻意做得很薄，只管三件事：
//   1. 把 serve.py 拉起来（它再拉起接收机守护进程），等它报出端口
//   2. 开一个窗口加载那个地址——页面和浏览器里跑的是同一份，一行不改
//   3. 自己退出时把整条进程链带走
// 射频、解调、地图逻辑一概不在这里。不装 Electron 的人用浏览器加 serve.py，
// 得到的是完全相同的界面。
//
// 「带走进程链」不靠信号，靠一根管道：serve.py 的标准输入连着我们，我们一没
// （包括被强杀，来不及做任何清理）管道就断，serve.py 读到文件结束便自行退出，
// 守护进程和解码器再用同样的办法逐级跟着走。所以这里的退出代码只是让它走得
// 体面一点，不是它走得掉的前提。
"use strict";
if (process.env.ELECTRON_RUN_AS_NODE) {
  // VS Code 的扩展宿主会给它派生的进程带上这个变量，Electron 见了就把自己当成
  // 普通 Node 跑，不开窗口，接下来的报错也完全看不出原因。直接说清楚。
  console.error("环境里设置了 ELECTRON_RUN_AS_NODE，Electron 不会以应用方式启动。\n" +
                "请在普通终端里运行，或先执行 unset ELECTRON_RUN_AS_NODE。");
  process.exit(1);
}
const { app, BrowserWindow, Menu, shell, session, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

// 从仓库里 npm start 时，页面和 serve.py 就在上一级目录。打包成应用后（见
// build-app.sh）它们被拷进应用包的 Resources/airports 里。
const ROOT = app.isPackaged ? path.join(process.resourcesPath, "airports") : path.join(__dirname, "..");
const WITH_SDR = !process.argv.includes("--no-sdr");

// 打包后的应用不往自己的包里写东西：设置、接收机的运行期状态、Python 依赖都放在
// 用户数据目录（~/Library/Application Support/Airports）。应用包因此可以放在只读
// 的地方，换新版本时这些也不会丢。
const DATA = app.getPath("userData");
const settingsFile = path.join(DATA, "settings.json");
function loadSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile, "utf8")) || {}; } catch (_e) { return {}; }
}
function saveSettings(s) {
  try { fs.mkdirSync(DATA, { recursive: true }); fs.writeFileSync(settingsFile, JSON.stringify(s, null, 1)); }
  catch (_e) { /* 记不住就算了 */ }
}
const PORT = 8000;                    // 起点；被占用时 serve.py 会自己顺延，以它报回来的为准

let win = null, child = null, baseUrl = null;
let quitting = false, childGone = false;
const tail = [];                      // serve.py 最近的输出，出事时给用户看

// ---------- 只允许一个实例 ----------
// 接收机只有一台。第二个实例什么也干不了，直接把已有窗口叫到前面。
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });
  app.whenReady().then(start);
}

// ---------- serve.py ----------
function start() {
  buildMenu();
  // 页面不需要摄像头、定位、通知之类的任何权限，一律拒绝
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));

  // 从访达或程序坞启动时拿不到终端里的 PATH，Homebrew 和 uv 的目录要自己补上
  const extra = ["/opt/homebrew/bin", "/usr/local/bin", path.join(os.homedir(), ".local/bin")];
  const env = Object.assign({}, process.env, {
    PATH: extra.concat((process.env.PATH || "").split(path.delimiter)).join(path.delimiter),
  });
  if (app.isPackaged) {
    env.AIRPORTS_RUNTIME = path.join(DATA, "runtime");
    env.UV_PROJECT_ENVIRONMENT = path.join(DATA, "sdr-venv");
    env.PYTHONDONTWRITEBYTECODE = "1";            // 别在应用包里留 __pycache__
  }
  // 离线瓦片有一百多 GB，不在应用包里。用户在菜单里选过目录就用那个目录
  const tiles = loadSettings().tiles;
  if (tiles && fs.existsSync(tiles)) env.AIRPORTS_TILES = tiles;
  const args = ["-u", "serve.py", String(PORT), "--parent-pipe"];   // -u：不加的话就绪行会留在缓冲区里出不来
  if (WITH_SDR) args.push("--sdr");
  child = spawn("python3", args, { cwd: ROOT, env, stdio: ["pipe", "pipe", "pipe"] });

  let buf = "";
  const onData = (chunk) => {
    // 两路输出都必须一直读：没人读的管道写满之后，serve.py 会卡在 print 上
    buf += chunk.toString("utf8");
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      tail.push(line);
      if (tail.length > 30) tail.shift();
      if (!baseUrl && line.startsWith("{")) {
        try {
          const j = JSON.parse(line);
          if (j.ready && j.port) {
            // 用 127.0.0.1 而不是 localhost：守护进程只放行这两个来源，而 localhost
            // 在有些机器上先解析成 IPv6
            baseUrl = "http://127.0.0.1:" + j.port + "/";
            createWindow();
          }
        } catch (_e) { /* 不是就绪行 */ }
      }
    }
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);
  child.stdin.on("error", () => {});            // 子进程先走时，往它的管道收尾会报 EPIPE，不算事
  child.on("error", (err) => fail("起不了 python3：" + err.message));
  child.on("exit", (code, signal) => {
    childGone = true;
    if (quitting) { app.quit(); return; }
    fail("serve.py 意外退出（" + (signal || "退出码 " + code) + "）");
  });
}

function stopChild() {
  if (!child || childGone) return;
  try { child.stdin.end(); } catch (_e) { /* 已经关了 */ }
  // 正常情况下它 1 秒左右就走了。下面两步只是它卡住时的后手。
  setTimeout(() => { if (!childGone) child.kill("SIGTERM"); }, 5000);
  setTimeout(() => { if (!childGone) child.kill("SIGKILL"); }, 8000);
}

// ---------- 窗口 ----------
const boundsFile = () => path.join(app.getPath("userData"), "window.json");

function createWindow() {
  let saved = {};
  try { saved = JSON.parse(fs.readFileSync(boundsFile(), "utf8")); } catch (_e) { /* 首次运行 */ }
  win = new BrowserWindow({
    width: saved.width || 1440, height: saved.height || 900, x: saved.x, y: saved.y,
    minWidth: 900, minHeight: 600, title: "Airports", backgroundColor: "#eef1f5",
    // 页面是普通网页，不给它任何 Node 能力，也不需要预加载脚本
    webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false },
  });
  win.on("close", () => {
    try { fs.writeFileSync(boundsFile(), JSON.stringify(win.getNormalBounds())); } catch (_e) { /* 记不住就算了 */ }
  });
  win.on("closed", () => { win = null; });

  // 窗口里只待我们自己的页面。数据源、航图、ChartFox 这些外链交给系统浏览器。
  const ours = (u) => u.startsWith(baseUrl);
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (ours(url)) return;
    e.preventDefault();
    if (/^https?:/.test(url)) shell.openExternal(url);
  });
  win.loadURL(baseUrl);
}

function fail(message) {
  if (quitting) return;
  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const html = "<meta charset=utf-8><body style='font:14px/1.6 -apple-system,sans-serif;padding:32px;color:#1c2128'>" +
    "<h2 style='font-size:17px'>" + esc(message) + "</h2>" +
    "<p style='color:#6b7280'>下面是它最后的输出。需要 python3；接收机功能还需要 uv、librtlsdr 和 readsb，见 README。</p>" +
    "<pre style='background:#f7f8fa;padding:14px;border-radius:8px;white-space:pre-wrap'>" +
    esc(tail.join("\n") || "（没有输出）") + "</pre>";
  if (!win) {
    win = new BrowserWindow({ width: 760, height: 520, title: "Airports",
                              webPreferences: { contextIsolation: true, sandbox: true } });
    win.on("closed", () => { win = null; });
  }
  win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
}

// ---------- 菜单 ----------
// 菜单项不直接操作接收机，只往页面派发一个事件，页面收到后走的是和点侧栏、
// 按快捷键完全相同的路径。这样外壳里没有第二套接收机逻辑。
function toPage(name, arg) {
  if (!win || !baseUrl) return;
  win.webContents.executeJavaScript(
    "window.dispatchEvent(new CustomEvent('sdr-command',{detail:" + JSON.stringify({ name, arg }) + "}))"
  ).catch(() => {});
}

// 选离线瓦片所在的目录（里面放着 planet.pmtiles 和 dem/）。serve.py 是带着这个路径
// 启动的，所以选完要重启一次才生效。
function chooseTiles() {
  const s = loadSettings();
  dialog.showOpenDialog(win, {
    title: "选择离线瓦片目录", message: "选择放着 planet.pmtiles（和 dem/）的那个目录",
    defaultPath: s.tiles || os.homedir(), properties: ["openDirectory"],
  }).then((r) => {
    if (r.canceled || !r.filePaths[0]) return;
    s.tiles = r.filePaths[0];
    saveSettings(s);
    app.relaunch();
    app.quit();
  });
}
function forgetTiles() {
  const s = loadSettings();
  delete s.tiles;
  saveSettings(s);
  app.relaunch();
  app.quit();
}

function buildMenu() {
  const tpl = [];
  if (process.platform === "darwin") tpl.push({ role: "appMenu" });
  tpl.push({ role: "editMenu" });               // 搜索框里的复制粘贴靠它
  tpl.push({ role: "viewMenu" });
  const tilesNow = loadSettings().tiles;
  tpl.push({ label: "底图", submenu: [
    { label: "选择离线瓦片目录…", click: chooseTiles },
    { label: "不使用外部瓦片目录", enabled: !!tilesNow, click: forgetTiles },
    { type: "separator" },
    { label: tilesNow ? "当前：" + tilesNow : (app.isPackaged ? "当前：未选择，离线底图为内置简图" : "当前：仓库里的 tiles/"),
      enabled: false },
  ] });
  if (WITH_SDR) {
    tpl.push({ label: "接收机", submenu: [
      { label: "接收 ADS-B", accelerator: "CmdOrCtrl+1", click: () => toPage("mode", "adsb") },
      { label: "回到话音监听", accelerator: "CmdOrCtrl+2", click: () => toPage("mode", "air") },
      { label: "关闭并释放设备", accelerator: "CmdOrCtrl+.", click: () => toPage("mode", "off") },
      { type: "separator" },
      { label: "频谱与瀑布图", accelerator: "CmdOrCtrl+3", click: () => toPage("view") },
      { label: "设定接收机位置…", click: () => toPage("pick", true) },
    ] });
  }
  tpl.push({ role: "windowMenu" });
  Menu.setApplicationMenu(Menu.buildFromTemplate(tpl));
}

// ---------- 退出 ----------
// 关掉窗口就退出，macOS 上也一样。按惯例 Mac 应用关窗后留在程序坞里，但那样
// 接收机会被一个看不见的进程一直占着，别的 SDR 软件打不开它，用户还找不到原因。
app.on("window-all-closed", () => app.quit());
app.on("before-quit", (e) => {
  if (childGone || !child) return;
  // 等 serve.py 走了再真正退出：它走了，文件锁和接收机才算让出来，
  // 用户退出后马上重开才不会撞上「已有一个在运行」。
  e.preventDefault();
  if (quitting) return;
  quitting = true;
  stopChild();
});
process.on("SIGTERM", () => app.quit());
process.on("SIGINT", () => app.quit());

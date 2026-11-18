// Airports · 本地接收机界面
// SPDX-License-Identifier: GPL-3.0-only
//
// 这个文件只在 serve.py 报告「接收机已启用」时由 index.html 注入。它通过
// window.__app 拿到地图、飞机状态表和几个钩子，自己不碰主页面的任何内部变量。
//
// 设计上只有一条规矩：界面不保存接收机的状态。守护进程每秒推一份完整快照，
// 这里把快照画出来；用户点了什么就发一条命令，然后等下一份快照告诉我们结果。
// 这样浏览器和 Electron 两个窗口同时开着也不会各说各话，断线重连后第一份
// 快照就是全部真相。
(function(){
"use strict";
var app = window.__app;
if (!app || window.__sdr) return;
var map = app.map(), el = app.el, fmt = app.fmt;

var FRESH_S = 15;                 // 本地位置超过这么久没更新就不再上图，让联网数据接手
var RINGS_NM = [50, 100, 150, 200];
var ws = null, port = null, link = "connecting", linkWhy = "", retryMs = 1000;
var st = null, outline = null, picking = false, seq = 0;
var pillText = "", lastLocal = 0;

// ---------- 连接 ----------
// 先问 serve.py 守护进程在哪个端口。首次启用时它可能还在装依赖，没端口就过会儿再问。
function discover(){
  fetch("api/sdr").then(function(r){ return r.json(); }).then(function(j){
    if (j.port){ port = j.port; connect(); return; }
    link = (j.state === "failed" || j.state === "exited") ? "down" : "connecting";
    linkWhy = j.state === "installing" ? "正在安装依赖，约一分钟" : (j.reason || "");
    render();
    setTimeout(discover, 2000);
  }).catch(function(){ link = "down"; linkWhy = "serve.py 没有响应"; render(); setTimeout(discover, 3000); });
}

function connect(){
  // 固定连 127.0.0.1 而不是页面的主机名：守护进程只监听 IPv4 回环，
  // 而 localhost 在有些机器上先解析成 ::1
  ws = new WebSocket("ws://127.0.0.1:" + port + "/ws");
  ws.onopen = function(){ link = "up"; retryMs = 1000; render(); };
  ws.onmessage = function(ev){
    var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.t === "status"){ st = m; onStatus(); }
    else if (m.t === "aircraft") onAircraft(m.list || []);
    else if (m.t === "outline"){ outline = m; drawHome(); }
    else if (m.t === "ack" && m.ok === false) flash(m.msg || "命令被拒绝");
  };
  ws.onclose = function(){
    ws = null; link = "connecting"; linkWhy = ""; st = null;
    onAircraft([]);                       // 连不上就别让旧飞机挂在图上装作还在收
    render();
    setTimeout(discover, retryMs);        // 重新发现：守护进程重启后端口会变
    retryMs = Math.min(8000, retryMs * 2);
  };
}

function send(op){
  if (!ws || ws.readyState !== 1) return false;
  op.id = ++seq;
  ws.send(JSON.stringify(op));
  return true;
}

// ---------- 飞机 ----------
function onAircraft(list){
  var now = Date.now() / 1000, out = [];
  for (var i=0;i<list.length;i++){
    var a = list[i];
    // a = [hex, 呼号, lat, lon, 高度, 地速, 航向, 位置时刻, 信号强度, 距上次报文秒数]
    if (a[2] == null || now - a[7] > FRESH_S) continue;
    out.push({ hex:a[0], cs:a[1], lat:a[2], lon:a[3], alt:a[4], gs:a[5], tr:a[6] || 0,
               t:a[7], x:{ rssi:a[8], seen:a[9] } });
  }
  lastLocal = out.length;
  app.ac.ingest("local", out, true);
  app.ac.render();
}

function onStatus(){
  app.ac.show("local", st.mode === "adsb");
  render();
  drawHome();
}

// ---------- 状态条与侧栏 ----------
// 把快照翻译成一句人话。返回 [样式类, 文字]。顺序就是优先级：先说最该处理的事。
function describe(){
  if (picking) return ["pick", "点击地图设定接收机位置 · Esc 取消"];
  if (link === "down") return ["err", "接收机服务未运行" + (linkWhy ? " · " + linkWhy : "")];
  if (link !== "up" || !st) return ["busy", "接收机 · 连接中…" + (linkWhy ? " " + linkWhy : "")];
  if (st.mode === "off") return ["", "接收机已关闭 · 设备已释放，可供其他程序使用"];
  var d = st.device.state;
  if (d === "absent")    return ["warn", "未检测到接收机 · 请检查 USB 连接，插上后会自动开始"];
  if (d === "busy")      return ["warn", "接收机被别的程序占用 · 关掉那个程序后会自动开始"];
  if (d === "denied")    return ["err", "没有访问接收机的权限"];
  if (d === "no_driver") return ["err", "缺少驱动 · 终端执行 brew install librtlsdr"];
  if (d === "error")     return ["err", "接收机出错 · " + (st.error || "")];
  if (d !== "ready")     return ["busy", "接收机 · 正在打开设备…"];
  var a = st.adsb;
  if (a.state === "no_readsb") return ["err", "缺少解码器 · 终端执行 brew install readsb"];
  if (a.state === "failed")    return ["warn", "解码器异常退出，正在重启"];
  var warn = "";
  for (var i=0;i<st.warnings.length;i++){
    if (st.warnings[i].code === "stalled") return ["warn", "接收机已打开但没有数据 · 试试重新插拔"];
    if (st.warnings[i].code === "clip") warn = " · ⚠ 增益过高";
  }
  if (a.state !== "running") return ["busy", "本地接收 · ADS-B 1090 · 启动中…"];
  return [warn ? "warn" : "ok",
          "本地接收 · ADS-B 1090 · " + a.ac + " 架 · " + a.msg_rate + " 条/秒" + warn];
}

var flashTimer = null, flashMsg = "";
function flash(msg){
  flashMsg = msg; render();
  clearTimeout(flashTimer);
  flashTimer = setTimeout(function(){ flashMsg = ""; render(); }, 4000);
}

var pill, grp, cb, cnt, hint, homeBtn, homeClr, gainSel, dev;
function build(){
  pill = el("div"); pill.id = "sdrPill";
  pill.append(el("span", "dot"), el("span", "txt"));
  document.getElementById("wrap").appendChild(pill);

  grp = el("div", "grp");
  grp.append(el("label", "t", "本地接收机"));
  var row = el("label", "row");
  cb = document.createElement("input"); cb.type = "checkbox";
  cnt = el("span", "c");
  row.append(cb, el("span", "n", "接收 ADS-B（1090 MHz）"), cnt);
  hint = el("div", "hint");
  var btns = el("div", "sdr-btns");
  homeBtn = el("button", "sdr-btn"); homeBtn.type = "button";
  homeClr = el("button", "sdr-btn", "清除"); homeClr.type = "button";
  btns.append(homeBtn, homeClr);

  // 增益这类参数平时用不着，收在「高级」里。默认自动，不必让人先学会调增益才能看到飞机。
  var adv = document.createElement("details"); adv.id = "sdrAdv";
  adv.append(el("summary", null, "高级"));
  var f = el("div", "f");
  gainSel = document.createElement("select");
  f.append(el("span", null, "增益"), gainSel);
  dev = el("div"); dev.id = "sdrDev";
  adv.append(f, dev);

  grp.append(row, hint, btns, adv);
  document.getElementById("scroll").appendChild(grp);

  cb.addEventListener("change", function(){
    if (!send({ op:"mode", mode: cb.checked ? "adsb" : "off" })) cb.checked = !cb.checked;
  });
  homeBtn.addEventListener("click", function(){ setPicking(!picking); });
  homeClr.addEventListener("click", function(){ send({ op:"home", lat:null, lon:null }); });
  gainSel.addEventListener("change", function(){
    send({ op:"set", gain_db: gainSel.value === "auto" ? "auto" : +gainSel.value });
  });
}

var gainKey = "";
function render(){
  if (!pill) return;
  var d = flashMsg ? ["warn", flashMsg] : describe();
  pill.className = d[0];
  pillText = d[1];
  pill.lastChild.textContent = d[1];
  pill.title = d[1];

  var up = link === "up" && !!st;
  cb.disabled = !up;
  cb.checked = up && st.mode === "adsb";
  homeBtn.disabled = homeClr.disabled = gainSel.disabled = !up;
  homeBtn.textContent = picking ? "取消点选" : (up && st.home ? "重设接收机位置" : "设定接收机位置");
  homeClr.style.display = up && st.home ? "" : "none";

  var running = up && st.mode === "adsb" && st.adsb.state === "running";
  cnt.textContent = running ? fmt(st.adsb.ac) + " 架" : "";
  var bits = [];
  if (running){
    bits.push(st.adsb.ac_pos + " 架有位置");
    if (st.home && st.adsb.max_nm) bits.push("最远收到 " + Math.round(st.adsb.max_nm) + " nm");
    if (!st.home) bits.push("设定接收机位置后可显示距离圈和接收范围");
  }
  hint.textContent = bits.join(" · ");

  // 设备信息如实写：是什么型号由驱动认定，外壳上印的不算
  var info = [];
  if (up && st.device.model){
    info.push(st.device.model + (st.device.serial ? " · 序列号 " + st.device.serial : ""));
    if (st.rf && st.rf.gain_db != null) info.push("当前增益 " + st.rf.gain_db + " dB" + (st.rf.auto ? "（自动）" : ""));
    if (st.device.range_hz && st.device.range_hz[1])
      info.push("可调谐 " + (st.device.range_hz[0] / 1e6) + "–" + (st.device.range_hz[1] / 1e6) + " MHz");
    if (st.adsb.pipe_drops) info.push("解码管道丢弃 " + fmt(st.adsb.pipe_drops) + " 字节");
  }
  dev.textContent = info.join("\n");
  dev.style.whiteSpace = "pre-line";
  // 增益档位来自设备本身；只在档位表变了时重建下拉框，免得把正在操作的下拉框冲掉
  var gains = (up && st.device.gains) || [], key = gains.join(",");
  if (key !== gainKey || !gainSel.options.length){
    gainKey = key; gainSel.innerHTML = "";
    var o = document.createElement("option"); o.value = "auto"; o.textContent = "自动"; gainSel.append(o);
    gains.forEach(function(g){
      var x = document.createElement("option"); x.value = g; x.textContent = g + " dB"; gainSel.append(x);
    });
  }
  if (up && st.rf && document.activeElement !== gainSel)
    gainSel.value = st.rf.auto ? "auto" : String(st.rf.gain_db);
}

// ---------- 接收机位置、距离圈、最远轮廓 ----------
function dest(lat, lon, brg, nm){
  // 从 (lat, lon) 沿方位 brg 走 nm 海里到哪。地球半径与主页面 nmBetween 一致。
  var R = 3440.065, toR = Math.PI / 180, d = nm / R, b = brg * toR, p1 = lat * toR, l1 = lon * toR;
  var p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(b));
  var l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
  return [l2 / toR, p2 / toR];
}

function homeData(){
  var f = [], h = st && st.home;
  if (!h) return { type:"FeatureCollection", features:f };
  f.push({ type:"Feature", geometry:{ type:"Point", coordinates:[h[1], h[0]] }, properties:{ k:"home" } });
  RINGS_NM.forEach(function(nm){
    var ring = [];
    for (var b=0;b<=360;b+=5) ring.push(dest(h[0], h[1], b, nm));
    f.push({ type:"Feature", geometry:{ type:"LineString", coordinates:ring }, properties:{ k:"ring" } });
    f.push({ type:"Feature", geometry:{ type:"Point", coordinates:dest(h[0], h[1], 0, nm) },
             properties:{ k:"label", t:nm + " nm" } });
  });
  // 最远轮廓：每 5° 方位一个历史最远距离。没收到过的方位记 0，轮廓在那里缩回原点——
  // 这是实话：那个方向确实什么都没收到过，多半是被楼挡住了。
  var o = outline && outline.home && outline.home[0] === h[0] && outline.home[1] === h[1] ? outline : null;
  if (o && o.nm.some(function(v){ return v > 0; })){
    var poly = [];
    for (var i=0;i<o.nm.length;i++) poly.push(dest(h[0], h[1], i * o.step_deg + o.step_deg / 2, o.nm[i]));
    poly.push(poly[0]);
    f.push({ type:"Feature", geometry:{ type:"Polygon", coordinates:[poly] }, properties:{ k:"outline" } });
  }
  return { type:"FeatureCollection", features:f };
}

function addHomeLayers(){
  if (map.getSource("sdr-home")) return;
  map.addSource("sdr-home", { type:"geojson", data:homeData() });
  var before = map.getLayer("flight-trails") ? "flight-trails" : undefined;   // 压在飞机和航迹下面
  map.addLayer({ id:"sdr-outline", type:"fill", source:"sdr-home", filter:["==",["get","k"],"outline"],
    paint:{ "fill-color":"#0e9f6e", "fill-opacity":.10, "fill-outline-color":"#0e9f6e" } }, before);
  map.addLayer({ id:"sdr-rings", type:"line", source:"sdr-home", filter:["==",["get","k"],"ring"],
    paint:{ "line-color":"#64748b", "line-width":1, "line-opacity":.55, "line-dasharray":[3,3] } }, before);
  map.addLayer({ id:"sdr-ring-label", type:"symbol", source:"sdr-home", filter:["==",["get","k"],"label"],
    layout:{ "text-field":["get","t"], "text-font":["Noto Sans Regular"], "text-size":10.5, "text-offset":[0,-.7] },
    paint:{ "text-color":"#475569", "text-halo-color":"rgba(255,255,255,.9)", "text-halo-width":1.3 } }, before);
  map.addLayer({ id:"sdr-home-pt", type:"circle", source:"sdr-home", filter:["==",["get","k"],"home"],
    paint:{ "circle-radius":6, "circle-color":"#0e9f6e", "circle-stroke-width":2.5, "circle-stroke-color":"#fff" } }, before);
}

function drawHome(){
  if (map.getSource("sdr-home")) map.getSource("sdr-home").setData(homeData());
}

function setPicking(on){
  picking = !!on;
  document.getElementById("wrap").classList.toggle("sdr-pick", picking);
  render();
}

// ---------- 航班详情里的本地信息 ----------
function flightDetail(kv, hex){
  var r = app.ac.get(hex), b = r && r.by.local;
  if (!b){ if (st && st.mode === "adsb") kv("数据来源", "联网数据（本地未收到）"); return; }
  kv("数据来源", "本地接收" + (b.x && b.x.rssi != null ? " · 信号 " + b.x.rssi + " dBFS" : ""));
  if (st && st.home)
    kv("距接收机", fmt(Math.round(app.nmBetween([st.home[1], st.home[0]], [b.lon, b.lat]))) + " nm");
}

// ---------- 启动 ----------
build();
addHomeLayers();
app.hooks.styleReady.push(addHomeLayers);          // 切换底图会销毁图层，要跟着重挂
app.hooks.flightDetail = flightDetail;
app.hooks.esc = function(){ if (!picking) return false; setPicking(false); return true; };
map.on("click", function(e){
  if (!picking) return;
  setPicking(false);
  send({ op:"home", lat:+e.lngLat.lat.toFixed(5), lon:+e.lngLat.lng.toFixed(5) });
});
render();
discover();

// ---------- 命令入口 ----------
// 侧栏、键盘、桌面外壳的菜单、自动化验收，四条路最后都走这一个函数。
function act(name, arg){
  if (name === "mode") return send({ op:"mode", mode:arg });
  if (name === "home") return send(arg ? { op:"home", lat:arg[0], lon:arg[1] } : { op:"home", lat:null, lon:null });
  if (name === "gain") return send({ op:"set", gain_db:arg });
  if (name === "pick"){ setPicking(arg); return true; }
  if (name === "wsDrop"){ if (ws) ws.close(); return true; }
  return null;
}

// 桌面外壳的菜单项不直接碰接收机，只派发这个事件
window.addEventListener("sdr-command", function(e){
  var d = e.detail || {};
  act(d.name, d.arg);
});

// 键盘：1 开始接收 ADS-B，0 关闭并释放设备。正在输入框里打字时不抢键，
// 带修饰键的组合留给浏览器和外壳的菜单。
document.addEventListener("keydown", function(e){
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target;
  if (t && (/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || t.isContentEditable)) return;
  if (e.key === "1") act("mode", "adsb");
  else if (e.key === "0") act("mode", "off");
});

// 给自动化验收用，与主页面的 __probe / __act 是一个路数
window.__sdr = {
  probe:function(){
    return { loaded:true, link:link, port:port, pill:pillText, pillClass:pill.className,
             mode:st ? st.mode : null, device:st ? st.device : null, adsb:st ? st.adsb : null,
             home:st ? st.home : null, localShown:lastLocal, picking:picking,
             outlineMax:outline ? Math.max.apply(null, outline.nm) : 0,
             layers:{ home:!!map.getLayer("sdr-home-pt"), rings:!!map.getLayer("sdr-rings"),
                      outline:!!map.getLayer("sdr-outline") },
             checkbox:{ checked:cb.checked, disabled:cb.disabled }, hint:hint.textContent };
  },
  act:act
};
})();

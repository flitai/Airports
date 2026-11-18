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
// 距离圈：公制 100–400 km，英制 50–200 nm。守护进程和主页面内部一律用海里，
// 显示时按侧栏选的单位换算（app.units）。
var RINGS = { metric:[100, 200, 300, 400], imperial:[50, 100, 150, 200] };
var ws = null, port = null, link = "connecting", linkWhy = "", retryMs = 1000;
var st = null, outline = null, picking = false, seq = 0;
var pillText = "", lastLocal = 0;
var chan = null;                  // 最近一份 channels 快照：各频道电平、谁开着静噪

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
  ws.binaryType = "arraybuffer";
  ws.onopen = function(){ link = "up"; retryMs = 1000; render(); };
  ws.onmessage = function(ev){
    if (typeof ev.data !== "string"){ onBinary(ev.data); return; }
    var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.t === "status"){ st = m; onStatus(); }
    else if (m.t === "channels"){ chan = m; render(); }
    else if (m.t === "aircraft") onAircraft(m.list || []);
    else if (m.t === "outline"){ outline = m; drawHome(); }
    else if (m.t === "ack" && m.ok === false) flash(m.msg || "命令被拒绝");
  };
  ws.onclose = function(){
    ws = null; link = "connecting"; linkWhy = ""; st = null; chan = null;
    audioOn = false;                      // 新连接默认不要声音，连上后由 syncAudio 重新申请
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
  app.ac.ingest("local", out);
  app.ac.render();
}

function onStatus(){
  app.ac.show("local", st.mode === "adsb");
  if (!volTouched) volume = st.volume == null ? volume : st.volume;
  syncAudio();
  render();
  drawHome();
}

// ---------- 声音 ----------
// 守护进程发来的是 16 kHz 的 PCM 帧。AudioContext 直接开在 16 kHz 上，重采样到
// 声卡的事交给浏览器；sdr/ui/audio-worklet.js 里的环形缓冲负责把一帧一帧的数据
// 接成不断的流。
//
// 浏览器不允许页面不经用户操作就出声，所以 AudioContext 只能在点击里创建。
// 「开没开声音」因此是每个窗口自己的事：这边能出声了才向守护进程要音频帧，
// 守护进程也靠「有没有窗口在要」判断还有没有人在听。
var actx = null, node = null, gainNode = null, audioOn = false, audioErr = "";
var muted = false, volume = 0.8, volTouched = false, volTimer = null;
var heard = { frames:0, played:0, underruns:0, hz:0, rms:0 };

function soundReady(){ return !!(node && actx && actx.state === "running"); }

function audioStart(){                    // 必须从点击或按键的处理函数里调用
  var AC = window.AudioContext || window.webkitAudioContext;
  if (!AC || !window.AudioWorkletNode){ audioErr = "这个浏览器不支持音频播放"; render(); return; }
  if (!actx){
    try { actx = new AC({ sampleRate:16000, latencyHint:"interactive" }); }
    catch (e) { audioErr = "打不开音频设备"; render(); return; }
    actx.onstatechange = function(){ syncAudio(); render(); };
    gainNode = actx.createGain();
    gainNode.connect(actx.destination);
    actx.audioWorklet.addModule("sdr/ui/audio-worklet.js").then(function(){
      node = new AudioWorkletNode(actx, "radio-player", { numberOfInputs:0, outputChannelCount:[1] });
      node.port.onmessage = function(e){ heard.played = e.data.played; heard.underruns = e.data.underruns; };
      node.connect(gainNode);
      syncAudio(); render();
    }).catch(function(){ audioErr = "音频模块载入失败"; render(); });
  }
  if (actx.state !== "running") actx.resume().catch(function(){});
}

function syncAudio(){
  if (gainNode) gainNode.gain.value = muted ? 0 : volume;
  var want = soundReady() && !!st && st.mode === "air";
  if (want !== audioOn && send({ op:"audio", on:want })){
    audioOn = want;
    if (!want && node) node.port.postMessage({ reset:true });
  }
}

function onBinary(buf){
  var head = new Uint8Array(buf, 0, 4);
  if (head[0] !== 1 || buf.byteLength < 6) return;        // 1 = 音频帧；别的类型留给以后的频谱
  var pcm = new Int16Array(buf, 4), start = !!(head[1] & 1);
  // 顺手量一下这帧的响度和过零率。过零率 ÷ 2 就是单音的频率，验收时用它确认
  // 「听到的确实是那个台」，平时没有别的用处。
  var cross = 0, sum = 0;
  for (var i=1;i<pcm.length;i++){
    if ((pcm[i-1] < 0) !== (pcm[i] < 0)) cross++;
    sum += pcm[i] * pcm[i];
  }
  heard.frames++;
  heard.hz += 0.2 * (cross * 16000 / (2 * pcm.length) - heard.hz);
  heard.rms += 0.2 * (Math.sqrt(sum / pcm.length) / 32768 - heard.rms);
  if (node) node.port.postMessage({ pcm:pcm, start:start }, [buf]);
}

function setMuted(on){ muted = !!on; syncAudio(); render(); }
function setVolume(v){
  volume = Math.max(0, Math.min(1, v)); volTouched = true;
  syncAudio();
  clearTimeout(volTimer);                // 拖滑块时别每一格都去写一次设置文件
  volTimer = setTimeout(function(){ send({ op:"set", volume:volume }); volTouched = false; }, 400);
}

// ---------- 话音：频率表里的按钮 ----------
// 主页面每建好频率表的一行就调一次 freqRow。这里给每行加一个指示灯和一个按钮；
// 点了之后把这个机场的整张频率表交给守护进程，由它换算载波、划分窗口。
var rowsUI = { ap:null, items:[] };

function unlistenable(mhz){               // 与 sdrd/dsp/channels.py 的同名函数一致
  var v = parseFloat(mhz);
  if (!isFinite(v) || v <= 0) return "频率无法识别";
  if (v >= 117.975 && v <= 137) return "";
  if (v < 30) return "短波频率，暂不支持";
  if (v >= 108 && v < 117.975) return "导航台频率，不是话音频道";
  if (v >= 225 && v <= 400) return "军用 UHF 话音，暂不支持";
  return "不在民航话音波段（118–137 MHz）";
}

function freqRow(tr, fq, i, ap){
  if (i === 0){
    rowsUI = { ap:ap, items:[] };
    tr.parentNode.firstChild.append(el("th", "sdr-th", "本地收听"));
  }
  var td = el("td", "sdr-td"), dot = el("i", "sdr-dot"), b = el("button", "sdr-listen", "收听");
  b.type = "button";
  var why = unlistenable(fq[2]);
  if (why){ b.disabled = true; b.textContent = "—"; b.title = td.title = why; }
  else b.addEventListener("click", function(){ b.blur(); listenRow(i); });
  td.append(dot, b); tr.append(td);
  rowsUI.items.push({ tr:tr, btn:b, dot:dot, ok:!why });
}

function planFor(ap){                     // 守护进程手里的频道表是不是这个机场的
  return st && st.air && ap && st.air.airport === ap.id ? st.air : null;
}
function channelOfRow(air, i){
  for (var k=0;k<air.channels.length;k++) if (air.channels[k].rows.indexOf(i) >= 0) return air.channels[k];
  return null;
}

function listenRow(i){
  var ap = rowsUI.ap; if (!ap || link !== "up" || !st) return;
  audioStart();
  var air = planFor(ap), ch = air && channelOfRow(air, i);
  if (ch && st.mode === "air" && chan && chan.listen === ch.id){ leaveAir(); return; }   // 再点一次即停
  if (ch) send({ op:"listen", ch:ch.id });
  else send({ op:"channels", label:ap.label, airport:ap.id, list:ap.list, pick:i });
}

function leaveAir(){ if (st) send({ op:"mode", mode:st.return_to || "adsb" }); }

function lamp(dot, ch){                   // 指示灯：亮 = 此刻有载波；空心 = 常驻载波；暗 = 不在当前窗口
  var cls = "sdr-dot", tip = "";
  if (!ch || !chan || !st || st.mode !== "air"){ dot.className = cls; dot.title = ""; return; }
  if (ch.win !== chan.win){ cls += " far"; tip = "不在当前窗口，点一下切过去"; }
  else if (chan.steady.indexOf(ch.id) >= 0){ cls += " steady"; tip = "一直亮着不灭：持续播发的台，或者干扰载波"; }
  else if (chan.active.indexOf(ch.id) >= 0){ cls += " on"; tip = "有信号"; }
  else tip = "安静";
  dot.className = cls; dot.title = tip;
}

function paintRows(){
  var air = planFor(rowsUI.ap), listening = air && st.mode === "air";
  for (var i=0;i<rowsUI.items.length;i++){
    var it = rowsUI.items[i]; if (!it.ok) continue;
    var ch = air && channelOfRow(air, i), mine = !!(listening && ch && chan && chan.listen === ch.id);
    it.btn.textContent = mine ? "停止" : "收听";
    it.btn.disabled = link !== "up" || !st;
    it.btn.classList.toggle("cur", mine);
    it.tr.classList.toggle("sdr-cur", mine);
    lamp(it.dot, listening ? ch : null);
  }
}

// ---------- 话音：底部频道条 ----------
// 这是停靠栏的第一层：窗口分组、每个频道一个标签和指示灯。瀑布图在下一个里程碑
// 加到它上面。它只在话音模式下出现，高度写进 --dock-h，详情面板和地图控件据此让位。
var dock, dkTitle, dkNow, dkSound, dkPatrol, dkVoice, dkSq, dkSqTxt, dkVol, dkMute, dkBack, dkWins, dockKey = "";

function range(min, max, step){
  var r = document.createElement("input"); r.type = "range"; r.min = min; r.max = max; r.step = step; return r;
}
function field(label, node, extra){
  var f = el("label", "f"); f.append(el("span", null, label), node); if (extra) f.append(extra); return f;
}

function buildDock(){
  dock = el("div"); dock.id = "sdrDock"; dock.tabIndex = -1;
  var hd = el("div", "hd");
  dkTitle = el("b", "ttl"); dkNow = el("span", "now");
  dkSound = el("button", "sdr-btn go", "开启声音"); dkSound.type = "button";
  dkPatrol = document.createElement("input"); dkPatrol.type = "checkbox";
  var pat = el("label", "f tog"); pat.append(dkPatrol, el("span", null, "轮巡"));
  pat.title = "在各窗口间循环，有人说话时停下来听；连续播发的通播不会让它停下";
  dkVoice = document.createElement("input"); dkVoice.type = "checkbox";
  var vo = el("label", "f tog"); vo.append(dkVoice, el("span", null, "无话音时静音"));
  vo.title = "载波上没有话音（干扰载波、信号太弱只剩噪声）就不出声。关掉则只要有载波就放出来";
  dkSq = range(0, 30, 1); dkSqTxt = el("span", "v");
  dkVol = range(0, 100, 1);
  dkMute = el("button", "sdr-btn", "静音"); dkMute.type = "button";
  dkBack = el("button", "sdr-btn"); dkBack.type = "button";
  var ctl = el("div", "ctl");
  ctl.append(dkSound, pat, vo, field("静噪", dkSq, dkSqTxt), field("音量", dkVol), dkMute, dkBack);
  hd.append(dkTitle, dkNow, ctl);
  dkWins = el("div", "wins");
  dock.append(hd, dkWins);
  (document.getElementById("sdrMount") || document.getElementById("wrap")).appendChild(dock);

  dkSound.addEventListener("click", function(){ dkSound.blur(); audioStart(); });
  dkPatrol.addEventListener("change", function(){ audioStart(); send({ op:"set", patrol:dkPatrol.checked }); dkPatrol.blur(); });
  dkVoice.addEventListener("change", function(){ send({ op:"set", voice_only:dkVoice.checked }); dkVoice.blur(); });
  dkSq.addEventListener("change", function(){ send({ op:"set", squelch_db:+dkSq.value }); dkSq.blur(); });
  dkSq.addEventListener("input", function(){ dkSqTxt.textContent = dkSq.value + " dB"; });
  dkVol.addEventListener("input", function(){ setVolume(dkVol.value / 100); });
  dkVol.addEventListener("change", function(){ dkVol.blur(); });
  dkMute.addEventListener("click", function(){ dkMute.blur(); setMuted(!muted); });
  dkBack.addEventListener("click", function(){ dkBack.blur(); leaveAir(); });
  // 详情面板和地图控件要给它让位，高度变了就更新
  if (window.ResizeObserver) new ResizeObserver(dockHeight).observe(dock);
}

function dockHeight(){
  var on = dock && dock.classList.contains("on");
  document.getElementById("wrap").style.setProperty("--dock-h", (on ? dock.offsetHeight : 0) + "px");
}

function mhz(hz){ return (hz / 1e6).toFixed(3); }
function signed(v, digits){ return (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(digits); }

function fillWindows(air){               // 频道表或窗口划分变了才重建，免得把鼠标悬停和焦点冲掉
  dkWins.innerHTML = "";
  air.windows.forEach(function(w, wi){
    var box = el("div", "win"), head = el("button", "wl"); head.type = "button";
    head.textContent = (air.windows.length > 1 ? "窗口 " + (wi + 1) + " · " : "") +
                       (w.lo_hz === w.hi_hz ? mhz(w.lo_hz) : mhz(w.lo_hz) + "–" + mhz(w.hi_hz));
    head.title = "接收机一次只能看 1.92 MHz。点这里切到这个窗口";
    head.addEventListener("click", function(){ head.blur(); audioStart(); send({ op:"window", i:wi }); });
    var chips = el("div", "chips");
    w.ch.forEach(function(id){
      var ch = null;
      for (var k=0;k<air.channels.length;k++) if (air.channels[k].id === id) ch = air.channels[k];
      if (!ch) return;
      var c = el("button", "chip"); c.type = "button"; c.dataset.ch = id;
      c.append(el("i", "sdr-dot"), el("b", null, ch.type || "—"), el("span", null, ch.label));
      c.title = [ch.desc, mhz(ch.hz) + " MHz", ch.spacing === "8.33" ? "8.33 kHz 频道" : ""].filter(Boolean).join(" · ");
      c.addEventListener("click", function(){ c.blur(); audioStart(); send({ op:"listen", ch:id }); });
      chips.append(c);
    });
    box.append(head, chips);
    dkWins.append(box);
  });
}

function nowLine(air){
  var ch = null;
  if (chan) for (var k=0;k<air.channels.length;k++) if (air.channels[k].id === chan.listen) ch = air.channels[k];
  if (!ch) return ["", "正在调谐…"];
  var name = (ch.type ? ch.type + " " : "") + ch.label, lv = chan.level[ch.id];
  if (chan.open && st.voice_only && !chan.voice)   // 有载波但没人说话：不出声，并说清楚为什么
    return ["", name + " · 有载波 " + signed(lv, 0) + " dB，无话音，已静音" +
                (chan.steady.indexOf(ch.id) >= 0 ? " · 常驻载波，多半是干扰" : "")];
  if (chan.open){
    var bits = [name, "信号 " + signed(lv, 0) + " dB"];
    if (chan.afc_hz != null && Math.abs(chan.afc_hz) >= 500) bits.push("载波偏 " + signed(chan.afc_hz / 1000, 1) + " kHz");
    if (chan.steady.indexOf(ch.id) >= 0) bits.push(chan.voice ? "持续播发" : "常驻载波，多半是干扰");
    return ["on", bits.join(" · ")];
  }
  if (lv != null && lv >= 1 && lv < st.squelch_db)
    return ["", name + " · 有弱载波 " + signed(lv, 0) + " dB，低于静噪门限 " + st.squelch_db + " dB"];
  return ["", name + " · 静噪中，等待通话"];
}

function renderAir(){
  if (!dock) return;
  var up = link === "up" && !!st, air = up && st.mode === "air" ? st.air : null;
  dock.classList.toggle("on", !!air);
  if (air){
    var key = JSON.stringify([air.windows, air.channels.map(function(c){ return [c.id, c.type, c.label]; })]);
    if (key !== dockKey){ dockKey = key; fillWindows(air); }
    dkTitle.textContent = "话音监听" + (air.label ? " · " + air.label : "");
    var now = st.device.state !== "ready" ? ["", "等待接收机…"] : nowLine(air);
    if (st.auto_return_s != null && !soundReady())
      now = ["warn", "声音未开启 · " + st.auto_return_s + " 秒后" + (st.return_to === "off" ? "关闭接收机" : "回到 ADS-B")];
    dkNow.className = "now " + now[0]; dkNow.textContent = now[1];
    dkSound.style.display = soundReady() ? "none" : "";
    dkSound.textContent = audioErr || "开启声音";
    dkSound.disabled = !!audioErr;
    dkPatrol.checked = !!air.patrol;
    dkVoice.checked = !!st.voice_only;
    if (document.activeElement !== dkSq){ dkSq.value = st.squelch_db; dkSqTxt.textContent = st.squelch_db + " dB"; }
    if (document.activeElement !== dkVol) dkVol.value = Math.round(volume * 100);
    dkMute.textContent = muted ? "取消静音" : "静音";
    dkMute.classList.toggle("cur", muted);
    dkBack.textContent = st.return_to === "off" ? "停止并释放设备" : "返回 ADS-B";
    var wins = dkWins.children;
    for (var w=0;w<wins.length;w++) wins[w].classList.toggle("cur", !!chan && chan.win === w);
    var chips = dkWins.querySelectorAll(".chip");
    for (var i=0;i<chips.length;i++){
      var id = +chips[i].dataset.ch, ch = null;
      for (var k=0;k<air.channels.length;k++) if (air.channels[k].id === id) ch = air.channels[k];
      chips[i].classList.toggle("cur", !!chan && chan.listen === id);
      lamp(chips[i].firstChild, ch);
    }
  }
  dockHeight();
  paintRows();
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
  if (st.mode === "air") return describeAir();
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

// 话音模式下的状态条。本地 ADS-B 此时是停的，这件事每一种说法里都带着。
function describeAir(){
  var air = st.air, tail = " · 本地 ADS-B 已暂停";
  for (var i=0;i<st.warnings.length;i++){
    if (st.warnings[i].code === "stalled") return ["warn", "接收机已打开但没有数据 · 试试重新插拔"];
    if (st.warnings[i].code === "dsp") return ["err", "解调出错 · " + st.warnings[i].msg];
  }
  if (!air || !chan) return ["busy", "本地接收 · 话音 · 正在调谐…"];
  if (!soundReady())
    return ["warn", "话音 · " + (audioErr || "声音未开启，点下方「开启声音」") +
            (st.auto_return_s != null ? " · " + st.auto_return_s + " 秒后" + (st.return_to === "off" ? "关闭" : "回到 ADS-B") : "")];
  var now = nowLine(air), head = "本地接收 · 话音 · ";
  if (air.patrol) head += "轮巡" + (air.windows.length > 1 ? " " + (chan.win + 1) + "/" + air.windows.length : "") + " · ";
  if (muted) tail = " · 已静音" + tail;
  if (st.audio.dsp_drops) tail += " · ⚠ 解调丢块 " + st.audio.dsp_drops;
  return [now[0] === "on" ? "ok" : "idle", head + now[1] + tail];
}

var flashTimer = null, flashMsg = "";
function flash(msg){
  flashMsg = msg; render();
  clearTimeout(flashTimer);
  flashTimer = setTimeout(function(){ flashMsg = ""; render(); }, 4000);
}

var pill, pillBack, grp, cb, cnt, hint, homeBtn, homeClr, gainSel, dev;
function build(){
  pill = el("div"); pill.id = "sdrPill";
  pillBack = el("button", "back"); pillBack.type = "button";
  pill.append(el("span", "dot"), el("span", "txt"), pillBack);
  document.getElementById("wrap").appendChild(pill);
  pillBack.addEventListener("click", function(){ pillBack.blur(); leaveAir(); });
  buildDock();

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
  pill.children[1].textContent = d[1];
  pill.title = d[1];

  var up = link === "up" && !!st, inAir = up && st.mode === "air";
  // 一键返回：话音模式下状态条末尾常驻一个按钮
  pillBack.style.display = inAir && !picking ? "" : "none";
  pillBack.textContent = inAir && st.return_to === "off" ? "停止" : "返回 ADS-B";
  renderAir();
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
    if (st.home && st.adsb.max_nm) bits.push("最远收到 " + app.units.dist(st.adsb.max_nm));
    if (!st.home) bits.push("设定接收机位置后可显示距离圈和接收范围");
  }
  if (inAir) bits.push("正在监听话音，本地 ADS-B 已暂停。勾选上面这项即返回");
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
  var km = app.units.metric();
  RINGS[km ? "metric" : "imperial"].forEach(function(v){
    var ring = [], nm = km ? v / 1.852 : v;
    for (var b=0;b<=360;b+=5) ring.push(dest(h[0], h[1], b, nm));
    f.push({ type:"Feature", geometry:{ type:"LineString", coordinates:ring }, properties:{ k:"ring" } });
    f.push({ type:"Feature", geometry:{ type:"Point", coordinates:dest(h[0], h[1], 0, nm) },
             properties:{ k:"label", t:v + (km ? " km" : " nm") } });
  });
  // 最远轮廓：每 5° 方位一个历史最远距离，只增不减，随设置一起存盘。没收到过的方位
  // 记 0，轮廓在那里缩回原点——这是实话：那个方向确实什么都没收到过，多半是被楼挡住了。
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
    kv("距接收机", app.units.dist(app.nmBetween([st.home[1], st.home[0]], [b.lon, b.lat])));
}

// ---------- 启动 ----------
build();
addHomeLayers();
app.hooks.styleReady.push(addHomeLayers);          // 切换底图会销毁图层，要跟着重挂
app.hooks.flightDetail = flightDetail;
app.hooks.freqRow = freqRow;
app.hooks.units = function(){ drawHome(); render(); };      // 距离圈的标注和「最远收到」跟着换
app.hooks.detail = function(id){ if (id == null) rowsUI = { ap:null, items:[] }; paintRows(); };
// 这个脚本是异步到的。它来之前机场详情可能已经开着，那张频率表上还没有按钮：重画一遍
if (app.airport.shown() != null) app.airport.show(app.airport.shown());
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
  if (name === "listenRow"){                 // 走真实的点击路径
    var it = rowsUI.items[arg];
    if (!it || it.btn.disabled) return false;
    it.btn.click(); return true;
  }
  if (name === "listen"){ audioStart(); return send({ op:"listen", ch:arg }); }
  if (name === "window"){ audioStart(); return send({ op:"window", i:arg }); }
  if (name === "step"){ audioStart(); return send({ op:"tune", step:arg > 0 ? 1 : -1, fine:Math.abs(arg) < 1 }); }
  if (name === "patrol") return send({ op:"set", patrol:!!arg });
  if (name === "squelch") return send({ op:"set", squelch_db:arg });
  if (name === "voiceOnly") return send({ op:"set", voice_only:!!arg });
  if (name === "volume"){ setVolume(arg); return true; }
  if (name === "mute"){ setMuted(arg == null ? !muted : arg); return true; }
  if (name === "sound"){ audioStart(); return true; }
  if (name === "back"){ leaveAir(); return true; }
  if (name === "wsDrop"){ if (ws) ws.close(); return true; }
  return null;
}

// 桌面外壳的菜单项不直接碰接收机，只派发这个事件
window.addEventListener("sdr-command", function(e){
  var d = e.detail || {};
  act(d.name, d.arg);
});

// 键盘：1 接收 ADS-B，2 回到话音，0 关闭并释放设备；话音模式下空格静音，
// 左右方向键按 25 kHz 步进（按住 Shift 为 8.33 kHz）。
// 正在输入框里打字时不抢键，带修饰键的组合留给浏览器和外壳的菜单。
// 焦点在地图上时方向键归地图（平移），所以点一下地图之外的地方再用方向键调谐。
document.addEventListener("keydown", function(e){
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target;
  if (t && (/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || t.isContentEditable)) return;
  if (e.key === "1") act("mode", "adsb");
  else if (e.key === "2"){ audioStart(); act("mode", "air"); }
  else if (e.key === "0") act("mode", "off");
  if (!st || st.mode !== "air") return;
  var onMap = t && t.closest && t.closest(".maplibregl-canvas-container");
  if (e.key === " " && !(t && /^(BUTTON|A|SUMMARY)$/.test(t.tagName))){ e.preventDefault(); act("mute"); }
  else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !onMap){
    e.preventDefault();
    act("step", (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 0.5 : 1));
  }
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
             checkbox:{ checked:cb.checked, disabled:cb.disabled }, hint:hint.textContent,
             air:st && st.air ? { label:st.air.label, airport:st.air.airport, windows:st.air.windows.length,
                                  channels:st.air.channels.length, patrol:st.air.patrol,
                                  win:chan ? chan.win : null, listen:chan ? chan.listen : null,
                                  listenLabel:(function(){
                                    for (var k=0;k<st.air.channels.length;k++)
                                      if (chan && st.air.channels[k].id === chan.listen) return st.air.channels[k].label;
                                    return null; })(),
                                  open:chan ? chan.open : false, voice:chan ? chan.voice : false,
                                  afc:chan ? chan.afc_hz : null,
                                  level:chan ? chan.level : {}, active:chan ? chan.active : [],
                                  steady:chan ? chan.steady : [] } : null,
             returnTo:st ? st.return_to : null, autoReturn:st ? st.auto_return_s : null,
             squelch:st ? st.squelch_db : null, voiceOnly:st ? st.voice_only : null,
             dock:{ on:dock.classList.contains("on"), h:dock.offsetHeight, now:dkNow.textContent,
                    chips:dkWins.querySelectorAll(".chip").length,
                    lit:dkWins.querySelectorAll(".chip .sdr-dot.on").length,
                    cur:(function(){ var c = dkWins.querySelector(".chip.cur"); return c ? c.textContent : ""; })(),
                    back:dkBack.textContent, soundBtn:dkSound.style.display !== "none" },
             rows:rowsUI.items.map(function(it){
               return { text:it.btn.textContent, disabled:it.btn.disabled, why:it.btn.title,
                        lamp:it.dot.className.replace("sdr-dot", "").trim() }; }),
             audio:{ ctx:actx ? actx.state : null, ready:soundReady(), on:audioOn, muted:muted, volume:volume,
                     frames:heard.frames, played:heard.played, hz:Math.round(heard.hz),
                     rms:+heard.rms.toFixed(3), err:audioErr } };
  },
  act:act
};
})();

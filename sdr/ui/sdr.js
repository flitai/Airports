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
var cands = [];                   // 最近一份 candidates 快照：窗口里强到能开静噪的载波
var recs = { list:[], count:0, bytes:0, quota_bytes:0 };   // 最近一份 recordings 快照：存下的录音片段
var iqs = { list:[], count:0, bytes:0, quota_bytes:0 };    // 最近一份 iqs 快照：存下的原始 I/Q

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
    else if (m.t === "candidates"){ cands = m.list || []; renderSignals(); paintMarks(); }
    else if (m.t === "recordings"){ recs = m; renderSide(); }
    else if (m.t === "iqs"){ iqs = m; renderSide(); }
    else if (m.t === "hello"){
      // 带 spectrum 的才是会发频谱的守护进程。页面文件改了一刷新就生效，守护进程却要
      // 重启才换新：两边版本对不上时如实说明，别对着一个不认识这条命令的进程空等
      specAvail = !!m.spectrum;
      if (m.spectrum){ specScale = m.spectrum; if (wf) wf.setScale(specScale); }
    }
    else if (m.t === "ack" && m.ok === false) flash(m.msg || "命令被拒绝");
  };
  ws.onclose = function(){
    ws = null; link = "connecting"; linkWhy = ""; st = null; chan = null; cands = [];
    stopClip();
    audioOn = specOn = specAvail = false; // 新连接默认不要声音也不要频谱，连上后各自重新申请
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
  // 回放录音时现场的声音先让一让，放完自己回来
  if (gainNode) gainNode.gain.value = muted || (playing && playing.audio) ? 0 : volume;
  var want = soundReady() && !!st && st.mode === "air";
  if (want !== audioOn && send({ op:"audio", on:want })){
    audioOn = want;
    if (!want && node) node.port.postMessage({ reset:true });
  }
}

function onBinary(buf){
  var head = new Uint8Array(buf, 0, 4);
  if (head[0] === 2){ if (wf && specOn) wf.push(buf); return; }   // 2 = 一行频谱
  if (head[0] !== 1 || buf.byteLength < 6) return;        // 1 = 音频帧
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

// 「返回」：回放时是回到实时接收，平时是离开话音模式
function leaveAir(){
  if (st && st.replay) send({ op:"replay", off:true });
  else if (st) send({ op:"mode", mode:st.return_to || "adsb" });
}

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

// ---------- 底部停靠栏 ----------
// 从上到下三层：一行标题和控件；频谱曲线、瀑布图和信号列表；按窗口分组的频道条。
// 中间那层有三档：收起（bar，只剩频道条）、半高（half）、展开（full）。话音和
// ADS-B 各记各的：听话音时想看瀑布图，不等于收 ADS-B 时也想让它占着地图。
// 档位是这个窗口自己的版面，和「开没开声音」一样不进守护进程的设置。
// 高度写进 --dock-h，详情面板和地图控件据此让位。
var VIEWS = ["bar", "half", "full"];
var view = { air:"half", adsb:"bar" };
try {
  ["air", "adsb"].forEach(function(k){
    var v = localStorage.getItem("ap.sdr.dock." + k);
    if (VIEWS.indexOf(v) >= 0) view[k] = v;
  });
} catch (e) {}
var wf = null, wfLoading = false, specOn = false, specAvail = false, specScale = { db_min:-120, db_step:.5 };

var dock, dkTitle, dkNow, dkSound, dkPatrol, dkVoice, dkSq, dkSqTxt, dkVol, dkMute, dkBack, dkWins, dockKey = "";
var dkFreq, dkViews, dkCanvas, dkZoom, dkSig, dkSigNote, sigEls = {}, sigShown = false;
var dkRec, dkRecBox, dkRecTxt, tbSig, tbRec, dkRecList, dkRecNote, dkRecSum, dkRecClr, recEls = {};
var dkIq, dkIqVoice, tbIq, dkIqList, dkIqNote, dkIqSum, dkIqClr, iqEls = {}, iqClearArmed = 0;
var sideTab = "sig", sideKey = "", playing = null, clearArmed = 0;

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
  dkSound = el("button", "sdr-btn go air", "开启声音"); dkSound.type = "button";
  dkPatrol = document.createElement("input"); dkPatrol.type = "checkbox";
  var pat = el("label", "f tog air"); pat.append(dkPatrol, el("span", null, "轮巡"));
  pat.title = "在各窗口间循环，有人说话时停下来听；连续播发的通播不会让它停下";
  dkVoice = document.createElement("input"); dkVoice.type = "checkbox";
  var vo = el("label", "f tog air"); vo.append(dkVoice, el("span", null, "无话音时静音"));
  vo.title = "载波上没有话音（干扰载波、信号太弱只剩噪声）就不出声。关掉则只要有载波就放出来";
  dkSq = range(0, 30, 1); dkSqTxt = el("span", "v");
  dkVol = range(0, 100, 1);
  var sq = field("静噪", dkSq, dkSqTxt), vol = field("音量", dkVol);
  sq.classList.add("air"); vol.classList.add("air");
  dkMute = el("button", "sdr-btn air", "静音"); dkMute.type = "button";
  dkRec = document.createElement("input"); dkRec.type = "checkbox";
  dkRecBox = el("label", "f tog air rec"); dkRecTxt = el("span", null, "录音");
  dkRecBox.append(dkRec, dkRecTxt);
  dkRecBox.title = "有人说话就自动存一段，一次通话一个文件。不必有页面在听；开着录音时接收机不会自动回到 ADS-B";
  // 原始 I/Q：接收机当时看到的整个窗口，原样存下来，之后可以回放、换着频道重听。每秒 4.8 MB
  dkIqVoice = document.createElement("input"); dkIqVoice.type = "checkbox";
  var iqv = el("label", "f tog air"); iqv.append(dkIqVoice, el("span", null, "录 I/Q"));
  iqv.title = "有人说话就自动存那一段的整窗原始 I/Q，之后可以回放（每秒 4.8 MB，一次十秒的通话约 50 MB）。" +
              "和「录音」各管各的，可以只开一个";
  dkIq = el("button", "sdr-btn"); dkIq.type = "button";
  dkBack = el("button", "sdr-btn air"); dkBack.type = "button";
  // 直接输入频率：不在任何机场频率表里的也能去听，从 ADS-B 模式也能直接进话音
  dkFreq = document.createElement("input"); dkFreq.type = "text"; dkFreq.className = "fq";
  dkFreq.placeholder = "MHz"; dkFreq.inputMode = "decimal"; dkFreq.size = 7; dkFreq.spellcheck = false;
  dkFreq.autocomplete = "off"; dkFreq.setAttribute("aria-label", "调到指定频率（MHz）");
  var fq = field("调到", dkFreq);
  fq.title = "输入 118–137 MHz 之间的频率或频道名，回车即听。不必在机场的频率表里";
  dkViews = el("div", "views");
  [["bar", "▁", "收起"], ["half", "▄", "半高"], ["full", "█", "展开"]].forEach(function(v){
    var b = el("button", "sdr-btn", v[1]); b.type = "button"; b.dataset.view = v[0];
    b.title = "瀑布图" + v[2] + "（W 键循环切换）"; b.setAttribute("aria-label", "瀑布图" + v[2]);
    b.addEventListener("click", function(){ b.blur(); setView(v[0]); });
    dkViews.append(b);
  });
  var ctl = el("div", "ctl");
  ctl.append(dkSound, pat, vo, dkRecBox, iqv, sq, vol, dkMute, dkIq, fq, dkViews, dkBack);
  hd.append(dkTitle, dkNow, ctl);

  var spec = el("div", "spec"), wrap = el("div", "wfwrap");
  dkCanvas = document.createElement("canvas");
  dkZoom = el("button", "zoom"); dkZoom.type = "button"; dkZoom.style.display = "none";
  wrap.append(dkCanvas, dkZoom);
  // 右侧一栏两页：窗口里的信号，和存下的录音。ADS-B 模式下只有录音那一页
  var sig = el("div", "sig"), sh = el("div", "sh");
  tbSig = el("button", "tb air", "信号"); tbRec = el("button", "tb", "录音");
  tbSig.type = tbRec.type = "button";
  tbSig.title = "这个窗口里强到能开静噪的载波，不管在不在频率表里。点一下就听";
  tbRec.title = "话音触发录音存下的片段，点一下回放。文件在运行目录的 recordings/ 里（WAV 加一份同名 JSON）";
  tbIq = el("button", "tb", "I/Q"); tbIq.type = "button";
  tbIq.title = "存下的原始 I/Q，点一下回放：接收机换成这段录制，瀑布图、调谐、收听照常可用。文件在运行目录的 iq/ 里（.cu8 加一份同名 JSON）";
  sh.append(tbSig, tbRec, tbIq);
  dkSig = el("div", "sl"); dkSigNote = el("div", "sn");
  dkRecList = el("div", "sl"); dkRecNote = el("div", "sn rn");
  dkRecSum = el("span"); dkRecClr = el("button", "lnk", "清空"); dkRecClr.type = "button";
  dkRecNote.append(dkRecSum, dkRecClr);
  dkIqList = el("div", "sl"); dkIqNote = el("div", "sn rn");
  dkIqSum = el("span"); dkIqClr = el("button", "lnk", "清空"); dkIqClr.type = "button";
  dkIqNote.append(dkIqSum, dkIqClr);
  sig.append(sh, dkSig, dkSigNote, dkRecList, dkRecNote, dkIqList, dkIqNote);
  spec.append(wrap, sig);

  dkWins = el("div", "wins air");
  dock.append(hd, spec, dkWins);
  (document.getElementById("sdrMount") || document.getElementById("wrap")).appendChild(dock);

  dkSound.addEventListener("click", function(){ dkSound.blur(); audioStart(); });
  dkPatrol.addEventListener("change", function(){ audioStart(); send({ op:"set", patrol:dkPatrol.checked }); dkPatrol.blur(); });
  dkVoice.addEventListener("change", function(){ send({ op:"set", voice_only:dkVoice.checked }); dkVoice.blur(); });
  dkSq.addEventListener("change", function(){ send({ op:"set", squelch_db:+dkSq.value }); dkSq.blur(); });
  dkSq.addEventListener("input", function(){ dkSqTxt.textContent = dkSq.value + " dB"; });
  dkVol.addEventListener("input", function(){ setVolume(dkVol.value / 100); });
  dkVol.addEventListener("change", function(){ dkVol.blur(); });
  dkMute.addEventListener("click", function(){ dkMute.blur(); setMuted(!muted); });
  dkRec.addEventListener("change", function(){ send({ op:"set", record:dkRec.checked }); dkRec.blur(); });
  tbSig.addEventListener("click", function(){ tbSig.blur(); sideTab = "sig"; renderSide(); });
  tbRec.addEventListener("click", function(){ tbRec.blur(); sideTab = "rec"; renderSide(); });
  tbIq.addEventListener("click", function(){ tbIq.blur(); sideTab = "iq"; renderSide(); });
  dkIq.addEventListener("click", function(){ dkIq.blur(); send({ op:"iq", on:!(st && st.iq && st.iq.active) }); });
  dkIqVoice.addEventListener("change", function(){ send({ op:"set", iq_with_voice:dkIqVoice.checked }); dkIqVoice.blur(); });
  dkIqClr.addEventListener("click", function(){
    dkIqClr.blur();
    if (Date.now() < iqClearArmed){ iqClearArmed = 0; send({ op:"iq", clear:true }); }
    else { iqClearArmed = Date.now() + 3000; setTimeout(renderIqs, 3100); }
    renderIqs();
  });
  // 清空是删文件，点两下才算数。不弹对话框
  dkRecClr.addEventListener("click", function(){
    dkRecClr.blur();
    if (Date.now() < clearArmed){ clearArmed = 0; stopClip(); send({ op:"record", clear:true }); }
    else { clearArmed = Date.now() + 3000; setTimeout(renderRecs, 3100); }
    renderRecs();
  });
  dkBack.addEventListener("click", function(){ dkBack.blur(); leaveAir(); });
  dkFreq.addEventListener("keydown", function(e){
    if (e.key === "Enter"){ if (tuneMHz(dkFreq.value)){ dkFreq.value = ""; dkFreq.blur(); } }
    else if (e.key === "Escape"){ dkFreq.value = ""; dkFreq.blur(); e.stopPropagation(); }
  });
  dkZoom.addEventListener("click", function(){ dkZoom.blur(); if (wf) wf.zoom(1); });
  // 详情面板和地图控件要给它让位，高度变了就更新；画布大小变了瀑布图要重画
  if (window.ResizeObserver){
    new ResizeObserver(dockHeight).observe(dock);
    new ResizeObserver(function(){ if (wf) wf.resize(); }).observe(wrap);
  }
}

function dockHeight(){
  var on = dock && dock.classList.contains("on");
  document.getElementById("wrap").style.setProperty("--dock-h", (on ? dock.offsetHeight : 0) + "px");
}

function dockMode(){               // 停靠栏此刻为谁而开："air"、"adsb"，不开则是 ""
  if (link !== "up" || !st) return "";
  if (st.mode === "air" && st.air) return "air";
  if (st.mode === "adsb" && st.device.state === "ready" && view.adsb !== "bar") return "adsb";
  return "";
}
function curView(){ var m = dockMode(); return m ? view[m] : "bar"; }

function setView(v){               // 不给 v（或给了不认识的）就在三档间循环
  if (link !== "up" || !st || (st.mode !== "air" && st.mode !== "adsb")) return false;
  var k = st.mode;
  if (VIEWS.indexOf(v) < 0) v = VIEWS[(VIEWS.indexOf(view[k]) + 1) % VIEWS.length];
  view[k] = v;
  try { localStorage.setItem("ap.sdr.dock." + k, v); } catch (e) {}   // 记不住也无妨
  render();
  return true;
}

// ---------- 频谱与瀑布图 ----------
// 画图的代码在 waterfall.js 里，第一次打开瀑布图才去取。这里管三件事：向守护进程
// 订阅频谱帧、告诉瀑布图该标哪些频率、把点击翻译成调谐命令。
function loadWaterfall(){
  if (wf || wfLoading) return;
  wfLoading = true;
  var s = document.createElement("script");
  s.src = "sdr/ui/waterfall.js";
  s.onload = function(){
    wf = window.__sdrWaterfall.create(dkCanvas, { onPick:pickFreq, describe:describeFreq, onZoom:function(k){
      dkZoom.style.display = k > 1.01 ? "" : "none";
      dkZoom.textContent = k.toFixed(1) + "× · 回到全景";
    } });
    wf.setScale(specScale);
    render();
  };
  s.onerror = function(){ wfLoading = false; };
  document.head.appendChild(s);
}

// 看得见才要：停靠栏开着、不是收起那一档、标签页在前台。守护进程在 ADS-B 模式下
// 只有有人要才算频谱，所以不看的时候一定要退订。
function syncSpectrum(){
  if (wf) wf.empty(link === "up" && !specAvail ? "接收机服务是旧版本，重启 serve.py 后才有频谱" : "等待频谱数据…");
  var want = !!wf && specAvail && curView() !== "bar" && !document.hidden;
  if (want === specOn || !send({ op:"spectrum", on:want })) return;
  specOn = want;
  if (!want) wf.clear();           // 退订期间的行是缺的，留着旧图只会让人以为时间是连着的
}
document.addEventListener("visibilitychange", function(){ syncSpectrum(); });

function reachOf(ch){ return ch.spacing === "8.33" ? 2500 : 9000; }   // 频率跟踪的范围，与守护进程一致

function channelNear(hz, slack){   // 当前窗口里离 hz 最近、且 hz 落在它跟踪范围（或 slack）内的频道
  var air = st && st.mode === "air" && st.air, best = null, bd = 1e12;
  if (!air || !chan) return null;
  for (var k=0;k<air.channels.length;k++){
    var ch = air.channels[k], d = Math.abs(hz - ch.hz);
    if (ch.win === chan.win && d < bd && d <= Math.max(reachOf(ch), slack || 0)){ best = ch; bd = d; }
  }
  return best;
}
function candNear(hz, within){     // 此刻还在的候选里离 hz 最近的
  var best = null, bd = within;
  for (var i=0;i<cands.length;i++){
    var d = Math.abs(cands[i].hz - hz);
    if (cands[i].age_s === 0 && d <= bd){ best = cands[i]; bd = d; }
  }
  return best;
}
function pickSlack(){ return wf ? Math.max(1500, 3 * wf.hzPerPx()) : 1500; }   // 鼠标点不了那么准，给三个像素的余量

function pickFreq(hz){             // 点了瀑布图上的一点
  if (!st || st.mode !== "air" || !st.air) return;        // ADS-B 模式下只看不调
  audioStart();
  var c = candNear(hz, pickSlack());                      // 点在一根载波上：以它实测的频率为准
  if (c && c.ch != null){ send({ op:"listen", ch:c.ch }); return; }
  var ch = c ? null : channelNear(hz, pickSlack());
  if (ch) send({ op:"listen", ch:ch.id });
  else send({ op:"tune", hz:Math.round(c ? sigName(c).hz : hz) });
}

function describeFreq(hz){         // 鼠标悬停处的说明
  if (!st || st.mode !== "air") return "";
  var c = candNear(hz, pickSlack());
  if (c) return sigName(c).text;
  var ch = channelNear(hz, pickSlack());
  return ch ? (ch.type ? ch.type + " " : "") + ch.label : "";
}

function paintMarks(){
  if (!wf) return;
  var m = { channels:[], cands:[], listen:null };
  if (st && st.mode === "air" && st.air && chan){
    st.air.channels.forEach(function(ch){
      if (ch.win !== chan.win) return;
      var mine = chan.listen === ch.id;
      m.channels.push({ hz:ch.hz, text:(ch.type ? ch.type.split(" / ")[0] + " " : "") + ch.label,
                        state:mine ? "cur" : (chan.steady.indexOf(ch.id) >= 0 ? "steady" :
                                              (chan.active.indexOf(ch.id) >= 0 ? "on" : "")) });
      if (mine) m.listen = { hz:ch.hz, afc:chan.open ? chan.afc_hz || 0 : 0, bw:7600 };
    });
    cands.forEach(function(c){ if (c.ch == null && c.age_s === 0) m.cands.push({ hz:c.hz }); });
  }
  wf.marks(m);
}

// ---------- 信号列表 ----------
// 守护进程只说「这个频率上有一根够强的载波」。它可能是谁，由这里拿三样东西去对：
// 当前的频道表、接收机附近各机场的频率表、几个全世界通用的频率。对上了也只写
// 「可能是」：同一个频率会被相隔几百公里的机场重复使用，听到的未必是最近的那个。
var NEAR_NM = 250;                 // 巡航高度的飞机，甚高频视距大约就这么远
var WELL_KNOWN = [[121500000, "国际应急频率"], [123450000, "空对空通话频率"],
                  [131550000, " ACARS 数据链"], [131450000, " ACARS 数据链"],
                  [131725000, " ACARS 数据链"], [131825000, " ACARS 数据链"],
                  [136975000, " VDL2 数据链"], [136725000, " VDL2 数据链"], [136775000, " VDL2 数据链"],
                  [136825000, " VDL2 数据链"], [136875000, " VDL2 数据链"]];

function carrierOf(mhz){           // 频道名 → 真实载波。与 sdrd/dsp/channels.py 的 carrier 一致
  var v = parseFloat(mhz);
  if (!isFinite(v) || v < 117.975 || v > 137) return null;
  var khz = Math.round(v * 1000), m = khz % 25, base = (khz - m) * 1000, third = 25000 / 3;
  if (m === 0) return { hz:base, sp:"25" };
  if (m === 5 || m === 10 || m === 15) return { hz:base + Math.round((m - 5) / 5 * third), sp:"8.33" };
  if (m === 20) return { hz:base + 25000, sp:"25" };
  var k = Math.round(khz * 1000 / third);
  return { hz:Math.round(k * third), sp:k % 3 === 0 ? "25" : "8.33" };
}

var nearKey = null, nearIdx = [];
function buildNear(){              // 接收机周围各机场的话音频率。位置变了才重建
  var h = st && st.home, key = h ? h.join(",") : "";
  if (key === nearKey) return;
  nearKey = key; nearIdx = [];
  if (!h || !window.DT || !app.airport.info) return;
  for (var id in window.DT){
    var f = window.DT[id].f; if (!f || !f.length) continue;
    var a = app.airport.info(+id); if (!a) continue;
    var nm = app.nmBetween([h[1], h[0]], [a.lon, a.lat]);
    if (!(nm <= NEAR_NM)) continue;
    for (var i=0;i<f.length;i++){
      var c = carrierOf(f[i][2]);
      if (c) nearIdx.push({ hz:c.hz, sp:c.sp, type:f[i][0] || "", id:+id, code:a.code, name:a.name, nm:nm });
    }
  }
}
function guessAirport(hz){         // 列了这个频率的机场里最近的一个
  var best = null;
  for (var i=0;i<nearIdx.length;i++){
    var e = nearIdx[i];
    if (Math.abs(hz - e.hz) <= (e.sp === "8.33" ? 2500 : 9000) && (!best || e.nm < best.nm)) best = e;
  }
  return best;
}

function sigName(c){               // → { text, hz, ap }：这个信号可能是谁，点它时该调到哪儿
  var air = st && st.air, manual = null;
  if (c.ch != null && air)
    for (var k=0;k<air.channels.length;k++) if (air.channels[k].id === c.ch){
      var ch = air.channels[k];
      // 手动调出来的频道不来自任何频率表，「手动」二字没说它是谁：接着往下猜
      if (ch.rows.length) return { text:(ch.type ? ch.type + " " : "") + ch.label, hz:ch.hz };
      manual = ch;
    }
  var g = guessAirport(c.hz);
  if (g) return { text:"可能是 " + g.code + (g.type ? " " + g.type : "") + " · " + app.units.dist(g.nm), hz:g.hz, ap:g };
  for (var i=0;i<WELL_KNOWN.length;i++)
    if (Math.abs(c.hz - WELL_KNOWN[i][0]) <= 5000) return { text:"可能是" + WELL_KNOWN[i][1], hz:WELL_KNOWN[i][0] };
  return { text:"未知", hz:manual ? manual.hz : c.hz };
}

function howLong(s){ return s < 90 ? Math.round(s) + " 秒" : Math.round(s / 60) + " 分钟"; }

function makeSig(){
  var e = { root:el("div", "sg"), go:el("button", "go"), dot:el("i", "sdr-dot"), mhz:el("b"),
            db:el("span", "db"), when:el("span", "du"), name:el("span", "nm"), ap:el("button", "ap", "机场"),
            c:null, who:null };
  e.go.type = e.ap.type = "button";
  e.go.append(e.dot, e.mhz, e.db, e.name, e.when);
  e.root.append(e.go, e.ap);
  e.go.addEventListener("click", function(){
    e.go.blur(); audioStart();
    if (e.c.ch != null) send({ op:"listen", ch:e.c.ch });
    else send({ op:"tune", hz:Math.round(e.who.hz) });
  });
  e.ap.addEventListener("click", function(){ e.ap.blur(); if (e.who.ap) app.airport.show(e.who.ap.id); });
  return e;
}

// 「信号」页只在话音模式下有。ADS-B 模式下停靠栏右侧只有录音和 I/Q 两页
function curTab(){ return dockMode() !== "air" && sideTab === "sig" ? "rec" : sideTab; }

function renderSide(){             // 右侧栏此刻显示哪一页
  if (!dkSig) return;
  var tab = curTab();
  tbSig.classList.toggle("cur", tab === "sig");
  tbRec.classList.toggle("cur", tab === "rec");
  tbIq.classList.toggle("cur", tab === "iq");
  tbRec.textContent = "录音" + (recs.count ? " " + recs.count : "");
  tbIq.textContent = "I/Q" + (iqs.count ? " " + iqs.count : "");
  dkSig.style.display = dkSigNote.style.display = tab === "sig" ? "" : "none";
  dkRecList.style.display = dkRecNote.style.display = tab === "rec" ? "" : "none";
  dkIqList.style.display = dkIqNote.style.display = tab === "iq" ? "" : "none";
  renderSignals();
  renderRecs();
  renderIqs();
}

function renderSignals(){
  if (!dkSig) return;
  sigShown = dockMode() === "air" && curView() !== "bar" && curTab() === "sig";
  if (!sigShown) return;
  buildNear();
  var list = cands.filter(function(c){ return c.age_s <= 300; }).slice(0, 30), seen = {};
  list.forEach(function(c, i){
    var e = sigEls[c.id] || (sigEls[c.id] = makeSig());
    var live = c.age_s === 0, steady = live && c.run_s >= 20, who = sigName(c);
    e.c = c; e.who = who; seen[c.id] = 1;
    e.root.className = "sg" + (live ? " live" : "") + (chan && c.ch != null && c.ch === chan.listen ? " cur" : "");
    e.dot.className = "sdr-dot" + (steady ? " steady" : (live ? " on" : ""));
    e.mhz.textContent = mhz(c.hz);
    e.db.textContent = signed(c.db, 0) + " dB";
    e.name.textContent = who.text;
    e.when.textContent = live ? (steady ? "持续 " + howLong(c.run_s) : "正在发射") : howLong(c.age_s) + "前";
    e.go.title = (steady ? "一直在：通播这类持续播发的台，或者干扰载波。" : "") + "点一下收听 · 带宽约 " +
                 (c.bw_hz / 1000).toFixed(1) + " kHz · 累计出现 " + howLong(c.seen_s);
    e.ap.style.display = who.ap ? "" : "none";
    e.ap.title = who.ap ? "打开 " + who.ap.name : "";
    if (dkSig.children[i] !== e.root) dkSig.insertBefore(e.root, dkSig.children[i] || null);
  });
  for (var id in sigEls) if (!seen[id]){ sigEls[id].root.remove(); delete sigEls[id]; }
  dkSigNote.textContent = !list.length ? "这个窗口里暂时没有够强的信号（门限即静噪门限 " + st.squelch_db + " dB）"
                        : (!st.home ? "设定接收机位置后，可以推测未知信号属于附近哪个机场" : "");
}

// ---------- 录音 ----------
// 录音是守护进程自己的事：开着就录，不管有没有页面在听。这里只是把存下的片段列出来、
// 取回来放。片段用 fetch 取（带着页面来源，守护进程照常校验），不用 <audio src> 直连。
function pad2(n){ return (n < 10 ? "0" : "") + n; }
function clockOf(t){               // 今天的只写时刻，别的日子带上月日
  var d = new Date(t * 1000), hm = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  return d.toDateString() === new Date().toDateString() ? hm : (d.getMonth() + 1) + "-" + d.getDate() + " " + hm;
}
function megabytes(b){ return (b / 1e6).toFixed(b < 1e7 ? 1 : 0) + " MB"; }
function minSec(s){ return Math.floor(s / 60) + ":" + pad2(Math.floor(s % 60)); }

function makeRec(r){
  var e = { root:el("div", "sg rc live"), go:el("button", "go"), icon:el("i", "pl", "▶"), del:el("button", "ap", "删除") };
  e.go.type = e.del.type = "button";
  var code = (r.airport_label || "").split(" ")[0];
  e.go.append(e.icon, el("b", null, clockOf(r.start)), el("span", "db", r.duration_s.toFixed(1) + " 秒"),
              el("span", "nm", (r.type ? r.type + " " : "") + r.label + (code ? " · " + code : "")),
              el("span", "du", r.peak_db == null ? "" : signed(r.peak_db, 0) + " dB"));
  e.go.title = [r.desc, r.airport_label, r.start_utc, r.end === "limit" ? "到了单段时长上限" : "", r.name].filter(Boolean).join(" · ");
  e.del.title = "删除这段录音";
  e.root.append(e.go, e.del);
  e.go.addEventListener("click", function(){ e.go.blur(); playClip(r.name); });
  e.del.addEventListener("click", function(){
    e.del.blur(); if (playing && playing.name === r.name) stopClip();
    send({ op:"record", delete:r.name });
  });
  return e;
}

function renderRecs(){
  if (!dkRecList || !dockMode() || curView() === "bar" || curTab() !== "rec") return;
  var seen = {};
  recs.list.slice(0, 60).forEach(function(r, i){
    var e = recEls[r.name] || (recEls[r.name] = makeRec(r)), on = !!playing && playing.name === r.name;
    seen[r.name] = 1;
    e.root.classList.toggle("cur", on);
    e.icon.textContent = on ? "■" : "▶";
    if (dkRecList.children[i] !== e.root) dkRecList.insertBefore(e.root, dkRecList.children[i] || null);
  });
  for (var n in recEls) if (!seen[n]){ recEls[n].root.remove(); delete recEls[n]; }
  var on = !!(st && st.rec && st.rec.on);
  dkRecSum.textContent = recs.count
    ? recs.count + " 段 · " + megabytes(recs.bytes) + " / " + megabytes(recs.quota_bytes) + (on ? "" : " · 录音已关")
    : (on ? "录音开着：有人说话就会自动存一段" : "还没有录音。勾上标题行的「录音」，有人说话就自动存一段");
  dkRecSum.title = "超过上限时从最旧的删起";
  dkRecClr.style.display = recs.count ? "" : "none";
  dkRecClr.textContent = Date.now() < clearArmed ? "再点一次确认" : "清空";
}

// ---------- 原始 I/Q ----------
// 一段 I/Q 是接收机当时看到的整个窗口。点一段即回放：守护进程放开真接收机，换成读这个
// 文件的采样源，于是这里的瀑布图、频道条、点击调谐都不用知道「现在是回放」——除了要
// 把这件事明明白白写出来，并给一个回到实时的出口。
function iqTitle(r){
  var code = (r.airport_label || "").split(" ")[0];
  return (r.mode === "adsb" ? "ADS-B 1090 MHz" : "窗口 " + mhz(r.center_hz) + (code ? " · " + code : "")) +
         (r.kind === "voice" ? " · 话音触发" : "");
}
function makeIq(r){
  var e = { root:el("div", "sg rc live"), go:el("button", "go"), icon:el("i", "pl", "▶"), del:el("button", "ap", "删除") };
  e.go.type = e.del.type = "button";
  e.go.append(e.icon, el("b", null, clockOf(r.start)), el("span", "db", r.duration_s.toFixed(1) + " 秒"),
              el("span", "nm", iqTitle(r)), el("span", "du", megabytes(r.bytes)));
  e.go.title = ["点一下回放", r.airport_label, r.listen ? "当时在听 " + r.listen : "", r.start_utc,
                r.gain_db != null ? "增益 " + r.gain_db + " dB" : "", r.dropped_blocks ? "写盘丢了 " + r.dropped_blocks + " 块" : "",
                r.end === "limit" ? "到了单段时长上限" : (r.end === "retune" ? "因为换了频率而结束" : ""), r.name].filter(Boolean).join(" · ");
  e.del.title = "删除这段 I/Q";
  e.root.append(e.go, e.del);
  e.go.addEventListener("click", function(){
    e.go.blur(); audioStart();
    if (st && st.replay && st.replay.name === r.name) send({ op:"replay", off:true });   // 再点正在放的那段即返回实时
    else send({ op:"replay", name:r.name });
  });
  e.del.addEventListener("click", function(){ e.del.blur(); send({ op:"iq", delete:r.name }); });
  return e;
}

function renderIqs(){
  if (!dkIqList || !dockMode() || curView() === "bar" || curTab() !== "iq") return;
  var seen = {}, now = st && st.replay ? st.replay.name : null;
  iqs.list.slice(0, 60).forEach(function(r, i){
    var e = iqEls[r.name] || (iqEls[r.name] = makeIq(r));
    seen[r.name] = 1;
    e.root.classList.toggle("cur", now === r.name);
    e.icon.textContent = now === r.name ? "■" : "▶";
    if (dkIqList.children[i] !== e.root) dkIqList.insertBefore(e.root, dkIqList.children[i] || null);
  });
  for (var n in iqEls) if (!seen[n]){ iqEls[n].root.remove(); delete iqEls[n]; }
  dkIqSum.textContent = iqs.count
    ? iqs.count + " 段 · " + megabytes(iqs.bytes) + " / " + megabytes(iqs.quota_bytes)
    : "还没有 I/Q。点标题行的「立即录 I/Q」录下当前整个窗口，或勾上「录 I/Q」让它在有人说话时自动录";
  dkIqSum.title = "超过上限时从最旧的删起";
  dkIqClr.style.display = iqs.count ? "" : "none";
  dkIqClr.textContent = Date.now() < iqClearArmed ? "再点一次确认" : "清空";
}

function playClip(name){           // 再点正在放的那段即停
  if (playing){ var same = playing.name === name; stopClip(); if (same) return; }
  var p = playing = { name:name, audio:null, url:null };
  renderRecs();
  fetch("http://127.0.0.1:" + port + "/rec/" + encodeURIComponent(name)).then(function(r){
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.blob();
  }).then(function(blob){
    if (playing !== p) return;
    p.url = URL.createObjectURL(blob);
    p.audio = new Audio(p.url);
    p.audio.volume = volume;
    p.audio.onended = function(){ if (playing === p) stopClip(); };
    syncAudio();
    return p.audio.play();
  }).catch(function(){ if (playing === p){ stopClip(); flash("这段录音放不出来"); } });
}
function stopClip(){
  var p = playing; playing = null;
  if (p){ if (p.audio) p.audio.pause(); if (p.url) URL.revokeObjectURL(p.url); }
  syncAudio(); renderRecs();
}

function tuneMHz(text){            // 频率框里敲的数：按频道名理解，118.005 是 118.000 上的 8.33 kHz 频道
  if (link !== "up" || !st) return false;
  var v = parseFloat(String(text).replace(",", "."));
  if (!isFinite(v)){ flash("请输入频率，例如 118.5"); return false; }
  var c = carrierOf(v);
  if (!c){ flash("只能调到民航话音波段（118–137 MHz）"); return false; }
  audioStart();
  return send({ op:"tune", hz:c.hz });
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
      if (!ch.rows.length){              // 手动调出来的频道不属于任何频率表：给一个删除的叉
        var x = el("span", "x", "×"); x.title = "删除这个手动频道"; x.setAttribute("role", "button");
        x.setAttribute("aria-label", "删除手动频道 " + ch.label);
        x.addEventListener("click", function(e){ e.stopPropagation(); c.blur(); send({ op:"drop", ch:id }); });
        c.append(x);
      }
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

function renderDock(){
  if (!dock) return;
  var mode = dockMode(), air = mode === "air" ? st.air : null, v = curView();
  dock.classList.toggle("on", !!mode);
  dock.classList.toggle("adsb", mode === "adsb");
  dock.dataset.view = v;
  if (mode && v !== "bar") loadWaterfall();
  for (var b=0;b<dkViews.children.length;b++)
    dkViews.children[b].classList.toggle("cur", dkViews.children[b].dataset.view === v);
  dkCanvas.style.cursor = air ? "crosshair" : "default";
  dkCanvas.title = "";
  if (air){
    var key = JSON.stringify([air.windows, air.channels.map(function(c){ return [c.id, c.type, c.label]; })]);
    if (key !== dockKey){ dockKey = key; fillWindows(air); }
    dkTitle.textContent = (st.replay ? "回放 · " : "") + "话音监听" + (air.label ? " · " + air.label : "");
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
    // 正在录的这段话音已经多长。只开了「录 I/Q」时触发也在跑，但话音并不存，所以不算
    var recS = chan && st.rec && st.rec.on ? chan.rec_s : null;
    dkRec.checked = !!(st.rec && st.rec.on);
    dkRecBox.classList.toggle("live", recS != null);
    dkRecTxt.textContent = recS != null ? "录音 " + minSec(recS) : "录音";
    dkBack.textContent = st.replay ? "返回实时" : (st.return_to === "off" ? "停止并释放设备" : "返回 ADS-B");
    dkIqVoice.checked = !!(st.iq && st.iq.with_voice);
    var wins = dkWins.children;
    for (var w=0;w<wins.length;w++) wins[w].classList.toggle("cur", !!chan && chan.win === w);
    var chips = dkWins.querySelectorAll(".chip");
    for (var i=0;i<chips.length;i++){
      var id = +chips[i].dataset.ch, ch = null;
      for (var k=0;k<air.channels.length;k++) if (air.channels[k].id === id) ch = air.channels[k];
      chips[i].classList.toggle("cur", !!chan && chan.listen === id);
      lamp(chips[i].firstChild, ch);
    }
  } else if (mode === "adsb"){
    // ADS-B 模式下只看不调。1090 MHz 上是一个个 120 微秒的脉冲，平均下来是中心附近的一片隆起
    var s = wf && specOn ? wf.stats() : null;
    dkTitle.textContent = (st.replay ? "回放 · " : "") + "频谱 · ADS-B 1090 MHz";
    dkNow.className = "now";
    dkNow.textContent = s && s.floorDb != null ? "底噪 " + String(s.floorDb.toFixed(0)).replace("-", "−") + " dBFS" : "";
  }
  // 「立即录 I/Q」：正在录时变红，写明已经录了多久、多大；回放时没有它（录下来的只会是同一段）
  var cap = mode && st.iq ? st.iq.active : null;
  dkIq.style.display = mode && st.iq && !st.replay ? "" : "none";
  dkIq.classList.toggle("rec-on", !!cap);
  dkIq.textContent = cap ? "● I/Q " + minSec(cap.s) + " · " + megabytes(cap.bytes) : "立即录 I/Q";
  dkIq.title = cap ? (cap.kind === "voice" ? "话音触发的这段正在录。点一下改为手动：由你来停" : "点一下停止") +
                     (cap.dropped ? " · 写盘跟不上，已丢 " + cap.dropped + " 块" : "")
                   : "现在就开始录，不等人说话：把接收机此刻看到的整个窗口原样录下来（每秒 4.8 MB），" +
                     "并带上按下之前的 1.5 秒。再点停止，最长 " + (st && st.iq ? st.iq.max_s : 60) + " 秒";
  syncSpectrum();
  paintMarks();
  var sk = mode + v + (st && st.rec ? st.rec.on : "") + (st && st.replay ? st.replay.name : "");
  if (sideKey !== sk){ sideKey = sk; renderSide(); }
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
  var who = st.replay ? "回放 · " : "本地接收 · ";
  if (a.state !== "running") return ["busy", who + "ADS-B 1090 · 启动中…"];
  return [warn ? "warn" : "ok",
          who + "ADS-B 1090 · " + a.ac + " 架 · " + a.msg_rate + " 条/秒" + warn];
}

function autoRec(){               // 开着哪种自动录制：写进提示里的名字；都没开是 ""
  var a = !!(st && st.rec && st.rec.on), b = !!(st && st.iq && st.iq.with_voice);
  return a && b ? "录音和录 I/Q" : (a ? "录音" : (b ? "录 I/Q" : ""));
}

// 话音模式下的状态条。本地 ADS-B 此时是停的，这件事每一种说法里都带着。
function describeAir(){
  var air = st.air, tail = st.replay ? " · 真接收机已释放" : " · 本地 ADS-B 已暂停";
  for (var i=0;i<st.warnings.length;i++){
    if (st.warnings[i].code === "stalled") return ["warn", "接收机已打开但没有数据 · 试试重新插拔"];
    if (st.warnings[i].code === "dsp") return ["err", "解调出错 · " + st.warnings[i].msg];
  }
  if (!air || !chan) return ["busy", (st.replay ? "回放 · " : "本地接收 · ") + "话音 · 正在调谐…"];
  if (!soundReady())
    return [autoRec() ? "idle" : "warn", "话音 · " + (audioErr || "声音未开启，点下方「开启声音」") +
            (st.auto_return_s != null ? " · " + st.auto_return_s + " 秒后" + (st.return_to === "off" ? "关闭" : "回到 ADS-B")
                                      : (autoRec() ? " · " + autoRec() + "开着，不会自动返回" : ""))];
  var now = nowLine(air), head = (st.replay ? "回放 · " : "本地接收 · ") + "话音 · ";
  if (air.patrol) head += "轮巡" + (air.windows.length > 1 ? " " + (chan.win + 1) + "/" + air.windows.length : "") + " · ";
  if (muted) tail = " · 已静音" + tail;
  if (chan.rec_s != null && st.rec && st.rec.on) tail = " · 正在录音" + tail;
  if (st.audio.dsp_drops) tail += " · ⚠ 解调丢块 " + st.audio.dsp_drops;
  return [now[0] === "on" ? "ok" : "idle", head + now[1] + tail];
}

var flashTimer = null, flashMsg = "";
function flash(msg){
  flashMsg = msg; render();
  clearTimeout(flashTimer);
  flashTimer = setTimeout(function(){ flashMsg = ""; render(); }, 4000);
}

var pill, pillBack, grp, cb, cnt, hint, homeBtn, homeClr, specBtn, gainSel, dev;
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
  specBtn = el("button", "sdr-btn"); specBtn.type = "button";
  specBtn.title = "底部的频谱曲线和瀑布图：这一段频率上此刻有什么信号（W 键）";
  btns.append(homeBtn, homeClr, specBtn);

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
  specBtn.addEventListener("click", function(){ specBtn.blur(); setView(specShown() ? "bar" : "half"); });
  gainSel.addEventListener("change", function(){
    send({ op:"set", gain_db: gainSel.value === "auto" ? "auto" : +gainSel.value });
  });
}

function specShown(){             // 当前模式下瀑布图开着没有（不管停靠栏此刻是否可见）
  return !!st && (st.mode === "air" || st.mode === "adsb") && view[st.mode] !== "bar";
}

var gainKey = "";
function render(){
  if (!pill) return;
  var d = flashMsg ? ["warn", flashMsg] : describe();
  pill.className = d[0] + (st && st.replay && link === "up" ? " replay" : "");
  pillText = d[1];
  pill.children[1].textContent = d[1];
  pill.title = d[1];

  var up = link === "up" && !!st, inAir = up && st.mode === "air";
  // 一键返回：话音模式下状态条末尾常驻一个按钮
  // 回放时这个按钮是「返回实时」，在哪个模式下都有
  var replaying = up && !!st.replay;
  pillBack.style.display = (inAir || replaying) && !picking ? "" : "none";
  pillBack.textContent = replaying ? "返回实时" : (inAir && st.return_to === "off" ? "停止" : "返回 ADS-B");
  renderDock();
  specBtn.disabled = !up || (st.mode !== "air" && st.mode !== "adsb");
  specBtn.textContent = specShown() ? "隐藏瀑布图" : "显示瀑布图";
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
// Esc 逐层收：先取消点选；瀑布图展开着就先收到半高；都不是才轮到主页面关详情面板
app.hooks.esc = function(){
  if (picking){ setPicking(false); return true; }
  if (dockMode() && curView() === "full"){ setView("half"); return true; }
  return false;
};
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
  if (name === "view") return setView(arg);                // 不带参数即在三档间循环
  // 录音：都走真实的点击路径
  if (name === "record"){ if (dkRec.checked !== !!arg) dkRec.click(); return true; }
  if (name === "sideTab"){ (arg === "rec" ? tbRec : (arg === "iq" ? tbIq : tbSig)).click(); return true; }
  if (name === "playRec" || name === "deleteRec"){
    var rc = dkRecList.querySelectorAll(".rc")[arg];
    if (!rc) return false;
    rc.querySelector(name === "playRec" ? ".go" : ".ap").click(); return true;
  }
  if (name === "clearRec"){ dkRecClr.click(); return true; }
  // 原始 I/Q：同样走真实的点击路径
  if (name === "iq"){ dkIq.click(); return true; }
  if (name === "iqVoice"){ if (dkIqVoice.checked !== !!arg) dkIqVoice.click(); return true; }
  if (name === "replayIq" || name === "deleteIq"){
    var iq = dkIqList.querySelectorAll(".rc")[arg];
    if (!iq) return false;
    iq.querySelector(name === "replayIq" ? ".go" : ".ap").click(); return true;
  }
  if (name === "clearIq"){ dkIqClr.click(); return true; }
  if (name === "dropManual"){                              // 走真实路径：点手动频道上的叉
    var cross = dkWins.querySelector(".chip .x");
    if (!cross) return false;
    cross.click(); return true;
  }
  if (name === "tuneMHz") return tuneMHz(arg);
  if (name === "zoom"){ if (wf) wf.zoom(arg[0], arg[1]); return !!wf; }
  // 下面三个走真实的事件路径：频率框里回车、点瀑布图上某个频率、点信号列表的某一行
  if (name === "freqBox"){
    dkFreq.value = arg;
    dkFreq.dispatchEvent(new KeyboardEvent("keydown", { key:"Enter", bubbles:true }));
    return true;
  }
  if (name === "clickFreq"){
    var x = wf ? wf.xOf(arg) : null; if (x == null) return false;
    var r = dkCanvas.getBoundingClientRect();
    dkCanvas.dispatchEvent(new MouseEvent("click", { clientX:r.left + x, clientY:r.top + r.height * .7, bubbles:true }));
    return true;
  }
  if (name === "clickSignal"){
    var go = dkSig.querySelectorAll(".sg .go")[arg];
    if (!go) return false;
    go.click(); return true;
  }
  if (name === "wsDrop"){ if (ws) ws.close(); return true; }
  return null;
}

// 桌面外壳的菜单项不直接碰接收机，只派发这个事件
window.addEventListener("sdr-command", function(e){
  var d = e.detail || {};
  act(d.name, d.arg);
});

// 键盘：1 接收 ADS-B，2 回到话音，0 关闭并释放设备，W 切换瀑布图的三档；
// 话音模式下空格静音，左右方向键按 25 kHz 步进（按住 Shift 为 8.33 kHz）。
// 正在输入框里打字时不抢键，带修饰键的组合留给浏览器和外壳的菜单。
// 焦点在地图上时方向键归地图（平移），所以点一下地图之外的地方再用方向键调谐。
document.addEventListener("keydown", function(e){
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target;
  if (t && (/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || t.isContentEditable)) return;
  if (e.key === "1") act("mode", "adsb");
  else if (e.key === "2"){ audioStart(); act("mode", "air"); }
  else if (e.key === "0") act("mode", "off");
  else if (e.key === "w" || e.key === "W") act("view");
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
             rf:st ? st.rf : null,
             spectrum:{ view:curView(), views:{ air:view.air, adsb:view.adsb }, mode:dockMode(), on:specOn,
                        loaded:!!wf, stats:wf ? wf.stats() : null, title:dkTitle.textContent,
                        watchers:st && st.spectrum ? st.spectrum.watchers : null,
                        daemonRows:st && st.spectrum ? st.spectrum.rows : null,
                        zoomBtn:dkZoom.style.display !== "none", button:specBtn.textContent,
                        canvas:{ w:dkCanvas.clientWidth, h:dkCanvas.clientHeight } },
             cands:cands,
             replay:st ? st.replay : null,
             iq:{ active:st && st.iq ? st.iq.active : null, withVoice:st && st.iq ? st.iq.with_voice : null,
                  button:dkIq.textContent, buttonShown:dkIq.offsetParent !== null, live:dkIq.classList.contains("rec-on"),
                  count:iqs.count, bytes:iqs.bytes, tabText:tbIq.textContent, sum:dkIqSum.textContent,
                  clear:dkIqClr.style.display === "none" ? null : dkIqClr.textContent, list:iqs.list.slice(0, 5),
                  shown:dkIqList.offsetParent !== null,
                  rows:[].map.call(dkIqList.querySelectorAll(".rc"), function(e){
                    var t = e.querySelectorAll(".go > *");
                    return { icon:t[0].textContent, time:t[1].textContent, dur:t[2].textContent, name:t[3].textContent,
                             size:t[4].textContent, cur:e.classList.contains("cur") }; }) },
             pillBack:pillBack.style.display === "none" ? null : pillBack.textContent,
             rec:{ on:st && st.rec ? st.rec.on : null, label:dkRecTxt.textContent, live:dkRecBox.classList.contains("live"),
                   count:recs.count, bytes:recs.bytes, tab:curTab(), tabText:tbRec.textContent,
                   sum:dkRecSum.textContent, clear:dkRecClr.style.display === "none" ? null : dkRecClr.textContent,
                   list:recs.list.slice(0, 5),
                   rows:[].map.call(dkRecList.querySelectorAll(".rc"), function(e){
                     var t = e.querySelectorAll(".go > *");
                     return { icon:t[0].textContent, time:t[1].textContent, dur:t[2].textContent, name:t[3].textContent,
                              db:t[4].textContent, cur:e.classList.contains("cur") }; }),
                   shown:dkRecList.offsetParent !== null,
                   playing:playing ? { name:playing.name, t:playing.audio ? +playing.audio.currentTime.toFixed(2) : null,
                                       paused:playing.audio ? playing.audio.paused : null } : null,
                   gain:gainNode ? gainNode.gain.value : null },
             signals:[].map.call(dkSig.querySelectorAll(".sg"), function(e){
               var t = e.querySelectorAll(".go > *");
               return { mhz:t[1].textContent, db:t[2].textContent, name:t[3].textContent, when:t[4].textContent,
                        lamp:t[0].className.replace("sdr-dot", "").trim(), cur:e.classList.contains("cur"),
                        airport:e.querySelector(".ap").style.display !== "none" }; }),
             signalNote:dkSigNote.textContent,
             rows:rowsUI.items.map(function(it){
               return { text:it.btn.textContent, disabled:it.btn.disabled, why:it.btn.title,
                        lamp:it.dot.className.replace("sdr-dot", "").trim() }; }),
             audio:{ ctx:actx ? actx.state : null, ready:soundReady(), on:audioOn, muted:muted, volume:volume,
                     frames:heard.frames, played:heard.played, hz:Math.round(heard.hz),
                     rms:+heard.rms.toFixed(3), err:audioErr } };
  },
  act:act,
  carrierOf:function(m){ var c = carrierOf(m); return c ? [c.hz, c.sp] : null; }
};
})();

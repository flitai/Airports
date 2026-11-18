// Airports · 频谱曲线与瀑布图
// SPDX-License-Identifier: GPL-3.0-only
//
// 这个文件只管画。它不知道 WebSocket，也不知道接收机处在什么模式：sdr.js 把
// 守护进程发来的频谱帧原样递进来，再告诉它哪些频率上该画标记。首次打开瀑布图
// 时才由 sdr.js 注入。
//
// 两点设计：
//   * 留着的是原始数据（每行 3276 格的 8 位电平），不是画好的图。缩放、改窗口大小、
//     接收机换了中心频率，都从原始数据重画一遍，所以历史永远对在正确的频率上。
//   * 一个屏幕像素通常盖住好几格。取这几格里的最大值而不是让浏览器去插值缩小：
//     话音载波只有两三格宽，插值会让它时亮时暗，甚至整条漏掉。
(function(){
"use strict";
if (window.__sdrWaterfall) return;

var HIST = 512;                    // 留多少行历史。每秒约 12 行，一个 CSS 像素一行
var BG = [9, 13, 28];
// 色标：深蓝 → 蓝 → 青 → 黄 → 近白。亮度单调上升，底噪沉在暗处，信号越强越亮
var STOPS = [[0, 9, 13, 28], [.18, 20, 38, 104], [.38, 24, 104, 168], [.58, 34, 176, 150],
             [.76, 214, 208, 72], [.9, 255, 236, 160], [1, 255, 255, 255]];
var FLOOR_DB = 3, RANGE_DB = 48;   // 色标从底噪以下 3 dB 铺到底噪以上 45 dB
var TRACE_LO = 6, TRACE_HI = 54;   // 频谱曲线的纵轴：底噪以下 6 dB 到以上 54 dB
var TRACE_TOP_DBFS = 3;            // 但不超过满幅：底噪高的时候，满幅以上那一截是空的，白占地方
var FONT = "10.5px -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif";

function rgba(c){ return ((255 << 24) | (c[2] << 16) | (c[1] << 8) | c[0]) >>> 0; }   // 小端内存序 R G B A

function buildLut(){
  var lut = new Uint32Array(256), s = 0;
  for (var i=0;i<256;i++){
    var t = i / 255;
    while (s < STOPS.length - 2 && t > STOPS[s+1][0]) s++;
    var a = STOPS[s], b = STOPS[s+1], u = (t - a[0]) / (b[0] - a[0]);
    lut[i] = rgba([Math.round(a[1] + (b[1] - a[1]) * u), Math.round(a[2] + (b[2] - a[2]) * u),
                   Math.round(a[3] + (b[3] - a[3]) * u)]);
  }
  return lut;
}

function tickStep(hzPerPx){        // 刻度间隔：两条刻度至少隔 72 像素
  var steps = [5e3, 1e4, 2.5e4, 5e4, 1e5, 2.5e5, 5e5, 1e6];
  for (var i=0;i<steps.length;i++) if (steps[i] / hzPerPx >= 72) return steps[i];
  return 2e6;
}
function tickText(hz, step){
  var d = step >= 1e5 && step % 1e5 === 0 ? 1 : (step >= 5e4 && step % 5e4 === 0 ? 2 : 3);
  return (hz / 1e6).toFixed(d);
}

function create(canvas, opts){
  opts = opts || {};
  var ctx = canvas.getContext("2d"), hist = document.createElement("canvas"), hctx = hist.getContext("2d");
  var lut = buildLut(), bgPx = rgba(BG);
  var dpr = 1, W = 0, H = 0, cssW = 0, cssH = 0;       // W/H 是设备像素
  var rowImg = null, rowPx = null;
  var n = 0, data = null, rowCenter = new Float64Array(HIST), rowLo = new Float32Array(HIST);
  var head = 0, filled = 0;                            // head：最新一行在环形历史里的下标
  var center = 0, span = 0, floorQ = null, last = null, hold = null;
  var scale = { db_min:-120, db_step:.5 };
  var zoomK = 1, zoomOff = 0, viewLo = 0, viewHi = 0;  // 当前画的频率范围
  var marks = { channels:[], cands:[], listen:null };
  var hover = null, raf = 0, rows = 0, stamps = [], emptyText = "等待频谱数据…";

  function traceH(){ return Math.max(40, Math.min(130, Math.round(cssH * .34))); }
  var AXIS_H = 15;

  function setView(){
    var width = span / zoomK, lo = center + zoomOff - width / 2;
    lo = Math.max(center - span / 2, Math.min(center + span / 2 - width, lo));
    viewLo = lo; viewHi = lo + width; zoomOff = lo + width / 2 - center;
  }

  // 环形历史里的第 idx 行 → 一行像素。每行按它自己当时的中心频率落位
  function renderRow(idx, px, off){
    var b = span / n, x0 = (viewLo - rowCenter[idx]) / b + n / 2, step = (viewHi - viewLo) / W / b;
    var lo = rowLo[idx], k = 255 / (RANGE_DB / scale.db_step), base = idx * n;
    for (var c=0;c<W;c++){
      var i0 = Math.floor(x0 + c * step), i1 = Math.floor(x0 + (c + 1) * step);
      if (i1 <= i0) i1 = i0 + 1;
      if (i1 <= 0 || i0 >= n){ px[off + c] = bgPx; continue; }
      if (i0 < 0) i0 = 0;
      if (i1 > n) i1 = n;
      var m = 0;
      for (var i=i0;i<i1;i++){ var v = data[base + i]; if (v > m) m = v; }
      var t = (m - lo) * k;
      px[off + c] = lut[t < 0 ? 0 : (t > 255 ? 255 : t | 0)];
    }
  }

  function redrawAll(){
    if (!W) return;
    var img = hctx.createImageData(W, HIST), px = new Uint32Array(img.data.buffer);
    px.fill(bgPx);
    if (n) for (var j=0;j<filled;j++){ var idx = (head + j) % HIST; renderRow(idx, px, idx * W); }
    hctx.putImageData(img, 0, 0);
  }

  function resize(){
    var r = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    var w = Math.round(r.width * d), h = Math.round(r.height * d);
    if (!w || !h || (w === W && h === H && d === dpr)) return;
    dpr = d; W = w; H = h; cssW = r.width; cssH = r.height;
    canvas.width = W; canvas.height = H;
    hist.width = W; hist.height = HIST;
    rowImg = hctx.createImageData(W, 1); rowPx = new Uint32Array(rowImg.data.buffer);
    redrawAll(); invalidate();
  }

  function push(buf){
    if (!buf || buf.byteLength < 17) return;
    var dv = new DataView(buf);
    if (dv.getUint8(0) !== 2) return;
    var c = dv.getUint32(4, true), sp = dv.getUint32(8, true), fl = dv.getUint8(12), cnt = dv.getUint16(14, true);
    if (!cnt || buf.byteLength < 16 + cnt) return;
    var q = new Uint8Array(buf, 16, cnt), moved = false;
    if (cnt !== n || sp !== span){                     // 头一行，或者行的格式变了：从头来
      n = cnt; span = sp; data = new Uint8Array(HIST * n); hold = new Float32Array(n);
      head = filled = 0; floorQ = null; center = c; zoomK = 1; zoomOff = 0; setView(); moved = true;
    } else if (c !== center){
      // 接收机换了中心频率。眼下看的那一段如果还在新窗口里就留在原处，否则回到全景
      var keep = zoomK > 1 && viewLo >= c - span / 2 && viewHi <= c + span / 2;
      zoomOff = keep ? (viewLo + viewHi) / 2 - c : 0;
      if (!keep) zoomK = 1;
      center = c; hold.fill(0); setView(); moved = true;
      if (opts.onZoom) opts.onZoom(zoomK);
    }
    floorQ = floorQ == null ? fl : floorQ + .08 * (fl - floorQ);     // 平滑：增益变了，颜色不跳
    head = (head + HIST - 1) % HIST; filled = Math.min(HIST, filled + 1);
    data.set(q, head * n); rowCenter[head] = c; rowLo[head] = floorQ - FLOOR_DB / scale.db_step;
    last = q;
    for (var i=0;i<n;i++){ var h = hold[i] - .35; hold[i] = q[i] > h ? q[i] : h; }   // 峰值保持，慢慢回落
    rows++; stamps.push(performance.now()); if (stamps.length > 40) stamps.shift();
    if (W){
      if (moved) redrawAll();
      else { renderRow(head, rowPx, 0); hctx.putImageData(rowImg, 0, head); }
    }
    invalidate();
  }

  function invalidate(){ if (!raf) raf = requestAnimationFrame(draw); }

  function xOf(hz){ return (hz - viewLo) / (viewHi - viewLo) * cssW; }
  function freqAt(clientX){
    var r = canvas.getBoundingClientRect();
    return viewLo + (clientX - r.left) / r.width * (viewHi - viewLo);
  }
  function binAt(hz){ return Math.round((hz - center) / (span / n) + n / 2); }

  function colMax(arr, c){           // 第 c 个设备像素列盖住的那几格里的最大值；不在窗口里返回 -1
    var b = span / n, x0 = (viewLo - center) / b + n / 2, step = (viewHi - viewLo) / W / b;
    var i0 = Math.floor(x0 + c * step), i1 = Math.floor(x0 + (c + 1) * step);
    if (i1 <= i0) i1 = i0 + 1;
    if (i1 <= 0 || i0 >= n) return -1;
    if (i0 < 0) i0 = 0;
    if (i1 > n) i1 = n;
    var m = 0;
    for (var i=i0;i<i1;i++) if (arr[i] > m) m = arr[i];
    return m;
  }

  function traceTopQ(){            // 纵轴顶端对应的量化值
    return Math.min(floorQ + TRACE_HI / scale.db_step, (TRACE_TOP_DBFS - scale.db_min) / scale.db_step);
  }
  function traceY(q, th){
    var lo = floorQ - TRACE_LO / scale.db_step, hi = traceTopQ();
    var t = (q - lo) / (hi - lo);
    return th - 2 - Math.max(0, Math.min(1, t)) * (th - 7);
  }

  function tracePath(arr, th){
    ctx.beginPath();
    var started = false;
    for (var c=0;c<W;c++){
      var m = colMax(arr, c); if (m < 0) continue;
      var x = (c + .5) / dpr, y = traceY(m, th);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    }
    return started;
  }

  function label(text, x, y, color, taken){
    var w = ctx.measureText(text).width, l = Math.max(2, Math.min(cssW - w - 2, x - w / 2));
    if (taken){
      for (var i=0;i<taken.length;i++) if (l < taken[i][1] + 5 && l + w > taken[i][0] - 5) return false;
      taken.push([l, l + w]);
    }
    ctx.fillStyle = color; ctx.fillText(text, l, y);
    return true;
  }

  function draw(){
    raf = 0;
    if (!W) return;
    var th = traceH(), top = th + AXIS_H;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "rgb(" + BG.join(",") + ")"; ctx.fillRect(0, 0, W, H);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = FONT; ctx.textBaseline = "alphabetic";
    if (!n || !filled){
      ctx.fillStyle = "rgba(255,255,255,.5)"; ctx.textAlign = "center";
      ctx.fillText(emptyText, cssW / 2, cssH / 2); ctx.textAlign = "left";
      return;
    }

    // ---- 瀑布图：最新的一行在最上面 ----
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    var rowH = Math.max(1, Math.round(dpr)), y0 = Math.round(top * dpr);
    var vis = Math.min(filled, Math.ceil((H - y0) / rowH)), a = Math.min(vis, HIST - head);
    ctx.drawImage(hist, 0, head, W, a, 0, y0, W, a * rowH);
    if (vis > a) ctx.drawImage(hist, 0, 0, W, vis - a, 0, y0 + a * rowH, W, (vis - a) * rowH);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // 瀑布图上不叠任何线：信号本身就是一条细线，画在它上面的标记只会把它挡住。
    // 标记都放在上半的曲线区里，而且垫在曲线下面。
    // ---- 正在听的频道：信道带宽一条浅带，以实际载波为中心 ----
    var L = marks.listen, lx = 0, half = 0;
    if (L){
      lx = xOf(L.hz + (L.afc || 0)); half = Math.max(1.5, (L.bw || 7600) / 2 / (viewHi - viewLo) * cssW);
      ctx.fillStyle = "rgba(96,165,250,.22)"; ctx.fillRect(lx - half, 0, 2 * half, th);
    }
    // ---- 各频道的标称频率：一条细线 ----
    var order = marks.channels.slice().sort(function(p, q){ return rank(q) - rank(p); });
    for (var i=0;i<order.length;i++){
      var ch = order[i], x = xOf(ch.hz);
      if (x < -20 || x > cssW + 20) continue;
      ctx.fillStyle = markColor(ch); ctx.globalAlpha = ch.state ? .55 : .3;
      ctx.fillRect(x - .5, 13, 1, th - 13); ctx.globalAlpha = 1;
    }
    // 窗口正中是接收机自己的直流毛刺，不是信号。标出来，免得被当成一个台
    var cx = xOf(center);
    if (cx > 0 && cx < cssW){
      ctx.strokeStyle = "rgba(255,255,255,.3)"; ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, 25); ctx.lineTo(cx, th); ctx.stroke(); ctx.setLineDash([]);
    }

    // ---- 纵轴：实际电平（dBFS）。横线垫在曲线下面，数字最后再写 ----
    var loDb = floorQ * scale.db_step + scale.db_min - TRACE_LO, hiDb = traceTopQ() * scale.db_step + scale.db_min;
    var dbStep = (th - 7) / (hiDb - loDb) * 10 >= 14 ? 10 : 20, levels = [];
    for (var d = Math.ceil(loDb / dbStep) * dbStep; d <= hiDb; d += dbStep){
      var ly = traceY((d - scale.db_min) / scale.db_step, th);
      if (ly < 17 || ly > th - 3) continue;            // 顶上那一行留给频道名，底边紧贴刻度条
      levels.push([d, ly]);
      ctx.fillStyle = "rgba(255,255,255,.09)"; ctx.fillRect(0, Math.round(ly) - .5, cssW, 1);
    }

    // ---- 频谱曲线 ----
    ctx.lineJoin = "round";
    if (hold && tracePath(hold, th)){ ctx.strokeStyle = "rgba(255,255,255,.28)"; ctx.lineWidth = 1; ctx.stroke(); }
    if (tracePath(last, th)){
      ctx.strokeStyle = "rgba(94,234,212,.95)"; ctx.lineWidth = 1.2; ctx.stroke();
      ctx.lineTo(cssW, th); ctx.lineTo(0, th); ctx.closePath();
      ctx.fillStyle = "rgba(45,212,191,.16)"; ctx.fill();
    }

    // 纵轴的数字写在左边，垫一块深色底：底噪那条线正好从这儿穿过
    for (var v=0;v<levels.length;v++){
      var txt = String(levels[v][0]).replace("-", "−") + (v === levels.length - 1 ? " dBFS" : "");
      var tw = ctx.measureText(txt).width;
      ctx.fillStyle = "rgba(9,13,28,.72)"; ctx.fillRect(2, levels[v][1] - 6, tw + 6, 12);
      ctx.fillStyle = "rgba(255,255,255,.7)"; ctx.fillText(txt, 5, levels[v][1] + 3.5);
    }

    // ---- 频率刻度 ----
    var hzPerPx = (viewHi - viewLo) / cssW, step = tickStep(hzPerPx);
    ctx.fillStyle = "rgba(9,13,28,.82)"; ctx.fillRect(0, th, cssW, AXIS_H);
    for (var f = Math.ceil(viewLo / step) * step; f <= viewHi; f += step){
      var tx = xOf(f);
      ctx.fillStyle = "rgba(255,255,255,.10)"; ctx.fillRect(tx - .5, 0, 1, th);
      ctx.fillStyle = "rgba(255,255,255,.35)"; ctx.fillRect(tx - .5, th, 1, 3);
      label(tickText(f, step), tx, th + 12, "rgba(255,255,255,.62)");
    }
    // 正在听的那一段在刻度条上再标一道，往下对着瀑布图里的那一列
    if (L){ ctx.fillStyle = "rgba(96,165,250,1)"; ctx.fillRect(lx - half, th, 2 * half, 3); }

    // ---- 频道的名字。挤不下就让正在听的、有信号的优先 ----
    var taken = [];
    for (var j=0;j<order.length;j++){
      var cx2 = xOf(order[j].hz);
      if (cx2 >= -20 && cx2 <= cssW + 20) label(order[j].text, cx2, 10, markColor(order[j]), taken);
    }
    // 不在频道表里的信号：顶上一个小三角
    for (var k=0;k<marks.cands.length;k++){
      var px = xOf(marks.cands[k].hz);
      if (px < 0 || px > cssW) continue;
      ctx.fillStyle = "rgba(251,191,36,.95)";
      ctx.beginPath(); ctx.moveTo(px - 4, th + 1); ctx.lineTo(px + 4, th + 1); ctx.lineTo(px, th + 7); ctx.closePath(); ctx.fill();
    }
    if (cx > 30 && cx < cssW - 30) label("接收机中心", cx, 22, "rgba(255,255,255,.4)", taken);

    // ---- 鼠标悬停：读数 ----
    if (hover && hover.x >= 0 && hover.x <= cssW){
      var hz = viewLo + hover.x / cssW * (viewHi - viewLo), bin = binAt(hz);
      ctx.fillStyle = "rgba(255,255,255,.55)"; ctx.fillRect(hover.x - .5, 0, 1, th + AXIS_H);   // 同样不伸进瀑布图
      var lines = [(hz / 1e6).toFixed(4) + " MHz"];
      if (bin >= 0 && bin < n){
        var m = 0;
        for (var j=Math.max(0, bin - 2); j<=Math.min(n - 1, bin + 2); j++) if (last[j] > m) m = last[j];
        var up = (m - floorQ) * scale.db_step;
        lines.push((m * scale.db_step + scale.db_min).toFixed(0).replace("-", "−") + " dBFS" +
                   (up >= 3 ? " · 高出底噪 " + up.toFixed(0) + " dB" : ""));
      }
      var extra = opts.describe ? opts.describe(hz) : "";
      if (extra) lines.push(extra);
      var bw = 0;
      for (var q=0;q<lines.length;q++) bw = Math.max(bw, ctx.measureText(lines[q]).width);
      var bx = hover.x + 10 + bw + 12 > cssW ? hover.x - bw - 22 : hover.x + 10;
      var by = Math.max(4, Math.min(cssH - lines.length * 14 - 10, hover.y - 10));
      ctx.fillStyle = "rgba(9,13,28,.9)"; ctx.fillRect(bx, by, bw + 12, lines.length * 14 + 6);
      ctx.strokeStyle = "rgba(255,255,255,.25)"; ctx.lineWidth = 1; ctx.strokeRect(bx + .5, by + .5, bw + 11, lines.length * 14 + 5);
      for (var r=0;r<lines.length;r++){ ctx.fillStyle = r ? "rgba(255,255,255,.78)" : "#fff"; ctx.fillText(lines[r], bx + 6, by + 14 + r * 14); }
    }
  }
  function rank(ch){ return ch.state === "cur" ? 3 : (ch.state === "on" ? 2 : (ch.state === "steady" ? 1 : 0)); }
  function markColor(ch){
    return ch.state === "cur" ? "rgba(147,197,253,1)" : (ch.state === "on" ? "rgba(110,231,183,1)" :
           (ch.state === "steady" ? "rgba(252,211,77,.95)" : "rgba(255,255,255,.6)"));
  }

  function zoom(k, atHz){
    if (!span) return;
    k = Math.max(1, Math.min(8, k));
    if (atHz == null) atHz = (viewLo + viewHi) / 2;
    var frac = (atHz - viewLo) / (viewHi - viewLo), width = span / k;
    zoomK = k; zoomOff = atHz - frac * width + width / 2 - center;
    setView(); redrawAll(); invalidate();
    if (opts.onZoom) opts.onZoom(zoomK);
  }
  function pan(hz){ if (span && zoomK > 1){ zoomOff += hz; setView(); redrawAll(); invalidate(); } }

  canvas.addEventListener("mousemove", function(e){
    var r = canvas.getBoundingClientRect(); hover = { x:e.clientX - r.left, y:e.clientY - r.top }; invalidate(); });
  canvas.addEventListener("mouseleave", function(){ hover = null; invalidate(); });
  canvas.addEventListener("click", function(e){ if (span && opts.onPick) opts.onPick(freqAt(e.clientX)); });
  // 滚轮：以鼠标所在的频率为中心缩放；横向滚动（触控板左右滑）平移
  canvas.addEventListener("wheel", function(e){
    if (!span) return;
    e.preventDefault();
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) pan(e.deltaX * (viewHi - viewLo) / cssW);
    else if (e.deltaY) zoom(zoomK * (e.deltaY < 0 ? 1.4 : 1 / 1.4), freqAt(e.clientX));
  }, { passive:false });

  resize();
  return {
    push:push, resize:resize, zoom:zoom, freqAt:freqAt,
    xOf:function(hz){ return span ? xOf(hz) : null; },
    hzPerPx:function(){ return span && cssW ? (viewHi - viewLo) / cssW : 0; },
    setScale:function(s){ if (s && s.db_step) scale = s; },
    empty:function(text){ if (text !== emptyText){ emptyText = text; invalidate(); } },   // 还没有数据时画布上写什么
    marks:function(m){ marks = m; invalidate(); },
    clear:function(){ n = 0; span = 0; filled = 0; last = null; floorQ = null; redrawAll(); invalidate(); },
    // 给自动化验收用：行数、行速率、当前窗口、最强的一格在哪儿
    stats:function(){
      var peak = -1, at = 0;
      if (last) for (var i=0;i<n;i++) if (Math.abs(i - n / 2) > 4 && last[i] > peak){ peak = last[i]; at = i; }
      var dt = stamps.length > 1 ? (stamps[stamps.length - 1] - stamps[0]) / 1000 : 0;
      return { rows:rows, held:filled, center:center, span:span, bins:n, zoom:+zoomK.toFixed(2),
               viewLo:Math.round(viewLo), viewHi:Math.round(viewHi), w:W, h:H,
               rate:dt ? +((stamps.length - 1) / dt).toFixed(1) : 0,
               floorDb:floorQ == null ? null : +(floorQ * scale.db_step + scale.db_min).toFixed(1),
               peakHz:last ? Math.round(center + (at - n / 2) * span / n) : null,
               peakDb:last ? peak * scale.db_step + scale.db_min : null,
               // 画布正中偏下那一点画出来的颜色：全黑说明瀑布图其实没画上去
               lit:(function(){ try { var d = ctx.getImageData(W >> 1, Math.min(H - 1, Math.round((traceH() + AXIS_H + 3) * dpr)), 1, 1).data;
                                      return d[0] + d[1] + d[2]; } catch (e) { return -1; } })() };
    }
  };
}

window.__sdrWaterfall = { create:create };
})();

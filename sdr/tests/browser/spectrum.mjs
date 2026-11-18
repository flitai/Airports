// 频谱与瀑布图端到端验收（合成信号）。用法：node spectrum.mjs <调试端口> <页面URL>
// 合成的话音台见 run.sh：在 voice 那一组的基础上多一个 119.3，希思罗的频率表里没有它。
const [port, url] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let wsUrl; for (let i=0;i<50;i++){ try { const l = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); const p = l.find(t=>t.type==="page"); if (p){ wsUrl=p.webSocketDebuggerUrl; break; } } catch {} await sleep(200); }
const ws = new WebSocket(wsUrl); await new Promise(r => ws.onopen = r);
let seq=0; const pend=new Map(); const errors=[];
ws.onmessage = e => { const m=JSON.parse(e.data);
  if (m.id && pend.has(m.id)){ pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method==="Runtime.exceptionThrown") errors.push("EXC "+(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text).slice(0,300));
  if (m.method==="Runtime.consoleAPICalled" && m.params.type==="error") errors.push("CONSOLE "+m.params.args.map(a=>a.value??a.description).join(" ").slice(0,300)); };
const send=(method,params={})=>new Promise(r=>{const id=++seq;pend.set(id,r);ws.send(JSON.stringify({id,method,params}));});
const ev = async x => { const r = await send("Runtime.evaluate",{expression:x,returnByValue:true,awaitPromise:true}); if (r.result?.exceptionDetails) throw new Error("eval failed: "+x.slice(0,80)+" :: "+(r.result.exceptionDetails.exception?.description||"")); return r.result.result.value; };
const probe = () => ev("window.__probe ? window.__probe() : null");
const sdr = async () => (await probe())?.sdr;
async function until(label, fn, ms=30000){ const t0=Date.now(); while(Date.now()-t0<ms){ const v = await fn(); if (v) return v; await sleep(200);} const s = await sdr(); throw new Error("timeout: "+label+" :: "+JSON.stringify({pill:s?.pill, spectrum:s?.spectrum, signals:s?.signals, air:s?.air, errors})); }
let fails=0; const check=(name,ok,detail)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+name+(detail!==undefined?"  "+JSON.stringify(detail):"")); };
const key = async (k, code, vk) => { for (const type of ["keyDown","keyUp"]) await send("Input.dispatchKeyEvent",{type,key:k,code,windowsVirtualKeyCode:vk,text:type==="keyDown"&&k.length===1?k:undefined}); };
const near = (v, want, tol) => typeof v === "number" && Math.abs(v - want) <= tol;
const layout = () => ev('(function(){var w=document.getElementById("wrap");return {dock:getComputedStyle(w).getPropertyValue("--dock-h").trim(),detail:getComputedStyle(document.getElementById("detail")).bottom,h:document.getElementById("sdrDock").offsetHeight}})()');
const rowRate = async () => { const a = (await sdr()).spectrum.stats.rows, t0 = Date.now(); await sleep(3000); return ((await sdr()).spectrum.stats.rows - a) / ((Date.now() - t0) / 1000); };
const sig = (s, mhz) => s.signals.find(x => x.mhz === mhz);

await send("Runtime.enable"); await send("Page.enable"); await send("Page.navigate",{url});
await until("ready", async()=>{ const p=await probe(); return p&&p.ready&&p.styleLoaded; }, 60000);
await ev("__act.fly(-0.45, 51.47, 8.5)");
let s = await until("adsb running", async()=>{ const p=await probe(); return p.sdr && p.sdr.link==="up" && p.sdr.adsb?.state==="running" && p.flights.local>=3 ? p.sdr : null; }, 60000);

// ---- ADS-B 模式：默认不开，不开就不让守护进程算频谱 ----
check("默认：ADS-B 下不出停靠栏，瀑布图的代码也没加载", !s.dock.on && s.spectrum.view==="bar" && !s.spectrum.loaded && !s.spectrum.on, s.spectrum);
check("没人看时守护进程一行频谱都不算", s.spectrum.watchers===0 && s.spectrum.daemonRows===0, {watchers:s.spectrum.watchers, rows:s.spectrum.daemonRows});
const EGLL = 2434;
await ev(`__act.detail(${EGLL})`);
await ev('document.activeElement && document.activeElement.blur()');
await key("w", "KeyW", 87);
s = await until("adsb spectrum", async()=>{ const q=await sdr(); return q.spectrum.mode==="adsb" && q.spectrum.stats && q.spectrum.stats.rows>30 ? q : null; }, 15000);
check("按 W 打开：ADS-B 模式下也有频谱，中心 1090 MHz", s.dock.on && s.spectrum.view==="half" && s.spectrum.stats.center===1090000000 && s.spectrum.title==="频谱 · ADS-B 1090 MHz", {title:s.spectrum.title, center:s.spectrum.stats.center});
check("一行 3276 格，跨度约 1.92 MHz", s.spectrum.stats.bins===3276 && near(s.spectrum.stats.span, 1920000, 1000), {bins:s.spectrum.stats.bins, span:s.spectrum.stats.span});
let rate = await rowRate();
// 页面这头的下限放宽到 8：无头浏览器用软件渲染地图，忙起来会让最新一行顶掉上一行。
// 守护进程出行的速率由单元测试按 10–15 卡死
check("瀑布图每秒十来行", rate>=8 && rate<=15, {rate:+rate.toFixed(1)});
s = await sdr();
check("瀑布图真的画上去了（不是一块黑）", s.spectrum.stats.lit>60 && s.spectrum.canvas.h===150 && s.spectrum.canvas.w>400, {lit:s.spectrum.stats.lit, canvas:s.spectrum.canvas});
check("ADS-B 模式下只有频谱：话音控件、频道条、信号列表都不出现", await ev('[].every.call(document.querySelectorAll("#sdrDock .air"),function(e){return e.offsetParent===null})'));
let lay = await layout();
check("详情面板给停靠栏让位", lay.h>180 && lay.dock===lay.h+"px" && lay.detail===lay.h+"px", lay);
check("守护进程知道有一个页面在看", s.spectrum.watchers===1 && s.spectrum.on, {watchers:s.spectrum.watchers});
check("底噪读数如实显示", /^底噪 −\d+ dBFS$/.test(s.dock.now) && s.spectrum.stats.floorDb<-50, {now:s.dock.now, floor:s.spectrum.stats.floorDb});

// ---- 三档；Esc 逐层收 ----
await ev('__act.sdr("view","full")');
s = await until("full", async()=>{ const q=await sdr(); return q.spectrum.view==="full" && q.spectrum.canvas.h>=240 ? q : null; }, 5000);
check("展开一档更高", true, s.spectrum.canvas);
await key("Escape", "Escape", 27);
s = await until("esc", async()=>{ const q=await sdr(); return q.spectrum.view==="half" ? q : null; }, 5000);
check("展开时按 Esc 先收到半高，详情面板还开着", (await probe()).detailOpen===true, s.spectrum.view);
await key("w", "KeyW", 87); await key("w", "KeyW", 87);
s = await until("bar", async()=>{ const q=await sdr(); return !q.dock.on && q.spectrum.view==="bar" && q.spectrum.watchers===0 ? q : null; }, 8000);
const r1 = s.spectrum.daemonRows; await sleep(1500); s = await sdr();
check("W 循环到收起：停靠栏消失，退订，守护进程不再算频谱", s.spectrum.daemonRows===r1 && !s.spectrum.on, {rows:[r1, s.spectrum.daemonRows]});
lay = await layout();
check("版面复原", lay.dock==="0px", lay);

// ---- 进话音：瀑布图默认半高，跟到窗口上 ----
await ev('__act.sdr("listenRow", 12)');
s = await until("air spectrum", async()=>{ const q=await sdr(); return q.mode==="air" && q.air?.open && q.spectrum.stats && q.spectrum.stats.center<200e6 && q.spectrum.stats.rows>24 ? q : null; }, 20000);
check("话音模式默认带半高的瀑布图，中心就是窗口中心", s.spectrum.view==="half" && s.spectrum.stats.center===s.rf.center_hz && s.dock.chips===13, {center:s.spectrum.stats.center, rf:s.rf.center_hz});
rate = await rowRate();
check("话音模式下同样每秒十来行", rate>=8 && rate<=15, {rate:+rate.toFixed(1)});
s = await until("signals", async()=>{ const q=await sdr(); return q.signals.length>=3 && sig(q, "119.300") && sig(q, "119.300").lamp==="on" ? q : null; }, 15000);
check("最强的一格落在某个合成台的载波上", [118493000, 119300000, 119725000].some(f => near(s.spectrum.stats.peakHz, f, 700)), {peakHz:s.spectrum.stats.peakHz});
check("信号列表：偏了 7 kHz 的塔台仍认作表里的 TWR 118.500，并标出正在听", sig(s,"118.493")?.name==="TWR 118.500" && sig(s,"118.493").cur && /^\+\d+ dB$/.test(sig(s,"118.493").db), sig(s,"118.493"));
check("信号列表：同窗口的干扰载波也在，带频道名", sig(s,"119.725")?.name==="DIR 119.725" && !sig(s,"119.725").cur, sig(s,"119.725"));
check("信号列表：频率表里没有的台标为未知", sig(s,"119.300")?.name==="未知" && !sig(s,"119.300").airport, sig(s,"119.300"));
check("信号按频率从低到高排，顺序是稳的", s.signals.filter(x=>x.lamp).map(x=>x.mhz).join(",")==="118.493,119.300,119.725", s.signals.map(x=>x.mhz));
check("没设接收机位置时给出内联提示", /设定接收机位置后/.test(s.signalNote), s.signalNote);
s = await until("steady", async()=>{ const q=await sdr(); return sig(q,"119.725")?.lamp==="steady" ? q : null; }, 30000);
check("一直在的载波标为持续", /^持续 \d+ 秒$/.test(sig(s,"119.725").when), sig(s,"119.725"));

// ---- 设了接收机位置：推测未知信号属于附近哪个机场 ----
await ev('__act.sdr("home", [51.47, -0.45])');
s = await until("guess", async()=>{ const q=await sdr(); const x=sig(q,"119.300"); return x && /^可能是 /.test(x.name) ? q : null; }, 10000);
check("未知信号给出「可能是」哪个机场，并能打开那个机场", /^可能是 \S+ .*· \d+ km$/.test(sig(s,"119.300").name) && sig(s,"119.300").airport && s.signalNote==="", sig(s,"119.300"));
const carriers = await ev('JSON.stringify(["118.5","118.005","118.010","118.015","118.02","118.025","113.75","137.5"].map(window.__sdr.carrierOf))');
check("页面这份频道名换算与守护进程一致", carriers===JSON.stringify([[118500000,"25"],[118000000,"8.33"],[118008333,"8.33"],[118016667,"8.33"],[118025000,"25"],[118025000,"25"],null,null]), carriers);

// ---- 点击调谐 ----
await ev('__act.sdr("clickFreq", 119725000)');
s = await until("click known", async()=>{ const q=await sdr(); return q.air.listenLabel==="119.725" ? q : null; }, 8000);
check("点瀑布图上表里的频道：直接切过去", s.dock.chips===13 && /DIR119\.725/.test(s.dock.cur), s.dock.cur);
await until("stray live", async()=>{ const q=await sdr(); return sig(q,"119.300")?.lamp==="on"; }, 10000);
await ev('__act.sdr("clickFreq", 119301500)');           // 故意点偏 1.5 kHz
s = await until("click unknown", async()=>{ const q=await sdr(); return q.air.listenLabel==="119.300" ? q : null; }, 8000);
check("点表外的信号：多出一个手动频道并收听", s.dock.chips===14 && /手动119\.300/.test(s.dock.cur), s.dock.cur);
s = await until("unknown heard", async()=>{ const q=await sdr(); return q.air.listenLabel==="119.300" && q.air.open && near(q.audio.hz, 400, 60) ? q : null; }, 15000);
check("听到的确实是那个台的 400 Hz", true, {hz:s.audio.hz});
const row = s.signals.findIndex(x => x.mhz==="118.493");
await ev(`__act.sdr("clickSignal", ${row})`);
s = await until("click list", async()=>{ const q=await sdr(); return q.air.listenLabel==="118.500" && sig(q,"118.493").cur ? q : null; }, 8000);
check("点信号列表里的一行：切回塔台", /TWR118\.500/.test(s.dock.cur), s.dock.cur);
check("刚才手动调出的频道留在频道条上，列表里仍按机场去猜它是谁", s.dock.chips===14 && /^可能是 /.test(sig(s,"119.300").name), sig(s,"119.300"));
await ev('__act.sdr("dropManual")');
s = await until("drop manual", async()=>{ const q=await sdr(); return q.dock.chips===13 ? q : null; }, 8000);
check("点手动频道上的叉：它从频道条上消失，正在听的塔台不受打扰", s.air.listenLabel==="118.500" && s.mode==="air" && sig(s,"119.300")?.name!=="手动 119.300", s.dock.cur);

// ---- 缩放 ----
await ev('__act.sdr("zoom", [4, 118500000])');
s = await sdr();
check("放大 4 倍：看的范围缩到四分之一，118.5 仍在画面里", s.spectrum.stats.zoom===4 && near(s.spectrum.stats.viewHi - s.spectrum.stats.viewLo, s.spectrum.stats.span/4, 2) && s.spectrum.stats.viewLo<118500000 && s.spectrum.stats.viewHi>118500000 && s.spectrum.zoomBtn, s.spectrum.stats);
await ev('document.querySelector("#sdrDock .zoom").click()');
s = await sdr();
check("点「回到全景」复位", s.spectrum.stats.zoom===1 && !s.spectrum.zoomBtn);

// ---- 频率框：调到别的窗口，瀑布图跟过去 ----
const c0 = s.rf.center_hz, n0 = s.spectrum.stats.rows;
await ev('__act.sdr("freqBox", "128.075")');
s = await until("freq box", async()=>{ const q=await sdr(); return q.air.listenLabel==="128.075" && q.air.open && q.spectrum.stats.center===q.rf.center_hz && q.rf.center_hz!==c0 ? q : null; }, 10000);
check("在频率框里敲 128.075：中心频率改变，瀑布图跟到新窗口", s.spectrum.stats.rows>n0 && Math.abs(s.rf.center_hz - 128075000) < 1e6, {from:c0, to:s.rf.center_hz});
s = await until("gone", async()=>{ const q=await sdr(); return sig(q,"118.493") && sig(q,"118.493").lamp==="" ? q : null; }, 6000);   // 候选每秒报一次
check("换了窗口：刚才那些信号还记着，但不再算正在发射", /^\d+ 秒前$/.test(sig(s,"118.493").when) && sig(s,"128.075")?.lamp, {old:sig(s,"118.493"), now:sig(s,"128.075")});
await ev('__act.sdr("freqBox", "136.975")');
s = await until("vdl2 freq", async()=>{ const q=await sdr(); return q.air.listenLabel==="136.975" && q.spectrum.stats.center===q.rf.center_hz ? q : null; }, 10000);
check("敲一个哪张表里都没有的频率（136.975）：照样调过去", /手动136\.975/.test(s.dock.cur) && near(s.rf.center_hz, 136975000, 1e6), {cur:s.dock.cur, center:s.rf.center_hz});
await ev('__act.sdr("freqBox", "150")'); s = await sdr();
check("超出波段的频率：拒绝并说明", /只能调到民航话音波段/.test(s.pill) && s.air.listenLabel==="136.975", s.pill);

// ---- 标签页不在前台：退订 ----
await ev('Object.defineProperty(document,"hidden",{value:true,configurable:true}); document.dispatchEvent(new Event("visibilitychange"))');
s = await until("hidden", async()=>{ const q=await sdr(); return !q.spectrum.on && q.spectrum.watchers===0 ? q : null; }, 8000);
check("标签页不在前台时退订频谱", true);
await ev('delete document.hidden; document.dispatchEvent(new Event("visibilitychange"))');
s = await until("visible", async()=>{ const q=await sdr(); return q.spectrum.on && q.spectrum.watchers===1 && q.spectrum.stats.rows>5 ? q : null; }, 8000);
check("回到前台重新订阅", true);

// ---- 切底图 ----
let p = await probe();
const target = p.activeMode==="online" ? "offline" : "online";
await ev(`__act.base(${JSON.stringify(target)})`);
p = await until("restyle", async()=>{ const q=await probe(); return q.styleLoaded && q.sdr.layers.home ? q : null; }, 12000);
const n1 = p.sdr.spectrum.stats.rows; await sleep(1000); p = await probe();
check("切底图后接收机图层还在，停靠栏和瀑布图照常", p.sdr.layers.rings && p.sdr.dock.on && p.sdr.spectrum.stats.rows>n1, {layers:p.sdr.layers});

// ---- 回 ADS-B：那边的档位是各记各的；刷新后还记得 ----
await ev('document.querySelector("#sdrPill .back").click()');
s = await until("back", async()=>{ const p=await probe(); return p.sdr.mode==="adsb" && p.sdr.adsb.state==="running" && !p.sdr.dock.on && p.sdr.spectrum.watchers===0 ? p.sdr : null; }, 20000);
check("回到 ADS-B：之前在这边收起过，所以不出停靠栏", s.spectrum.views.adsb==="bar" && s.spectrum.views.air==="half", s.spectrum.views);
await ev('document.querySelector("#scroll .sdr-btns").lastChild.click()');
s = await until("sidebar button", async()=>{ const q=await sdr(); return q.dock.on && q.spectrum.mode==="adsb" && q.spectrum.button==="隐藏瀑布图" ? q : null; }, 8000);
check("侧栏的按钮也能开关瀑布图", true, s.spectrum.button);
await send("Page.reload");
s = await until("reload", async()=>{ const q=await probe(); return q&&q.ready&&q.sdr&&q.sdr.link==="up"&&q.sdr.spectrum.stats&&q.sdr.spectrum.stats.rows>5 ? q.sdr : null; }, 60000);
check("刷新页面后记得档位，瀑布图自己回来", s.spectrum.view==="half" && s.spectrum.mode==="adsb" && s.spectrum.stats.center===1090000000, s.spectrum.views);

check("页面无脚本异常", errors.length===0, errors.slice(0,5));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
ws.close(); process.exit(fails?1:0);

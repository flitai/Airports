// 原始 I/Q 的录制与回放端到端验收（合成信号）。用法：node iq.mjs <调试端口> <页面URL>
// 合成台同 voice 那一组：塔台 118.5 偏 −7 kHz 连续 1 kHz；119.725 是空载波；地面 121.9 说 2 秒停 5 秒。
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
async function until(label, fn, ms=30000){ const t0=Date.now(); while(Date.now()-t0<ms){ const v = await fn(); if (v) return v; await sleep(200);} const s = await sdr(); throw new Error("timeout: "+label+" :: "+JSON.stringify({pill:s?.pill, iq:s?.iq, replay:s?.replay, device:s?.device, air:s?.air, errors})); }
let fails=0; const check=(name,ok,detail)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+name+(detail!==undefined?"  "+JSON.stringify(detail):"")); };
const chip = label => ev(`(function(){var c=[].find.call(document.querySelectorAll("#sdrDock .chip"),function(c){return c.textContent.indexOf(${JSON.stringify(label)})>=0});if(c)c.click();return !!c})()`);
const near = (v, want, tol) => typeof v === "number" && Math.abs(v - want) <= tol;

await send("Runtime.enable"); await send("Page.enable"); await send("Page.navigate",{url});
await until("ready", async()=>{ const p=await probe(); return p&&p.ready&&p.styleLoaded; }, 60000);
await ev("__act.fly(-0.45, 51.47, 8.5)");
await until("adsb running", async()=>{ const p=await probe(); return p.sdr && p.sdr.link==="up" && p.sdr.adsb?.state==="running" && p.flights.local>=3; }, 60000);
const EGLL = 2434;
await ev(`__act.detail(${EGLL})`); await sleep(300);
await ev('__act.sdr("listenRow", 12)');                  // 塔台 118.5
let s = await until("tower", async()=>{ const q=await sdr(); return q.mode==="air" && q.air?.open && q.audio.frames>40 && q.spectrum.stats ? q : null; }, 20000);

// ---- 手动录一段 ----
check("标题行有「立即录 I/Q」按钮；还没有录过", s.iq.buttonShown && s.iq.button==="立即录 I/Q" && !s.iq.live && s.iq.count===0 && s.iq.tabText==="I/Q" && !s.replay, s.iq);
await ev('__act.sdr("sideTab", "iq")'); s = await sdr();
check("右侧栏的「I/Q」页：空的，并说明怎么录", s.iq.shown && /还没有 I\/Q/.test(s.iq.sum) && s.iq.clear===null, s.iq.sum);
const center = s.rf.center_hz;
await sleep(1600);                                       // 让守护进程留着的那 1.5 秒攒满
await ev('__act.sdr("iq")');
s = await until("capturing", async()=>{ const q=await sdr(); return q.iq.live && q.iq.active ? q : null; }, 5000);
check("按下开录：按钮变红，写明时长和大小", /^● I\/Q \d:\d\d · \d+(\.\d)? MB$/.test(s.iq.button) && s.iq.active.kind==="manual", s.iq.button);
await sleep(2500);
await ev('__act.sdr("iq")');
s = await until("captured", async()=>{ const q=await sdr(); return q.iq.count===1 && !q.iq.active && q.iq.rows.length===1 ? q : null; }, 8000);
const cap = s.iq.list[0];
check("再按停止：存下整个窗口，带上按下之前的一秒半", cap.kind==="manual" && cap.mode==="air" && cap.center_hz===center && cap.listen==="118.500" && cap.airport===EGLL && cap.duration_s>=3.6 && cap.duration_s<=6 && cap.dropped_blocks===0 && cap.bytes===Math.round(cap.duration_s*4.8e6/65536)*65536, {dur:cap.duration_s, bytes:cap.bytes, name:cap.name});
check("列表里写明时刻、时长、哪个窗口、多大", /^\d\d:\d\d:\d\d$/.test(s.iq.rows[0].time) && /^\d\.\d 秒$/.test(s.iq.rows[0].dur) && s.iq.rows[0].name==="窗口 "+(center/1e6).toFixed(3)+" · EGLL" && /^\d+(\.\d)? MB$/.test(s.iq.rows[0].size) && s.iq.rows[0].icon==="▶", s.iq.rows[0]);
check("页签和底栏报数", s.iq.tabText==="I/Q 1" && /^1 段 · \d+(\.\d)? MB \/ 2000 MB$/.test(s.iq.sum) && s.iq.button==="立即录 I/Q", {tab:s.iq.tabText, sum:s.iq.sum});

// ---- 回放 ----
const f0 = s.audio.frames;
await ev('__act.sdr("replayIq", 0)');
s = await until("replaying", async()=>{ const q=await sdr(); return q.replay && q.replay.name===cap.name && q.device.state==="ready" && q.air?.open && q.audio.frames>f0+40 ? q : null; }, 20000);
check("点一段即回放：接收机换成这段录制", /^回放 · /.test(s.device.model) && s.mode==="air" && s.air.listenLabel==="118.500", s.device.model);
check("状态条、标题、按钮都写明这是回放", /^回放 · 话音 · TWR 118\.500 · .*真接收机已释放$/.test(s.pill) && /replay/.test(s.pillClass) && s.pillBack==="返回实时" && s.dock.back==="返回实时" && /^回放 · 话音监听/.test(s.spectrum.title), {pill:s.pill, cls:s.pillClass});
check("回放时没有「立即录 I/Q」按钮；列表里标出正在放的那段", !s.iq.buttonShown && s.iq.rows[0].icon==="■" && s.iq.rows[0].cur, s.iq.rows[0]);
s = await until("replay tone", async()=>{ const q=await sdr(); return near(q.audio.hz, 1000, 60) && near(q.air.afc, -7000, 80) ? q : null; }, 10000);
check("听到的是录下来的塔台：1 kHz，载波仍偏 −7 kHz", true, {hz:s.audio.hz, afc:s.air.afc});
const n0 = s.spectrum.stats.rows; await sleep(1500); s = await sdr();
check("瀑布图照常在走", s.spectrum.stats.rows>n0 && s.spectrum.stats.center===s.rf.center_hz, {rows:[n0, s.spectrum.stats.rows]});
check("同窗口里别的频道也录在里面", await chip("119.725"));
s = await until("other channel", async()=>{ const q=await sdr(); return q.air.listenLabel==="119.725" && q.air.open && !q.air.voice ? q : null; }, 10000);
check("换着听：那根没有调制的载波也在", true, s.dock.now);
check("频率表上的指示灯和按钮仍对着原来的行", s.rows[5].text==="停止" && s.rows[5].lamp!=="far" && s.rows[12].text==="收听" && s.rows[11].lamp==="far", {dir:s.rows[5], twr:s.rows[12], gnd:s.rows[11]});
check("去另一个窗口", await chip("121.900"));
await until("other window", async()=>{ const q=await sdr(); return q.air.listenLabel==="121.900" && q.rf.center_hz!==center; }, 10000);
await sleep(4000); s = await sdr();
check("录的时候接收机没看着那个窗口：回放里那儿是空的", !s.air.open && s.air.active.length===0, {active:s.air.active, now:s.dock.now});
check("回到塔台的窗口", await chip("118.500"));
s = await until("back in window", async()=>{ const q=await sdr(); return q.air.listenLabel==="118.500" && q.air.open && near(q.audio.hz, 1000, 60) ? q : null; }, 10000);
check("录下来的信号还在原来的频率上", near(s.air.afc, -7000, 80), {afc:s.air.afc});

// ---- 返回实时 ----
await ev('document.querySelector("#sdrPill .back").click()');
let p = await until("live again", async()=>{ const q=await probe(); return !q.sdr.replay && q.sdr.mode==="adsb" && q.sdr.adsb.state==="running" && q.flights.local>=3 && !/回放/.test(q.sdr.device.model) ? q : null; }, 25000);
check("点「返回实时」：回到回放之前的模式，本地飞机回来", /^本地接收 · ADS-B 1090/.test(p.sdr.pill) && !/replay/.test(p.sdr.pillClass) && p.sdr.pillBack===null, p.sdr.pill);

// ---- 跟着话音触发自动录 ----
await ev('__act.sdr("listenRow", 11)');                  // 地面 121.9：说 2 秒停 5 秒
await until("ground", async()=>{ const q=await sdr(); return q.mode==="air" && q.air?.listenLabel==="121.900"; }, 15000);
await ev('__act.sdr("iqVoice", true)');
s = await until("iq only", async()=>{ const q=await sdr(); return q.iq.withVoice && q.iq.count>=2 && !q.iq.active ? q : null; }, 30000);
check("只勾「录 I/Q」不勾「录音」：有人说话照样自动存 I/Q，话音不存", s.rec.on===false && s.rec.count===0 && s.iq.list[0].kind==="voice" && s.rec.label==="录音" && !s.rec.live, {rec:s.rec.count, iq:s.iq.count});
check("两个勾选框和按钮的名字不重样", await ev('[].map.call(document.querySelectorAll("#sdrDock .ctl .tog span"),function(e){return e.textContent}).join("|")')==="轮巡|无话音时静音|录音|录 I/Q" && s.iq.button==="立即录 I/Q", s.iq.button);
await ev('__act.sdr("record", true)');
s = await until("voice iq", async()=>{ const q=await sdr(); return q.rec.on && q.rec.count>=2 && q.iq.count>=4 && !q.iq.active && !q.rec.live ? q : null; }, 40000);
const clip = s.rec.list[0], viq = s.iq.list.find(x => x.name===clip.iq);
check("每段话音都有一段同时录下的 I/Q，开头不晚于话音、盖住整段", viq && viq.kind==="voice" && viq.listen==="121.900" && viq.duration_s>clip.duration_s-0.3 && viq.duration_s<clip.duration_s+2.5 && viq.start<clip.start+0.5, {clip:clip.duration_s, iq:viq?.duration_s});
check("列表里标明是话音触发的", s.iq.rows.some(r => / · 话音触发$/.test(r.name)), s.iq.rows.map(r => r.name));
await ev('__act.sdr("iqVoice", false)'); await ev('__act.sdr("record", false)');
await until("both off", async()=>{ const q=await sdr(); return !q.iq.withVoice && !q.rec.on; }, 5000);

// ---- ADS-B 模式：也能录、也能翻 ----
await ev('document.querySelector("#sdrPill .back").click()');
await until("adsb", async()=>{ const q=await sdr(); return q.mode==="adsb" && q.adsb.state==="running"; }, 20000);
await ev('__act.sdr("view", "half")');
s = await until("adsb dock", async()=>{ const q=await sdr(); return q.dock.on && q.spectrum.mode==="adsb" && q.iq.shown ? q : null; }, 8000);
check("ADS-B 模式下打开停靠栏：有「立即录 I/Q」，右侧能翻 I/Q", s.iq.buttonShown && s.iq.rows.length>=3, {rows:s.iq.rows.length});
const n1 = s.iq.count;
await ev('__act.sdr("iq")'); await sleep(1500); await ev('__act.sdr("iq")');
s = await until("adsb capture", async()=>{ const q=await sdr(); return q.iq.count===n1+1 && !q.iq.active ? q : null; }, 8000);
check("录下 1090 MHz 的一段", s.iq.list[0].mode==="adsb" && s.iq.list[0].center_hz===1090000000 && s.iq.rows[0].name==="ADS-B 1090 MHz", s.iq.rows[0]);
await ev('__act.sdr("replayIq", 0)');
p = await until("adsb replay", async()=>{ const q=await probe(); return q.sdr.replay && q.sdr.mode==="adsb" && q.sdr.adsb.state==="running" && /^回放 · ADS-B 1090/.test(q.sdr.pill) ? q : null; }, 25000);
check("回放它：解码器照常跑，状态条写明是回放", p.sdr.pillBack==="返回实时" && /^回放 · 频谱/.test(p.sdr.spectrum.title), p.sdr.pill);

// ---- 删除与清空 ----
await ev('__act.sdr("deleteIq", 0)');                    // 删掉正在放的这段：先回到实时
s = await until("deleted", async()=>{ const q=await sdr(); return q.iq.count===n1 && !q.replay && q.device.state==="ready" && !/回放/.test(q.device.model) ? q : null; }, 25000);
check("删掉正在回放的那段：自动回到实时接收", s.mode==="adsb", s.pill);
await ev('__act.sdr("clearIq")'); s = await sdr();
check("清空要点两下", s.iq.clear==="再点一次确认" && s.iq.count===n1);
await ev('__act.sdr("clearIq")');
s = await until("cleared", async()=>{ const q=await sdr(); return q.iq.count===0 && q.iq.rows.length===0 ? q : null; }, 5000);
check("清空", s.iq.tabText==="I/Q" && s.iq.clear===null && s.rec.count>=1, {iq:s.iq.count, rec:s.rec.count});

check("页面无脚本异常", errors.length===0, errors.slice(0,5));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
ws.close(); process.exit(fails?1:0);

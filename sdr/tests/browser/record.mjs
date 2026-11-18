// 话音触发录音端到端验收（合成信号）。用法：node record.mjs <调试端口> <页面URL>
// 用的是 voice 那一组的合成台：地面 121.9 说 2 秒停 5 秒，119.725 是一根没有调制的干扰载波。
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
async function until(label, fn, ms=30000){ const t0=Date.now(); while(Date.now()-t0<ms){ const v = await fn(); if (v) return v; await sleep(200);} const s = await sdr(); throw new Error("timeout: "+label+" :: "+JSON.stringify({pill:s?.pill, rec:s?.rec, air:s?.air, errors})); }
let fails=0; const check=(name,ok,detail)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+name+(detail!==undefined?"  "+JSON.stringify(detail):"")); };
const chip = label => ev(`(function(){var c=[].find.call(document.querySelectorAll("#sdrDock .chip"),function(c){return c.textContent.indexOf(${JSON.stringify(label)})>=0});if(c)c.click();return !!c})()`);

await send("Runtime.enable"); await send("Page.enable"); await send("Page.navigate",{url});
await until("ready", async()=>{ const p=await probe(); return p&&p.ready&&p.styleLoaded; }, 60000);
await ev("__act.fly(-0.45, 51.47, 8.5)");
await until("adsb running", async()=>{ const p=await probe(); return p.sdr && p.sdr.link==="up" && p.sdr.adsb?.state==="running"; }, 60000);
const EGLL = 2434;
await ev(`__act.detail(${EGLL})`); await sleep(300);
await ev('__act.sdr("listenRow", 11)');                  // 地面 121.9
let s = await until("ground", async()=>{ const q=await sdr(); return q.mode==="air" && q.air?.listenLabel==="121.900" && q.spectrum.stats ? q : null; }, 20000);

// ---- 默认不录 ----
check("录音默认关着", s.rec.on===false && s.rec.label==="录音" && s.rec.count===0 && s.rec.tabText==="录音", s.rec);
await ev('__act.sdr("sideTab", "rec")');
s = await sdr();
check("右侧栏切到「录音」页：空的，并说明怎么开", s.rec.tab==="rec" && s.rec.shown && /还没有录音/.test(s.rec.sum) && s.rec.clear===null && s.signals.length>=0, s.rec.sum);

// ---- 打开：有人说话就自动存一段 ----
await ev('__act.sdr("record", true)');
s = await until("on", async()=>{ const q=await sdr(); return q.rec.on===true ? q : null; }, 5000);
check("勾上「录音」", /录音开着/.test(s.rec.sum), s.rec.sum);
s = await until("live", async()=>{ const q=await sdr(); return q.rec.live && /^录音 \d:\d\d$/.test(q.rec.label) ? q : null; }, 12000);
check("正在录时开关变成走秒的计时，状态条也写明", /正在录音/.test(s.pill), {label:s.rec.label, pill:s.pill});
s = await until("two clips", async()=>{ const q=await sdr(); return q.rec.count>=2 && !q.rec.live ? q : null; }, 25000);
const c = s.rec.list[0];
check("一次通话一段：2 秒的通话录成约 3 秒（含前置留白和收尾）", c.label==="121.900" && c.type==="GND" && c.airport===EGLL && c.end==="closed" && c.duration_s>=2.5 && c.duration_s<=3.6 && Math.abs(c.pre_s-0.5)<0.06, {dur:c.duration_s, pre:c.pre_s, speech:c.speech_s, name:c.name});
check("文件名是 UTC 时刻加频率", /^\d{8}T\d{6}Z_121\.900(-\d+)?\.wav$/.test(c.name), c.name);
check("列表里写明时刻、时长、频道和机场", s.rec.rows.length===s.rec.count && /^\d\d:\d\d:\d\d$/.test(s.rec.rows[0].time) && /^\d\.\d 秒$/.test(s.rec.rows[0].dur) && s.rec.rows[0].name==="GND 121.900 · EGLL" && /^\+\d+ dB$/.test(s.rec.rows[0].db) && s.rec.rows[0].icon==="▶", s.rec.rows[0]);
check("页签和底栏报数", s.rec.tabText==="录音 "+s.rec.count && new RegExp("^"+s.rec.count+" 段 · \\d+\\.\\d MB / 200 MB$").test(s.rec.sum) && s.rec.clear==="清空", {tab:s.rec.tabText, sum:s.rec.sum});

// ---- 回放 ----
await ev('__act.sdr("playRec", 0)');
s = await until("playing", async()=>{ const q=await sdr(); return q.rec.playing && q.rec.playing.t>0.3 && !q.rec.playing.paused ? q : null; }, 8000);
check("点一下回放；这时现场的声音让一让", s.rec.playing.name===c.name && s.rec.rows[0].icon==="■" && s.rec.rows[0].cur && s.rec.gain===0, {playing:s.rec.playing, gain:s.rec.gain});
s = await until("ended", async()=>{ const q=await sdr(); return !q.rec.playing ? q : null; }, 8000);
check("放完自己停，现场的声音回来", s.rec.rows[0].icon==="▶" && s.rec.gain>0, {gain:s.rec.gain});
await ev('__act.sdr("playRec", 0)'); await until("again", async()=> (await sdr()).rec.playing?.t>0.1, 8000);
await ev('__act.sdr("playRec", 0)'); s = await sdr();
check("再点正在放的那段即停", !s.rec.playing && s.rec.gain>0);

// ---- 没有话音的载波不录 ----
check("切到那根干扰载波", await chip("119.725"));
await until("bare", async()=>{ const q=await sdr(); return q.air.listenLabel==="119.725" && q.air.open && !q.air.voice; }, 10000);
await sleep(1500); const n0 = (await sdr()).rec.count; await sleep(5000); s = await sdr();
check("有载波但没有话音：一段也不录", s.rec.count===n0 && !s.rec.live, {before:n0, after:s.rec.count});

// ---- 删除与清空 ----
await ev('__act.sdr("deleteRec", 0)');
s = await until("deleted", async()=>{ const q=await sdr(); return q.rec.count===n0-1 && q.rec.rows.length===n0-1 ? q : null; }, 5000);
check("删掉一段", !s.rec.list.some(x => x.name===c.name) || n0>=2, {count:s.rec.count});
await ev('__act.sdr("clearRec")'); s = await sdr();
check("清空要点两下：第一下只是换成确认", s.rec.clear==="再点一次确认" && s.rec.count===n0-1, s.rec.clear);
await ev('__act.sdr("clearRec")');
s = await until("cleared", async()=>{ const q=await sdr(); return q.rec.count===0 && q.rec.rows.length===0 ? q : null; }, 5000);
check("第二下清空", /录音开着/.test(s.rec.sum) && s.rec.tabText==="录音" && s.rec.clear===null, s.rec.sum);

// ---- 开着录音就不自动返回（这一组把倒计时设成了 8 秒）----
await send("Page.reload");
await until("reload", async()=>{ const q=await probe(); return q&&q.ready&&q.sdr&&q.sdr.link==="up"&&q.sdr.mode; }, 60000);
await sleep(12000); s = await sdr();
check("刷新后没有页面在出声，但录音开着：留在话音模式，并说明原因", s.mode==="air" && s.autoReturn===null && /录音开着，不会自动返回/.test(s.pill) && s.rec.on, s.pill);

// ---- ADS-B 模式下也能翻录音 ----
await ev(`__act.detail(${EGLL})`); await sleep(300);     // 刷新后详情面板是关着的
await ev('__act.sdr("listenRow", 11)');                  // 回到地面，攒一段出来
await until("one more", async()=> (await sdr()).rec.count>=1, 20000);
await ev('document.querySelector("#sdrPill .back").click()');
await until("adsb", async()=>{ const q=await sdr(); return q.mode==="adsb" && q.adsb.state==="running"; }, 20000);
await ev('__act.sdr("view", "half")');
s = await until("adsb dock", async()=>{ const q=await sdr(); return q.dock.on && q.spectrum.mode==="adsb" && q.rec.shown ? q : null; }, 8000);
check("ADS-B 模式下打开停靠栏：右侧只有录音一页，片段还在", s.rec.tab==="rec" && s.rec.rows.length>=1 && await ev('document.querySelector("#sdrDock .tb.air").offsetParent===null'), {rows:s.rec.rows.length});
await ev('__act.sdr("playRec", 0)');
s = await until("adsb play", async()=>{ const q=await sdr(); return q.rec.playing && q.rec.playing.t>0.2 ? q : null; }, 8000);
check("在这儿也能回放", true, s.rec.playing);
await ev('__act.sdr("record", false)');
s = await until("off", async()=>{ const q=await sdr(); return q.rec.on===false ? q : null; }, 5000);
check("关掉录音；已有的片段留着", s.rec.count>=1 && /录音已关/.test(s.rec.sum), s.rec.sum);

check("页面无脚本异常", errors.length===0, errors.slice(0,5));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
ws.close(); process.exit(fails?1:0);

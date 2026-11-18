// 话音监听端到端验收（合成信号）。用法：node voice.mjs <调试端口> <页面URL>
// 合成的三个话音台见 run.sh；频率表用的是希思罗（EGLL）的真实数据。
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
async function until(label, fn, ms=30000){ const t0=Date.now(); while(Date.now()-t0<ms){ const v = await fn(); if (v) return v; await sleep(200);} const s = await sdr(); throw new Error("timeout: "+label+" :: "+JSON.stringify({pill:s?.pill, air:s?.air, audio:s?.audio, dock:s?.dock})); }
let fails=0; const check=(name,ok,detail)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+name+(detail!==undefined?"  "+JSON.stringify(detail):"")); };
const key = async (k, code, vk, mods=0) => { for (const type of ["keyDown","keyUp"]) await send("Input.dispatchKeyEvent",{type,key:k,code,windowsVirtualKeyCode:vk,modifiers:mods,text:type==="keyDown"&&k.length===1?k:undefined}); };
const chip = label => ev(`(function(){var c=[].find.call(document.querySelectorAll("#sdrDock .chip"),function(c){return c.textContent.indexOf(${JSON.stringify(label)})>=0});if(c)c.click();return !!c})()`);
const near = (v, want, tol) => typeof v === "number" && Math.abs(v - want) <= tol;

await send("Runtime.enable"); await send("Page.enable"); await send("Page.navigate",{url});
await until("ready", async()=>{ const p=await probe(); return p&&p.ready&&p.styleLoaded; }, 60000);
await ev("__act.fly(-0.45, 51.47, 8.5)");
await until("adsb running", async()=>{ const p=await probe(); return p.sdr && p.sdr.link==="up" && p.sdr.adsb?.state==="running" && p.flights.local>=3; }, 60000);

// ---- 频率表 ----
const EGLL = 2434;
await ev(`__act.detail(${EGLL})`);
let s = await until("rows", async()=>{ const q=await sdr(); return q.rows.length===15 ? q : null; });
check("频率表每行有收听按钮", s.rows.length===15 && s.rows[12].text==="收听" && !s.rows[12].disabled, s.rows[12]);
check("导航台频率的行置灰并说明原因", s.rows[0].disabled && /导航台/.test(s.rows[0].why), s.rows[0]);
check("没进话音模式时不出频道条", !s.dock.on && s.mode==="adsb");

// ---- 一键收听：塔台 118.5，合成的载波偏 −7 kHz ----
let t0 = Date.now();
await ev('__act.sdr("listenRow", 12)');
s = await until("tower audio", async()=>{ const q=await sdr(); return q.mode==="air" && q.air?.open && q.audio.frames>40 && q.audio.played>8000 ? q : null; }, 15000);
check("点一下就出声（从 ADS-B 热切换）", true, {ms:Date.now()-t0});
let p = await probe();
check("本地 ADS-B 暂停，本地飞机撤下", p.flights.local===0 && s.adsb.state==="paused", {local:p.flights.local, adsb:s.adsb.state});
check("状态条如实说明", /^本地接收 · 话音 · TWR 118\.500 · 信号 \+\d+ dB · 载波偏 −7\.0 kHz · 本地 ADS-B 已暂停$/.test(s.pill) && s.pillClass==="ok", {pill:s.pill, cls:s.pillClass});
check("自动频率跟踪找到偏掉的载波", near(s.air.afc, -7000, 60), {afc:s.air.afc});
check("听到的是塔台那个 1 kHz 单音", near(s.audio.hz, 1000, 60) && s.audio.rms>0.2, s.audio);
check("声音真的送进了播放线程", s.audio.ready && s.audio.on && s.audio.ctx==="running", {played:s.audio.played});
check("频道表分成多个窗口，频道条列出全部可收听的频道", s.air.windows>=4 && s.dock.on && s.dock.chips===13 && s.air.channels===13, {windows:s.air.windows, chips:s.dock.chips});
check("频道条高亮正在听的频道", /TWR118\.500/.test(s.dock.cur), s.dock.cur);
check("频率表那一行变成「停止」，指示灯亮", s.rows[12].text==="停止" && s.rows[12].lamp==="on", s.rows[12]);
check("同窗口里安静的频道灯不亮；别的窗口的频道标为不在窗口内", s.rows[13].lamp==="" && s.rows[11].lamp==="far", {twr2:s.rows[13].lamp, gnd:s.rows[11].lamp});
const layout = await ev('(function(){var w=document.getElementById("wrap");return {dock:getComputedStyle(w).getPropertyValue("--dock-h").trim(),detail:getComputedStyle(document.getElementById("detail")).bottom,h:document.getElementById("sdrDock").offsetHeight}})()');
check("详情面板给频道条让位", layout.h>40 && layout.dock===layout.h+"px" && layout.detail===layout.h+"px", layout);

// ---- 同窗口里一根没有话音的载波（干扰）：灯亮，但不出声 ----
check("同窗口的干扰载波：指示灯照实亮着", s.rows[5].lamp==="on", s.rows[5]);
check("频道条上有它的标签", await chip("119.725"));
s = await until("bare carrier", async()=>{ const q=await sdr(); return q.air.listenLabel==="119.725" && q.air.open && !q.air.voice ? q : null; }, 8000);
let f1 = s.audio.frames; await sleep(1200); s = await sdr();
check("有载波但没有话音：默认不出声，并说明原因", s.voiceOnly===true && s.audio.frames===f1 && /DIR 119\.725 · 有载波 \+\d+ dB，无话音，已静音/.test(s.dock.now) && s.pillClass==="idle", {now:s.dock.now, frames:s.audio.frames-f1});
s = await until("flagged steady", async()=>{ const q=await sdr(); return q.air.steady.length===1 && q.rows[5].lamp==="steady" ? q : null; }, 10000);
check("几秒后认定为常驻载波，灯变空心", /常驻载波/.test(s.dock.now), s.dock.now);
await ev('__act.sdr("voiceOnly", false)');
s = await until("noise through", async()=>{ const q=await sdr(); return q.voiceOnly===false && q.audio.frames>f1+20 ? q : null; }, 5000);
check("关掉「无话音时静音」：只要有载波就放出来", true, {frames:s.audio.frames-f1});
await ev('__act.sdr("voiceOnly", true)');
await until("voice only back", async()=>{ const q=await sdr(); return q.voiceOnly===true; }, 5000);

// ---- 另一个窗口里断续的地面频道：静噪开合 ----
await ev('__act.sdr("listenRow", 11)');
s = await until("ground speaking", async()=>{ const q=await sdr(); return q.air.listenLabel==="121.900" && q.air.open && near(q.audio.hz, 700, 60) ? q : null; }, 15000);
check("切到另一个窗口的地面频道，听到 700 Hz", true, {win:s.air.win, hz:s.audio.hz});
s = await until("ground quiet", async()=>{ const q=await sdr(); return !q.air.open ? q : null; }, 8000);
const f0 = s.audio.frames; await sleep(600); s = await sdr();
check("通话间隙静噪关闭，不再送音频", /静噪中/.test(s.dock.now) && s.audio.frames===f0 && s.pillClass==="idle", {now:s.dock.now, cls:s.pillClass});

// ---- 点频道条上的标签：通播 ----
check("频道条上有通播的标签", await chip("128.075"));
s = await until("atis", async()=>{ const q=await sdr(); return q.air.listenLabel==="128.075" && q.air.open && near(q.audio.hz, 500, 60) ? q : null; }, 10000);
check("点标签切窗口并收听，听到 500 Hz", true, {win:s.air.win});

// ---- 键盘 ----
await ev('document.activeElement && document.activeElement.blur()');
await key(" ", "Space", 32); s = await sdr();
check("空格静音", s.audio.muted && /已静音/.test(s.pill), s.pill);
await key(" ", "Space", 32); s = await sdr();
check("再按空格恢复", !s.audio.muted);
await key("ArrowRight", "ArrowRight", 39);
s = await until("step up", async()=>{ const q=await sdr(); return q.air.listenLabel==="128.100" ? q : null; }, 5000);
check("右方向键上调 25 kHz，成为一个手动频道", /手动128\.100/.test(s.dock.cur) && s.dock.chips===14, s.dock.cur);
await key("ArrowLeft", "ArrowLeft", 37);
s = await until("step down", async()=>{ const q=await sdr(); return q.air.listenLabel==="128.075" && q.dock.chips===13 ? q : null; }, 5000);
check("左方向键调回表里的频道，手动频道消失", true);

// ---- 静噪门限 ----
await ev('__act.sdr("squelch", 40)');
s = await until("squelch high", async()=>{ const q=await sdr(); return q.squelch===40 && !q.air.open ? q : null; }, 8000);
check("门限抬到信号之上：静噪关闭，并提示有弱载波", /低于静噪门限 40 dB/.test(s.dock.now), s.dock.now);
await ev('__act.sdr("squelch", 7)');
await until("squelch back", async()=>{ const q=await sdr(); return q.squelch===7 && q.air.open; }, 8000);

// ---- 轮巡 ----
await ev('__act.sdr("patrol", true)');
const seen = new Set(); let heldGround = false, heldAtis = false; t0 = Date.now();
s = await until("patrol", async()=>{ const q=await sdr(); seen.add(q.air.win);
  if (q.air.open && q.air.listenLabel==="121.900") heldGround = true;
  if (q.air.open && q.air.listenLabel==="128.075" && Date.now()-t0>3000) heldAtis = true;
  return q.air.steady.length && heldGround && seen.size>=3 ? q : null; }, 90000);
check("轮巡走过多个窗口，地面有人说话时停下来听", heldGround && seen.size>=3, {windows:[...seen], pill:s.pill});
check("轮巡不为连续播发的通播停留", !heldAtis);
check("一直亮着不灭的载波被标为常驻，轮巡不再为它们停留", s.air.steady.length>=1, s.air.steady);
check("状态条显示轮巡", /轮巡 \d\/\d/.test(s.pill), s.pill);
await ev('__act.sdr("listenRow", 12)');
s = await until("explicit pick", async()=>{ const q=await sdr(); return q.air.listenLabel==="118.500" && !q.air.patrol && q.air.open ? q : null; }, 8000);
check("明确点一个频道即退出轮巡；换窗口回来仍记得它是常亮的", s.rows[12].lamp==="steady" && /持续播发/.test(s.dock.now), {lamp:s.rows[12].lamp, now:s.dock.now});

// ---- 一键返回 ----
t0 = Date.now();
await ev('document.querySelector("#sdrPill .back").click()');
p = await until("back to adsb", async()=>{ const q=await probe(); return q.sdr.mode==="adsb" && q.sdr.adsb.state==="running" && q.flights.local===3 ? q : null; }, 20000);
check("状态条上一键返回 ADS-B，本地飞机回来", !p.sdr.dock.on && p.sdr.rows[12].text==="收听", {ms:Date.now()-t0});
const dockH = await ev('getComputedStyle(document.getElementById("wrap")).getPropertyValue("--dock-h").trim()');
check("频道条收起后布局复原，不再要音频", dockH==="0px" && !p.sdr.audio.on, {dockH, on:p.sdr.audio.on});

// ---- 数字键切模式；频道表还留着 ----
await key("2", "Digit2", 50);
s = await until("key 2", async()=>{ const q=await sdr(); return q.mode==="air" && q.air.open && q.audio.on ? q : null; }, 10000);
check("按 2 回到话音，接着听上次的频道", s.air.listenLabel==="118.500", s.air.listenLabel);

// ---- 刷新页面：浏览器不让页面自己出声，没人听就倒计时返回 ----
await send("Page.reload");
await until("reload", async()=>{ const q=await probe(); return q&&q.ready&&q.styleLoaded&&q.sdr&&q.sdr.link==="up"&&q.sdr.mode; }, 60000);
s = await sdr();
if (s.mode==="air"){
  s = await until("countdown", async()=>{ const q=await sdr(); return q.mode!=="air" || q.autoReturn!=null ? q : null; }, 5000);
  check("刷新后声音未开启：给出按钮和倒计时", s.mode!=="air" || (s.dock.soundBtn && /声音未开启/.test(s.pill) && /秒后回到 ADS-B/.test(s.pill)), {pill:s.pill});
}
p = await until("auto return", async()=>{ const q=await probe(); return q.sdr.mode==="adsb" && q.flights.local===3 ? q : null; }, 30000);
check("没人听了自动回到 ADS-B", true, {pill:p.sdr.pill});

// ---- 从「关闭」进话音，再停止：回到关闭而不是 ADS-B ----
await ev('__act.sdr("mode","off")');
await until("off", async()=>{ const q=await sdr(); return q.mode==="off" && q.device.state==="released"; }, 8000);
await ev(`__act.detail(${EGLL})`); await sleep(300);
await ev('__act.sdr("listenRow", 12)');
s = await until("air from off", async()=>{ const q=await sdr(); return q.mode==="air" && q.air?.open && q.audio.on ? q : null; }, 15000);
check("接收机关着时点收听：自动打开设备", s.returnTo==="off" && s.dock.back==="停止并释放设备", {back:s.dock.back});
await ev('__act.sdr("listenRow", 12)');
s = await until("stop", async()=>{ const q=await sdr(); return q.mode==="off" && q.device.state==="released" ? q : null; }, 8000);
check("再点同一行即停止，设备释放", true, {pill:s.pill});

check("页面无脚本异常", errors.length===0, errors.slice(0,5));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
ws.close(); process.exit(fails?1:0);

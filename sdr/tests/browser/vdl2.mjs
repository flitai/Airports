// VDL2 数据链端到端验收（合成信号 + 假的 dumpvdl2）。用法：node vdl2.mjs <调试端口> <页面URL> [nodecoder]
// 假解码器（sdr/tests/fake_dumpvdl2.py）每喂够一秒的采样吐一条报文，轮着是：
//   地面站 → 飞机 a23721 的一帧（载荷是一段气象报文）、飞机 abcdef 的 ACARS（航班 TS1234）、
//   地面站的广播、飞机 abcdef 的确认帧。
// 合成信号里 135.9 MHz 有一个连续播发的话音台，落在数据链窗口里。
// 带 nodecoder 时守护进程是以 SDR_DUMPVDL2=none 启动的，只验「缺少解码器」怎么呈现。
const [port, url, variant] = process.argv.slice(2);
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
async function until(label, fn, ms=30000){ const t0=Date.now(); while(Date.now()-t0<ms){ const v = await fn(); if (v) return v; await sleep(200);} const s = await sdr(); throw new Error("timeout: "+label+" :: "+JSON.stringify({pill:s?.pill, mode:s?.mode, vdl2:s?.vdl2, air:s?.air, dock:s?.dock, errors})); }
let fails=0; const check=(name,ok,detail)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+name+(detail!==undefined?"  "+JSON.stringify(detail):"")); };
const key = async (k, code, vk) => { for (const type of ["keyDown","keyUp"]) await send("Input.dispatchKeyEvent",{type,key:k,code,windowsVirtualKeyCode:vk,text:type==="keyDown"&&k.length===1?k:undefined}); };
const act = (name, arg) => ev(`__act.sdr(${JSON.stringify(name)}, ${JSON.stringify(arg === undefined ? null : arg)})`);
const row = (s, pred) => s.vdl2.rows.find(pred);
const finish = () => { check("页面无脚本异常", errors.length===0, errors.slice(0,5)); console.log(fails ? `\n${fails} 项失败` : "\n全部通过"); ws.close(); process.exit(fails?1:0); };

await send("Runtime.enable"); await send("Page.enable"); await send("Page.navigate",{url});
await until("ready", async()=>{ const p=await probe(); return p&&p.ready&&p.styleLoaded; }, 60000);
await ev("__act.fly(-0.45, 51.47, 8.5)");
let s = await until("adsb running", async()=>{ const p=await probe(); return p.sdr && p.sdr.link==="up" && p.sdr.adsb?.state==="running" ? p.sdr : null; }, 60000);
await ev('document.activeElement && document.activeElement.blur()');

if (variant === "nodecoder"){
  await key("3", "Digit3", 51);
  s = await until("no decoder", async()=>{ const q=await sdr(); return q.mode==="vdl2" && q.vdl2.status.state==="no_dumpvdl2" ? q : null; }, 20000);
  check("没编译解码器：状态条说明缺什么、怎么装", s.pillClass.includes("err") && s.pill.includes("缺少解码器") && s.pill.includes("build-dumpvdl2.sh"), s.pill);
  check("窗口照样调过去，停靠栏照样出来", s.dock.on && s.rf.center_hz===136412500 && s.vdl2.chips.filter(c=>c.data).length===5, {center:s.rf.center_hz});
  finish();
}

// ---- 进入 ----
check("侧栏有数据链这一项，可以勾", s.vdl2.avail && !s.vdl2.checkbox.disabled && !s.vdl2.checkbox.checked, s.vdl2.checkbox);
check("ADS-B 模式下没有「报文」页签", !s.vdl2.tab.shown, s.vdl2.tab);
await key("3", "Digit3", 51);
s = await until("vdl2 running", async()=>{ const q=await sdr(); return q.mode==="vdl2" && q.vdl2.status.state==="running" && q.dock.on && q.vdl2.rows.length>=1 ? q : null; }, 30000);
check("按 3 进入数据链：窗口中心 136.4125 MHz", s.rf.center_hz===136412500 && s.vdl2.checkbox.checked && !s.checkbox.checked, {center:s.rf.center_hz});
check("状态条写明在收什么，也写明本地 ADS-B 停了", /VDL2 136\.975 · \d+ 条\/分 · \d+ 架/.test(s.pill) && s.pill.includes("本地 ADS-B 已暂停"), s.pill);
check("停靠栏：标题、半高、右侧翻到「报文」", s.spectrum.title==="数据链 · VDL2" && s.spectrum.view==="half" && s.vdl2.tab.shown && s.vdl2.tab.cur && s.rec.tab==="msg", {title:s.spectrum.title, tab:s.vdl2.tab});
const data = s.vdl2.chips.filter(c=>c.data);
check("频道条上五个数据链频道，不带删除的叉", data.length===5 && data.every(c=>!c.cross && c.type==="VDL2") && data.some(c=>c.label==="136.975"), s.vdl2.chips);
check("常驻模式：没有返回键，没有轮巡，默认不听话音", s.pillBack===null && !s.vdl2.patrolShown && s.air.listen===null && s.dock.now.startsWith("未收听话音"), {back:s.pillBack, now:s.dock.now});
check("本地飞机不再上图（接收机不在 1090 MHz 上）", (await probe()).flights.local===0);

// ---- 报文 ----
s = await until("four kinds", async()=>{ const q=await sdr(); return q.vdl2.rows.length>=4 ? q : null; }, 20000);
const acars = row(s, r=>r.sum.startsWith("ACARS H1"));
check("ACARS：下行，写航班号，摘要里有标签和正文的第一行", acars && acars.dir==="↓" && acars.who==="TS1234" && acars.sum.includes("#DFB/POS") && acars.text.includes("FUEL 12.4"), acars);
const up = row(s, r=>r.sum.startsWith("X.25"));
check("上行帧：飞机不在图上时写它的地址，没有「定位」", up && up.dir==="↑" && up.who==="A23721" && !up.locate && up.text.includes("METAR KROC"), up);
const gs = row(s, r=>r.sum.startsWith("XID"));
check("地面站的广播：写地面站地址，频率是它自己的", gs && gs.who==="地面站 10AB2C" && gs.mhz==="136.725", gs);
check("报文还没对上飞机时，提示去开实时航班", s.vdl2.sum.includes("实时航班") && s.vdl2.sum.includes("日志"), s.vdl2.sum);
const lit = s.vdl2.chips.find(c=>c.label==="136.975");
check("数据链频道的标签上有条数", +lit.count>=1 && +s.vdl2.chips.find(c=>c.label==="136.725").count>=1, s.vdl2.chips.filter(c=>c.data).map(c=>[c.label,c.count]));
await act("clickMsg", "ACARS H1");
s = await until("open", async()=>{ const q=await sdr(); const r=row(q, r=>r.sum.startsWith("ACARS H1")&&r.open); return r&&r.textShown ? q : null; }, 5000);
check("点一行展开正文", true);

// ---- 对到地图上 ----
let p = await probe();
check("图上没有这些飞机时，谁也不带圈", p.flights.tagged.length===0 && p.flights.dlLayer, p.flights.tagged);
const now = Date.now()/1000;
await ev(`__act.ingest("net", [{hex:"a23721",cs:"TEST23",reg:"N-TEST",ty:"B738",lat:51.50,lon:-0.40,alt:31000,gs:440,tr:90,t:${now}},
                              {hex:"c0ffee",cs:"OTHER1",lat:51.42,lon:-0.52,alt:9000,gs:250,tr:270,t:${now}}]); window.__app.ac.show("net", true)`);
p = await until("tagged", async()=>{ const q=await probe(); return q.flights.tagged.length ? q : null; }, 3000);
check("发过报文的那架立刻带圈，没发过的不带", p.flights.tagged.join()==="a23721", p.flights.tagged);
s = await until("callsign", async()=>{ const q=await sdr(); const r=row(q, r=>r.sum.startsWith("X.25")); return r && r.who==="TEST23" && r.locate ? q : null; }, 8000);
check("报文行换成了地图上的呼号，并有「定位」", !s.vdl2.sum.includes("实时航班"), row(s, r=>r.sum.startsWith("X.25")));
const bases = await ev('[].map.call(document.getElementById("fBase").options, function(o){ return o.value; })');
const cur = await ev('document.getElementById("fBase").value');
await ev(`__act.base(${JSON.stringify(bases.find(b=>b!==cur))})`);
p = await until("restyled", async()=>{ const q=await probe(); return q.styleLoaded && q.flights.dlLayer && q.sdr.layers.rings!==undefined ? q : null; }, 30000);
check("切底图后圈还在", p.flights.dlLayer && p.flights.tagged.join()==="a23721", p.flights.tagged);
await act("locateMsg", "TEST23");
s = await until("flight detail", async()=>{ const q=await sdr(); return q.vdl2.flightBox ? q : null; }, 5000);
const name = await ev('document.getElementById("dName").textContent');
check("点「定位」打开那架飞机的详情", name==="TEST23", name);
check("详情里有「数据链报文」，排在表格和航线下面", s.vdl2.flightBox.title==="数据链报文" && s.vdl2.flightBox.rows>=1 && s.vdl2.flightBox.after==="cgrp" && s.vdl2.flightBox.text.includes("METAR KROC"), {rows:s.vdl2.flightBox.rows, after:s.vdl2.flightBox.after});
const had = s.vdl2.flightBox.rows;
if (had < 6){
  s = await until("detail grows", async()=>{ const q=await sdr(); return q.vdl2.flightBox && q.vdl2.flightBox.rows>had ? q : null; }, 12000);
  check("详情开着时来了新报文，这一组跟着长", true, s.vdl2.flightBox.rows);
}

// ---- 同窗的话音 ----
const EGLL = 2434;
await act("freqBox", "135.9");
s = await until("voice in window", async()=>{ const q=await sdr(); return q.air.listenLabel==="135.900" && q.air.open ? q : null; }, 15000);
const f0 = s.audio.frames, n0 = s.vdl2.seq;
s = await until("audio and messages", async()=>{ const q=await sdr(); return q.audio.frames>f0+50 && q.vdl2.seq>n0 ? q : null; }, 15000);
check("调到窗内的 135.9：仍是数据链，一边出声一边收报文", s.mode==="vdl2" && s.vdl2.status.state==="running" && Math.abs(s.audio.hz-600)<60, {mode:s.mode, hz:s.audio.hz});
check("状态条写上同时在听谁", s.pill.includes("同时听") && s.pill.includes("135.900"), s.pill);
const manual = s.vdl2.chips.find(c=>c.label==="135.900");
check("手动频道和数据链频道在同一个窗口里；手动的带叉", manual && manual.cross && manual.cur && s.air.windows===1, s.vdl2.chips.map(c=>c.label));
await act("clickChip", "135.900");
s = await until("stop listening", async()=>{ const q=await sdr(); return q.air.listen===null ? q : null; }, 8000);
check("再点一下正在听的频道：停止收听，不离开数据链", s.mode==="vdl2" && s.dock.now.startsWith("未收听话音"), s.dock.now);
await act("clickChip", "136.975");
await sleep(600);
s = await sdr();
check("点数据链频道不去「听」它，只是翻到报文页", s.mode==="vdl2" && s.air.listen===null && s.vdl2.tab.cur && !s.pill.includes("没有话音可听"), s.pill);
await act("dropManual");
s = await until("dropped", async()=>{ const q=await sdr(); return q.vdl2.chips.length===5 ? q : null; }, 8000);
check("删掉手动频道：只剩数据链频道，仍在数据链", s.mode==="vdl2" && s.vdl2.status.state==="running");
await key("2", "Digit2", 50);
await sleep(800);
s = await sdr();
check("表里没有话音频道时按 2 不离开", s.mode==="vdl2" && s.pill.includes("还没选频道"), s.pill);

// ---- 窗外的话音：离开，再回来 ----
await ev(`__act.detail(${EGLL})`);
await until("freq rows", async()=>{ const q=await sdr(); return q.rows.length===15 && !q.rows[12].disabled; }, 8000);
await act("listenRow", 12);                               // 希思罗频率表的第 13 行是塔台 118.5
s = await until("left for voice", async()=>{ const q=await sdr(); return q.mode==="air" && q.air.open && q.vdl2.status.state==="off" ? q : null; }, 20000);
check("在频率表里点窗外的塔台：去话音模式，解码器停", s.returnTo==="vdl2" && s.rf.center_hz!==136412500 && !s.vdl2.chips.some(c=>c.data), {returnTo:s.returnTo});
check("返回键写的是回数据链", s.pillBack==="返回 VDL2" && s.dock.back==="返回 VDL2" && !s.vdl2.tab.shown, {pill:s.pillBack, dock:s.dock.back});
check("侧栏的说明跟着变", s.hint.includes("任一项"), s.hint);
await ev('document.querySelector("#sdrPill .back").click()');
s = await until("back to vdl2", async()=>{ const q=await sdr(); return q.mode==="vdl2" && q.vdl2.status.state==="running" && q.rf.center_hz===136412500 ? q : null; }, 20000);
check("按返回：回到数据链，窗口和解码器都对", s.air.listen===null && s.vdl2.chips.filter(c=>c.data).length===5 && s.air.windows>1, {windows:s.air.windows});
const n1 = s.vdl2.seq;
s = await until("messages resume", async()=>{ const q=await sdr(); return q.vdl2.seq>n1 ? q : null; }, 15000);
check("报文接着来，之前的还在", s.vdl2.count>4, s.vdl2.count);
const far = s.vdl2.chips.find(c=>c.label==="118.500");
check("这个机场窗外的频道也列着，在别的窗口里", far && !far.data);

// ---- 清空、离开 ----
await act("clearMsg");
s = await sdr();
check("清空要点两下", s.vdl2.clear==="再点一次确认" && s.vdl2.count>0, s.vdl2.clear);
await act("clearMsg");
s = await until("cleared", async()=>{ const q=await sdr(); return q.vdl2.seq<3 ? q : null; }, 8000);
check("清空后从头数", true, s.vdl2.seq);
await key("1", "Digit1", 49);
s = await until("adsb again", async()=>{ const q=await sdr(); return q.mode==="adsb" && q.adsb.state==="running" ? q : null; }, 30000);
check("按 1 回 ADS-B：数据链那一项取消勾选，「报文」页签收起", s.checkbox.checked && !s.vdl2.checkbox.checked && s.vdl2.status.state==="off", s.vdl2.checkbox);
await send("Page.reload");
s = await until("reload", async()=>{ const q=await probe(); return q&&q.ready&&q.sdr&&q.sdr.link==="up"&&q.sdr.mode==="adsb" ? q.sdr : null; }, 60000);
await key("3", "Digit3", 51);
s = await until("vdl2 after reload", async()=>{ const q=await sdr(); return q.mode==="vdl2" && q.vdl2.status.state==="running" && q.vdl2.tab.cur ? q : null; }, 30000);
check("刷新页面后再进数据链照常", s.vdl2.chips.filter(c=>c.data).length===5);
finish();

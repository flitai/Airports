// 本地接收机端到端验收（合成信号）。用法：node e2e-sdr.mjs <调试端口> <页面URL>
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
async function until(label, fn, ms=30000){ const t0=Date.now(); let last; while(Date.now()-t0<ms){ last = await fn(); if (last) return last; await sleep(300);} throw new Error("timeout: "+label+" :: "+JSON.stringify((await probe())?.sdr)); }
let fails=0; const check=(name,ok,detail)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+name+(detail!==undefined?"  "+JSON.stringify(detail):"")); };

await send("Runtime.enable"); await send("Page.enable"); await send("Page.navigate",{url});
await until("ready", async()=>{ const p=await probe(); return p&&p.ready&&p.styleLoaded; }, 60000);
await ev("__act.fly(-0.45, 51.47, 8.5)");

let p = await until("sdr ui + link", async()=>{ const q=await probe(); return q.sdr && q.sdr.link==="up" && q.sdr.adsb && q.sdr.adsb.state==="running" && q.flights.local>=3 ? q : null; }, 60000);
check("接收机界面按需加载并连上守护进程", p.sdr.loaded && p.sdr.link==="up", {port:p.sdr.port, device:p.sdr.device.model});
check("状态条文字", /^本地接收 · ADS-B 1090 · 3 架 · [\d.]+ 条\/秒$/.test(p.sdr.pill) && p.sdr.pillClass==="ok", {pill:p.sdr.pill, cls:p.sdr.pillClass});
check("本地飞机进入状态表（联网航班未开）", p.flights.local===3 && p.flights.net===0 && p.flights.n===3, p.flights);
check("侧栏复选框反映模式", p.sdr.checkbox.checked && !p.sdr.checkbox.disabled, p.sdr.checkbox);
await sleep(1500); p = await probe();
check("本地飞机真的画在地图上", p.flights.shown===3, {shown:p.flights.shown});

// 每秒更新：同一架飞机的位置在 3 秒内应当变化
const pos = async()=> ev('__act.flightPos("abc001")');
const a1 = await pos(); await sleep(3200); const a2 = await pos();
check("本地位置持续更新", a1 && a2 && (a1[0]!==a2[0]||a1[1]!==a2[1]), {a1,a2});
p = await probe();
check("每架飞机身后有一段尾迹", p.flights.trails===3, {trails:p.flights.trails});

// 详情面板里的本地信息
// 呼号报文每 2 秒才来一条，飞机先有位置后有呼号：等到了再点
await until("callsign", async()=> (await ev("__act.callsigns()")).includes("SYN001"), 15000);
const cs = await ev('__act.clickFlight("SYN001")');
const dl0 = await ev('[].map.call(document.querySelectorAll("#dbody dt"),function(d){return d.textContent+"="+d.nextElementSibling.textContent}).join(" | ")');
check("航班详情标明本地接收和信号强度", cs==="SYN001" && /数据来源=本地接收 · 信号 -?[\d.]+ dBFS/.test(dl0), dl0);

// 接收机位置
check("设定前没有距离圈数据", p.sdr.home===null && p.sdr.layers.home===true);
await ev('__act.sdr("home", [51.47, -0.45])');
p = await until("home", async()=>{ const q=await probe(); return q.sdr.home && q.sdr.outlineMax>0 ? q : null; }, 10000);
check("接收机位置生效并开始累计最远轮廓", p.sdr.home[0]===51.47 && p.sdr.outlineMax>10, {home:p.sdr.home, outlineMax:p.sdr.outlineMax, hint:p.sdr.hint});
await until("adsb back after decoder restart", async()=>{ const q=await probe(); return q.sdr.adsb.state==="running" && q.flights.local===3; }, 15000);
// 改接收机位置会重启解码器，飞机记录是新建的，呼号报文每 2 秒才来一条
await until("callsign back", async()=> (await ev("__act.callsigns()")).includes("SYN001"), 15000);
await ev('__act.clickFlight("SYN001")');
const dl1 = await ev('[].map.call(document.querySelectorAll("#dbody dt"),function(d){return d.textContent+"="+d.nextElementSibling.textContent}).join(" | ")');
check("详情显示距接收机距离（合成飞机半径 12 nm ≈ 22 km）", /距接收机=2[0-5] km/.test(dl1), dl1);
// 单位切换：距离圈、最远收到、距接收机一起换
const rings = () => ev('__app.map().getSource("sdr-home").serialize().data.features.filter(function(f){return f.properties.k==="label"}).map(function(f){return f.properties.t}).join(",")');
check("距离圈默认按公里", await rings() === "100 km,200 km,300 km,400 km", await rings());
await ev('__act.units("imperial")');
const dl2 = await ev('[].map.call(document.querySelectorAll("#dbody dt"),function(d){return d.textContent+"="+d.nextElementSibling.textContent}).join(" | ")');
p = await probe();
check("切到英制：距离圈、最远收到、距接收机都换成海里", await rings() === "50 nm,100 nm,150 nm,200 nm" && /最远收到 \d+ nm/.test(p.sdr.hint) && /距接收机=1[0-4] nm/.test(dl2), {rings:await rings(), hint:p.sdr.hint});
await ev('__act.units("metric")');
check("切回公制", await rings() === "100 km,200 km,300 km,400 km");

// 点选模式与 Esc
await ev('__act.sdr("pick", true)'); p = await probe();
check("点选模式改写状态条", p.sdr.picking && /点击地图设定接收机位置/.test(p.sdr.pill), p.sdr.pill);
await send("Input.dispatchKeyEvent",{type:"keyDown",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
await send("Input.dispatchKeyEvent",{type:"keyUp",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
p = await probe();
check("Esc 先取消点选，不关详情抽屉", !p.sdr.picking && p.detailOpen, {picking:p.sdr.picking, detailOpen:p.detailOpen});

// 与联网数据融合
await ev("__act.flights(true)");
p = await until("net", async()=>{ const q=await probe(); return q.flights.net>0 ? q : null; }, 40000);
check("联网与本地并存：总数 = 联网 + 本地独有", p.flights.n===p.flights.net+3 && p.flights.local===3, p.flights);
await ev("__act.flights(false)"); p = await probe();
check("关掉联网航班后本地飞机还在（断网也有飞机）", p.flights.n===3 && p.flights.local===3 && p.flights.net===0, p.flights);
await sleep(1300); p = await probe();
check("图层仍然可见", p.flights.shown===3, {shown:p.flights.shown});

// 切底图
const target = p.activeMode==="online" ? "offline" : "online";
await ev(`__act.base(${JSON.stringify(target)})`);
p = await until("restyle", async()=>{ const q=await probe(); return q.styleLoaded && q.flights.layer && q.sdr.layers.home && q.flights.shown===3 ? q : null; }, 12000);
check("切底图后本地飞机、航迹与接收机图层都重挂", p.sdr.layers.rings && p.sdr.layers.outline && p.flights.shown===3, {layers:p.sdr.layers, shown:p.flights.shown});

// 模式热切换
await ev('__act.sdr("mode","off")');
p = await until("off", async()=>{ const q=await probe(); return q.sdr.mode==="off" && q.flights.local===0 ? q : null; }, 8000);
check("关闭：设备释放、本地飞机撤下、状态条如实说明", p.sdr.device.state==="released" && /已关闭/.test(p.sdr.pill) && !p.sdr.checkbox.checked, {pill:p.sdr.pill, device:p.sdr.device.state});
let t0=Date.now(); await ev('__act.sdr("mode","adsb")');
p = await until("adsb again", async()=>{ const q=await probe(); return q.sdr.adsb.state==="running" && q.flights.local===3 ? q : null; }, 15000);
check("重新开启：不用停止再启动，几秒内恢复", p.flights.local===3, {ms:Date.now()-t0});

// 断线重连
await ev('__act.sdr("wsDrop")'); await sleep(300); p = await probe();
check("连接断开时本地飞机立即撤下", p.flights.local===0 && p.sdr.link!=="up", {link:p.sdr.link, pill:p.sdr.pill});
p = await until("reconnect", async()=>{ const q=await probe(); return q.sdr.link==="up" && q.flights.local===3 ? q : null; }, 15000);
check("自动重连并恢复", true, {pill:p.sdr.pill});

check("页面无脚本异常", errors.length===0, errors.slice(0,5));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
ws.close(); process.exit(fails?1:0);

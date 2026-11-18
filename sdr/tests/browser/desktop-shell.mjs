// 用法：node shell.mjs <调试端口> <阶段>
const [port, phase] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let wsUrl, pageUrl; for (let i=0;i<150;i++){ try { const l = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); const p = l.find(t=>t.type==="page" && /^http:\/\/127\.0\.0\.1/.test(t.url)); if (p){ wsUrl=p.webSocketDebuggerUrl; pageUrl=p.url; break; } } catch {} await sleep(200); }
if (!wsUrl){ console.log("FAIL 没有等到页面窗口"); process.exit(1); }
const ws = new WebSocket(wsUrl); await new Promise(r => ws.onopen = r);
let seq=0; const pend=new Map();
ws.onmessage = e => { const m=JSON.parse(e.data); if (m.id && pend.has(m.id)){ pend.get(m.id)(m); pend.delete(m.id); } };
const send=(method,params={})=>new Promise(r=>{const id=++seq;pend.set(id,r);ws.send(JSON.stringify({id,method,params}));});
const ev = async x => (await send("Runtime.evaluate",{expression:x,returnByValue:true,awaitPromise:true})).result?.result?.value;
const probe = () => ev("window.__probe ? window.__probe() : null");
async function until(label, fn, ms=40000){ const t0=Date.now(); while(Date.now()-t0<ms){ const v=await fn(); if (v) return v; await sleep(300);} console.log("FAIL 超时: "+label+" :: "+JSON.stringify((await probe())?.sdr)); process.exit(1); }
let fails=0; const check=(n,ok,d)=>{ if(!ok) fails++; console.log((ok?"PASS ":"FAIL ")+n+(d!==undefined?"  "+JSON.stringify(d):"")); };
let p = await until("页面就绪", async()=>{ const q=await probe(); return q&&q.ready&&q.styleLoaded?q:null; }, 60000);
check("窗口加载的是本机页面", /^http:\/\/127\.0\.0\.1:\d+\/$/.test(pageUrl), pageUrl);
p = await until("接收机上线", async()=>{ const q=await probe(); return q.sdr&&q.sdr.link==="up"&&q.sdr.adsb&&q.sdr.adsb.state==="running"&&q.flights.local>=3?q:null; });
check("接收机界面加载、守护进程连上、本地飞机上图", p.flights.local===3, {pill:p.sdr.pill});
if (phase === "full") {
  check("页面没有 Node 能力", await ev("typeof require==='undefined' && typeof process==='undefined'"));
  check("Electron 版本", true, await ev("navigator.userAgent.match(/Electron\\/[\\d.]+/)[0]"));
  // 菜单项做的事：向页面派发 sdr-command 事件
  await ev("window.dispatchEvent(new CustomEvent('sdr-command',{detail:{name:'mode',arg:'off'}}))");
  p = await until("菜单关闭", async()=>{ const q=await probe(); return q.sdr.mode==="off"&&q.flights.local===0?q:null; }, 10000);
  check("菜单事件「关闭并释放设备」生效", p.sdr.device.state==="released", p.sdr.pill);
  // 键盘 1。测试窗口不一定拿得到系统焦点，CDP 的按键注入在那种情况下不送达，
  // 所以这里直接在页面里派发键盘事件，验的是处理逻辑本身。
  await ev("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'1',bubbles:true}))");
  p = await until("按键 1", async()=>{ const q=await probe(); return q.sdr.mode==="adsb"&&q.flights.local===3?q:null; }, 15000);
  check("按键 1 重新开始接收", true, p.sdr.pill);
  await ev("document.getElementById('q').dispatchEvent(new KeyboardEvent('keydown',{key:'0',bubbles:true}))");
  await ev("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'0',metaKey:true,bubbles:true}))");
  await sleep(1500); p = await probe();
  check("焦点在搜索框里、或带修饰键时，0 不触发关闭", p.sdr.mode==="adsb", {mode:p.sdr.mode});
  await ev("window.dispatchEvent(new CustomEvent('sdr-command',{detail:{name:'pick',arg:true}}))"); p = await probe();
  check("菜单事件「设定接收机位置」进入点选模式", p.sdr.picking, p.sdr.pill);
  await ev("__act.sdr('pick', false)");
}
console.log(fails?`${fails} 项失败`:"本阶段通过"); ws.close(); process.exit(fails?1:0);

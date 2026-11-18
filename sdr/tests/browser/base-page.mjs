// 极简 CDP 驱动：Node 自带 WebSocket/fetch，无依赖。
// 用法：node cdp.mjs <调试端口> <页面URL> <场景名>
const [port, url, scenario] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const p = list.find(t => t.type === "page");
      if (p) return p.webSocketDebuggerUrl;
    } catch {}
    await sleep(200);
  }
  throw new Error("no page target");
}

const ws = new WebSocket(await target());
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let seq = 0; const pending = new Map(); const errors = [];
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === "Runtime.exceptionThrown")
    errors.push("EXC " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error")
    errors.push("CONSOLE " + m.params.args.map(a => a.value ?? a.description).join(" "));
};
const send = (method, params = {}) => new Promise(res => {
  const id = ++seq; pending.set(id, res); ws.send(JSON.stringify({ id, method, params }));
});
const ev = async expr => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) throw new Error("eval: " + (r.result.exceptionDetails.exception?.description || expr));
  return r.result.result.value;
};
const probe = () => ev("window.__probe ? window.__probe() : null");
async function until(label, fn, ms = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await sleep(250); }
  throw new Error("timeout: " + label);
}
const out = {}; let fails = 0;
function check(name, ok, detail) {
  if (!ok) fails++;
  console.log((ok ? "PASS " : "FAIL ") + name + (detail !== undefined ? "  " + JSON.stringify(detail) : ""));
}

await send("Runtime.enable"); await send("Page.enable");
await send("Page.navigate", { url });
await until("ready", async () => { const p = await probe(); return p && p.ready && p.styleLoaded; }, 60000);

if (scenario === "file") {
  const p = await probe();
  check("file:// 下航班功能标为不支持", p.flights.supported === false, p.flights);
  check("file:// 下复选框被禁用", await ev('document.getElementById("fFlights").disabled') === true);
  check("file:// 下状态表为空", p.flights.n === 0);
}

if (scenario === "server") {
  await ev("__act.fly(-0.45, 51.47, 8)");
  await ev("__act.flights(true)");
  let p = await until("net flights", async () => { const q = await probe(); return q.flights.n > 0 && q.flights.shown > 0 ? q : null; }, 40000);
  check("联网航班进表并画出", p.flights.n > 0 && p.flights.shown > 0, p.flights);
  check("来源计数：全部来自 net", p.flights.net === p.flights.n && p.flights.local === 0);
  const hint = await ev('document.getElementById("flightHint").textContent');
  const cnt = await ev('document.getElementById("flightCount").textContent');
  check("侧栏计数与提示照旧", /架$/.test(cnt) && /覆盖/.test(hint), { cnt, hint });

  // 点飞机：详情抽屉 + 航线
  const cs = await ev("__act.clickFlight()");
  p = await probe();
  check("clickFlight 打开详情", !!cs && p.detailOpen && p.detailName === cs, { cs, name: p.detailName });
  const hex = await ev('(function(){var dt=document.querySelectorAll("#dbody dt");for(var i=0;i<dt.length;i++)if(dt[i].textContent==="ICAO24")return dt[i].nextElementSibling.textContent;return ""})()');
  const sub0 = await ev('document.getElementById("dSub").textContent');
  const pos0 = await ev(`__act.flightPos(${JSON.stringify(hex)})`);
  check("curFlightPos 从状态表取到位置", Array.isArray(pos0) && pos0.length === 2, { hex, pos0 });
  const routeTxt = await until("route settle", () => ev('(function(){var h=document.querySelector("#dbody .cgrp .hint");return h&&h.textContent!=="查询中…"?document.querySelector("#dbody .cgrp").textContent.slice(0,80):""})()'), 20000);
  p = await probe();
  check("航线查询有结论", !!routeTxt, { routeTxt, routeN: p.route.n });

  // 多来源合并：本地给同一架一个更新的位置，但不带注册号和机型
  const n0 = p.flights.n, tNow = Date.now() / 1000;
  const lp = [pos0[0] + 1.0, pos0[1] + 1.0];
  await ev(`__act.ingest("local", [{hex:${JSON.stringify(hex)}, cs:"", reg:"", ty:"", lat:${lp[1]}, lon:${lp[0]}, alt:1234, gs:99, tr:10, t:${tNow + 5}}])`);
  p = await probe();
  const pos1 = await ev(`__act.flightPos(${JSON.stringify(hex)})`);
  check("同一架两个来源只算一架", p.flights.n === n0 && p.flights.local === 1, p.flights);
  check("位置取位置时刻更新的来源（本地）", Math.abs(pos1[0] - lp[0]) < 1e-9 && Math.abs(pos1[1] - lp[1]) < 1e-9, { pos1 });
  await ev(`__act.clickFlight(${JSON.stringify(cs)})`);
  const sub1 = await ev('document.getElementById("dSub").textContent');
  const alt1 = await ev('document.querySelector("#dbody dd").textContent');
  check("注册号/机型没被本地空值冲掉", sub1 === sub0, { sub0, sub1 });
  check("高度随位置来源整组切换", /1,?234/.test(alt1), { alt1 });

  // 本地位置比联网旧时不该赢
  await ev(`__act.ingest("local", [{hex:${JSON.stringify(hex)}, lat:${lp[1]}, lon:${lp[0]}, alt:1, gs:1, tr:1, t:1000}])`);
  const pos2 = await ev(`__act.flightPos(${JSON.stringify(hex)})`);
  check("旧位置不覆盖新位置", Math.abs(pos2[0] - pos0[0]) < 0.3 && Math.abs(pos2[1] - pos0[1]) < 0.3, { pos0, pos2 });

  // 本地独有的一架 + 撤掉本地来源
  await ev(`__act.ingest("local", [{hex:"zzz001", cs:"TEST1", lat:51.47, lon:-0.45, alt:5000, gs:200, tr:90, t:${tNow}}])`);
  p = await probe();
  check("本地独有的飞机加进来；上一架回落到联网来源而不是消失", p.flights.n === n0 + 1 && p.flights.local === 1, p.flights);
  const pos3 = await ev(`__act.flightPos(${JSON.stringify(hex)})`);
  check("回落后位置回到联网值", Math.abs(pos3[0] - pos0[0]) < 0.3, { pos3 });

  // 切底图：图层被销毁重建，飞机应当立刻还在
  const before = (await probe()).flights;
  const target = (await probe()).activeMode === "offline" ? "online" : "offline";
  await ev(`__act.base(${JSON.stringify(target)})`);
  const t0 = Date.now();
  p = await until("restyle", async () => { const q = await probe(); return q.styleLoaded && q.flights.layer && q.flights.shown > 0 ? q : null; }, 9000);
  check("切底图后飞机立刻重画（不等下一次轮询）", p.flights.shown > 0 && Date.now() - t0 < 9000, { ms: Date.now() - t0, mode: p.activeMode, before: before.shown, after: p.flights.shown });

  // 关掉联网航班：只撤 net，本地那架留在表里
  await ev("__act.flights(false)");
  p = await probe();
  check("关闭联网航班只撤掉 net 来源", p.flights.net === 0 && p.flights.local === 1 && p.flights.n === 1, p.flights);
  await ev('__act.ingest("local", [])');
  p = await probe();
  check("撤掉本地来源后表清空", p.flights.n === 0, p.flights);
  check("关闭后计数与提示清空", (await ev('document.getElementById("flightCount").textContent')) === "");
}

check("页面无脚本异常", errors.length === 0, errors.slice(0, 5));
console.log(fails ? `\n${fails} 项失败` : "\n全部通过");
ws.close();
process.exit(fails ? 1 : 0);

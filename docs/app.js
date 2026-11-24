/* 支撑压力区扫描 - 纯静态站。快照信息每次都加时间戳取最新；两个大扫描文件用快照发布时间做缓存键，同一份快照不再重复下载，换快照才重新取。*/
"use strict";
/* 发布时如仓库名不是 support-resistance-zones，改这里。*/
const REPO = "morriseliu/support-resistance-zones";
const $ = (id) => document.getElementById(id);

let DATA = { A: [], US: [] };
let MANIFEST = null;
let curMarket = "A";
let curDanger = "support";
let curView = "danger";
let selected = null;
let lastRowKey = null;

function findVisibleRow(key) {
  if (!key) return null;
  const tbody = curView === "danger" ? $("dangerRows") : $("rows");
  return tbody ? tbody.querySelector('tr[data-key="' + key + '"]') : null;
}

function bust(url) {
  const sep = url.includes("?") ? "&" : "?";
  return url + sep + "t=" + Date.now();
}

function fmtPct(x) {
  if (x === null || x === undefined) return "—";
  return (x * 100).toFixed(2) + "%";
}

function zoneText(z) {
  if (!z) return "";
  const dates = z.touches.map((t) => t.date).join(" / ");
  let s = z.lo.toFixed(2) + " – " + z.hi.toFixed(2) + " · " + z.n_touches + " 次 · " + dates;
  if (z.flipped) s += '<span class="tag flip">翻转</span>';
  if (z.crossed_edge) s += '<span class="tag">已穿区沿</span>';
  if (z.weekly_same_price) s += '<span class="tag wk">日周同价</span>';
  return '<div class="zone">' + s + "</div>";
}

function tradingDaysBetween(fromDate, toDate) {
  // 仅按周一至周五计（不含节假日，属近似口径，见代码注释）；用于过期提示。
  let n = 0;
  const d = new Date(fromDate + "T00:00:00");
  const end = new Date(toDate + "T00:00:00");
  while (d < end) {
    d.setDate(d.getDate() + 1);
    const w = d.getDay();
    if (w !== 0 && w !== 6) n++;
  }
  return n;
}

function renderBanner() {
  const el = $("banner");
  if (!MANIFEST) {
    el.className = "banner stale";
    el.innerHTML = "<b>快照未发布。</b>暂无已核验的扫描快照数据。";
    return;
  }
  const m = MANIFEST;
  const today = new Date();
  const todayStr = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0") + "-" + String(today.getDate()).padStart(2, "0");
  const oldest = [m.snapshot_date_us, m.snapshot_date_a].filter(Boolean).sort()[0];
  const age = oldest ? tradingDaysBetween(oldest, todayStr) : 999;
  const stale = age > 2;
  el.className = "banner" + (stale ? " stale" : "");
  const val = m.validation || {};
  const valText = val.status === "PASS"
    ? '<span class="ok">校验通过</span>'
    : '<span class="bad">校验：' + (val.status || "未知") + "</span>";
  el.innerHTML =
    "快照日期：美股 " + (m.snapshot_date_us || "—") + " · A 股 " + (m.snapshot_date_a || "—") +
    "<br>生成时间：" + (m.generated_at || "—") + "（UTC）" +
    "<br>校验状态：" + valText + (val.checked_at ? " · 校验时间：" + val.checked_at : "") +
    (val.details ? " · " + val.details : "") +
    "<br>成功取数：美股 " + (m.us_success ?? "—") + "/" + (m.us_universe ?? "—") +
    " · A 股 " + (m.a_success ?? "—") + "/" + (m.a_universe ?? "—") +
    (stale ? '<br><b class="bad">过期提示：快照已超过 2 个交易日未更新（最早快照日 ' + oldest + "，距今约 " + age + " 个交易日），请以最新快照发布为准，旧数据不得当作新数据使用。</b>" : "");
  $("actionsLink").href = "https://github.com/" + REPO + "/actions/workflows/refresh.yml";
}

function allZones(r) {
  return (r.supports || []).concat(r.resistances || [], r.flipped_supports || [], r.flipped_resistances || []);
}

function renderTable() {
  const q = ($("search").value || "").trim().toLowerCase();
  const f = $("zoneFilter").value;
  let rows = DATA[curMarket].slice();
  if (q) rows = rows.filter((r) => (r.symbol + " " + (r.name || "")).toLowerCase().includes(q));
  if (f === "valid") rows = rows.filter((r) => r.has_valid_zone);
  if (f === "none") rows = rows.filter((r) => !r.has_valid_zone);
  if (f === "weekly") rows = rows.filter((r) => allZones(r).some((z) => z.weekly_same_price));
  $("countText").textContent = "共 " + rows.length + " 只";
  $("rows").innerHTML = rows.map((r) => {
    const sup = (r.supports || []).map(zoneText).join("") || (r.has_valid_zone ? "" : "无有效区");
    const res = (r.resistances || []).map(zoneText).join("");
    const flipS = (r.flipped_supports || []).map(zoneText).join("");
    const flipR = (r.flipped_resistances || []).map(zoneText).join("");
    const supHtml = sup + flipS;
    const resHtml = res + flipR;
    const dist =
      "支撑 " + fmtPct(r.nearest_support_distance) + " / 压力 " + fmtPct(r.nearest_resistance_distance);
    const cur = r.currency === "CNY" ? "元" : "$";
    return '<tr class="clickable" data-key="' + r.market + ":" + r.symbol + '"><td><b>' + r.symbol + "</b><br>" + (r.name || "") + "</td><td>" + (r.close === null ? "数据未取到" : r.close.toFixed(2) + " " + cur) + "</td><td>" + (supHtml || "无有效区") + "</td><td>" + (resHtml || "无有效区") + "</td><td>" + dist + "</td></tr>";
  }).join("");
  document.querySelectorAll("#rows tr").forEach((tr) => {
    tr.addEventListener("click", () => showDetail(tr.getAttribute("data-key")));
  });
}

function renderView() {
  $("dangerSection").style.display = curView === "danger" ? "block" : "none";
  $("allSection").style.display = curView === "all" ? "block" : "none";
}

function renderDanger() {
  const key = curDanger === "support" ? "in_support_danger" : "in_resistance_danger";
  // 只显示当前市场，不混两个市场。
  let rows = DATA[curMarket].filter((r) => r[key]);
  rows.sort((a, b) => {
    const da = curDanger === "support" ? a.nearest_support_distance : a.nearest_resistance_distance;
    const db = curDanger === "support" ? b.nearest_support_distance : b.nearest_resistance_distance;
    return (da ?? 9) - (db ?? 9);
  });
  $("dangerRows").innerHTML = rows.map((r) => {
    const zones = curDanger === "support"
      ? (r.supports || []).concat(r.flipped_supports || [])
      : (r.resistances || []).concat(r.flipped_resistances || []);
    const near = zones.filter((z) => z.distance <= 0.03).map(zoneText).join("");
    const d = curDanger === "support" ? r.nearest_support_distance : r.nearest_resistance_distance;
    const cur = r.currency === "CNY" ? "元" : "$";
    return '<tr class="clickable" data-key="' + r.market + ":" + r.symbol + '"><td><b>' + r.symbol + "</b><br>" + (r.name || "") + "（" + (r.market === "A" ? "A 股" : "美股") + "）</td><td>" + (r.close === null ? "数据未取到" : r.close.toFixed(2) + " " + cur) + "</td><td>" + near + "</td><td>" + fmtPct(d) + "</td></tr>";
  }).join("") || '<tr><td colspan="4">暂无进入该危险区的票。</td></tr>';
  document.querySelectorAll("#dangerRows tr").forEach((tr) => {
    tr.addEventListener("click", () => showDetail(tr.getAttribute("data-key")));
  });
}

function drawChart(canvas, kl, rec) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const padL = 56, padR = 10, padT = 14, padB = 22;
  const volH = 70, priceH = H - volH - padT - padB;
  const n = kl.date.length;
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) { lo = Math.min(lo, kl.low[i]); hi = Math.max(hi, kl.high[i]); }
  const zones = allZones(rec);
  zones.forEach((z) => { lo = Math.min(lo, z.lo); hi = Math.max(hi, z.hi); });
  const span = (hi - lo) || 1;
  lo -= span * 0.04; hi += span * 0.04;
  const px = (i) => padL + (i / Math.max(n - 1, 1)) * (W - padL - padR);
  const py = (v) => padT + ((hi - v) / (hi - lo)) * priceH;
  const cw = Math.max(1, (W - padL - padR) / n * 0.7);
  // 区色带：支撑蓝、压力/翻转橙、第二道及更远支撑灰虚线
  const supList = (rec.supports || []).slice();
  const resList = (rec.resistances || []).concat(rec.flipped_resistances || []);
  const flipSup = rec.flipped_supports || [];
  function band(z, color, fill, dashed, label) {
    ctx.fillStyle = fill;
    ctx.fillRect(padL, py(z.hi), W - padL - padR, py(z.lo) - py(z.hi));
    ctx.strokeStyle = color; ctx.lineWidth = 1.4;
    ctx.setLineDash(dashed ? [5, 4] : []);
    ctx.beginPath(); ctx.moveTo(padL, py(z.hi)); ctx.lineTo(W - padR, py(z.hi)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(padL, py(z.lo)); ctx.lineTo(W - padR, py(z.lo)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color; ctx.font = "12px sans-serif"; ctx.textAlign = "left";
    ctx.fillText(label, padL + 4, Math.max(py(z.hi) - 4, 12));
  }
  supList.forEach((z, i) => {
    if (i === 0) band(z, "#2563eb", "rgba(37,99,235,0.12)", false,
      "支撑 " + z.lo.toFixed(2) + "–" + z.hi.toFixed(2) + " · " + z.n_touches + "次 · " + z.first_touch + "～" + z.last_touch);
    else band(z, "#6b7280", "rgba(107,114,128,0.08)", true,
      "支撑 " + z.lo.toFixed(2) + "–" + z.hi.toFixed(2) + " · " + z.n_touches + "次");
  });
  flipSup.forEach((z) => band(z, "#d97706", "rgba(217,119,6,0.10)", false,
    "翻转支撑 " + z.lo.toFixed(2) + "–" + z.hi.toFixed(2) + " · " + z.n_touches + "次"));
  resList.forEach((z) => band(z, "#d97706", "rgba(217,119,6,0.12)", false,
    (z.flipped ? "翻转压力 " : "压力 ") + z.lo.toFixed(2) + "–" + z.hi.toFixed(2) + " · " + z.n_touches + "次 · " + z.first_touch + "～" + z.last_touch));
  // K 线
  for (let i = 0; i < n; i++) {
    const up = kl.close[i] >= kl.open[i];
    const c = up ? "#e05252" : "#2ca06a";
    ctx.strokeStyle = c; ctx.fillStyle = c; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px(i), py(kl.high[i])); ctx.lineTo(px(i), py(kl.low[i])); ctx.stroke();
    const y1 = py(Math.max(kl.open[i], kl.close[i])), y2 = py(Math.min(kl.open[i], kl.close[i]));
    ctx.fillRect(px(i) - cw / 2, y1, cw, Math.max(1, y2 - y1));
  }
  // 价格轴
  for (let k = 0; k <= 4; k++) {
    const v = lo + ((hi - lo) * k) / 4;
    ctx.fillStyle = "#4b5563"; ctx.font = "11px sans-serif"; ctx.textAlign = "left";
    ctx.strokeStyle = "#e5e7eb"; ctx.beginPath(); ctx.moveTo(padL, py(v)); ctx.lineTo(W - padR, py(v)); ctx.stroke();
  }
  // 成交量
  const vTop = padT + priceH + 8, vBot = H - padB;
  let vmax = 0; for (let i = 0; i < n; i++) vmax = Math.max(vmax, kl.volume[i] || 0);
  for (let i = 0; i < n; i++) {
    const up = kl.close[i] >= kl.open[i];
    ctx.fillStyle = up ? "#e05252" : "#2ca06a";
    const h = vmax ? ((kl.volume[i] || 0) / vmax) * (vBot - vTop) : 0;
    ctx.fillRect(px(i) - cw / 2, vBot - h, cw, h);
  }
  ctx.fillStyle = "#4b5563"; ctx.fillText("成交量", 4, vTop + 10);
  // 日期轴
  ctx.fillStyle = "#4b5563"; ctx.textAlign = "center";
  const step = Math.max(1, Math.floor(n / 6));
  for (let i = 0; i < n; i += step) ctx.fillText(kl.date[i].slice(0, 7), px(i), H - 6);
}

async function showDetail(key) {
  const [market, symbol] = key.split(":");
  const rec = DATA[market].find((r) => r.symbol === symbol);
  if (!rec) return;
  selected = rec;
  lastRowKey = key;
  $("detail").style.display = "block";
  $("detailTitle").textContent = rec.symbol + " " + (rec.name || "") + "（" + (market === "A" ? "A 股" : "美股") + "）";
  $("detailMeta").textContent = "收盘价 " + (rec.close === null ? "数据未取到" : rec.close.toFixed(2)) + " · 快照日 " + (MANIFEST ? (market === "A" ? MANIFEST.snapshot_date_a : MANIFEST.snapshot_date_us) : "—");
  // 图在列表上方：先立刻把图滚到视野顶部，K 线画好后再滚一次（不平滑滚动，避免被拉到页面底部）。
  $("detail").scrollIntoView({ behavior: "auto", block: "start" });
  // 取 K 线失败时显示真实原因（HTTP 状态 / 网络错误），并重试一次；再失败就明说，不留空白图。
  let kl = null;
  let lastErr = null;
  for (let attempt = 0; attempt < 2 && !kl; attempt++) {
    try {
      const url = bust("data/kline/" + market.toLowerCase() + "/" + symbol + ".json");
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("HTTP " + resp.status + "（" + url.split("?")[0] + "）");
      kl = await resp.json();
    } catch (e) {
      lastErr = e;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 600));
    }
  }
  if (!kl) {
    $("detailMeta").textContent += " · K 线数据未取到：" + (lastErr ? lastErr.message : "未知错误") + "。请把页面关掉重新打开再试一次。";
    return;
  }
  try {
    drawChart($("chart"), kl, rec);
  } catch (e) {
    $("detailMeta").textContent += " · 画图失败：" + e.message + "。";
    return;
  }
  $("detail").scrollIntoView({ behavior: "auto", block: "start" });
}

let DATA_VER = "";

// 取数失败时重试一次（手机网络抖动常见），并报出真实原因，不笼统说“检查网络”。
async function fetchJSON(url, label) {
  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return await resp.json();
    } catch (e) {
      lastErr = e;
      if (attempt === 0) await new Promise((r) => setTimeout(r, 600));
    }
  }
  throw new Error(label + "取不到（" + (lastErr ? lastErr.message : "未知错误") + "）");
}

async function loadAll() {
  const m = await fetchJSON(bust("data/manifest.json"), "快照信息");
  MANIFEST = m;
  // 快照换了就先清空旧数据：没取到的市场宁可显示为空，也不能把旧快照的数据顶着新日期显示。
  const ver = m.generated_at ? encodeURIComponent(m.generated_at) : encodeURIComponent((m.snapshot_date_us || "") + "_" + (m.snapshot_date_a || ""));
  if (ver !== DATA_VER) { DATA = { A: [], US: [] }; DATA_VER = ver; }
  const errs = [];
  try { DATA.US = await fetchJSON("data/us_scan.json?v=" + ver, "美股数据"); }
  catch (e) { errs.push(e.message); }
  try { DATA.A = await fetchJSON("data/a_scan.json?v=" + ver, "A 股数据"); }
  catch (e) { errs.push(e.message); }
  renderBanner(); renderView(); renderTable(); renderDanger();
  if (errs.length) throw new Error(errs.join("；"));
}

$("refreshBtn").addEventListener("click", async () => {
  $("refreshNote").textContent = "正在重取……";
  try {
    await loadAll();
    $("refreshNote").textContent = "已重取最新已发布数据 " + new Date().toLocaleString("zh-CN") + "（快照日见页顶）。";
  } catch (e) {
    $("refreshNote").textContent = "重取失败：" + e.message + "，请重试。";
  }
});
document.querySelectorAll("#marketTabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#marketTabs button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on"); curMarket = b.getAttribute("data-market");
    $("detail").style.display = "none"; selected = null; lastRowKey = null;
    renderTable(); renderDanger();
  });
});
document.querySelectorAll("#viewTabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#viewTabs button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on"); curView = b.getAttribute("data-view"); renderView();
  });
});
$("closeDetail").addEventListener("click", () => {
  $("detail").style.display = "none"; selected = null;
  // 回到刚才点的那一行（居中并短暂高亮），不留在页面最上面，不用重新往下滑。
  const tr = findVisibleRow(lastRowKey);
  if (tr) {
    tr.scrollIntoView({ behavior: "auto", block: "center" });
    tr.classList.add("returned");
    setTimeout(() => tr.classList.remove("returned"), 1600);
  }
});
document.querySelectorAll("#dangerTabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#dangerTabs button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on"); curDanger = b.getAttribute("data-danger"); renderDanger();
  });
});
$("search").addEventListener("input", renderTable);
$("zoneFilter").addEventListener("change", renderTable);
loadAll().catch((e) => { renderBanner(); if (e && e.message) $("refreshNote").textContent = "初次取数失败：" + e.message + "，请点手动刷新重试。"; });

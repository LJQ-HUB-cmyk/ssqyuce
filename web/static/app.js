/* 双色球智能预测分析系统 - 前端逻辑（零构建，直接运行） */
"use strict";

const $ = (s) => document.querySelector(s);
const toast = (msg, ms = 2600) => {
  const t = $("#toast");
  t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), ms);
};
const fmt = (x, d = 3) => (x == null ? "-" : Number(x).toFixed(d));
const pct = (p) => (p ? (p * 100).toFixed(1) + "%" : "-");
let win = "long";
let charts = {};

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

function setBusy(id, text) {
  const b = $(id);
  b.disabled = true; b.innerHTML = '<span class="spin">⏳</span> ' + text;
}
function setFree(id, text) {
  const b = $(id);
  b.disabled = false; b.innerHTML = text;
}

/* ---------------- 加载与刷新 ---------------- */

async function loadAll() {
  try {
    const [feat, pats, preds, hist, ev] = await Promise.all([
      api("/api/features"), api("/api/patterns"), api("/api/predictions/last"),
      api("/api/draws/history?n=120"), api("/api/eval"),
    ]);
    renderStats(feat);
    renderPatterns(pats.items, pats.summary);
    if (preds.tickets && preds.tickets.length) renderPredictions({ issue: preds.issue, tickets: preds.tickets, note: "" });
    renderHistory(hist);
    renderOnline(ev);
  } catch (e) {
    toast("加载失败: " + e.message);
  }
}

async function refresh() {
  setBusy("#btnRefresh", "抓取中…");
  try {
    const r = await api("/api/refresh", { method: "POST" });
    if (!r.ok) throw new Error(r.error || "刷新失败");
    toast(`新增 ${r.inserted_new} 期，最大期号 ${r.local_max}`);
    await loadAll();
  } catch (e) { toast("刷新失败: " + e.message); }
  setFree("#btnRefresh", "⟳ 刷新开奖数据");
}

async function runBacktest() {
  setBusy("#btnBacktest", "回测中…");
  try {
    const r = await api("/api/patterns/backtest", { method: "POST" });
    renderPatterns(r.items, r.summary);
    toast("回测完成");
  } catch (e) { toast("回测失败: " + e.message); }
  setFree("#btnBacktest", "🧪 规律回测");
}

async function runPredict() {
  setBusy("#btnPredict", "生成中（LLM 推理约 30-60s）…");
  try {
    const r = await api("/api/predict?n_tickets=10&regenerate=true", { method: "POST" });
    renderPredictions({ issue: r.issue, tickets: r.tickets, note: r.note });
    toast(r.from_cache ? "已复用缓存预测" : "预测已生成");
  } catch (e) { toast("预测失败: " + e.message); }
  setFree("#btnPredict", "🎯 生成预测");
}

async function runOfflineEval() {
  setBusy("#btnEval", "评估中（60-120s）…");
  try {
    const r = await api("/api/eval/backtest?issues=120&n=10", { method: "POST" });
    renderEval(r);
    toast("离线评估完成");
  } catch (e) { toast("评估失败: " + e.message); }
  setFree("#btnEval", "📊 离线评估");
}

async function runOnline() {
  setBusy("#btnOnline", "对照中…");
  try {
    const r = await api("/api/eval/online", { method: "POST" });
    renderOnline(r.rows || []);
    toast(`已对照 ${r.newly_checked || 0} 期`);
  } catch (e) { toast("在线对照失败: " + e.message); }
  setFree("#btnOnline", "✔ 在线对照");
}

/* ---------------- 预测渲染 ---------------- */

function renderPredictions(res) {
  $("#predIssue").textContent = res.issue ? `目标期号：${res.issue}` : "";
  const list = $("#predList");
  const items = res.tickets || [];
  if (!items.length) {
    list.innerHTML = '<div class="note">暂无预测，点击「生成预测」。</div>';
    return;
  }
  list.innerHTML = items.map((t, i) => {
    const balls = t.reds.map((r) => `<span class="ball red sm">${String(r).padStart(2, "0")}</span>`).join("");
    const blue = `<span class="ball blue sm">${String(t.blue).padStart(2, "0")}</span>`;
    const badge = t.method === "llm" ? '<span class="badge llm">LLM推理</span>'
      : `<span class="badge">${t.method}</span>`;
    const rt = t.reasoning ? `<div class="reasoning">💬 ${t.reasoning}</div>` : "";
    const used = (t.patterns_used || []).length ? `<div class="reasoning">规律引用：${t.patterns_used.join("、")}</div>` : "";
    return `<div class="ticket">
      <div class="row1">
        <span style="color:var(--muted)">#${i + 1}</span>
        <div class="balls">${balls} ${blue}</div>
        ${badge}
        <div class="conf"><num>置信度 ${Number(t.confidence).toFixed(1)}/100</num><div class="bar"><i style="width:${Math.min(100, t.confidence)}%"></i></div></div>
      </div>${rt}${used}
    </div>`;
  }).join("");
  $("#predNote").textContent = res.note || "";
}

/* ---------------- 多尺度统计 ---------------- */

function setWin(name) {
  win = name;
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("on", el.dataset.w === name));
  const feat = window._feat;
  if (feat) renderWindow(feat);
}
function renderStats(feat) {
  window._feat = feat;
  $("#winStats").textContent =
    `最新一期 ${feat.issue} ${feat.date}：红 ${feat.last_reds.join(" ")} 蓝 ${feat.last_blue}`;
  renderWindow(feat);
}
function renderWindow(feat) {
  const w = feat.windows[win];
  renderHeat(feat, w.red.freq);
  renderOmit(w.red.omission_current);
  renderSumChart(feat.recent);
  renderZoneChart(feat.recent);
}

function heatColor(v, max) {
  const t = Math.max(0, Math.min(1, v / max));
  // 低->高: 深蓝 -> 紫 -> 红
  const r = Math.round(20 + t * 205), g = Math.round(24 + t * 40), b = Math.round(40 + t * 60);
  return `rgb(${r},${g},${b})`;
}
function renderHeat(feat, freq) {
  const max = Math.max(...freq);
  const mean = freq.reduce((a, x) => a + x, 0) / 33;
  const cells = freq.map((v, i) => {
    const num = i + 1;
    const isBlue = num === feat.last_blue;
    const cls = isBlue ? "cell b" : "cell";
    const title = `${isBlue ? "上期蓝球 · " : ""}号码${num}: ${v}次${v > mean * 1.15 ? " (热)" : v < mean * 0.85 ? " (冷)" : ""}`;
    return `<div class="${cls}" style="background:${isBlue ? "" : heatColor(v, max)}" title="${title}">${String(num).padStart(2, "0")}</div>`;
  }).join("");
  $("#heatFreq").innerHTML = cells;
}

function renderOmit(om) {
  const top = om.map((v, i) => [i + 1, v]).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const ch = echartsInit("chOmit");
  if (!ch) return;
  ch.setOption({
    backgroundColor: "transparent", grid: { left: 36, right: 8, top: 8, bottom: 22 },
    xAxis: { type: "category", data: top.map((x) => x[0]), axisLabel: { color: "#8b949e" } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#2d333b" } }, axisLabel: { color: "#8b949e" } },
    series: [{
      type: "bar", data: top.map((x) => x[1]), itemStyle: { color: "#e5484d", borderRadius: [3, 3, 0, 0] },
      label: { show: true, position: "top", color: "#e6edf3", fontSize: 10 },
    }],
    tooltip: { trigger: "axis" },
  });
}

function renderSumChart(recent) {
  const ch = echartsInit("chSum");
  if (!ch) return;
  const issues = recent.map((r) => r.issue.slice(4));
  const sums = recent.map((r) => r.sum);
  const mean = sums.reduce((a, b) => a + b, 0) / sums.length;
  ch.setOption({
    backgroundColor: "transparent", grid: { left: 40, right: 8, top: 20, bottom: 22 },
    xAxis: { type: "category", data: issues, axisLabel: { color: "#8b949e", interval: 9 } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: "#2d333b" } }, axisLabel: { color: "#8b949e" } },
    series: [
      { name: "和值", type: "line", showSymbol: false, data: sums, lineStyle: { width: 2, color: "#3b82f6" }, areaStyle: { color: "rgba(59,130,246,.15)" } },
      { name: "均值", type: "line", showSymbol: false, data: sums.map(() => mean), lineStyle: { type: "dashed", color: "#d29922" } },
    ],
    tooltip: { trigger: "axis" },
  });
}

function renderZoneChart(recent) {
  const ch = echartsInit("chZone");
  if (!ch) return;
  const n = recent.length;
  const z1 = [], z2 = [], z3 = [];
  recent.forEach((r) => {
    const a = r.reds.filter((x) => x <= 11).length;
    const b = r.reds.filter((x) => x >= 12 && x <= 22).length;
    z1.push(a); z2.push(b); z3.push(6 - a - b);
  });
  ch.setOption({
    backgroundColor: "transparent", grid: { left: 40, right: 8, top: 20, bottom: 22 },
    xAxis: { type: "category", data: recent.map((r, i) => i), axisLabel: { show: false } },
    yAxis: { type: "value", max: 6, splitLine: { lineStyle: { color: "#2d333b" } }, axisLabel: { color: "#8b949e" } },
    series: [
      { name: "一区1-11", type: "bar", stack: "z", data: z1, itemStyle: { color: "#e5484d" } },
      { name: "二区12-22", type: "bar", stack: "z", data: z2, itemStyle: { color: "#3b82f6" } },
      { name: "三区23-33", type: "bar", stack: "z", data: z3, itemStyle: { color: "#3fb950" } },
    ],
    tooltip: { trigger: "axis" },
    legend: { textStyle: { color: "#8b949e" }, top: 0 },
  });
}

function echartsInit(id) {
  if (typeof echarts === "undefined") return null;
  const el = $("#" + id);
  if (!el) return null;
  if (charts[id]) { charts[id].dispose(); }
  charts[id] = echarts.init(el, null, { renderer: "canvas" });
  return charts[id];
}

/* ---------------- 规律 ---------------- */

function renderPatterns(items, summary) {
  $("#patSummary").textContent = summary ? `A:${summary.A} B:${summary.B} C:${summary.C}` : "";
  const tb = $("#patTable tbody");
  tb.innerHTML = items.map((p) => {
    const bt = p.backtest || {};
    const g = p.grade || "C";
    return `<tr>
      <td>${p.name_zh}</td><td>${p.kind}</td>
      <td style="color:var(--muted)">${(p.desc || "").slice(0, 40)}</td>
      <td>${bt.n ?? p.sample_size ?? "-"}</td>
      <td>${fmt(bt.avg_hits)}</td><td>${fmt(bt.expected)}</td>
      <td style="color:${(p.margin || 0) >= 0 ? "var(--green)" : "var(--red)"}">${fmt(p.margin, 3)}</td>
      <td class="mono">${fmt(p.p_value, 4)}</td><td class="mono">${fmt(p.p_adj, 4)}</td>
      <td><span class="badge grade${g}">${g}${p.refuted ? " ⚠️证伪" : ""}</span></td>
    </tr>`;
  }).join("");
  const ch = echartsInit("chPattern");
  if (ch) {
    const names = items.map((p) => p.name_zh);
    const margins = items.map((p) => p.margin || 0);
    const cols = margins.map((m) => (m >= 0 ? "#3fb950" : "#e5484d"));
    ch.setOption({
      backgroundColor: "transparent", grid: { left: 60, right: 16, top: 8, bottom: 70 },
      xAxis: { type: "category", data: names, axisLabel: { color: "#8b949e", rotate: 38, fontSize: 10 } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#2d333b" } }, axisLabel: { color: "#8b949e" } },
      series: [{
        type: "bar", data: margins.map((m, i) => ({ value: m, itemStyle: { color: cols[i], borderRadius: [3, 3, 0, 0] } })),
        label: { show: true, position: "top", fontSize: 9, color: "#8b949e" },
      }],
      tooltip: { trigger: "axis" },
    });
  }
}

/* ---------------- 历史 ---------------- */

function renderHistory(hist) {
  const issues = hist.issues.slice(-20);
  const reds = hist.reds.slice(-20);
  const blues = hist.blues.slice(-20);
  const rows = issues.map((iss, i) => {
    const cells = reds[i].map((r) => `<span class="ball red sm">${String(r).padStart(2, "0")}</span>`).join(" ");
    const b = `<span class="ball blue sm">${String(blues[i]).padStart(2, "0")}</span>`;
    return `<tr><td class="mono">${iss}</td><td><div class="balls">${cells} ${b}</div></td></tr>`;
  }).join("");
  $("#histGrid").innerHTML = `<table><tbody>${rows}</tbody></table>`;
}

/* ---------------- 评估 ---------------- */

function renderEval(r) {
  const s = r.system, b = r.random_baseline;
  const diff = (a, c) => (c == null ? "" : ` <span style="color:var(--muted);font-size:11px">diff ${a > 0 ? "+" : ""}${fmt(a, 3)}</span>`);
  $("#evalArea").innerHTML = `
    <div class="metrics">
      <div class="metric"><div class="k">红球平均命中（系统）</div><div class="v">${fmt(s.red_hits_mean)}${diff(r.delta_red_hits)}</div></div>
      <div class="metric"><div class="k">红球平均命中（随机）</div><div class="v">${fmt(b.red_hits_mean)}</div></div>
      <div class="metric"><div class="k">蓝球命中率（系统）</div><div class="v" style="font-size:16px">${pct(s.blue_hit_rate)}</div></div>
      <div class="metric"><div class="k">蓝球命中率（随机）</div><div class="v" style="font-size:16px">${pct(b.blue_hit_rate)}</div></div>
      <div class="metric"><div class="k">≥五等奖率（系统）</div><div class="v" style="font-size:16px">${pct(s.prize_rate_ge5)}</div></div>
      <div class="metric"><div class="k">≥五等奖率（随机）</div><div class="v" style="font-size:16px">${pct(b.prize_rate_ge5)}</div></div>
      <div class="metric"><div class="k">总奖金（系统 / 随机）</div><div class="v" style="font-size:16px">¥${s.reward_total.toFixed(0)} / ¥${b.reward_total.toFixed(0)}</div></div>
      <div class="metric"><div class="k">ROI（系统，2元/注）</div><div class="v ${s.roi < 0 ? "neg" : "pos"}">${pct(s.roi)}</div></div>
    </div>
    <div class="chart" id="chRedDist" style="height:180px"></div>
    <div class="note">${r.note}</div>`;
  const ch = echartsInit("chRedDist");
  if (ch) {
    const keys = [...new Set([...Object.keys(s.red_hits_dist), ...Object.keys(b.red_hits_dist)])].sort();
    ch.setOption({
      backgroundColor: "transparent", grid: { left: 34, right: 8, top: 10, bottom: 22 },
      xAxis: { type: "category", data: keys.map((k) => k + "红"), axisLabel: { color: "#8b949e" } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#2d333b" } }, axisLabel: { color: "#8b949e" } },
      series: [
        { name: "系统", type: "bar", data: keys.map((k) => s.red_hits_dist[k] || 0), itemStyle: { color: "#3b82f6" } },
        { name: "随机", type: "bar", data: keys.map((k) => b.red_hits_dist[k] || 0), itemStyle: { color: "#8b949e" } },
      ],
      tooltip: { trigger: "axis" },
      legend: { textStyle: { color: "#8b949e" }, top: 0 },
    });
  }
}

function renderOnline(rows) {
  // 去重：先移除上次追加的在线记录块
  const old = document.getElementById("onlineBlock");
  if (old) old.remove();
  const html = rows.length
    ? `<div class="note" style="margin-bottom:6px">在线对照记录（预测 vs 开奖）：最近 ${rows.length} 期</div>
       <div class="scroll"><table>
       <thead><tr><th>期号</th><th>红球命中</th><th>蓝球命中</th><th>奖金</th></tr></thead>
       <tbody>${rows.map((r) => `<tr><td class="mono">${r.issue}</td><td>${r.red_hits}</td>
         <td>${r.blue_hit ? "✔" : "—"}</td><td>¥${Number(r.reward || 0).toFixed(0)}</td></tr>`).join("")}
       </tbody></table></div>`
    : '<div class="note">暂无在线对照记录（开奖后可点「在线对照」）。</div>';
  const ev = $("#evalArea");
  const add = document.createElement("div");
  add.id = "onlineBlock";
  add.style.cssText = "border-top:1px solid var(--border);margin-top:12px;padding-top:10px";
  add.innerHTML = html;
  ev.appendChild(add);
}

window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
loadAll();
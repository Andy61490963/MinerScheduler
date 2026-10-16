"use strict";
const $ = id => document.getElementById(id);
const finite = value => typeof value === "number" && Number.isFinite(value);
const format = (value, digits = 1) => finite(value) ? value.toLocaleString("zh-TW", {minimumFractionDigits: digits, maximumFractionDigits: digits}) : "--";
const put = (id, value) => { $(id).textContent = value; };
const timeText = timestamp => finite(timestamp) ? new Date(timestamp * 1000).toLocaleTimeString("zh-TW", {hour12: false, timeZone: "Asia/Taipei"}) : "--";
const ageText = seconds => !finite(seconds) ? "等待資料" : seconds < 2 ? "剛剛更新" : `${Math.round(seconds)} 秒前`;
let snapshot = null;
let paused = false;
let metric = "hashrate";
let range = 15;
let chartPoints = [];
let chartGeometry = null;
let disconnected = false;
let timer;
let startRequestPending=false;
let stopRequestPending=false;
let settingsData=null;
let settingsPending=false;
let settingsLoading=false;
let settingsWasBusy=false;
let pollPending=false;
let logSource="miner";
let logTimer;
let logRequest=0;
const metrics = {
  hashrate: {label: "算力", unit: "TH/s", color: "#6ccdeb", legend: "礦工回報算力", digits: 2},
  power: {label: "耗電", unit: "W", color: "#f5c36a", legend: "GPU 實際耗電", digits: 1},
  efficiency: {label: "每瓦效率", unit: "TH/s/W", color: "#64e6bb", legend: "回報區間效率估算", digits: 3},
  core: {label: "核心溫度", unit: "°C", color: "#d9b47d", legend: "GPU 核心溫度", digits: 1},
  hotspot: {label: "熱點溫度", unit: "°C", color: "#d78f8f", legend: "GPU 晶片熱點", digits: 1},
  memory: {label: "顯存溫度", unit: "°C", color: "#a59dd9", legend: "GPU 顯存溫度", digits: 1},
  utilization: {label: "GPU 使用率", unit: "%", color: "#79b6d5", legend: "GPU 使用率", digits: 0},
  core_clock: {label: "核心時脈", unit: "MHz", color: "#b4c8dd", legend: "GPU 核心時脈", digits: 0},
  fan_rpm: {label: "風扇轉速", unit: "RPM", color: "#80b7ac", legend: "兩組風扇平均轉速", digits: 0}
};

function setBadge(state, text) {
  const badge = $("live-badge");
  badge.className = `badge ${state}`;
  const dot = document.createElement("i");
  badge.replaceChildren(dot, document.createTextNode(text));
}

function duration(seconds) {
  if (!finite(seconds)) return "--";
  const minutes = Math.floor(seconds / 60);
  return `${Math.floor(minutes / 60)} 小時 ${String(minutes % 60).padStart(2, "0")} 分`;
}

function renderFans(data) {
  const board = data.board || {};
  const boardAge = finite(board.timestamp) ? data.server_time - board.timestamp : null;
  const boardFresh = finite(boardAge) && boardAge < 45 && !board.error;
  put("board-age", boardFresh ? `主機板 ${ageText(boardAge)} · 15 秒更新` : "主機板讀值暫時無法取得");
  const gpu = [1, 2].map((n, index) => ({...(data.gpu_fans || [])[index], name: `顯卡風扇 ${n}`, fresh: data.sensor_fresh}));
  const boardFans = [1, 2, 6].map(n => ({...((board.fans || []).find(f => f.name === `主機板 Fan #${n}`) || {}), name: `主機板 Fan #${n}`, fresh: boardFresh}));
  const nodes = [...gpu, ...boardFans].map(fan => {
    const container = document.createElement("article");
    container.className = "fan";
    const label = document.createElement("div");
    label.className = "fan-label";
    const icon = document.createElement("span");
    icon.className = "fan-icon";
    icon.textContent = "✥";
    icon.setAttribute("aria-hidden", "true");
    label.append(icon, document.createTextNode(fan.name));
    const value = document.createElement("div");
    value.className = "fan-value";
    value.textContent = format(fan.fresh ? fan.rpm : null, 0);
    const unit = document.createElement("small");
    unit.textContent = "RPM";
    value.append(unit);
    const bottom = document.createElement("div");
    bottom.className = "fan-bottom";
    const percent = document.createElement("b");
    percent.textContent = `${format(fan.fresh ? fan.percent : null, 0)}%`;
    bottom.append(document.createTextNode(fan.fresh ? "目前轉速比例" : "讀值暫停更新"), percent);
    const meter = document.createElement("div");
    meter.className = "fan-meter";
    const bar = document.createElement("i");
    bar.style.width = `${fan.fresh && finite(fan.percent) ? Math.min(100, Math.max(0, fan.percent)) : 0}%`;
    meter.append(bar);
    container.append(label, value, bottom, meter);
    return container;
  });
  $("fans").replaceChildren(...nodes);
}

function render(data) {
  snapshot = data;
  document.body.classList.toggle("stale", !data.sensor_fresh);
  const running = data.state === "running";
  setBadge(running ? "" : data.state === "stopped" ? "neutral" : "warning", running ? "挖礦運行中" : data.state === "stopped" ? data.manual_stop ? "人工停挖中" : "礦工未運行" : "等待最新狀態");
  $("live-badge").title = data.message || "";
  put("hashrate", format(data.hashrate, 2));
  put("hash-1m", format(data.one_minute, 2));
  put("hash-15m", format(data.fifteen_minutes, 2));
  put("hash-age", finite(data.hash_age) ? `${Math.round(data.hash_age)} 秒前回報` : "等待回報");
  $("hash-age").title = `最後算力回報：${timeText(data.hash_time)}`;
  put("power", format(data.sensor_fresh ? data.power : null, 1));
  put("power-limit", format(data.limit, 0));
  put("utilization", format(data.sensor_fresh ? data.utilization : null, 0));
  put("efficiency", format(data.efficiency, 3));
  put("efficiency-note", finite(data.efficiency) ? `回報前均耗 ${format(data.aligned_power, 1)} W · GPU 估算` : "等待有效算力與對齊耗電資料");
  put("accepted", format(data.accepted, 0));
  put("rejected", format(data.rejected, 0));
  put("hardware-errors", format(data.hardware_errors, 0));
  const temperatures = data.temperatures || {};
  for (const key of ["core", "hotspot", "memory"]) {
    put(`temp-${key}`, format(data.sensor_fresh ? temperatures[key] : null));
    put(`peak-${key}`, format((data.peaks || {})[key]));
    $(`bar-${key}`).style.width = `${data.sensor_fresh && finite(temperatures[key]) ? Math.min(100, Math.max(0, temperatures[key] / 120 * 100)) : 0}%`;
  }
  put("temp-delta", data.sensor_fresh && finite(temperatures.hotspot) && finite(temperatures.core) ? format(temperatures.hotspot - temperatures.core) : "--");
  $("bar-hotspot").style.backgroundColor = temperatures.hotspot >= 110 ? "#ff8583" : "#f5c36a";
  const alerts = (data.warnings || []).map(warning => {
    const element = document.createElement("div");
    element.className = "alert";
    element.textContent = `△  ${warning}`;
    return element;
  });
  $("alerts").replaceChildren(...alerts);
  renderFans(data);
  put("elapsed", duration(data.elapsed));
  put("core-clock", format(data.sensor_fresh ? data.core_clock : null, 0));
  put("memory-clock", format(data.sensor_fresh ? data.memory_clock : null, 0));
  put("profile-mode", data.active_profile || "等待資料");
  put("guard", data.guard_ok ? `正常 · ${ageText(data.guard_age)}` : `待確認 · ${ageText(data.guard_age)}`);
  $("guard").className = data.guard_ok ? "good" : "warning";
  put("sensor-age", `${ageText(data.sensor_age ?? data.status_age)} · ${timeText(data.sensor_time ?? data.status_time)}`);
  renderAction(data);
  renderSettingsState(data);
  drawChart();
}

function renderAction(data) {
  const action=data.action || {};
  const button=$("start-mining");
  const stopButton=$("stop-mining");
  const stopping=stopRequestPending || action.phase==="stopping";
  const manuallyStopped=data.state==="stopped" && data.manual_stop===true && !action.busy && !startRequestPending;
  const healthy=data.state==="running" && data.guard_ok && Date.now()/1000-data.server_time<=6;
  button.disabled=startRequestPending || stopRequestPending || !!action.busy || healthy || !data.action_token || paused;
  button.textContent=stopping ? "▶ 開始／恢復挖礦" : action.phase==="settings" && action.busy ? "◌ 套用設定中…" : startRequestPending || action.busy ? "◌ 啟動核對中…" : healthy ? "✓ 挖礦運行中" : "▶ 開始／恢復挖礦";
  stopButton.disabled=stopping || (!action.busy && manuallyStopped) || !data.action_token || disconnected;
  stopButton.textContent=stopping ? "◌ 停止中…" : manuallyStopped ? "■ 已人工停止" : "■ 停止挖礦";
  const message = (!action.busy && data.state === "stopped" && !data.manual_stop && data.reason) ? `停止原因：${data.reason}` : (action.message || "");
  $("action-status").hidden=!message;
  $("action-status").textContent=message;
  $("action-status").className="action-status"+(action.phase==="error" ? " error" : "");
}

function svgElement(tag, attributes = {}, content) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  if (content !== undefined) node.textContent = content;
  return node;
}

function drawChart() {
  const spec = metrics[metric];
  const svg = $("chart");
  svg.replaceChildren();
  put("chart-subtitle", `最近 ${range} 分鐘 · 可移到曲線查看讀值`);
  put("chart-unit", spec.unit);
  put("chart-legend", spec.legend);
  $("chart-dot").style.backgroundColor = spec.color;
  svg.setAttribute("aria-label", `最近 ${range} 分鐘${spec.label}趨勢，單位 ${spec.unit}`);
  if (!snapshot) return;
  const end = snapshot.server_time;
  const begin = end - range * 60;
  const source = (metric === "hashrate" || metric === "efficiency") ? (snapshot.hash_history || []) : (snapshot.history || []);
  const points = source.filter(p => p.t >= begin && p.t <= end).map(p => ({t: p.t, value: p[metric]}));
  const valid = points.filter(p => finite(p.value));
  chartPoints = valid;
  $("chart-empty").hidden = valid.length > 0;
  if (!valid.length) {
    put("chart-average", "平均 --");
    chartGeometry = null;
    return;
  }
  const width = Math.max(300, svg.clientWidth || 760);
  const height = Math.max(180, svg.clientHeight || 245);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const left = 54, right = width - 13, top = 16, bottom = height - 37;
  let low = Math.min(...valid.map(p => p.value));
  let high = Math.max(...valid.map(p => p.value));
  const padding = Math.max((high - low) * .25, high * .015, metric === "efficiency" ? .005 : 1);
  low = Math.max(0, low - padding);
  high += padding;
  const x = t => left + (t - begin) / (end - begin) * (right - left);
  const y = v => bottom - (v - low) / (high - low) * (bottom - top);
  chartGeometry = {left, right, top, bottom, begin, end, x, y, width};
  for (let i = 0; i < 5; i++) {
    const yy = top + (bottom - top) * i / 4;
    svg.append(svgElement("line", {x1: left, x2: right, y1: yy, y2: yy, stroke: "#253240", "stroke-dasharray": "3 5", "stroke-width": .8}));
    svg.append(svgElement("text", {x: left - 12, y: yy + 4, fill: "#8192a7", "font-size": 10, "text-anchor": "end"}, format(high - (high - low) * i / 4, metric === "efficiency" ? 3 : 1)));
  }
  for (let i = 0; i < 5; i++) {
    const timestamp = begin + (end - begin) * i / 4;
    const label = new Date(timestamp * 1000).toLocaleTimeString("zh-TW", {hour12: false, hour: "2-digit", minute: "2-digit", timeZone: "Asia/Taipei"});
    svg.append(svgElement("text", {x: x(timestamp), y: height - 12, fill: "#8192a7", "font-size": 10, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle"}, label));
  }
  const defs = svgElement("defs");
  const gradient = svgElement("linearGradient", {id: "chart-fill", x1: 0, y1: 0, x2: 0, y2: 1});
  gradient.append(svgElement("stop", {offset: "0%", "stop-color": spec.color, "stop-opacity": .16}), svgElement("stop", {offset: "100%", "stop-color": spec.color, "stop-opacity": 0}));
  defs.append(gradient);
  svg.append(defs);
  let segment = [];
  const segments = [];
  const maxGap = ["hashrate", "efficiency"].includes(metric) ? 180 : 10;
  for (const point of points) {
    if (!finite(point.value)) {
      if (segment.length) segments.push(segment);
      segment = [];
      continue;
    }
    if (segment.length && point.t - segment[segment.length - 1].t > maxGap) {
      segments.push(segment);
      segment = [];
    }
    segment.push(point);
  }
  if (segment.length) segments.push(segment);
  for (const group of segments) {
    const path = group.map((p, index) => `${index ? "L" : "M"}${x(p.t).toFixed(2)},${y(p.value).toFixed(2)}`).join(" ");
    if (group.length > 1) {
      svg.append(svgElement("path", {d: `${path} L${x(group[group.length - 1].t)},${bottom} L${x(group[0].t)},${bottom} Z`, fill: "url(#chart-fill)"}));
      svg.append(svgElement("path", {d: path, fill: "none", stroke: spec.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke"}));
    }
  }
  if (metric !== "power") {
    for (const point of valid) {
      const dot = svgElement("circle", {cx: x(point.t), cy: y(point.value), r: 2.7, fill: spec.color, stroke: "#131c27", "stroke-width": 1.3});
      dot.append(svgElement("title", {}, `${timeText(point.t)} · ${format(point.value, spec.digits)} ${spec.unit}`));
      svg.append(dot);
    }
  }
  const last = valid[valid.length - 1];
  svg.append(svgElement("circle", {cx: x(last.t), cy: y(last.value), r: 4, fill: spec.color, stroke: "#131c27", "stroke-width": 2}));
  const average = valid.reduce((sum, p) => sum + p.value, 0) / valid.length;
  put("chart-average", `樣本平均 ${format(average, spec.digits)} ${spec.unit}`);
  tooltip.hidden = true;
}

const tooltip = document.createElement("div");
tooltip.className = "chart-tooltip";
tooltip.hidden = true;
document.querySelector(".chart-container").append(tooltip);
$("chart").addEventListener("pointermove", event => {
  if (!chartGeometry || !chartPoints.length) return;
  const rect = $("chart").getBoundingClientRect();
  const px = (event.clientX - rect.left) / rect.width * chartGeometry.width;
  const {left, right, begin, end} = chartGeometry;
  if (px < left || px > right) { tooltip.hidden = true; return; }
  const timestamp = begin + (px - left) / (right - left) * (end - begin);
  const point = chartPoints.reduce((best, p) => Math.abs(p.t - timestamp) < Math.abs(best.t - timestamp) ? p : best);
  const spec = metrics[metric];
  tooltip.textContent = `${timeText(point.t)}  ·  ${format(point.value, spec.digits)} ${spec.unit}`;
  tooltip.style.left = `${Math.max(6, Math.min(rect.width - 220, event.clientX - rect.left + 10))}px`;
  tooltip.style.top = "12px";
  tooltip.hidden = false;
});
$("chart").addEventListener("pointerleave", () => { tooltip.hidden = true; });

document.querySelectorAll("[data-metric]").forEach(button => {
  button.addEventListener("click", () => {
    metric = button.dataset.metric;
    document.querySelectorAll("[data-metric]").forEach(item => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    drawChart();
  });
});
document.querySelectorAll("[data-range]").forEach(button => {
  button.addEventListener("click", () => {
    range = Number(button.dataset.range);
    document.querySelectorAll("[data-range]").forEach(item => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    drawChart();
  });
});
$("pause").addEventListener("click", () => {
  paused = !paused;
  $("pause").textContent = paused ? "▷ 繼續更新" : "Ⅱ 暫停畫面";
  $("pause").setAttribute("aria-pressed", String(paused));
  renderSettingsState(snapshot || {});
  clearTimeout(timer);
  if (paused) {
    setBadge("neutral", "畫面已暫停");
    put("source-note", "僅暫停此畫面更新，挖礦與原有保護持續運行。按「繼續更新」恢復讀值。");
    $("start-mining").disabled=true;
  } else {
    put("source-note", "算力依礦工實際回報更新；感測紀錄約每秒更新，主機板風扇約每 15 秒讀取。");
    poll();
    clearTimeout(logTimer);
    pollLogs();
  }
});

async function poll() {
  if (paused || pollPending) return;
  pollPending=true;
  try {
    const response = await fetch("/api/snapshot", {cache: "no-store", signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error("連線失敗");
    const data = await response.json();
    if (data.identity !== "test-mining-efficiency-monitor-v1") throw new Error("來源不符");
    if (Date.now()/1000-data.server_time>6) {
      data.state="unknown";
      data.sensor_fresh=false;
      data.guard_ok=false;
      data.hashrate=null;
      data.efficiency=null;
      data.warnings=["監控資料更新延遲，可查看 LOG 或按恢復挖礦。"];
    }
    if (paused) return;
    disconnected = false;
    $("connection-error").hidden = true;
    render(data);
  } catch (_) {
    if (!paused) {
      disconnected = true;
      $("connection-error").hidden = false;
      document.body.classList.add("stale");
      setBadge("warning", "連線中斷");
      $("start-mining").disabled=true;
      $("stop-mining").disabled=true;
      renderSettingsState(snapshot || {});
    }
  } finally {
    pollPending=false;
    if (!paused) timer = setTimeout(poll, 1000);
  }
}

$("start-mining").addEventListener("click",async()=>{
  if (!snapshot?.action_token || startRequestPending) return;
  startRequestPending=true;
  renderAction(snapshot);
  try {
    const response=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json","X-Monitor-Token":snapshot.action_token},body:"{}",signal:AbortSignal.timeout(10000)});
    const data=await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "啟動要求未能送出。");
    $("action-status").hidden=false;
    $("action-status").textContent=data.message;
    logSource="startup";
    updateLogTabs();
    clearTimeout(logTimer);
    pollLogs();
  } catch (error) {
    $("action-status").hidden=false;
    $("action-status").className="action-status error";
    $("action-status").textContent=error.message;
  } finally {
    startRequestPending=false;
    clearTimeout(timer);
    poll();
  }
});

$("stop-mining").addEventListener("click",async()=>{
  if (!snapshot?.action_token || stopRequestPending) return;
  stopRequestPending=true;
  if (paused) {
    paused=false;
    $("pause").textContent="Ⅱ 暫停畫面";
    $("pause").setAttribute("aria-pressed","false");
    put("source-note","算力依礦工實際回報更新；感測紀錄約每秒更新，主機板風扇約每 15 秒讀取。");
  }
  renderAction(snapshot);
  try {
    const response=await fetch("/api/stop",{method:"POST",headers:{"Content-Type":"application/json","X-Monitor-Token":snapshot.action_token},body:"{}",signal:AbortSignal.timeout(10000)});
    const data=await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || "停止要求未能送出。");
    $("action-status").hidden=false;
    $("action-status").textContent=data.message;
    logSource="actions";
    updateLogTabs();
    clearTimeout(logTimer);
    pollLogs();
  } catch (error) {
    $("action-status").hidden=false;
    $("action-status").className="action-status error";
    $("action-status").textContent=error.message;
  } finally {
    stopRequestPending=false;
    clearTimeout(timer);
    poll();
  }
});

function renderSettingsState(data){
  const busy=!!data.action?.busy;
  const disabled=settingsPending || settingsLoading || busy || disconnected || paused || !settingsData || !data.action_token;
  $("settings-fields").disabled=disabled;
  $("apply-settings").disabled=disabled;
  $("reload-settings").disabled=settingsPending || settingsLoading || busy || disconnected;
  $("apply-settings").textContent=settingsPending || (busy && data.action?.phase==="settings") ? "套用中…" : "套用設定";
  if (busy && data.action?.phase==="settings") {
    $("settings-message").textContent=data.action.message;
  }
  if (settingsWasBusy && !busy && !settingsPending) loadSettings();
  settingsWasBusy=busy;
}

async function loadSettings(){
  if (settingsLoading) return;
  settingsLoading=true;
  renderSettingsState(snapshot || {});
  try {
    const response=await fetch('/api/settings',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    const data=await response.json();
    if (!response.ok || !data.values || !data.revision) throw new Error(data.message || '設定讀取失敗');
    settingsData=data;
    for (const [key,value] of Object.entries(data.values)) {
      const input=$(`setting-${key}`);
      if (input.type === "checkbox") input.checked = value === true;
      else input.value = value;
      if (data.limits[key]) {
        for (const name of ['min','max','step']) input[name] = data.limits[key][name];
      }
    }
    $("settings-message").className="settings-message";
    $("settings-message").textContent='已讀取保存設定；上方即時讀值顯示實際功耗與轉速。';
  } catch (error) {
    $("settings-message").className="settings-message error";
    $("settings-message").textContent=error.message;
  } finally {
    settingsLoading=false;
    renderSettingsState(snapshot || {});
  }
}
$("reload-settings").addEventListener('click',loadSettings);
$("settings-form").addEventListener('submit',async event=>{
  event.preventDefault();
  if (!settingsData || settingsPending || !snapshot?.action_token || !$("settings-form").reportValidity()) return;
  const payload={revision:settingsData.revision};
  for (const key of Object.keys(settingsData.values)) {
    const control = $(`setting-${key}`);
    if (control.type === "checkbox") payload[key] = control.checked;
    else if (control.type === "time") payload[key] = control.value;
    else payload[key] = Number(control.value);
  }
  settingsPending=true;
  renderSettingsState(snapshot);
  try {
    const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Token':snapshot.action_token},body:JSON.stringify(payload),signal:AbortSignal.timeout(10000)});
    const data=await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || '設定未能送出');
    $("settings-message").className="settings-message";
    $("settings-message").textContent=data.message;
    logSource='actions';
    updateLogTabs();
    clearTimeout(logTimer);
    pollLogs();
  } catch (error) {
    $("settings-message").className="settings-message error";
    $("settings-message").textContent=error.message;
  } finally {
    settingsPending=false;
    clearTimeout(timer);
    poll();
    renderSettingsState(snapshot);
  }
});

function updateLogTabs(){
  document.querySelectorAll("[data-log]").forEach(button=>{
    const selected=button.dataset.log===logSource;
    button.classList.toggle("selected",selected);
    button.setAttribute("aria-pressed",String(selected));
  });
}
document.querySelectorAll("[data-log]").forEach(button=>{
  button.addEventListener("click",()=>{
    logSource=button.dataset.log;
    updateLogTabs();
    clearTimeout(logTimer);
    pollLogs();
  });
});
async function pollLogs(){
  clearTimeout(logTimer);
  if (paused) { logTimer=setTimeout(pollLogs,2000); return; }
  const request=++logRequest;
  try{
    const response=await fetch(`/api/logs?source=${encodeURIComponent(logSource)}`,{cache:"no-store",signal:AbortSignal.timeout(5000)});
    const data=await response.json();
    if (!response.ok) throw new Error(data.message || "LOG 讀取失敗");
    if (request!==logRequest || paused) return;
    const content=String(data.text || "尚無紀錄。").replace(/\u001b\[[0-9;]*[a-zA-Z]/g,"").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,"");
    const pane=$("log-content");
    if (pane.textContent!==content) {
      pane.textContent=content;
      if ($("log-follow").checked) pane.scrollTop=pane.scrollHeight;
    }
    put("log-updated",`${data.name} · 讀取 ${timeText(data.read_time)}`);
  }catch(error){
    if (request===logRequest) put("log-updated",error.message+"，正在重試");
  }finally{
    if (request===logRequest) logTimer=setTimeout(pollLogs,2000);
  }
}
setInterval(() => put("clock", new Date().toLocaleTimeString("zh-TW", {hour12: false, timeZone: "Asia/Taipei"})), 1000);
window.addEventListener("resize", drawChart);
poll();
pollLogs();
loadSettings();

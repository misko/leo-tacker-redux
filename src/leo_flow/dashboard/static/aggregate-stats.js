"use strict";

const HOUR_NS = 3_600_000_000_000n;
const METHOD_COLORS = Object.freeze([
  "#84e6b0", "#ffca6b", "#73b7ff", "#f28cb1",
  "#c6a0f6", "#8bd5ca", "#ed8796", "#a6da95",
]);
let distributions = [];
let visibleMethods = new Set();

const byId = (id) => document.getElementById(id);

function appendText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(text);
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function formatUtcNs(value) {
  const milliseconds = Number(value) / 1_000_000;
  return Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString() : "Unavailable";
}

function selectedBounds() {
  const stop = BigInt(Date.now()) * 1_000_000n;
  return { start: stop - BigInt(byId("density-window-hours").value) * HOUR_NS, stop };
}

async function fetchJson(path) {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body?.error?.message || `Request failed (${response.status})`);
  return body;
}

function setAppState(state, message) {
  byId("aggregate-status").dataset.state = state;
  byId("aggregate-status-text").textContent = message;
  byId("density-state").dataset.state = state;
  byId("density-state").textContent = message;
}

function renderSelector() {
  const selector = byId("method-selector");
  selector.replaceChildren(selector.querySelector("legend"));
  distributions.forEach((item, index) => {
    const label = document.createElement("label");
    label.className = "method-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = item.method;
    input.checked = visibleMethods.has(item.method);
    input.addEventListener("change", () => {
      if (input.checked) visibleMethods.add(item.method);
      else visibleMethods.delete(item.method);
      renderPlot();
    });
    const swatch = document.createElement("span");
    swatch.className = "method-swatch";
    swatch.style.backgroundColor = METHOD_COLORS[index % METHOD_COLORS.length];
    label.append(input, swatch, document.createTextNode(item.method));
    selector.append(label);
  });
}

function renderSummary() {
  const body = byId("score-summary-body");
  body.replaceChildren();
  for (const item of distributions) {
    const row = document.createElement("tr");
    appendText(row, "th", item.method).scope = "row";
    appendText(row, "td", item.recording_count);
    appendText(row, "td", item.score_count);
    appendText(row, "td", Number(item.mean).toFixed(6));
    appendText(row, "td", Number(item.standard_deviation).toFixed(6));
    appendText(row, "td", Number(item.minimum).toFixed(6));
    appendText(row, "td", Number(item.maximum).toFixed(6));
    body.append(row);
  }
}

function renderPlot() {
  const canvas = byId("density-canvas");
  const context = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const cssWidth = canvas.parentElement.clientWidth || 900;
  const cssHeight = Math.max(360, Math.min(560, cssWidth * 0.52));
  canvas.style.width = `${cssWidth}px`;
  canvas.style.height = `${cssHeight}px`;
  canvas.width = Math.round(cssWidth * ratio);
  canvas.height = Math.round(cssHeight * ratio);
  context.scale(ratio, ratio);
  context.clearRect(0, 0, cssWidth, cssHeight);

  const selected = distributions.filter((item) => visibleMethods.has(item.method));
  const margin = { left: 62, right: 20, top: 24, bottom: 48 };
  const width = cssWidth - margin.left - margin.right;
  const height = cssHeight - margin.top - margin.bottom;
  const maximumDensity = Math.max(1, ...selected.flatMap((item) => item.bins.map((bin) => bin.density)));

  context.strokeStyle = "#31453c";
  context.fillStyle = "#98aaa1";
  context.lineWidth = 1;
  context.font = "12px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.textAlign = "center";
  context.textBaseline = "top";
  for (let tick = 0; tick <= 5; tick += 1) {
    const x = margin.left + width * tick / 5;
    context.beginPath(); context.moveTo(x, margin.top); context.lineTo(x, margin.top + height); context.stroke();
    context.fillText((tick / 5).toFixed(1), x, margin.top + height + 10);
  }
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = maximumDensity * tick / 4;
    const y = margin.top + height - height * tick / 4;
    context.beginPath(); context.moveTo(margin.left, y); context.lineTo(margin.left + width, y); context.stroke();
    context.fillText(value.toFixed(1), margin.left - 9, y);
  }
  context.save();
  context.translate(16, margin.top + height / 2);
  context.rotate(-Math.PI / 2);
  context.textAlign = "center";
  context.fillText("density", 0, 0);
  context.restore();
  context.textAlign = "center";
  context.fillText("candidate score (native 0–1 domain)", margin.left + width / 2, cssHeight - 15);

  distributions.forEach((item, index) => {
    if (!visibleMethods.has(item.method)) return;
    const color = METHOD_COLORS[index % METHOD_COLORS.length];
    context.beginPath();
    context.moveTo(margin.left, margin.top + height);
    item.bins.forEach((bin, binIndex) => {
      const left = margin.left + width * bin.lower;
      const right = margin.left + width * bin.upper;
      const y = margin.top + height - height * bin.density / maximumDensity;
      if (binIndex === 0) context.lineTo(left, y);
      else context.lineTo(left, y);
      context.lineTo(right, y);
    });
    context.lineTo(margin.left + width, margin.top + height);
    context.closePath();
    context.globalAlpha = 0.10;
    context.fillStyle = color;
    context.fill();
    context.globalAlpha = 0.95;
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.stroke();
    context.globalAlpha = 1;
  });
}

async function refresh() {
  const bounds = selectedBounds();
  byId("density-window-label").textContent = `UTC [${formatUtcNs(bounds.start)}, ${formatUtcNs(bounds.stop)})`;
  setAppState("loading", "Loading score distributions…");
  try {
    const payload = await fetchJson(`/api/v7/score-distributions?start_utc_ns=${bounds.start}&stop_utc_ns=${bounds.stop}`);
    distributions = payload.distributions || [];
    const available = new Set(distributions.map((item) => item.method));
    visibleMethods = new Set([...visibleMethods].filter((method) => available.has(method)));
    if (visibleMethods.size === 0) visibleMethods = new Set(available);
    renderSelector();
    renderSummary();
    renderPlot();
    const scores = distributions.reduce((total, item) => total + item.score_count, 0);
    setAppState(
      distributions.length ? "ready" : "empty",
      distributions.length
        ? `${scores.toLocaleString()} scores across ${distributions.length} algorithms.`
        : "No completed detector-suite scores are available in this interval.",
    );
  } catch (error) {
    distributions = [];
    renderSelector(); renderSummary(); renderPlot();
    setAppState("error", error instanceof Error ? error.message : "Score distributions unavailable");
  }
}

byId("density-window-form").addEventListener("submit", (event) => {
  event.preventDefault();
  refresh();
});
window.addEventListener("resize", renderPlot);
refresh();

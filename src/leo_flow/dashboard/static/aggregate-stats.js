"use strict";

const HOUR_NS = 3_600_000_000_000n;
const METHOD_COLORS = Object.freeze([
  "#84e6b0", "#ffca6b", "#73b7ff", "#f28cb1",
  "#c6a0f6", "#8bd5ca", "#ed8796", "#a6da95",
]);
let strata = [];
let methodOrder = [];
let visibleMethods = new Set();

const byId = (id) => document.getElementById(id);

function appendText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(text);
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function scoreKindLabel(scoreKind) {
  return scoreKind === "conditioned-control"
    ? "rolled template at target-selected hypothesis"
    : scoreKind;
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
    method: "GET", credentials: "same-origin", headers: { accept: "application/json" },
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
  methodOrder.forEach((method, index) => {
    const label = document.createElement("label");
    label.className = "method-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = method;
    input.checked = visibleMethods.has(method);
    input.addEventListener("change", () => {
      if (input.checked) visibleMethods.add(method);
      else visibleMethods.delete(method);
      renderAll();
    });
    const swatch = document.createElement("span");
    swatch.className = "method-swatch";
    swatch.style.backgroundColor = METHOD_COLORS[index % METHOD_COLORS.length];
    label.append(input, swatch, document.createTextNode(method));
    selector.append(label);
  });
}

function populateSelect(id, values, allLabel) {
  const select = byId(id);
  const previous = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = allLabel;
  select.append(all);
  for (const value of [...new Set(values)].sort()) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
  select.value = [...select.options].some((item) => item.value === previous) ? previous : "all";
}

function filteredStrata() {
  const radio = byId("density-radio").value;
  const receiver = byId("density-receiver").value;
  const edge = byId("density-edge").value;
  const kinds = new Set();
  if (byId("show-candidate").checked) kinds.add("candidate");
  if (byId("show-control").checked) kinds.add("conditioned-control");
  return strata.filter((item) =>
    visibleMethods.has(item.method)
      && (radio === "all" || item.radio_id === radio)
      && (receiver === "all" || item.receiver_chain_id === receiver)
      && (edge === "all" || item.edge === edge)
      && kinds.has(item.score_kind)
  );
}

function aggregateSeries() {
  const groups = new Map();
  for (const item of filteredStrata()) {
    const key = `${item.method}\u0000${item.score_kind}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        method: item.method, score_kind: item.score_kind, point_count: 0,
        weighted_sum: 0, weighted_second: 0, minimum: 1, maximum: 0,
        counts: new Array(40).fill(0), bins: [],
      };
      groups.set(key, group);
    }
    group.point_count += item.point_count;
    group.weighted_sum += item.mean * item.point_count;
    group.weighted_second += (
      item.standard_deviation ** 2 + item.mean ** 2
    ) * item.point_count;
    group.minimum = Math.min(group.minimum, item.minimum);
    group.maximum = Math.max(group.maximum, item.maximum);
    item.bins.forEach((bin) => { group.counts[bin.index] += bin.count; });
  }
  return [...groups.values()].map((group) => {
    group.mean = group.weighted_sum / group.point_count;
    group.standard_deviation = Math.sqrt(Math.max(
      0, group.weighted_second / group.point_count - group.mean ** 2,
    ));
    group.bins = group.counts.map((count, index) => ({
      index, lower: index / 40, upper: (index + 1) / 40,
      count, density: count / group.point_count * 40,
    }));
    return group;
  }).sort((left, right) => (
    methodOrder.indexOf(left.method) - methodOrder.indexOf(right.method)
      || left.score_kind.localeCompare(right.score_kind)
  ));
}

function renderSummary(series) {
  const body = byId("score-summary-body");
  body.replaceChildren();
  for (const item of series) {
    const row = document.createElement("tr");
    appendText(row, "th", `${item.method} · ${scoreKindLabel(item.score_kind)}`).scope = "row";
    appendText(row, "td", item.point_count);
    appendText(row, "td", item.mean.toFixed(6));
    appendText(row, "td", item.standard_deviation.toFixed(6));
    appendText(row, "td", item.minimum.toFixed(6));
    appendText(row, "td", item.maximum.toFixed(6));
    body.append(row);
  }
}

function renderPlot(series) {
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

  const margin = { left: 62, right: 20, top: 24, bottom: 48 };
  const width = cssWidth - margin.left - margin.right;
  const height = cssHeight - margin.top - margin.bottom;
  const maximumDensity = Math.max(1, ...series.flatMap((item) => item.bins.map((bin) => bin.density)));
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
  context.fillText("single-section detector score (native 0–1 domain)", margin.left + width / 2, cssHeight - 15);

  for (const item of series) {
    const index = methodOrder.indexOf(item.method);
    const color = METHOD_COLORS[index % METHOD_COLORS.length];
    context.beginPath();
    item.bins.forEach((bin, binIndex) => {
      const left = margin.left + width * bin.lower;
      const right = margin.left + width * bin.upper;
      const y = margin.top + height - height * bin.density / maximumDensity;
      if (binIndex === 0) context.moveTo(left, y);
      else context.lineTo(left, y);
      context.lineTo(right, y);
    });
    context.strokeStyle = color;
    context.lineWidth = item.score_kind === "candidate" ? 2.3 : 1.7;
    context.setLineDash(item.score_kind === "candidate" ? [] : [7, 5]);
    context.globalAlpha = item.score_kind === "candidate" ? 0.95 : 0.75;
    context.stroke();
  }
  context.setLineDash([]);
  context.globalAlpha = 1;
}

function renderAll() {
  const series = aggregateSeries();
  renderSummary(series);
  renderPlot(series);
  const candidatePoints = series
    .filter((item) => item.score_kind === "candidate")
    .reduce((total, item) => total + item.point_count, 0);
  const controls = series
    .filter((item) => item.score_kind === "conditioned-control")
    .reduce((total, item) => total + item.point_count, 0);
  setAppState(
    series.length ? "ready" : "empty",
    series.length
      ? `${candidatePoints.toLocaleString()} candidate points and ${controls.toLocaleString()} target-conditioned rolled-template points in ${series.length} visible series.`
      : "No score series match the selected strata.",
  );
}

async function refresh() {
  const bounds = selectedBounds();
  byId("density-window-label").textContent = `UTC [${formatUtcNs(bounds.start)}, ${formatUtcNs(bounds.stop)})`;
  setAppState("loading", "Loading exact scan-section distributions…");
  try {
    const payload = await fetchJson(`/api/v8/score-distributions?start_utc_ns=${bounds.start}&stop_utc_ns=${bounds.stop}`);
    if (payload.point_identity !== "recording+segment+radio+receiver-chain+edge+method") {
      throw new Error("Dashboard returned an unsupported score-point identity");
    }
    strata = payload.distributions || [];
    methodOrder = [...new Set(strata.map((item) => item.method))].sort();
    const available = new Set(methodOrder);
    visibleMethods = new Set([...visibleMethods].filter((method) => available.has(method)));
    if (visibleMethods.size === 0) visibleMethods = new Set(available);
    populateSelect("density-radio", strata.map((item) => item.radio_id), "All radios");
    populateSelect("density-receiver", strata.map((item) => item.receiver_chain_id), "All RX chains");
    renderSelector();
    renderAll();
  } catch (error) {
    strata = []; methodOrder = [];
    renderSelector(); renderSummary([]); renderPlot([]);
    setAppState("error", error instanceof Error ? error.message : "Score distributions unavailable");
  }
}

byId("density-window-form").addEventListener("submit", (event) => { event.preventDefault(); refresh(); });
for (const id of ["density-radio", "density-receiver", "density-edge", "show-candidate", "show-control"]) {
  byId(id).addEventListener("change", renderAll);
}
window.addEventListener("resize", () => renderPlot(aggregateSeries()));
refresh();

"use strict";

const byId = (id) => document.getElementById(id);
let aggregatePayload = null;
const enabled = new Map();
const initialized = new Set();
const COLORS = ["#79d8a6", "#f5b95f", "#7bb8ff", "#e889c7", "#c6de65", "#ff897d", "#a996ff", "#71d6d2"];

function isoInput(date) { return date.toISOString().slice(0, 19); }
function utcNs(value) { return (BigInt(new Date(`${value}Z`).getTime()) * 1000000n).toString(); }
function safe(value) { return String(value ?? ""); }
function sourceKey(item) { return `${item.radio_id} / ${item.receiver_chain_id}`; }
function seriesKey(item) { return `${sourceKey(item)} / ${item.method} / ${item.model} / ${item.association_state}`; }
function color(key) { let hash = 0; for (const char of key) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0; return COLORS[hash % COLORS.length]; }
function values(category, payload) {
  if (category === "source") return payload.series.map(sourceKey);
  if (category === "radio") return payload.series.map((x) => x.radio_id);
  if (category === "receiver") return payload.series.map((x) => x.receiver_chain_id);
  if (category === "method") return payload.series.map((x) => x.method);
  if (category === "model") return payload.series.map((x) => x.model);
  if (category === "association") return payload.series.map((x) => x.association_state);
  if (category === "control") return payload.controls.map((x) => x.control_class);
  return [];
}
function ensureSelections(payload) {
  for (const category of ["source", "radio", "receiver", "method", "model", "association", "control"]) {
    const options = [...new Set(values(category, payload))].sort();
    if (!initialized.has(category)) {
      enabled.set(category, new Set(options)); initialized.add(category);
    } else {
      const selected = enabled.get(category);
      for (const option of options) if (!selected.has(option) && !selected.has(`!${option}`)) selected.add(option);
    }
  }
}
function selected(category, value) { return enabled.get(category)?.has(value) ?? true; }
function visibleSeries(item) {
  return selected("source", sourceKey(item)) && selected("radio", item.radio_id) &&
    selected("receiver", item.receiver_chain_id) && selected("method", item.method) &&
    selected("model", item.model) && selected("association", item.association_state);
}
function makeFilters(payload) {
  ensureSelections(payload);
  const host = byId("source-filters"); host.replaceChildren();
  const labels = {source: "Radio / receiver series", radio: "Radios", receiver: "Receiver ports", method: "Methods", model: "Models", association: "Association", control: "Evidence / controls"};
  for (const category of Object.keys(labels)) {
    const card = document.createElement("fieldset");
    const legend = document.createElement("legend"); legend.textContent = labels[category]; card.append(legend);
    for (const option of [...new Set(values(category, payload))].sort()) {
      const label = document.createElement("label"); label.className = "overlay-toggle";
      const input = document.createElement("input"); input.type = "checkbox"; input.checked = selected(category, option);
      input.dataset.category = category; input.dataset.option = option;
      input.addEventListener("change", () => {
        const selections = enabled.get(category);
        if (input.checked) { selections.add(option); selections.delete(`!${option}`); }
        else { selections.delete(option); selections.add(`!${option}`); }
        render();
      });
      label.append(input, document.createTextNode(` ${option}`)); card.append(label);
    }
    host.append(card);
  }
  byId("filter-panel").hidden = false;
}
function setupCanvas(id) {
  const canvas = byId(id), ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.fillStyle = "#09110f"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  return [canvas, ctx];
}
function axes(ctx, width, height, xLabel, yLabel) {
  ctx.strokeStyle = "#5d7169"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(70, 25); ctx.lineTo(70, height - 55); ctx.lineTo(width - 20, height - 55); ctx.stroke();
  ctx.fillStyle = "#b8c7c0"; ctx.font = "14px sans-serif"; ctx.fillText(xLabel, width / 2 - 60, height - 18);
  ctx.save(); ctx.translate(20, height / 2 + 55); ctx.rotate(-Math.PI / 2); ctx.fillText(yLabel, 0, 0); ctx.restore();
}
function normalizedDensity(values, bins, min, max) {
  const binWidth = (max - min) / bins;
  const counts = Array.from({length: bins}, () => 0);
  for (const value of values) counts[Math.min(bins - 1, Math.max(0, Math.floor((value - min) / binWidth)))]++;
  return {binWidth, densities: counts.map((count) => count / (values.length * binWidth))};
}
function drawHistogram(id, groups, valueOf, xLabel) {
  const [canvas, ctx] = setupCanvas(id); axes(ctx, canvas.width, canvas.height, xLabel, "Probability density");
  const values = groups.flatMap((group) => group.items.map(valueOf));
  if (!values.length) { ctx.fillStyle = "#b8c7c0"; ctx.fillText("No visible evidence", 90, 70); return; }
  const bins = 24;
  const rawMin = Math.min(...values), rawMax = Math.max(...values);
  const padding = rawMin === rawMax ? Math.max(Math.abs(rawMin) * 0.05, 1) : 0;
  const min = rawMin - padding, max = rawMax + padding;
  const groupedDensities = groups.map((group) => {
    const density = normalizedDensity(group.items.map(valueOf), bins, min, max);
    return {...group, densities: density.densities};
  });
  const binWidth = (max - min) / bins;
  const peak = Math.max(...groupedDensities.flatMap((group) => group.densities)); const plotWidth = canvas.width - 90;
  groupedDensities.forEach((group, groupIndex) => group.densities.forEach((density, index) => {
    ctx.fillStyle = group.color; ctx.globalAlpha = 0.28 + Math.min(groupIndex, 3) * 0.12;
    const barWidth = plotWidth / bins; const barHeight = density / peak * (canvas.height - 90);
    ctx.fillRect(70 + index * barWidth, canvas.height - 55 - barHeight, Math.max(1, barWidth - 1), barHeight);
  })); ctx.globalAlpha = 1; ctx.fillStyle = "#b8c7c0"; ctx.fillText(`${min.toFixed(2)} to ${max.toFixed(2)} · bin ${binWidth.toFixed(2)}`, 80, 45);
}
function replaceFacts(element, pairs) { element.replaceChildren(); for (const [term, value] of pairs) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = term; dd.textContent = value; element.append(dt, dd); } }
function render() {
  if (!aggregatePayload) return;
  const series = aggregatePayload.series.filter(visibleSeries);
  const bySource = [...new Set(series.map(sourceKey))].map((key) => ({key, color: color(key), items: series.filter((x) => sourceKey(x) === key)}));
  drawHistogram("drift-canvas", bySource, (x) => x.drift_rate_hz_s, "Drift rate (Hz/s)");
  const controls = aggregatePayload.controls.filter((item) => selected("radio", item.radio_id) && selected("receiver", item.receiver_chain_id) && selected("source", sourceKey(item)) && selected("control", item.control_class));
  const controlGroups = [...new Set(controls.map((x) => x.control_class))].map((key) => ({key, color: color(key), items: controls.filter((x) => x.control_class === key)}));
  drawHistogram("control-canvas", controlGroups, (x) => x.score, "Evidence score");
  const legend = byId("series-legend"); legend.replaceChildren(); bySource.forEach((group) => { const p = document.createElement("p"); p.textContent = `${group.key}: ${group.items.length} candidate series`; p.style.borderLeft = `0.7rem solid ${group.color}`; p.style.paddingLeft = "0.5rem"; legend.append(p); });
  const summary = byId("drift-summary"); summary.replaceChildren(); aggregatePayload.summaries.filter((x) => series.some((s) => s.radio_id === x.radio_id && s.receiver_chain_id === x.receiver_chain_id && s.method === x.method && s.model === x.model && s.association_state === x.association_state)).forEach((item) => { const p = document.createElement("p"); p.textContent = `${item.radio_id} / ${item.receiver_chain_id} · ${item.method}/${item.model}: p10 ${item.p10_drift_rate_hz_s.toFixed(1)}, median ${item.median_drift_rate_hz_s.toFixed(1)}, p90 ${item.p90_drift_rate_hz_s.toFixed(1)} Hz/s (n=${item.series_count})`; summary.append(p); });
  const provenance = byId("provenance-list"); provenance.replaceChildren(); series.forEach((item) => { const p = document.createElement("p"); p.textContent = `${seriesKey(item)} · ${item.recording_id} · ${item.waterfall_product_id} · ${item.doppler_id} · path ${item.candidate_or_path_id} · input ${item.input_identity_digest} · config ${item.config_digest} · basic ${item.basic_bundle_digest} · advanced ${item.advanced_bundle_digest}`; provenance.append(p); });
}
async function load() {
  byId("doppler-aggregate-state").dataset.state = "loading"; byId("doppler-aggregate-state").textContent = "Loading aggregate Doppler evidence…";
  try {
    const query = new URLSearchParams({start_utc_ns: utcNs(byId("start-utc").value), stop_utc_ns: utcNs(byId("stop-utc").value)});
    const response = await fetch(`/api/doppler-aggregate?${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.warnings?.includes("candidate-only-evidence-not-satellite-detection") || !payload.warnings?.includes("radio-and-receiver-series-are-never-pooled")) throw new Error("unsafe aggregate semantics");
    aggregatePayload = payload; makeFilters(payload); render();
    replaceFacts(byId("coverage-facts"), [["Recordings in interval", safe(payload.recording_count)], ["With Doppler products", safe(payload.available_recording_count)], ["Loaded tiles", safe(payload.tile_count)], ["Candidate series", safe(payload.series.length)], ["Truncated", payload.truncated ? "yes" : "no"]]);
    const state = byId("doppler-aggregate-state"); state.dataset.state = payload.series.length ? "complete" : "pending"; state.textContent = payload.series.length ? `Loaded ${payload.series.length} immutable candidate series.` : "No completed Doppler candidate products are available in this interval yet.";
  } catch (error) { const state = byId("doppler-aggregate-state"); state.dataset.state = "error"; state.textContent = `Aggregate Doppler evidence unavailable: ${error instanceof Error ? error.message : "unknown error"}`; }
}
const stop = new Date(), start = new Date(stop.getTime() - 6 * 60 * 60 * 1000); byId("start-utc").value = isoInput(start); byId("stop-utc").value = isoInput(stop);
byId("load-doppler").addEventListener("click", load); load();

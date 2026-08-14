"use strict";

const HOUR_NS = 3_600_000_000_000n;
const STALE_AFTER_MS = 120_000;
let currentBounds = null;
let lastSuccessfulRefresh = null;
let detailGeneration = 0;

const byId = (id) => document.getElementById(id);

function setState(id, state, message) {
  const node = byId(id);
  node.dataset.state = state;
  node.textContent = message;
}

function appendText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = String(text);
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function replaceFacts(target, facts) {
  target.replaceChildren();
  for (const [label, value] of facts) {
    appendText(target, "dt", label);
    appendText(target, "dd", value);
  }
}

function formatUtcNs(value) {
  const milliseconds = Number(value) / 1_000_000;
  if (!Number.isFinite(milliseconds)) return "Unavailable";
  return new Date(milliseconds).toISOString().replace(".000Z", "Z");
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "Unavailable";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let scaled = value;
  let index = 0;
  while (scaled >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  return `${scaled.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function safeError(error) {
  return error instanceof Error ? error.message : "Request failed";
}

async function fetchJson(path) {
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  let body;
  try {
    body = await response.json();
  } catch (_error) {
    throw new Error(`Dashboard returned an invalid response (${response.status})`);
  }
  if (!response.ok) {
    const message = body?.error?.message || `Dashboard request failed (${response.status})`;
    throw new Error(message);
  }
  return body;
}

function selectedBounds() {
  const hours = BigInt(byId("window-hours").value);
  const stop = BigInt(Date.now()) * 1_000_000n;
  return { start: stop - hours * HOUR_NS, stop };
}

function timeQuery(bounds) {
  return `start_utc_ns=${bounds.start.toString()}&stop_utc_ns=${bounds.stop.toString()}`;
}

function describeBounds(bounds) {
  return `UTC [${formatUtcNs(bounds.start)}, ${formatUtcNs(bounds.stop)}) — stop is exclusive`;
}

function badgeTone(state) {
  if (state === "complete") return "ok";
  if (state === "failed") return "error";
  return "warning";
}

async function loadActivity(bounds) {
  setState("activity-state", "loading", "Loading radio activity…");
  const body = byId("activity-body");
  body.replaceChildren();
  try {
    const payload = await fetchJson(`/api/activity?${timeQuery(bounds)}`);
    const radios = new Map();
    for (const item of payload.counts || []) {
      const counts = radios.get(item.radio_id) || { scan: 0, dwell: 0 };
      if (item.kind === "scan" || item.kind === "dwell") counts[item.kind] = item.count;
      radios.set(item.radio_id, counts);
    }
    if (radios.size === 0) {
      setState("activity-state", "empty", "No scans or dwells were cataloged in this window.");
      return;
    }
    for (const [radio, counts] of [...radios.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const row = document.createElement("tr");
      appendText(row, "th", radio).scope = "row";
      appendText(row, "td", counts.scan);
      appendText(row, "td", counts.dwell);
      appendText(row, "td", counts.scan + counts.dwell);
      body.append(row);
    }
    setState("activity-state", "ready", `${radios.size} radio${radios.size === 1 ? "" : "s"} represented.`);
  } catch (error) {
    setState("activity-state", "error", `Activity unavailable: ${safeError(error)}`);
    throw error;
  }
}

async function loadRecordings(bounds) {
  setState("recordings-state", "loading", "Loading recordings…");
  const body = byId("recordings-body");
  body.replaceChildren();
  try {
    const payload = await fetchJson(`/api/recordings?${timeQuery(bounds)}`);
    const items = payload.items || [];
    if (items.length === 0) {
      setState("recordings-state", "empty", "No recordings were cataloged in this window.");
      return;
    }
    for (const item of items) {
      const row = document.createElement("tr");
      const identityCell = document.createElement("th");
      identityCell.scope = "row";
      const button = appendText(identityCell, "button", item.recording_id, "recording-link");
      button.type = "button";
      button.addEventListener("click", () => loadRecordingDetail(item.recording_id));
      row.append(identityCell);
      appendText(row, "td", item.radio_id);
      appendText(row, "td", formatUtcNs(item.started_utc_ns));
      appendText(row, "td", (item.activity_kinds || []).join(", ") || "None recorded");
      const stateCell = document.createElement("td");
      const badge = appendText(stateCell, "span", item.analysis_state, "status-badge");
      badge.dataset.tone = badgeTone(item.analysis_state);
      row.append(stateCell);
      body.append(row);
    }
    const suffix = payload.next_cursor ? " Showing the first stable page." : "";
    setState("recordings-state", "ready", `${items.length} recording${items.length === 1 ? "" : "s"}.${suffix}`);
  } catch (error) {
    setState("recordings-state", "error", `Recordings unavailable: ${safeError(error)}`);
    throw error;
  }
}

async function loadRecordingDetail(recordingId) {
  const generation = ++detailGeneration;
  const card = byId("recording-detail");
  card.dataset.state = "loading";
  byId("recording-detail-heading").textContent = recordingId;
  setState("recording-detail-state", "loading", "Loading immutable recording detail…");
  byId("recording-detail-facts").replaceChildren();
  byId("features-region").hidden = true;
  try {
    const detail = await fetchJson(`/api/recordings/${encodeURIComponent(recordingId)}`);
    if (generation !== detailGeneration) return;
    const summary = detail.summary;
    replaceFacts(byId("recording-detail-facts"), [
      ["Radio", summary.radio_id],
      ["UTC interval", `[${formatUtcNs(summary.started_utc_ns)}, ${formatUtcNs(summary.finished_utc_ns)})`],
      ["Activities", (summary.activity_kinds || []).join(", ") || "None recorded"],
      ["Analysis state", summary.analysis_state],
      ["Segments", detail.segment_count],
      ["Recording object", detail.recording_object_available ? "Available" : "Missing / unavailable"],
    ]);
    card.dataset.state = detail.recording_object_available ? "ready" : "missing";
    setState(
      "recording-detail-state",
      detail.recording_object_available ? "ready" : "missing",
      detail.recording_object_available
        ? "Recording object is cataloged as available."
        : "Recording metadata remains visible, but the recording object is missing or unavailable.",
    );
    byId("features-region").hidden = false;
    await loadFeatures(recordingId, generation);
  } catch (error) {
    if (generation !== detailGeneration) return;
    card.dataset.state = "error";
    setState("recording-detail-state", "error", `Detail unavailable: ${safeError(error)}`);
  }
}

async function loadFeatures(recordingId, generation) {
  const list = byId("features-list");
  list.replaceChildren();
  setState("features-state", "loading", "Loading independent features…");
  try {
    const payload = await fetchJson(`/api/recordings/${encodeURIComponent(recordingId)}/features?selector=*`);
    if (generation !== detailGeneration) return;
    const items = payload.items || [];
    if (items.length === 0) {
      setState("features-state", "empty", "No independent features are available for this recording.");
      return;
    }
    for (const item of items) {
      const row = document.createElement("li");
      appendText(row, "strong", item.method_id);
      appendText(row, "span", `${item.feature_id} · ${item.score_semantics}: ${item.score}`);
      list.append(row);
    }
    const suffix = payload.next_cursor ? " First stable page shown." : "";
    setState("features-state", "ready", `${items.length} feature result${items.length === 1 ? "" : "s"}.${suffix}`);
  } catch (error) {
    if (generation !== detailGeneration) return;
    setState("features-state", "error", `Features unavailable: ${safeError(error)}`);
  }
}

async function loadTracks(bounds) {
  setState("tracks-state", "loading", "Loading tracks…");
  const list = byId("tracks-list");
  list.replaceChildren();
  try {
    const payload = await fetchJson(`/api/tracks?${timeQuery(bounds)}`);
    const items = payload.items || [];
    if (items.length === 0) {
      setState("tracks-state", "empty", "No tracks are available in this window.");
      return;
    }
    for (const item of items) {
      const row = document.createElement("li");
      appendText(row, "strong", item.track_id);
      appendText(row, "span", `[${formatUtcNs(item.started_utc_ns)}, ${formatUtcNs(item.finished_utc_ns)})`);
      const button = appendText(row, "button", item.model_snapshot_id, "model-link");
      button.type = "button";
      button.setAttribute("aria-label", `Load model ${item.model_snapshot_id}`);
      button.addEventListener("click", () => {
        byId("model-id").value = item.model_snapshot_id;
        loadModel(item.model_snapshot_id);
      });
      list.append(row);
    }
    const suffix = payload.next_cursor ? " First stable page shown." : "";
    setState("tracks-state", "ready", `${items.length} track${items.length === 1 ? "" : "s"}.${suffix}`);
  } catch (error) {
    setState("tracks-state", "error", `Tracks unavailable: ${safeError(error)}`);
    throw error;
  }
}

async function loadStorage() {
  setState("storage-state", "loading", "Loading storage projection…");
  const meter = byId("storage-meter");
  meter.hidden = true;
  try {
    const payload = await fetchJson("/api/storage-health");
    if (!payload.available || payload.total_bytes === null || payload.free_bytes === null) {
      setState("storage-state", "missing", "Storage health is unavailable; no capacity is inferred.");
      return;
    }
    const total = payload.total_bytes;
    const free = payload.free_bytes;
    const usedPercent = total > 0 ? Math.max(0, Math.min(100, ((total - free) / total) * 100)) : 0;
    byId("storage-free-label").textContent = `${formatBytes(free)} free`;
    byId("storage-total-label").textContent = `${formatBytes(total)} total`;
    byId("storage-progress").value = usedPercent;
    byId("storage-progress").textContent = `${usedPercent.toFixed(1)}% used`;
    meter.hidden = false;
    setState("storage-state", "ready", "Storage projection is available.");
  } catch (error) {
    setState("storage-state", "error", `Storage health unavailable: ${safeError(error)}`);
    throw error;
  }
}

async function loadEvaluation(identity) {
  setState("evaluation-state", "loading", "Loading immutable evaluation summary…");
  byId("evaluation-result").hidden = true;
  try {
    const payload = await fetchJson(`/api/evaluations/${encodeURIComponent(identity)}`);
    const facts = byId("evaluation-facts");
    facts.replaceChildren();
    for (const [label, value] of [
      ["Evaluation", payload.evaluation_id],
      ["Methods", payload.method_count],
      ["Union windows", payload.union_window_count],
    ]) {
      const metric = document.createElement("div");
      metric.className = "metric";
      appendText(metric, "span", label);
      appendText(metric, "strong", value);
      facts.append(metric);
    }
    const body = byId("evaluation-body");
    body.replaceChildren();
    for (const item of payload.methods || []) {
      const row = document.createElement("tr");
      appendText(row, "th", item.method_id).scope = "row";
      appendText(row, "td", item.split);
      appendText(row, "td", item.firing_count);
      appendText(row, "td", item.confusion.true_positive);
      appendText(row, "td", item.confusion.false_positive);
      appendText(row, "td", item.confusion.true_negative);
      appendText(row, "td", item.confusion.false_negative);
      appendText(row, "td", `${item.coverage.present_window_count}/${item.coverage.union_window_count}`);
      body.append(row);
    }
    byId("evaluation-result").hidden = false;
    const warnings = payload.warnings || [];
    setState(
      "evaluation-state",
      warnings.length ? "partial" : "ready",
      warnings.length ? `Loaded with ${warnings.length} recorded warning${warnings.length === 1 ? "" : "s"}: ${warnings.join("; ")}` : "Evaluation summary loaded.",
    );
  } catch (error) {
    setState("evaluation-state", "error", `Evaluation unavailable: ${safeError(error)}`);
  }
}

async function loadModel(identity) {
  setState("model-state", "loading", "Loading immutable model snapshot…");
  byId("model-facts").replaceChildren();
  try {
    const payload = await fetchJson(`/api/models/${encodeURIComponent(identity)}`);
    replaceFacts(byId("model-facts"), [
      ["Snapshot", payload.model_snapshot_id],
      ["Release alias", payload.release_alias || "Not released"],
      ["Parameter count", payload.parameter_count],
      ["Warnings", (payload.warnings || []).join("; ") || "None recorded"],
    ]);
    setState("model-state", (payload.warnings || []).length ? "partial" : "ready", "Model snapshot loaded.");
  } catch (error) {
    setState("model-state", "error", `Model unavailable: ${safeError(error)}`);
  }
}

async function refreshDashboard() {
  currentBounds = selectedBounds();
  byId("window-label").textContent = describeBounds(currentBounds);
  const appStatus = byId("app-status");
  appStatus.dataset.state = "loading";
  byId("app-status-text").textContent = "Refreshing catalog views…";
  const results = await Promise.allSettled([
    loadActivity(currentBounds),
    loadRecordings(currentBounds),
    loadTracks(currentBounds),
    loadStorage(),
  ]);
  const successes = results.filter((result) => result.status === "fulfilled").length;
  if (successes > 0) {
    lastSuccessfulRefresh = new Date();
    byId("last-refresh").dateTime = lastSuccessfulRefresh.toISOString();
    byId("last-refresh").textContent = lastSuccessfulRefresh.toISOString();
  }
  if (successes === results.length) {
    appStatus.dataset.state = "ready";
    byId("app-status-text").textContent = "Current catalog views loaded";
  } else if (successes > 0) {
    appStatus.dataset.state = "partial";
    byId("app-status-text").textContent = "Partial data — one or more views failed";
  } else {
    appStatus.dataset.state = "error";
    byId("app-status-text").textContent = "Dashboard data unavailable";
  }
}

byId("window-form").addEventListener("submit", (event) => {
  event.preventDefault();
  refreshDashboard();
});
byId("evaluation-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadEvaluation(byId("evaluation-id").value.trim());
});
byId("model-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadModel(byId("model-id").value.trim());
});

setInterval(() => {
  if (!lastSuccessfulRefresh) return;
  if (Date.now() - lastSuccessfulRefresh.getTime() <= STALE_AFTER_MS) return;
  const status = byId("app-status");
  if (["ready", "partial"].includes(status.dataset.state)) {
    status.dataset.state = "stale";
    byId("app-status-text").textContent = "Browser view is stale — refresh required";
  }
}, 30_000);

refreshDashboard();

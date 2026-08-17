"use strict";

const HOUR_NS = 3_600_000_000_000n;
const STALE_AFTER_MS = 120_000;
// UI-only V1 display aliases. Stable radio IDs remain the durable identity.
const RADIO_DISPLAY_ALIASES_V1 = Object.freeze({
  radio_pluto_5d4d: Object.freeze({ short: ".20", address: "192.168.1.20" }),
  radio_pluto_19f2: Object.freeze({ short: ".21", address: "192.168.1.21" }),
});
let currentBounds = null;
let lastSuccessfulRefresh = null;
let detailGeneration = 0;
let captureBatchCursor = null;
let loadedCaptureBatches = [];
let currentCaptureBounds = null;
let captureBatchGeneration = 0;

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
    const error = new Error(message);
    error.dashboardStatus = response.status;
    throw error;
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
  if (["complete", "succeeded", "eligible"].includes(state)) return "ok";
  if (["failed", "ineligible"].includes(state)) return "error";
  return "warning";
}

function formatNanoseconds(value) {
  return value === null || value === undefined ? "Not available" : `${value} ns`;
}

function captureModeText(item) {
  if (item.mode === "independent") {
    return "Independent — no synchronization claim";
  }
  return "Coordinated — measured software coordination; not hardware synchronization";
}

function eligibilityText(value) {
  if (value === "eligible") return "Paired analysis eligible";
  if (value === "ineligible") return "Paired analysis ineligible";
  return "Paired analysis pending";
}

function captureRadioFilter() {
  return byId("capture-radio-filter").value.trim().toLowerCase();
}

function radioDisplayName(radioId) {
  const stableId = String(radioId);
  const alias = RADIO_DISPLAY_ALIASES_V1[stableId];
  return alias ? `${alias.short} · ${stableId}` : stableId;
}

function radioMatchesFilter(radioId, radioFilter) {
  if (radioFilter === "") return true;
  const stableId = String(radioId).toLowerCase();
  const alias = RADIO_DISPLAY_ALIASES_V1[stableId];
  return [stableId, alias?.short, alias?.address]
    .filter((value) => value !== undefined)
    .some((value) => value.includes(radioFilter));
}

function captureRows() {
  const radioFilter = captureRadioFilter();
  return loadedCaptureBatches.flatMap((batch) =>
    (batch.attempts || [])
      .filter((attempt) => radioMatchesFilter(attempt.radio_id, radioFilter))
      .map((attempt) => ({ batch, attempt })),
  );
}

function refreshCaptureRadioOptions() {
  const options = byId("capture-radio-options");
  const radios = new Set(
    loadedCaptureBatches.flatMap((batch) =>
      (batch.attempts || []).map((attempt) => String(attempt.radio_id)),
    ),
  );
  options.replaceChildren();
  for (const radio of [...radios].sort((left, right) => left.localeCompare(right))) {
    const option = document.createElement("option");
    const alias = RADIO_DISPLAY_ALIASES_V1[radio];
    option.value = alias?.short || radio;
    option.label = alias ? `${alias.address} · ${radio}` : radio;
    options.append(option);
  }
}

function appendBatchContext(cell, batch, attempt) {
  const disclosure = document.createElement("details");
  disclosure.className = "batch-context";
  const summary = appendText(disclosure, "summary", batch.batch_id);
  summary.setAttribute("aria-label", `Show context for batch ${batch.batch_id}`);
  appendText(disclosure, "p", captureModeText(batch));
  appendText(disclosure, "p", eligibilityText(batch.paired_analysis_eligibility));
  appendText(
    disclosure,
    "p",
    `Attempt ${attempt.attempt_id}; plan ${attempt.plan_id}; requested ${formatUtcNs(attempt.requested_start_utc_ns)}.`,
  );
  appendText(
    disclosure,
    "p",
    `Requested skew ${formatNanoseconds(batch.requested_start_skew_ns)}; observed skew ${formatNanoseconds(batch.observed_start_skew_ns)}; limit ${formatNanoseconds(batch.maximum_observed_start_skew_ns)}.`,
  );
  const lifecycle = document.createElement("details");
  lifecycle.className = "radio-lifecycle-context";
  const lifecycleSummary = appendText(lifecycle, "summary", "Radio lifecycle");
  lifecycleSummary.setAttribute(
    "aria-label",
    `Show radio lifecycle evidence for ${attempt.attempt_id}`,
  );
  const state = appendText(lifecycle, "p", "Open to load bounded lifecycle evidence.");
  lifecycle.addEventListener("toggle", async () => {
    if (!lifecycle.open || lifecycle.dataset.loaded === "true") return;
    state.textContent = "Loading radio lifecycle evidence…";
    try {
      const payload = await fetchJson(
        `/api/v5/capture-attempts/${encodeURIComponent(attempt.attempt_id)}/radio-lifecycle`,
      );
      lifecycle.dataset.loaded = "true";
      state.textContent = payload.reason === null
        ? "No lifecycle change observed."
        : `${String(payload.reason).replaceAll("_", " ")} · ${payload.confidence} confidence.`;
      const facts = document.createElement("dl");
      facts.className = "facts compact-facts";
      replaceFacts(facts, [
        ["Preflight boot", payload.preflight_boot_id || "Unavailable"],
        ["Terminal boot", payload.terminal_boot_id || "Unavailable"],
        ["Evidence", (payload.evidence_codes || []).join(", ") || "No change"],
        ["Terminal observer", payload.observer_available_at_terminal ? "Available" : "Unavailable"],
      ]);
      lifecycle.append(facts);
    } catch (error) {
      state.textContent = error?.status === 404
        ? "Lifecycle evidence was not recorded for this capture."
        : `Lifecycle evidence unavailable: ${safeError(error)}`;
    }
  });
  disclosure.append(lifecycle);
  cell.append(disclosure);
}

function makeCaptureRowNavigable(row, detailLink, attempt) {
  row.classList.add("capture-row-link");
  row.tabIndex = 0;
  row.title = `View capture details for ${attempt.recording_id}`;
  const navigate = () => detailLink.click();
  row.addEventListener("click", (event) => {
    if (event.target.closest("a, button, input, select, textarea, summary")) return;
    navigate();
  });
  row.addEventListener("keydown", (event) => {
    if (event.target !== row || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    navigate();
  });
}

function appendBatchAttemptRow(body, batch, attempt) {
  const row = document.createElement("tr");
  row.dataset.batchId = batch.batch_id;
  row.dataset.attemptId = attempt.attempt_id;
  row.dataset.radioId = attempt.radio_id;

  const startedCell = document.createElement("th");
  startedCell.scope = "row";
  const observed = attempt.observed_start_utc_ns !== null;
  appendText(
    startedCell,
    "span",
    formatUtcNs(observed ? attempt.observed_start_utc_ns : attempt.requested_start_utc_ns),
  );
  appendText(startedCell, "span", observed ? "Observed first sample" : "Requested start", "cell-note");
  row.append(startedCell);
  const radioCell = appendText(row, "td", radioDisplayName(attempt.radio_id));
  radioCell.title = RADIO_DISPLAY_ALIASES_V1[attempt.radio_id]?.address || "";

  const captureCell = document.createElement("td");
  const captureBadge = appendText(captureCell, "span", attempt.capture_state, "status-badge");
  captureBadge.dataset.tone = badgeTone(attempt.capture_state);
  if (attempt.failure_reason) {
    appendText(captureCell, "span", `Failure: ${attempt.failure_reason}`, "failure-reason");
  }
  row.append(captureCell);

  const recordingCell = document.createElement("td");
  if (attempt.recording_id) {
    appendText(recordingCell, "span", attempt.recording_id, "recording-identity");
  } else {
    appendText(
      recordingCell,
      "span",
      attempt.capture_state === "failed" ? "None — capture failed" : "Not published",
    );
  }
  row.append(recordingCell);

  const analysisCell = document.createElement("td");
  if (attempt.analysis_state === "unavailable") {
    appendText(analysisCell, "span", "Not available");
  } else {
    const analysisBadge = appendText(
      analysisCell,
      "span",
      attempt.analysis_state,
      "status-badge",
    );
    analysisBadge.dataset.tone = badgeTone(attempt.analysis_state);
    appendText(
      analysisCell,
      "span",
      attempt.analysis_result_available ? "Results ready" : "No result yet",
      "cell-note",
    );
  }
  row.append(analysisCell);

  const detailsCell = document.createElement("td");
  if (attempt.recording_id) {
    const detailLink = appendText(detailsCell, "a", "View capture", "capture-detail-link");
    detailLink.href = `/recordings/${encodeURIComponent(attempt.recording_id)}`;
    detailLink.setAttribute(
      "aria-label",
      `View capture details, waterfall, and analysis for ${attempt.recording_id}`,
    );
    makeCaptureRowNavigable(row, detailLink, attempt);
  } else {
    appendText(detailsCell, "span", "No capture details", "cell-note");
  }
  row.append(detailsCell);

  const contextCell = document.createElement("td");
  appendBatchContext(contextCell, batch, attempt);
  row.append(contextCell);
  body.append(row);
}

function captureBatchProgress(visibleCount) {
  const attempts = loadedCaptureBatches.flatMap((item) => item.attempts || []);
  const radioFilter = byId("capture-radio-filter").value.trim();
  const filtered = radioFilter ? ` matching radio “${radioFilter}”` : "";
  const paging = captureBatchCursor
    ? "More batches are available."
    : "All matching batches are loaded.";
  return `${visibleCount} capture${visibleCount === 1 ? "" : "s"}${filtered}; ${attempts.length} capture${attempts.length === 1 ? "" : "s"} loaded from ${loadedCaptureBatches.length} batch${loadedCaptureBatches.length === 1 ? "" : "es"}. ${paging}`;
}

function renderCaptureRows() {
  const body = byId("capture-attempts-body");
  body.replaceChildren();
  const rows = captureRows();
  for (const { batch, attempt } of rows) appendBatchAttemptRow(body, batch, attempt);
  const loadMore = byId("capture-batches-more");
  loadMore.hidden = captureBatchCursor === null || captureRadioFilter() !== "";
  if (loadedCaptureBatches.length === 0) {
    setState("capture-batches-state", "empty", "No captures were requested in this time range.");
  } else if (rows.length === 0) {
    setState("capture-batches-state", "empty", "No captures match this radio filter in the selected time range.");
  } else {
    setState("capture-batches-state", "ready", captureBatchProgress(rows.length));
  }
}

async function loadCaptureBatches(bounds, cursor = null, drainForRadio = false) {
  const append = cursor !== null;
  const generation = append ? captureBatchGeneration : ++captureBatchGeneration;
  setState(
    "capture-batches-state",
    "loading",
    drainForRadio ? "Searching all stable pages for this radio…" : append ? "Loading more captures…" : "Loading captures…",
  );
  const loadMore = byId("capture-batches-more");
  loadMore.disabled = true;
  if (!append) {
    byId("capture-attempts-body").replaceChildren();
    captureBatchCursor = null;
    loadedCaptureBatches = [];
    currentCaptureBounds = bounds;
    byId("capture-window-label").textContent = describeBounds(bounds);
  }
  try {
    let nextCursor = cursor;
    do {
      const cursorQuery = nextCursor === null ? "" : `&cursor=${encodeURIComponent(nextCursor)}`;
      const payload = await fetchJson(`/api/v2/capture-batches?${timeQuery(bounds)}${cursorQuery}`);
      if (generation !== captureBatchGeneration) return;
      loadedCaptureBatches.push(...(payload.items || []));
      captureBatchCursor = payload.next_cursor || null;
      refreshCaptureRadioOptions();
      renderCaptureRows();
      nextCursor = captureBatchCursor;
    } while (drainForRadio && nextCursor !== null);
    loadMore.disabled = false;
    renderCaptureRows();
  } catch (error) {
    if (generation !== captureBatchGeneration) return;
    loadMore.disabled = false;
    if (error?.dashboardStatus === 404) {
      loadMore.hidden = true;
      setState("capture-batches-state", "missing", "Capture-batch projection is not enabled on this dashboard service.");
      return;
    }
    setState("capture-batches-state", "error", `Capture batches unavailable: ${safeError(error)}`);
    throw error;
  }
}

function selectedCaptureBounds() {
  const hours = BigInt(byId("capture-window-hours").value);
  const stop = BigInt(Date.now()) * 1_000_000n;
  return { start: stop - hours * HOUR_NS, stop };
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
      const detailLink = appendText(identityCell, "a", "Full capture", "full-capture-link");
      detailLink.href = `/recordings/${encodeURIComponent(item.recording_id)}`;
      detailLink.setAttribute("aria-label", `Open full capture page for ${item.recording_id}`);
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
  byId("capture-window-hours").value = byId("window-hours").value;
  byId("window-label").textContent = describeBounds(currentBounds);
  const appStatus = byId("app-status");
  appStatus.dataset.state = "loading";
  byId("app-status-text").textContent = "Refreshing catalog views…";
  const results = await Promise.allSettled([
    loadActivity(currentBounds),
    loadCaptureBatches(currentBounds, null, captureRadioFilter() !== ""),
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
byId("capture-batches-more").addEventListener("click", () => {
  if (currentCaptureBounds === null || captureBatchCursor === null) return;
  loadCaptureBatches(currentCaptureBounds, captureBatchCursor).catch(() => {});
});
byId("capture-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  const bounds = selectedCaptureBounds();
  loadCaptureBatches(bounds, null, captureRadioFilter() !== "").catch(() => {});
});
byId("capture-filters-clear").addEventListener("click", () => {
  byId("capture-radio-filter").value = "";
  renderCaptureRows();
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

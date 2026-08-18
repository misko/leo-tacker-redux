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
let starlinkRecordingStates = new Map();
let captureDopplerSummaries = new Map();
let captureDopplerBulkState = "loading";
let captureQamSummaries = new Map();
let captureQamGeneration = 0;
const captureDurationCache = new Map();
const captureDurationQueued = new Set();
const captureDurationQueue = [];
let activeCaptureDurationLoads = 0;
const MAX_CAPTURE_DURATION_LOADS = 4;

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

function formatCompactUtcNs(value) {
  const milliseconds = Number(value) / 1_000_000;
  if (!Number.isFinite(milliseconds)) return "Unavailable";
  const iso = new Date(milliseconds).toISOString();
  return `${iso.slice(0, 4)}/${iso.slice(5, 7)}/${iso.slice(8, 10)}-${iso.slice(11, 19)}`;
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

function captureRadioFilter() {
  return byId("capture-radio-filter").value.trim().toLowerCase();
}

function captureStarlinkFilter() {
  return byId("capture-starlink-filter").value;
}

function radioDisplayName(radioId) {
  const stableId = String(radioId);
  const alias = RADIO_DISPLAY_ALIASES_V1[stableId];
  return alias ? `${alias.short} · ${stableId}` : stableId;
}

function compactRadioDisplayName(radioId) {
  const stableId = String(radioId);
  const alias = RADIO_DISPLAY_ALIASES_V1[stableId];
  const serialSuffix = stableId.slice(-4);
  return alias ? `${alias.short} / ${serialSuffix}` : stableId;
}

function updateCaptureDurationCells(recordingId, text, title = "") {
  for (const row of document.querySelectorAll("#capture-attempts-body tr")) {
    if (row.dataset.recordingId !== recordingId) continue;
    const cell = row.querySelector(".capture-duration");
    if (!cell) continue;
    cell.textContent = text;
    cell.title = title;
  }
}

function pumpCaptureDurationQueue() {
  while (activeCaptureDurationLoads < MAX_CAPTURE_DURATION_LOADS && captureDurationQueue.length) {
    const recordingId = captureDurationQueue.shift();
    activeCaptureDurationLoads += 1;
    fetchJson(`/api/v3/recordings/${encodeURIComponent(recordingId)}`)
      .then((detail) => {
        const durationNs = Number(detail.capture_finished_utc_ns) - Number(detail.capture_started_utc_ns);
        if (!Number.isFinite(durationNs) || durationNs <= 0) throw new Error("Invalid capture duration");
        const text = formatDurationNs(durationNs);
        captureDurationCache.set(recordingId, text);
        updateCaptureDurationCells(recordingId, text, "Observed capture start to capture finish");
      })
      .catch(() => updateCaptureDurationCells(recordingId, "—", "Capture duration unavailable"))
      .finally(() => {
        activeCaptureDurationLoads -= 1;
        captureDurationQueued.delete(recordingId);
        pumpCaptureDurationQueue();
      });
  }
}

function requestCaptureDuration(recordingId) {
  const cached = captureDurationCache.get(recordingId);
  if (cached !== undefined) {
    updateCaptureDurationCells(recordingId, cached, "Observed capture start to capture finish");
    return;
  }
  if (captureDurationQueued.has(recordingId)) return;
  captureDurationQueued.add(recordingId);
  captureDurationQueue.push(recordingId);
  pumpCaptureDurationQueue();
}

function appendStatusIcon(cell, state, kind) {
  const icon = document.createElement("span");
  icon.className = `table-status-icon ${kind}-status-icon`;
  icon.dataset.state = state;
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = kind === "capture"
    ? (state === "succeeded" ? "✓" : state === "failed" ? "✕" : "●")
    : "●";
  cell.append(icon);
  appendText(cell, "span", state.replaceAll("_", " "), "visually-hidden");
  cell.title = `${kind === "capture" ? "Capture" : "Analysis"}: ${state.replaceAll("_", " ")}`;
}

function formatDriftRate(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "Unavailable";
  return Math.abs(numeric) >= 1000
    ? `${(numeric / 1000).toFixed(2)} kHz/s`
    : `${numeric.toFixed(1)} Hz/s`;
}

function appendCaptureDopplerCell(row, attempt) {
  const cell = document.createElement("td");
  cell.className = "capture-doppler-summary";
  if (!attempt.recording_id) {
    cell.textContent = "—";
    cell.dataset.state = "unavailable";
  } else if (captureDopplerBulkState === "loading") {
    cell.textContent = "Loading…";
    cell.dataset.state = "pending";
  } else if (captureDopplerBulkState === "error") {
    cell.textContent = "Error";
    cell.dataset.state = "error";
    cell.title = "Bulk measured Doppler projection failed";
  } else {
    const summary = captureDopplerSummaries.get(attempt.recording_id);
    const state = summary?.state || "unavailable";
    cell.dataset.state = state;
    if (state === "complete") {
      for (const candidate of summary.candidates || []) {
        const line = appendText(
          cell,
          "span",
          `${candidate.lnb_id} / ${candidate.receiver_chain_id}: ${formatDriftRate(candidate.drift_rate_hz_s)}`,
          "capture-doppler-candidate",
        );
        line.title = `Candidate-only published total fit; ${candidate.candidate_id}; ${candidate.model}; ranking score ${candidate.ranking_score}; Doppler product ${candidate.doppler_id}; algorithm ${candidate.algorithm_version}`;
      }
      cell.title = "Highest public ranking-score candidate selected independently for each authoritative LNB / receiver; not a calibrated detection";
    } else {
      cell.textContent = state === "pending" ? "Pending" : state === "error" ? "Error" : "Unavailable";
      cell.title = (summary?.reason_codes || ["published-total-doppler-unavailable"]).join(", ");
    }
  }
  row.append(cell);
}

function appendCaptureQamCell(row, attempt) {
  const cell = document.createElement("td");
  cell.className = "capture-qam-summary";
  if (!attempt.recording_id) {
    cell.textContent = "—";
    cell.dataset.state = "unavailable";
  } else {
    const summary = captureQamSummaries.get(attempt.recording_id);
    const state = summary?.state || "loading";
    cell.dataset.state = state;
    if (state === "complete") {
      for (const candidate of summary.candidates || []) {
        const line = appendText(
          cell,
          "span",
          `${candidate.lnb_id} / ${candidate.receiver_chain_id}: ${Number(candidate.qam_goodness).toFixed(3)} · ${qamGoodnessBand(Number(candidate.qam_goodness))}`,
          "capture-qam-candidate",
        );
        line.title = `Candidate-only QAM goodness; accuracy ${(100 * Number(candidate.hard_symbol_accuracy)).toFixed(2)}%; RMS EVM ${Number(candidate.rms_evm).toFixed(3)}; ${candidate.window_count} windows; ${candidate.edge}; analysis ${candidate.analysis_id}`;
      }
      cell.title = "Highest QAM goodness selected independently per authoritative LNB / receiver; calibration required; not a Starlink detection";
    } else {
      cell.textContent = state === "loading" ? "Loading…" : state === "pending" ? "Pending" : state === "error" ? "Error" : "Unavailable";
      cell.title = (summary?.reason_codes || ["published-acquired-qam-unavailable"]).join(", ");
    }
    const detail = appendText(cell, "a", "QAM details", "full-capture-link qam-detail-link");
    detail.href = `/recordings/${encodeURIComponent(attempt.recording_id)}#evidence-qam`;
    detail.setAttribute("aria-label", `Open QAM analysis for ${attempt.recording_id}`);
  }
  row.append(cell);
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
  const starlinkFilter = captureStarlinkFilter();
  return loadedCaptureBatches.flatMap((batch) =>
    (batch.attempts || [])
      .filter((attempt) => radioMatchesFilter(attempt.radio_id, radioFilter))
      .filter((attempt) => {
        if (starlinkFilter === "all") return true;
        if (!attempt.recording_id) return false;
        if (starlinkFilter === "detected") return false;
        return starlinkRecordingStates.get(attempt.recording_id) === starlinkFilter;
      })
      .map((attempt) => ({ batch, attempt })),
  );
}

function formatDurationNs(value) {
  const seconds = Number(value) / 1_000_000_000;
  if (!Number.isFinite(seconds)) return "Unavailable";
  return seconds < 1 ? `${(seconds * 1000).toFixed(1)} ms` : `${seconds.toFixed(2)} s`;
}

function formatRate(value) {
  return value === null || value === undefined ? "Calibration required" : `${(100 * value).toFixed(2)}%`;
}

function qamGoodnessBand(value) {
  if (value >= 0.7) return "high";
  if (value >= 0.35) return "moderate";
  return "low";
}

function qamGoodness(accuracy, rmsEvm) {
  const measuredAccuracy = Number(accuracy);
  const measuredEvm = Number(rmsEvm);
  if (!Number.isFinite(measuredAccuracy) || !Number.isFinite(measuredEvm) || measuredEvm < 0) return null;
  const chanceCorrected = Math.max(0, Math.min(1, (measuredAccuracy - 0.25) / 0.75));
  const compactness = 1 / (1 + (measuredEvm / 2) * (measuredEvm / 2));
  return Math.sqrt(chanceCorrected * compactness);
}

async function loadRetroQamCanary() {
  setState("retro-qam-canary-state", "loading", "Loading latest historical QAM acceptance receipt…");
  const metrics = byId("retro-qam-canary-metrics");
  const body = byId("retro-qam-canary-body");
  const provenance = byId("retro-qam-canary-provenance");
  metrics.hidden = true; metrics.replaceChildren(); body.replaceChildren(); provenance.replaceChildren();
  try {
    const payload = await fetchJson("/api/v21/canaries/retro-qam/latest");
    if (payload.candidate_only !== true || payload.calibrated_detection !== null) throw new Error("unsafe historical canary semantics");
    const completed = Number(payload.completed_utc_ns) / 1_000_000;
    const ageMinutes = Math.max(0, (Date.now() - completed) / 60_000);
    metrics.hidden = false;
    for (const [label, value] of [
      ["Oracle match", payload.metrics_match_oracle ? "PASS" : "FAIL"],
      ["Combined QAM goodness", `${Number(payload.combined_qam_goodness).toFixed(3)} · ${qamGoodnessBand(Number(payload.combined_qam_goodness))}`],
      ["Combined accuracy", `${(100 * Number(payload.combined_hard_symbol_accuracy)).toFixed(2)}%`],
      ["Combined RMS EVM", Number(payload.combined_rms_evm).toFixed(3)],
      ["Last run", `${formatUtcNs(payload.completed_utc_ns)} · ${ageMinutes.toFixed(1)} min ago`],
      ["Cadence", `${Number(payload.schedule_interval_seconds) / 60} min`],
    ]) {
      const item = document.createElement("div"); appendText(item, "span", label, "metric-label"); appendText(item, "strong", value, "metric-value"); metrics.append(item);
    }
    for (const receiver of payload.receivers || []) {
      const row = document.createElement("tr");
      appendText(row, "td", `RX${receiver.receiver_index}`);
      appendText(row, "td", `${Number(receiver.qam_goodness).toFixed(3)} · ${qamGoodnessBand(Number(receiver.qam_goodness))}`);
      appendText(row, "td", `${(100 * Number(receiver.hard_symbol_accuracy)).toFixed(2)}%`);
      appendText(row, "td", Number(receiver.rms_evm).toFixed(3));
      appendText(row, "td", receiver.winning_epoch_sample);
      appendText(row, "td", `${Number(receiver.winning_cfo_hz).toFixed(2)} Hz`);
      appendText(row, "td", `${Number(receiver.held_out_verify_score).toFixed(4)} / ${Number(receiver.conditioned_control_score).toFixed(4)} (Δ ${Number(receiver.verify_minus_control_margin).toFixed(4)})`);
      body.append(row);
    }
    replaceFacts(provenance, [
      ["Corpus", payload.corpus_id],
      ["IQ SHA-256", payload.iq_object_digest?.value || "Unavailable"],
      ["Receipt SHA-256", payload.receipt_digest?.value || "Unavailable"],
      ["Producer commit", payload.git_commit],
      ["Scope", "Historical known-positive acceptance canary; not a live recording or calibrated detection"],
    ]);
    setState("retro-qam-canary-state", payload.metrics_match_oracle ? "ready" : "error", payload.metrics_match_oracle ? "Latest native Redux replay matches the frozen leo-tracker oracle." : "Latest replay does not match the frozen oracle; QAM/acquisition regression suspected.");
  } catch (error) {
    setState("retro-qam-canary-state", error?.dashboardStatus === 404 ? "missing" : "error", error?.dashboardStatus === 404 ? "No historical QAM canary receipt is available yet." : `Historical QAM canary unavailable: ${safeError(error)}`);
  }
}

async function loadObservationAggregate(bounds) {
  setState("observation-aggregate-state", "loading", "Loading RF duty and Starlink evidence…");
  let payload;
  try {
    payload = await fetchJson(`/api/v6/observation-aggregate?${timeQuery(bounds)}`);
  } catch (error) {
    if (error?.dashboardStatus === 404) {
      starlinkRecordingStates = new Map();
      byId("observation-metrics").hidden = true;
      byId("duty-cycle-body").replaceChildren();
      byId("starlink-rate-body").replaceChildren();
      setState(
        "observation-aggregate-state",
        "missing",
        "Aggregate view is unavailable from this older dashboard API.",
      );
      return;
    }
    throw error;
  }
  starlinkRecordingStates = new Map(
    (payload.recording_states || []).map((item) => [item.recording_id, item.state]),
  );
  const metrics = byId("observation-metrics");
  metrics.hidden = false;
  metrics.replaceChildren();
  for (const [label, value] of [
    ["Recordings", payload.recording_count],
    ["Starlink candidates", payload.candidate_recording_count],
    ["Clipped / not evaluated", payload.not_evaluated_recording_count],
    ["Analysis unavailable", payload.unavailable_recording_count],
  ]) {
    const item = document.createElement("div");
    appendText(item, "span", label, "metric-label");
    appendText(item, "strong", value, "metric-value");
    metrics.append(item);
  }
  const dutyBody = byId("duty-cycle-body");
  dutyBody.replaceChildren();
  for (const item of payload.duty_cycles || []) {
    const row = document.createElement("tr");
    appendText(row, "td", item.dimension.toUpperCase());
    appendText(row, "td", item.dimension === "radio" ? radioDisplayName(item.identity) : item.identity);
    appendText(row, "td", formatDurationNs(item.active_ns));
    appendText(row, "td", `${(100 * item.duty_cycle).toFixed(4)}%`);
    dutyBody.append(row);
  }
  const evidenceBody = byId("starlink-rate-body");
  evidenceBody.replaceChildren();
  for (const item of payload.starlink_evidence || []) {
    const row = document.createElement("tr");
    appendText(row, "td", item.dimension.toUpperCase());
    appendText(row, "td", item.identity);
    appendText(row, "td", item.comparison_count);
    appendText(row, "td", `${item.candidate_positive_count} (${formatRate(item.candidate_positive_rate)})`);
    appendText(row, "td", formatRate(item.calibrated_detection_rate), "calibration-required");
    evidenceBody.append(row);
  }
  setState(
    "observation-aggregate-state",
    "warning",
    "Detection rates and the beacon-only filter require whole-search calibration. Candidate-positive means score > conditioned control; it is not a beacon detection.",
  );
  renderCaptureBatches();
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

function makeCaptureRowNavigable(row, detailHref, attempt) {
  row.classList.add("capture-row-link");
  row.tabIndex = 0;
  row.title = `View capture details for ${attempt.recording_id}`;
  row.setAttribute(
    "aria-label",
    `View capture details, waterfall, and analysis for ${attempt.recording_id}`,
  );
  const navigate = () => window.location.assign(detailHref);
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
  if (attempt.recording_id) row.dataset.recordingId = attempt.recording_id;

  const startedCell = document.createElement("th");
  startedCell.scope = "row";
  const observed = attempt.observed_start_utc_ns !== null;
  appendText(
    startedCell,
    "span",
    formatCompactUtcNs(observed ? attempt.observed_start_utc_ns : attempt.requested_start_utc_ns),
  );
  row.append(startedCell);
  const radioCell = appendText(row, "td", compactRadioDisplayName(attempt.radio_id));
  radioCell.title = RADIO_DISPLAY_ALIASES_V1[attempt.radio_id]?.address || "";

  const captureCell = document.createElement("td");
  appendStatusIcon(captureCell, attempt.capture_state, "capture");
  if (attempt.failure_reason) captureCell.title += ` — ${attempt.failure_reason}`;
  row.append(captureCell);

  const analysisCell = document.createElement("td");
  appendStatusIcon(analysisCell, attempt.analysis_state, "analysis");
  row.append(analysisCell);

  appendCaptureDopplerCell(row, attempt);
  appendCaptureQamCell(row, attempt);

  const pilotCell = document.createElement("td");
  pilotCell.className = "pilot-detection-counts";
  pilotCell.textContent = "— / —";
  pilotCell.title = "Calibrated Anchor-8 and GLRT beacon detections are unavailable";
  row.append(pilotCell);

  const durationCell = document.createElement("td");
  durationCell.className = "capture-duration";
  durationCell.textContent = attempt.recording_id ? "Loading…" : "—";
  row.append(durationCell);

  const satellitesCell = document.createElement("td");
  satellitesCell.textContent = "—";
  satellitesCell.title = "Recording-to-satellite association is not available in the dashboard contract";
  row.append(satellitesCell);

  body.append(row);
  if (attempt.recording_id) {
    const detailHref = `/recordings/${encodeURIComponent(attempt.recording_id)}`;
    makeCaptureRowNavigable(row, detailHref, attempt);
    requestCaptureDuration(attempt.recording_id);
  }
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

async function loadCaptureDopplerSummaries(bounds) {
  captureDopplerBulkState = "loading";
  captureDopplerSummaries = new Map();
  renderCaptureRows();
  try {
    const payload = await fetchJson(`/api/v18/capture-doppler-summaries?${timeQuery(bounds)}&maximum_recordings=400`);
    if (
      payload.candidate_only !== true
      || payload.calibrated_detection_count !== null
      || !(payload.warnings || []).includes("radio-lnb-receiver-candidates-are-never-pooled")
    ) {
      throw new Error("Dashboard returned unsafe capture Doppler semantics");
    }
    captureDopplerSummaries = new Map(
      (payload.recordings || []).map((item) => [item.recording_id, item]),
    );
    captureDopplerBulkState = "ready";
  } catch (error) {
    captureDopplerSummaries = new Map();
    captureDopplerBulkState = error?.dashboardStatus === 404 ? "unavailable" : "error";
    if (captureDopplerBulkState === "error") throw error;
  } finally {
    renderCaptureRows();
  }
}

async function loadOneCaptureQamSummary(attempt, generation) {
  const recordingId = attempt.recording_id;
  if (!recordingId) return;
  try {
    const parameters = new URLSearchParams({mode: "windows", maximum_streams: "16", maximum_windows_per_stream: "32", maximum_points_per_constellation: "1"});
    const payload = await fetchJson(`/api/v17/recordings/${encodeURIComponent(recordingId)}/starlink-acquired-constellation?${parameters}`);
    if (generation !== captureQamGeneration) return;
    if (payload.candidate_only !== true || payload.calibration_required !== true) throw new Error("Dashboard returned unsafe capture QAM semantics");
    const candidates = [];
    for (const stream of payload.streams || []) {
      const windows = (stream.windows || []).map((window) => ({window, goodness: qamGoodness(Number(window.hard_symbol_accuracy), Number(window.rms_evm))})).filter((item) => item.goodness !== null);
      windows.sort((left, right) => right.goodness - left.goodness || Number(right.window.verify_minus_control_margin) - Number(left.window.verify_minus_control_margin) || Number(left.window.window_index) - Number(right.window.window_index));
      const selected = windows[0];
      if (!selected) continue;
      candidates.push({
        lnb_id: stream.lnb_id,
        receiver_chain_id: stream.receiver_chain_id,
        edge: stream.edge,
        qam_goodness: selected.goodness,
        hard_symbol_accuracy: Number(selected.window.hard_symbol_accuracy),
        rms_evm: Number(selected.window.rms_evm),
        window_count: stream.overall?.window_count ?? (stream.windows || []).length,
        analysis_id: payload.analysis_ref?.artifact_id || "acquired-qam-v0.3",
      });
    }
    captureQamSummaries.set(recordingId, {state: candidates.length ? "complete" : "unavailable", candidates, reason_codes: candidates.length ? [] : ["no-acquired-qam-streams"]});
  } catch (error) {
    if (generation !== captureQamGeneration) return;
    const state = error?.dashboardStatus === 404 && ["pending", "running"].includes(attempt.analysis_state) ? "pending" : error?.dashboardStatus === 404 ? "unavailable" : "error";
    captureQamSummaries.set(recordingId, {state, candidates: [], reason_codes: [state === "pending" ? "acquired-qam-analysis-pending" : state === "unavailable" ? "published-acquired-qam-unavailable" : safeError(error)]});
  }
  if (generation === captureQamGeneration) renderCaptureRows();
}

async function loadCaptureQamSummaries() {
  const generation = ++captureQamGeneration;
  captureQamSummaries = new Map();
  renderCaptureRows();
  const attempts = [...new Map(
    loadedCaptureBatches.flatMap((batch) => batch.attempts || []).filter((attempt) => attempt.recording_id).map((attempt) => [attempt.recording_id, attempt]),
  ).values()];
  let cursor = 0;
  async function worker() {
    while (generation === captureQamGeneration && cursor < attempts.length) {
      const attempt = attempts[cursor++];
      await loadOneCaptureQamSummary(attempt, generation);
    }
  }
  await Promise.all(Array.from({length: Math.min(4, attempts.length)}, () => worker()));
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
  const captureBatches = loadCaptureBatches(currentBounds, null, captureRadioFilter() !== "");
  const results = await Promise.allSettled([
    loadActivity(currentBounds),
    loadObservationAggregate(currentBounds),
    loadRetroQamCanary(),
    captureBatches,
    loadCaptureDopplerSummaries(currentBounds),
    captureBatches.then(() => loadCaptureQamSummaries()),
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
  Promise.allSettled([
    loadCaptureBatches(bounds, null, captureRadioFilter() !== "").then(() => loadCaptureQamSummaries()),
    loadCaptureDopplerSummaries(bounds),
  ]);
});

byId("capture-starlink-filter").addEventListener("change", () => {
  renderCaptureBatches();
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

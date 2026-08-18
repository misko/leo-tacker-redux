"use strict";

const byId = (id) => document.getElementById(id);
let waterfallTiles = [];
let dopplerVisualization = null;
let dopplerRecordingId = null;
let dopplerRequestSequence = 0;
let surrogateRecordingId = null;
let surrogateRequestSequence = 0;
const surrogateKnownRadios = new Set();
let constellationRecordingId = null;
let constellationRequestSequence = 0;
const constellationKnownSegments = new Set();
const constellationKnownReceivers = new Set();
let temporalRecordingId = null;
let temporalRequestSequence = 0;
let temporalPayload = null;
let temporalChartPoints = [];
let temporalFocusedPoint = 0;

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

function recordingIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || parts[0] !== "recordings") return null;
  try {
    return decodeURIComponent(parts[1]);
  } catch (_error) {
    return null;
  }
}

function formatUtcNs(value) {
  const milliseconds = Number(value) / 1_000_000;
  if (!Number.isFinite(milliseconds)) return "Unavailable";
  return new Date(milliseconds).toISOString();
}

function formatHz(value) {
  if (!Number.isFinite(value)) return "Unavailable";
  const magnitude = Math.abs(value);
  if (magnitude >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(6)} GHz`;
  if (magnitude >= 1_000_000) return `${(value / 1_000_000).toFixed(3)} MHz`;
  if (magnitude >= 1_000) return `${(value / 1_000).toFixed(3)} kHz`;
  return `${value.toFixed(1)} Hz`;
}

const diagnosticSemantics = {
  "rms-magnitude-counts": { metric: "RMS magnitude", unit: "counts" },
  "peak-psd-to-median-psd-ratio": { metric: "Peak / median PSD", unit: "×" },
  "log_likelihood_ratio": { metric: "Log likelihood ratio", unit: "" },
  "snr_like": { metric: "SNR-like score", unit: "" },
};

const diagnosticFamilies = {
  "sample-quality": "Sample quality",
  "compact-psd": "Spectrum shape",
  "coarse-E": "Coarse energy",
};

function humanizeIdentifier(value) {
  const words = String(value || "Unknown").replaceAll("_", " ").replaceAll("-", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function diagnosticPresentation(item) {
  const semantics = diagnosticSemantics[item.score_semantics] || {
    metric: humanizeIdentifier(item.score_semantics),
    unit: "",
  };
  const numeric = Number(item.score);
  const formatted = Number.isFinite(numeric)
    ? numeric.toLocaleString(undefined, { maximumFractionDigits: 4 })
    : String(item.score);
  return {
    family: diagnosticFamilies[item.method_id] || humanizeIdentifier(item.method_id),
    metric: semantics.metric,
    value: semantics.unit ? `${formatted} ${semantics.unit}` : formatted,
  };
}

function renderStarlinkNotEvaluated() {
  const card = byId("starlink-decision");
  card.dataset.state = "not-evaluated";
  byId("starlink-decision-badge").dataset.tone = "warning";
  byId("starlink-decision-badge").textContent = "Not evaluated";
  byId("starlink-decision-summary").textContent = "No exact Starlink known-code candidate bundle is projected for this capture. This is not a zero-detection result.";
  byId("starlink-decision-facts").replaceChildren();
  byId("starlink-candidates").hidden = true;
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
    const error = new Error(body?.error?.message || `Dashboard request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function renderSegments(segments) {
  const body = byId("segments-body");
  body.replaceChildren();
  for (const segment of segments) {
    const row = document.createElement("tr");
    appendText(row, "th", segment.segment_id).scope = "row";
    appendText(row, "td", `${segment.activity_kind} · ${segment.activity_id}`);
    appendText(row, "td", formatUtcNs(segment.started_utc_ns));
    appendText(row, "td", formatHz(segment.center_frequency_hz));
    appendText(row, "td", formatHz(segment.sample_rate_hz));
    appendText(row, "td", formatHz(segment.bandwidth_hz));
    appendText(row, "td", segment.gain_mode === "manual" ? `${segment.gain_db} dB` : "AGC");
    appendText(row, "td", segment.sample_count.toLocaleString());
    appendText(row, "td", (segment.receiver_chain_ids || []).join(", "));
    body.append(row);
  }
  setState("segments-state", "ready", `${segments.length} observed segment${segments.length === 1 ? "" : "s"} in chronological order.`);
}

async function loadCaptureDetail(recordingId) {
  try {
    const detail = await fetchJson(`/api/v3/recordings/${encodeURIComponent(recordingId)}`);
    byId("capture-identity").textContent = detail.recording_id;
    document.title = `${detail.recording_id} · Capture detail · LEO Flow`;
    replaceFacts(byId("capture-detail-facts"), [
      ["Recording", detail.recording_id],
      ["Plan", detail.plan_id],
      ["Station", detail.station_id],
      ["Radio", `${detail.radio_id} · serial ${detail.radio_serial}`],
      ["Hardware snapshot", detail.hardware_snapshot_id],
      ["UTC interval", `[${formatUtcNs(detail.capture_started_utc_ns)}, ${formatUtcNs(detail.capture_finished_utc_ns)})`],
      ["Clock status", detail.clock_status],
      ["Producer", detail.producer],
      ["Analysis state", detail.analysis_state],
      ["Recording object", detail.recording_object_available ? "Available" : "Missing / unavailable"],
      ["Manifest digest", `${detail.manifest_digest.algorithm}:${detail.manifest_digest.value}`],
      ["Sample representation", `${detail.sample_dtype} · ${(detail.sample_layout || []).join(" × ")}`],
    ]);
    setState(
      "capture-detail-state",
      detail.recording_object_available ? "ready" : "missing",
      detail.recording_object_available
        ? "Immutable capture facts are available."
        : "Capture facts remain projected, but the recording object is unavailable.",
    );
    renderSegments(detail.segments || []);
    return true;
  } catch (error) {
    setState("capture-detail-state", "error", `Capture detail unavailable: ${safeError(error)}`);
    setState("segments-state", "error", "Segment details are unavailable.");
    return false;
  }
}

async function loadAllFeatures(recordingId) {
  const body = byId("analysis-body");
  body.replaceChildren();
  let cursor = null;
  const seen = new Set();
  const items = [];
  try {
    for (let page = 0; page < 100; page += 1) {
      const suffix = cursor === null ? "" : `&cursor=${encodeURIComponent(cursor)}`;
      const payload = await fetchJson(`/api/recordings/${encodeURIComponent(recordingId)}/features?selector=*${suffix}`);
      items.push(...(payload.items || []));
      cursor = payload.next_cursor || null;
      if (cursor === null) break;
      if (seen.has(cursor)) throw new Error("Feature projection returned a cursor cycle");
      seen.add(cursor);
      if (page === 99) throw new Error("Feature projection exceeded the page bound");
    }
    for (const item of items) {
      const row = document.createElement("tr");
      const presentation = diagnosticPresentation(item);
      const exactScore = String(item.score);
      row.dataset.featureId = item.feature_id;
      row.dataset.exactScore = exactScore;
      row.title = `Feature ${item.feature_id}; exact score ${exactScore}`;
      row.setAttribute("aria-label", `Diagnostic feature ${item.feature_id}`);
      appendText(row, "th", "Recording projection").scope = "row";
      appendText(row, "td", "Not projected");
      const family = appendText(row, "td", presentation.family);
      appendText(family, "span", ` Exact feature identity: ${item.feature_id}.`, "visually-hidden");
      const metric = appendText(row, "td", presentation.metric);
      metric.title = item.score_semantics;
      const value = appendText(row, "td", presentation.value);
      appendText(value, "span", ` Exact numeric value: ${exactScore}.`, "visually-hidden");
      body.append(row);
    }
    byId("diagnostic-features-count").textContent = `${items.length} row${items.length === 1 ? "" : "s"}`;
    if (items.length === 0) {
      setState("analysis-state", "empty", "No projected diagnostic features are available for this recording.");
      return;
    }
    setState("analysis-state", "ready", `${items.length} projected diagnostic feature${items.length === 1 ? "" : "s"}; hidden by default.`);
  } catch (error) {
    setState("analysis-state", "error", `Analysis unavailable: ${safeError(error)}`);
    byId("diagnostic-features-count").textContent = "Unavailable";
  }
}

function scoreText(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toPrecision(6) : "Unavailable";
}

async function loadStarlinkDecision(recordingId) {
  const body = byId("starlink-candidates-body");
  body.replaceChildren();
  try {
    const payload = await fetchJson(`/api/v4/recordings/${encodeURIComponent(recordingId)}/starlink-suite`);
    if (!(["candidates", "not_evaluated"].includes(payload.state)) || payload.calibrated_detection_count !== null) {
      throw new Error("Dashboard returned an invalid detector-suite state");
    }
    const card = byId("starlink-decision");
    card.dataset.state = payload.state;
    byId("starlink-decision-badge").dataset.tone = "warning";
    if (payload.state === "not_evaluated") {
      byId("starlink-decision-badge").textContent = "Not evaluated";
      byId("starlink-decision-summary").textContent = "This sample-rate stratum clips the pilot band. The recording reached an explicit terminal result without running an incompatible search.";
      replaceFacts(byId("starlink-decision-facts"), [
        ["Analysis", payload.analysis_ref.artifact_id],
        ["Detection count", "Not available"],
        ["Reason", (payload.reason_codes || []).join(", ")],
      ]);
      byId("starlink-candidates").hidden = true;
      return;
    }
    byId("starlink-decision-badge").textContent = "Candidates · uncalibrated";
    byId("starlink-decision-summary").textContent = `${payload.method_count} report-method result${payload.method_count === 1 ? "" : "s"} across ${payload.analyzed_stream_count} stream${payload.analyzed_stream_count === 1 ? "" : "s"}. No matching whole-search calibration is attached, so no detection verdict or count exists.`;
    replaceFacts(byId("starlink-decision-facts"), [
      ["Analysis", payload.analysis_ref.artifact_id],
      ["Methods", payload.method_count],
      ["Detection count", "Not available — calibration required"],
      ["Reason", (payload.reason_codes || []).join(", ")],
    ]);
    for (const candidate of (payload.methods || [])) {
      const row = document.createElement("tr");
      const stream = appendText(row, "th", `${candidate.segment_id} · ${candidate.receiver_chain_id}`);
      stream.scope = "row";
      appendText(row, "td", candidate.edge);
      appendText(row, "td", candidate.method);
      appendText(row, "td", scoreText(candidate.score));
      appendText(row, "td", scoreText(candidate.control_score));
      appendText(row, "td", scoreText(candidate.margin));
      appendText(row, "td", `epoch ${candidate.epoch_sample}; coarse ${formatHz(candidate.coarse_cfo_hz)}; residual ${formatHz(candidate.residual_cfo_hz)}; ${candidate.effective_search_cell_count} cells; ${candidate.frame_support} frames`);
      body.append(row);
    }
    byId("starlink-candidates").hidden = payload.methods.length === 0;
  } catch (error) {
    if (error?.status === 404) {
      renderStarlinkNotEvaluated();
      return;
    }
    const card = byId("starlink-decision");
    card.dataset.state = "unavailable";
    byId("starlink-decision-badge").dataset.tone = "error";
    byId("starlink-decision-badge").textContent = "Unavailable";
    byId("starlink-decision-summary").textContent = `Starlink projection unavailable: ${safeError(error)}`;
    byId("starlink-decision-facts").replaceChildren();
    byId("starlink-candidates").hidden = true;
  }
}

function colorForPower(value, floor, ceiling) {
  const normalized = Math.max(0, Math.min(1, (value - floor) / (ceiling - floor || 1)));
  const hue = 250 - normalized * 210;
  const lightness = 12 + normalized * 58;
  return `hsl(${hue} 88% ${lightness}%)`;
}

function drawWaterfall(tile) {
  const canvas = byId("waterfall-canvas");
  const context = canvas.getContext("2d", { alpha: false });
  const rows = tile.power_db || [];
  const columns = tile.frequency_bin_offsets_hz || [];
  if (!context || rows.length === 0 || columns.length === 0) return;
  const cellWidth = canvas.width / columns.length;
  const cellHeight = canvas.height / rows.length;
  context.fillStyle = "#09100e";
  context.fillRect(0, 0, canvas.width, canvas.height);
  for (let timeIndex = 0; timeIndex < rows.length; timeIndex += 1) {
    for (let frequencyIndex = 0; frequencyIndex < columns.length; frequencyIndex += 1) {
      context.fillStyle = colorForPower(rows[timeIndex][frequencyIndex], tile.floor_db, tile.ceiling_db);
      context.fillRect(
        frequencyIndex * cellWidth,
        timeIndex * cellHeight,
        Math.ceil(cellWidth),
        Math.ceil(cellHeight),
      );
    }
  }
  const firstOffset = columns[0];
  const lastOffset = columns[columns.length - 1];
  const midpoints = tile.time_bin_midpoint_utc_ns || [];
  byId("waterfall-time-axis").textContent = `Time ↓ ${formatUtcNs(midpoints[0])} to ${formatUtcNs(midpoints[midpoints.length - 1])}`;
  byId("waterfall-frequency-axis").textContent = `Frequency → ${formatHz(tile.center_frequency_hz + firstOffset)} to ${formatHz(tile.center_frequency_hz + lastOffset)}`;
  byId("waterfall-power-axis").textContent = `Power ${tile.floor_db} to ${tile.ceiling_db} dB · ${tile.power_reference}`;
  canvas.setAttribute(
    "aria-label",
    `Waterfall for ${tile.segment_id}, receiver ${tile.receiver_chain_id}; ${rows.length} time bins by ${columns.length} frequency bins`,
  );
}

function selectWaterfallTile(index) {
  const tile = waterfallTiles[index];
  if (tile) drawWaterfall(tile);
}

async function loadWaterfall(recordingId) {
  const picker = byId("waterfall-tile");
  picker.replaceChildren();
  byId("waterfall-figure").hidden = true;
  try {
    const payload = await fetchJson(`/api/v3/recordings/${encodeURIComponent(recordingId)}/waterfall`);
    if (payload.state !== "complete") {
      const messages = {
        unavailable: "Waterfall analysis has not been selected for this recording.",
        pending: "Waterfall analysis is queued or running.",
        failed: `Waterfall analysis failed${payload.reason_code ? `: ${payload.reason_code}` : "."}`,
      };
      setState("waterfall-state", payload.state === "failed" ? "error" : payload.state, messages[payload.state] || "Waterfall projection is unavailable.");
      return;
    }
    waterfallTiles = payload.tiles || [];
    for (const [index, tile] of waterfallTiles.entries()) {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${tile.segment_id} · ${tile.receiver_chain_id}`;
      picker.append(option);
    }
    picker.disabled = waterfallTiles.length < 2;
    byId("waterfall-figure").hidden = false;
    drawWaterfall(waterfallTiles[0]);
    setState("waterfall-state", "ready", `${waterfallTiles.length} projected segment/receiver waterfall tile${waterfallTiles.length === 1 ? "" : "s"}.`);
  } catch (error) {
    setState("waterfall-state", "error", `Waterfall unavailable: ${safeError(error)}`);
  }
}

const dopplerLayerLabels = {
  average: "Average power",
  residual: "Temporal-median residual",
  "high-percentile": "High percentile",
};

const trackColors = ["#fff176", "#ff8a80", "#80d8ff", "#b388ff", "#69f0ae"];

function quantile(sorted, fraction) {
  if (sorted.length === 0) return 0;
  const position = Math.max(0, Math.min(sorted.length - 1, (sorted.length - 1) * fraction));
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  const weight = position - lower;
  return sorted[lower] * (1 - weight) + sorted[upper] * weight;
}

function layerRange(rows) {
  const values = rows.flat().filter(Number.isFinite).sort((a, b) => a - b);
  let floor = quantile(values, 0.02);
  let ceiling = quantile(values, 0.995);
  if (!(ceiling > floor)) {
    floor -= 0.5;
    ceiling += 0.5;
  }
  return { floor, ceiling };
}

function selectedDopplerTile() {
  if (!dopplerVisualization) return null;
  return dopplerVisualization.tiles?.[Number(byId("doppler-tile").value) || 0] || null;
}

function dopplerTileKey(tile) {
  return tile ? `${tile.segment_id}\u0000${tile.receiver_chain_id}` : null;
}

function matchingDopplerCandidates(tile) {
  if (!dopplerVisualization || !tile) return [];
  return (dopplerVisualization.candidates || []).filter(
    (candidate) => candidate.segment_id === tile.segment_id
      && candidate.receiver_chain_id === tile.receiver_chain_id,
  );
}

function matchingDopplerEvidence(tile) {
  if (!dopplerVisualization || !tile) return [];
  return (dopplerVisualization.advanced_evidence || []).filter(
    (item) => item.segment_id === tile.segment_id
      && item.receiver_chain_id === tile.receiver_chain_id,
  );
}

function drawDopplerVisualization() {
  const tile = selectedDopplerTile();
  if (!tile) return;
  const layerName = dopplerVisualization.selected_layer;
  const rows = (tile.time_bins || []).map((row) => row.power_db || []);
  const offsets = tile.frequency_bin_offsets_hz || [];
  const canvas = byId("doppler-canvas");
  const context = canvas.getContext("2d", { alpha: false });
  if (!context || rows.length === 0 || offsets.length === 0) return;
  const range = layerRange(rows);
  const cellWidth = canvas.width / offsets.length;
  const cellHeight = canvas.height / rows.length;
  context.fillStyle = "#050907";
  context.fillRect(0, 0, canvas.width, canvas.height);
  for (let timeIndex = 0; timeIndex < rows.length; timeIndex += 1) {
    for (let frequencyIndex = 0; frequencyIndex < offsets.length; frequencyIndex += 1) {
      context.fillStyle = colorForPower(
        rows[timeIndex][frequencyIndex],
        range.floor,
        range.ceiling,
      );
      context.fillRect(
        frequencyIndex * cellWidth,
        timeIndex * cellHeight,
        Math.ceil(cellWidth),
        Math.ceil(cellHeight),
      );
    }
  }

  const midpoints = tile.time_bins.map((row) => Number(row.midpoint_utc_ns));
  const firstTime = midpoints[0];
  const lastTime = midpoints[midpoints.length - 1];
  const minimumFrequency = tile.center_frequency_hz + offsets[0];
  const maximumFrequency = tile.center_frequency_hz + offsets[offsets.length - 1];
  const candidates = matchingDopplerCandidates(tile);
  if (byId("doppler-overlays").checked) {
    for (const candidate of candidates) {
      const points = (candidate.points || []).filter(
        (point) => Number(point.midpoint_utc_ns) >= firstTime
          && Number(point.midpoint_utc_ns) <= lastTime
          && point.frequency_hz >= minimumFrequency
          && point.frequency_hz <= maximumFrequency,
      );
      if (points.length === 0) continue;
      context.beginPath();
      context.strokeStyle = trackColors[(candidate.rank - 1) % trackColors.length];
      context.lineWidth = 3;
      context.shadowColor = "#000";
      context.shadowBlur = 3;
      for (const [index, point] of points.entries()) {
        const x = (point.frequency_hz - minimumFrequency)
          / (maximumFrequency - minimumFrequency || 1) * canvas.width;
        const y = (Number(point.midpoint_utc_ns) - firstTime)
          / (lastTime - firstTime || 1) * canvas.height;
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      context.stroke();
      context.shadowBlur = 0;
    }
  }

  const layerLabel = layerName === "high-percentile"
    ? `High percentile (P${Number(tile.high_percentile).toFixed(1)})`
    : dopplerLayerLabels[layerName] || layerName;
  byId("doppler-time-axis").textContent = `Time ↓ ${formatUtcNs(firstTime)} to ${formatUtcNs(lastTime)}`;
  byId("doppler-frequency-axis").textContent = `Frequency → ${formatHz(minimumFrequency)} to ${formatHz(maximumFrequency)}`;
  byId("doppler-power-axis").textContent = `${layerLabel} ${range.floor.toFixed(2)} to ${range.ceiling.toFixed(2)} dB · ${tile.power_reference}`;
  canvas.setAttribute(
    "aria-label",
    `${layerLabel} Doppler waterfall for ${tile.segment_id}, receiver ${tile.receiver_chain_id}; ${rows.length} time bins by ${offsets.length} frequency bins; ${byId("doppler-overlays").checked ? candidates.length : 0} candidate track overlays`,
  );
}

function formatPercent(value) {
  return `${(Number(value) * 100).toFixed(3)}%`;
}

function renderDopplerCoverage(tile) {
  const coverage = tile.coverage;
  const resolution = Number(tile.sample_rate_hz) / Number(tile.fft_window_samples);
  replaceFacts(byId("doppler-coverage-facts"), [
    ["Selected tile", `${tile.segment_id} / ${tile.receiver_chain_id}`],
    ["RF coverage", formatPercent(coverage.coverage_fraction)],
    ["Contiguous RF spans", Number(coverage.contiguous_rf_span_count).toLocaleString()],
    ["Analyzed samples", Number(coverage.analyzed_sample_count).toLocaleString()],
    ["Contiguous RF samples", Number(coverage.contiguous_rf_sample_count).toLocaleString()],
    ["Discarded tail samples", Number(coverage.discarded_tail_sample_count).toLocaleString()],
    ["FFT frames", Number(coverage.fft_frame_count).toLocaleString()],
    ["FFT window", `${Number(tile.fft_window_samples).toLocaleString()} samples`],
    ["Frequency-bin spacing", formatHz(resolution)],
    ["Display grid", `${tile.time_bins.length} × ${tile.frequency_bin_offsets_hz.length}`],
  ]);
}

function provenanceFacts(label, provenance) {
  if (!provenance) return [];
  const facts = [
    [`${label} product`, provenance.artifact_id],
    [`${label} algorithm`, provenance.algorithm_version],
    [`${label} input digest`, `${provenance.input_identity_digest.algorithm}:${provenance.input_identity_digest.value}`],
  ];
  if (provenance.config_digest) {
    facts.push([`${label} config digest`, `${provenance.config_digest.algorithm}:${provenance.config_digest.value}`]);
  }
  if (provenance.analysis_run_id) facts.push([`${label} run`, provenance.analysis_run_id]);
  if (provenance.producer_name) {
    facts.push([`${label} producer`, `${provenance.producer_name} ${provenance.producer_version || ""}`.trim()]);
  }
  if (provenance.git_commit) facts.push([`${label} Git commit`, provenance.git_commit]);
  if (provenance.started_utc_ns !== null && provenance.started_utc_ns !== undefined) {
    facts.push([`${label} execution`, `${formatUtcNs(provenance.started_utc_ns)} to ${formatUtcNs(provenance.completed_utc_ns)}`]);
  }
  return facts;
}

function renderDopplerProvenance(payload, tile) {
  const facts = [...provenanceFacts("Waterfall", payload.waterfall_provenance)];
  const selected = (payload.doppler_provenance || []).filter(
    (item) => item.segment_id === tile.segment_id
      && item.receiver_chain_id === tile.receiver_chain_id,
  );
  for (const item of selected) {
    const tileLabel = `${item.segment_id} / ${item.receiver_chain_id}`;
    facts.push(...provenanceFacts(`${tileLabel} basic`, item.basic));
    facts.push(...provenanceFacts(`${tileLabel} advanced`, item.advanced));
  }
  replaceFacts(byId("doppler-provenance-facts"), facts);
  byId("doppler-provenance").hidden = facts.length === 0;
}

function addCandidateFact(target, label, value) {
  const item = document.createElement("div");
  appendText(item, "span", label, "metric-label");
  appendText(item, "strong", value, "metric-value");
  target.append(item);
}

function renderDopplerCandidates(payload, tile) {
  const list = byId("doppler-candidate-list");
  list.replaceChildren();
  const candidates = matchingDopplerCandidates(tile);
  const total = (payload.candidates || []).length;
  byId("doppler-candidate-count").textContent = `${candidates.length} selected · ${total} total`;
  if (candidates.length === 0) {
    appendText(list, "p", `No continuity-qualified blind tracks were projected for ${tile.segment_id} / ${tile.receiver_chain_id}. This is not a calibrated no-detection result.`, "availability-note");
    return;
  }
  for (const candidate of candidates) {
    const card = document.createElement("article");
    card.className = "doppler-candidate-card";
    card.dataset.rank = String(candidate.rank);
    const heading = document.createElement("div");
    heading.className = "decision-heading";
    const title = appendText(heading, "h4", `Candidate ${candidate.rank}`);
    const swatch = document.createElement("span");
    swatch.className = "doppler-track-swatch";
    swatch.style.backgroundColor = trackColors[(candidate.rank - 1) % trackColors.length];
    swatch.setAttribute("aria-hidden", "true");
    title.prepend(swatch);
    const model = appendText(heading, "span", candidate.selected_model, "status-badge");
    model.dataset.tone = candidate.stationary_control.moving_model_preferred ? "ok" : "warning";
    card.append(heading);
    const facts = document.createElement("div");
    facts.className = "candidate-metric-grid";
    addCandidateFact(facts, "Drift rate", `${Number(candidate.drift_rate_hz_s).toFixed(3)} Hz/s`);
    addCandidateFact(facts, "Acceleration", `${Number(candidate.drift_acceleration_hz_s2).toFixed(3)} Hz/s²`);
    addCandidateFact(facts, "Mean spectral peak excess", `${Number(candidate.mean_spectral_peak_excess_db).toFixed(2)} dB`);
    addCandidateFact(facts, "Peak layer value", `${Number(candidate.peak_layer_value_db).toFixed(2)} dB`);
    addCandidateFact(facts, "Duration", `${Number(candidate.duration_s).toFixed(3)} s`);
    addCandidateFact(facts, "Fit residual", formatHz(candidate.residual_rms_hz));
    addCandidateFact(facts, "Stationary improvement", formatPercent(candidate.stationary_control.residual_improvement_fraction));
    addCandidateFact(facts, "Missing rows", formatPercent(candidate.missing_row_fraction));
    addCandidateFact(facts, "Edge-truncated points", Number(candidate.edge_truncated_point_count).toLocaleString());
    addCandidateFact(facts, "Ranking score", scoreText(candidate.ranking_score));
    card.append(facts);
    appendText(
      card,
      "p",
      `Stationary control: ${formatHz(candidate.stationary_control.constant_residual_rms_hz)} RMS; selected fit: ${formatHz(candidate.stationary_control.selected_residual_rms_hz)} RMS; BIC margin ${Number(candidate.stationary_control.bic_margin_over_constant).toFixed(3)}.`,
      "availability-note",
    );
    list.append(card);
  }
}

function advancedFactRows(item) {
  const rows = [
    ["Physical drift rate", item.drift_rate_hz_s === null || item.drift_rate_hz_s === undefined ? "Unavailable" : `${Number(item.drift_rate_hz_s).toFixed(3)} Hz/s`],
    ["Slope-bank path", `${Number(item.slope_bins_per_row).toFixed(4)} bins/row`],
    ["Held-out score", scoreText(item.heldout_score)],
    ["Stationary control", scoreText(item.stationary_score)],
    ["Opposite-slope control", scoreText(item.opposite_slope_score)],
    ["Time-shuffled controls", (item.shuffled_scores || []).map(scoreText).join(", ")],
  ];
  if (item.spectral_peak_excess_reference) {
    rows.push(["Spectral peak excess reference", item.spectral_peak_excess_reference]);
  }
  if (item.association?.state === "matched-basic-candidate") {
    rows.push(
      ["Basic-track association", `Candidate ${item.association.basic_candidate_rank}`],
      ["Path overlap", `${item.association.overlap_point_count} points · ${formatPercent(item.association.overlap_fraction)}`],
      ["Path distance mean / max", `${formatHz(item.association.mean_distance_hz)} / ${formatHz(item.association.maximum_distance_hz)}`],
    );
  } else if (item.association?.state === "advanced-path-only") {
    rows.push(["Basic-track association", "Advanced path only — no basic candidate association"]);
  } else {
    rows.push(["Basic-track association", "Unavailable"]);
  }
  if (item.comb) {
    rows.push(
      ["Comb fit / held-out", `${scoreText(item.comb.fit_score)} / ${scoreText(item.comb.heldout_score)}`],
      ["Wrong-spacing control", scoreText(item.comb.wrong_spacing_score)],
    );
  }
  if (item.broadband) {
    rows.push(
      ["Broadband edge slopes", `${Number(item.broadband.lower_slope_bins_per_row).toFixed(4)} / ${Number(item.broadband.upper_slope_bins_per_row).toFixed(4)} bins/row`],
      ["Broadband width MAD", formatPercent(item.broadband.width_mad_fraction)],
      ["Texture motion / correlation", `${Number(item.broadband.texture_shift_bins).toFixed(3)} bins / ${Number(item.broadband.texture_correlation).toFixed(4)}`],
    );
  }
  if (item.dual_receiver) {
    rows.push(
      ["Dual-receiver common slope", `${Number(item.dual_receiver.common_slope_bins_per_row).toFixed(4)} bins/row`],
      ["Dual-receiver slope difference", `${Number(item.dual_receiver.slope_difference).toFixed(4)} bins/row`],
      ["Dual-receiver residual / correlation", `${Number(item.dual_receiver.offset_removed_rms_bins).toFixed(4)} bins / ${Number(item.dual_receiver.path_correlation).toFixed(4)}`],
    );
  }
  return rows;
}

function renderDopplerAdvanced(payload, tile) {
  const container = byId("doppler-advanced-list");
  container.replaceChildren();
  const evidence = matchingDopplerEvidence(tile);
  byId("doppler-advanced").hidden = evidence.length === 0;
  for (const item of evidence) {
    const card = document.createElement("article");
    card.className = "detail-card doppler-evidence-card";
    const identity = item.candidate_rank === null || item.candidate_rank === undefined
      ? "Advanced path controls"
      : `Candidate ${item.candidate_rank} controls`;
    appendText(card, "h4", identity);
    const facts = document.createElement("dl");
    facts.className = "fact-grid";
    replaceFacts(facts, advancedFactRows(item));
    card.append(facts);
    if (item.orbit_association) {
      const association = item.orbit_association;
      const panel = document.createElement("aside");
      panel.className = "orbit-association";
      appendText(panel, "h4", "Post-blind TLE association");
      appendText(
        panel,
        "p",
        `${association.name} · ${association.qualified ? "qualified" : "ambiguous"} · held-out RMS ${Number(association.heldout_rms_bins).toFixed(4)} bins · runner-up margin ${Number(association.runner_up_margin_bins).toFixed(4)} bins.`,
      );
      card.append(panel);
    }
    container.append(card);
  }
}

function renderSelectedDopplerTile() {
  const tile = selectedDopplerTile();
  if (!tile || !dopplerVisualization) return;
  drawDopplerVisualization();
  renderDopplerCoverage(tile);
  renderDopplerProvenance(dopplerVisualization, tile);
  renderDopplerCandidates(dopplerVisualization, tile);
  renderDopplerAdvanced(dopplerVisualization, tile);
}

function resetDopplerPresentation(badgeText = "Loading") {
  byId("doppler-figure").hidden = true;
  byId("doppler-warning").hidden = true;
  byId("doppler-coverage-facts").replaceChildren();
  byId("doppler-provenance-facts").replaceChildren();
  byId("doppler-provenance").hidden = true;
  byId("doppler-candidate-list").replaceChildren();
  byId("doppler-candidate-count").textContent = badgeText;
  byId("doppler-advanced-list").replaceChildren();
  byId("doppler-advanced").hidden = true;
  byId("doppler-time-axis").textContent = "";
  byId("doppler-frequency-axis").textContent = "";
  byId("doppler-power-axis").textContent = "";
}

async function loadDopplerVisualization(recordingId) {
  dopplerRecordingId = recordingId;
  const requestedLayer = byId("doppler-layer").value;
  const requestSequence = ++dopplerRequestSequence;
  const picker = byId("doppler-tile");
  const previouslySelectedKey = dopplerTileKey(selectedDopplerTile());
  dopplerVisualization = null;
  picker.replaceChildren();
  picker.disabled = true;
  resetDopplerPresentation();
  try {
    setState("doppler-state", "loading", `Loading ${dopplerLayerLabels[requestedLayer]} waterfall and Doppler evidence…`);
    const payload = await fetchJson(`/api/v9/recordings/${encodeURIComponent(recordingId)}/doppler-visualization?layer=${encodeURIComponent(requestedLayer)}`);
    if (requestSequence !== dopplerRequestSequence) return;
    if (payload.candidate_only !== true || payload.calibrated_detection_count !== null) {
      throw new Error("Dashboard returned unsafe Doppler detection semantics");
    }
    if (payload.selected_layer !== requestedLayer) {
      throw new Error("Dashboard returned the wrong waterfall layer");
    }
    byId("doppler-warning").hidden = false;
    if (payload.state !== "complete") {
      const messages = {
        unavailable: "Waterfall v0.2 and Doppler evidence are unavailable for this recording.",
        pending: "Waterfall v0.2 or Doppler analysis is queued or running.",
        failed: `Doppler visualization failed${payload.reason_codes?.length ? `: ${payload.reason_codes.join(", ")}` : "."}`,
      };
      byId("doppler-candidate-count").textContent = payload.state === "pending" ? "Pending" : "Unavailable";
      setState("doppler-state", payload.state === "failed" ? "error" : payload.state, messages[payload.state] || "Doppler visualization is unavailable.");
      return;
    }
    dopplerVisualization = payload;
    for (const [index, tile] of (payload.tiles || []).entries()) {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = `${tile.segment_id} · ${tile.receiver_chain_id}`;
      picker.append(option);
    }
    const restoredIndex = payload.tiles.findIndex(
      (tile) => dopplerTileKey(tile) === previouslySelectedKey,
    );
    picker.value = String(restoredIndex >= 0 ? restoredIndex : 0);
    picker.disabled = payload.tiles.length < 2;
    byId("doppler-figure").hidden = false;
    renderSelectedDopplerTile();
    setState(
      "doppler-state",
      "ready",
      `${payload.tiles.length} full-coverage waterfall tile${payload.tiles.length === 1 ? "" : "s"}; ${payload.candidates.length} uncalibrated candidate track${payload.candidates.length === 1 ? "" : "s"}.`,
    );
  } catch (error) {
    if (requestSequence !== dopplerRequestSequence) return;
    if (error?.status === 404) {
      byId("doppler-candidate-count").textContent = "Unavailable";
      setState("doppler-state", "missing", "Waterfall v0.2 and blind Doppler evidence have not been projected for this recording.");
      return;
    }
    byId("doppler-candidate-count").textContent = "Unavailable";
    setState("doppler-state", "error", `Doppler visualization unavailable: ${safeError(error)}`);
  }
}

function surrogateUtcNs(inputId) {
  const value = byId(inputId).value;
  if (!value) return null;
  const milliseconds = Date.parse(`${value}Z`);
  if (!Number.isFinite(milliseconds)) throw new Error("UTC filters must be valid dates");
  return String(BigInt(milliseconds) * 1_000_000n);
}

function surrogateQueryString() {
  const parameters = new URLSearchParams();
  parameters.set("methods", byId("surrogate-method").value);
  parameters.set("maximum_rows", "64");
  const radio = byId("surrogate-radio").value;
  const channel = byId("surrogate-channel").value;
  const edge = byId("surrogate-edge").value;
  if (radio) parameters.set("radio_ids", radio);
  if (channel) parameters.set("channel_numbers", channel);
  if (edge) parameters.set("edges", edge);
  const start = surrogateUtcNs("surrogate-start");
  const stop = surrogateUtcNs("surrogate-stop");
  if (start !== null) parameters.set("interval_start_utc_ns", start);
  if (stop !== null) parameters.set("interval_stop_utc_ns", stop);
  if (start !== null && stop !== null && BigInt(stop) <= BigInt(start)) {
    throw new Error("Until UTC must be later than From UTC");
  }
  return parameters.toString();
}

function resetSurrogatePresentation(badgeText = "Pending") {
  byId("surrogate-warning").hidden = true;
  byId("surrogate-summary").hidden = true;
  byId("surrogate-summary-facts").replaceChildren();
  byId("surrogate-row-count").textContent = badgeText;
  byId("surrogate-rows").replaceChildren();
}

function updateSurrogateRadioOptions(rows) {
  const picker = byId("surrogate-radio");
  const selected = picker.value;
  for (const row of rows) surrogateKnownRadios.add(row.radio_id);
  picker.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All radios";
  picker.append(all);
  for (const radioId of [...surrogateKnownRadios].sort()) {
    const option = document.createElement("option");
    option.value = radioId;
    option.textContent = radioId;
    picker.append(option);
  }
  picker.value = surrogateKnownRadios.has(selected) ? selected : "";
}

function surrogateScoreRow(label, score, role) {
  const row = document.createElement("div");
  row.className = "surrogate-score-row";
  row.dataset.role = role;
  appendText(row, "span", label, "surrogate-score-label");
  const track = document.createElement("span");
  track.className = "surrogate-score-track";
  const fill = document.createElement("span");
  fill.className = "surrogate-score-fill";
  fill.style.width = `${Math.max(0, Math.min(1, Number(score))) * 100}%`;
  track.append(fill);
  row.append(track);
  appendText(row, "span", Number(score).toFixed(6), "surrogate-score-value");
  return row;
}

function digestText(value) {
  return value ? `${value.algorithm}:${value.value}` : "Unavailable";
}

function artifactText(value) {
  return value ? `${value.artifact_id} · ${digestText(value.digest)}` : "Unavailable";
}

function surrogatePatternDisclosure(row, analysisRef) {
  const details = document.createElement("details");
  details.className = "surrogate-patterns";
  appendText(details, "summary", "Show pattern identities and immutable provenance");
  const patterns = document.createElement("ol");
  patterns.className = "surrogate-pattern-list";
  appendText(
    patterns,
    "li",
    `Qin exact known pattern · bound by analysis ${artifactText(analysisRef)}`,
  );
  for (const [index, pattern] of (row.surrogate_patterns || []).entries()) {
    appendText(
      patterns,
      "li",
      `Surrogate ${index + 1}: ${pattern.pattern_id} · codebook ${pattern.codebook_index} · seed ${pattern.generator_seed} · ${pattern.generator_id} · matrix ${digestText(pattern.qpsk_state_matrix_digest)} · template ${artifactText(pattern.template_ref)}`,
    );
  }
  details.append(patterns);
  const provenance = row.provenance;
  const facts = document.createElement("dl");
  facts.className = "fact-grid";
  replaceFacts(facts, [
    ["Producer", `${provenance.producer_name} ${provenance.producer_version}`],
    ["Git commit", provenance.git_commit],
    ["Host class", provenance.host_class],
    ["Execution", `${formatUtcNs(provenance.started_utc_ns)} to ${formatUtcNs(provenance.completed_utc_ns)}`],
    ["Environment digest", digestText(provenance.environment_digest)],
    ["Config digest", digestText(provenance.normalized_config_digest)],
    ["Input digests", (provenance.input_digests || []).map(digestText).join(", ")],
    ["Dependency digests", (provenance.dependency_digests || []).map(digestText).join(", ") || "None"],
  ]);
  details.append(facts);
  return details;
}

function renderSurrogateRow(row, analysisRef) {
  const card = document.createElement("article");
  card.className = "surrogate-row-card";
  card.dataset.radioId = row.radio_id;
  card.dataset.channel = String(row.channel_number);
  card.dataset.edge = row.edge;
  const heading = document.createElement("div");
  heading.className = "decision-heading";
  appendText(heading, "h3", `${row.radio_id} · CH${row.channel_number} ${row.edge}`);
  const method = appendText(heading, "span", row.method, "status-badge");
  method.dataset.tone = "warning";
  card.append(heading);
  const exceedances = (row.surrogate_scores || []).filter(
    (score) => Number(score) >= Number(row.qin_score),
  ).length;
  const facts = document.createElement("dl");
  facts.className = "fact-grid";
  replaceFacts(facts, [
    ["Segment / receiver", `${row.segment_id} / ${row.receiver_chain_id}`],
    ["UTC interval", `${formatUtcNs(row.interval_start_utc_ns)} to ${formatUtcNs(row.interval_stop_utc_ns)}`],
    ["Qin score", Number(row.qin_score).toFixed(6)],
    ["Finite upper-tail rank", `${Number(row.finite_upper_tail_rank).toFixed(6)} · (1 + ${exceedances}) / ${(row.surrogate_scores || []).length + 1}`],
    ["Winning epoch", `${Number(row.qin_winning_epoch_sample).toLocaleString()} samples`],
    ["Winning CFO", `${formatHz(row.qin_winning_coarse_cfo_hz)} coarse · ${formatHz(row.qin_winning_residual_cfo_hz)} residual`],
  ]);
  card.append(facts);
  const scores = document.createElement("div");
  scores.className = "surrogate-score-list";
  scores.setAttribute("aria-label", "Qin score compared with every surrogate score");
  scores.append(surrogateScoreRow("Qin exact", row.qin_score, "qin"));
  for (const [index, score] of (row.surrogate_scores || []).entries()) {
    const pattern = row.surrogate_patterns[index];
    scores.append(
      surrogateScoreRow(
        `Surrogate ${index + 1} · ${pattern.pattern_id}`,
        score,
        "surrogate",
      ),
    );
  }
  card.append(scores);
  appendText(
    card,
    "p",
    "This finite paired rank is descriptive candidate evidence. It is not a calibrated p-value and is not a detection decision.",
    "availability-note",
  );
  card.append(surrogatePatternDisclosure(row, analysisRef));
  return card;
}

function renderSurrogatePayload(payload) {
  if (
    payload.calibrated_detection_count !== null
    || !(payload.warnings || []).includes("finite-rank-not-calibrated-p-value")
    || !(payload.warnings || []).includes("candidate-evidence-not-detection")
  ) {
    throw new Error("Dashboard returned unsafe surrogate-control semantics");
  }
  byId("surrogate-warning").hidden = false;
  byId("surrogate-summary").hidden = false;
  updateSurrogateRadioOptions(payload.rows || []);
  const selectedMethod = byId("surrogate-method").value;
  const aggregate = (payload.aggregates || []).find(
    (item) => item.method === selectedMethod,
  ) || null;
  byId("surrogate-row-count").textContent = `${payload.rows.length} shown · ${payload.total_matching_rows} matching`;
  const facts = [
    ["Analysis product", artifactText(payload.analysis_ref)],
    ["Method", selectedMethod],
    ["Returned rows", `${payload.rows.length} of ${payload.total_matching_rows}`],
    ["Calibrated detections", "Not available — candidate evidence only"],
  ];
  if (aggregate) {
    facts.push(
      ["Mean Qin score", Number(aggregate.mean_qin_score).toFixed(6)],
      ["Mean surrogate score", Number(aggregate.mean_surrogate_score).toFixed(6)],
      ["Mean finite upper-tail rank", Number(aggregate.mean_finite_upper_tail_rank).toFixed(6)],
      ["Qin above every surrogate", `${aggregate.qin_above_all_surrogates_count} / ${aggregate.row_count}`],
      ["Statistic", "Finite paired upper-tail rank — not a calibrated p-value"],
    );
  }
  replaceFacts(byId("surrogate-summary-facts"), facts);
  const rows = byId("surrogate-rows");
  rows.replaceChildren();
  for (const row of payload.rows || []) {
    rows.append(renderSurrogateRow(row, payload.analysis_ref));
  }
  if (payload.state === "not_evaluated") {
    appendText(
      rows,
      "p",
      "This recording was explicitly not evaluated for paired surrogate evidence.",
      "availability-note",
    );
    setState("surrogate-state", "not-evaluated", "Paired surrogate search was not evaluated for this recording.");
    return;
  }
  if (payload.state !== "candidates") {
    throw new Error("Dashboard returned an unknown surrogate-control state");
  }
  const truncation = payload.total_matching_rows > payload.rows.length
    ? " The bounded response is truncated; narrow the filters to inspect every row."
    : "";
  setState(
    "surrogate-state",
    "ready",
    `${payload.rows.length} paired Qin/surrogate comparison${payload.rows.length === 1 ? "" : "s"} loaded.${truncation}`,
  );
}

async function loadSurrogateNull(recordingId) {
  surrogateRecordingId = recordingId;
  const requestSequence = ++surrogateRequestSequence;
  resetSurrogatePresentation();
  setState("surrogate-state", "pending", "Paired surrogate evidence is pending while this filtered projection loads…");
  let query;
  try {
    query = surrogateQueryString();
  } catch (error) {
    byId("surrogate-row-count").textContent = "Invalid filters";
    setState("surrogate-state", "error", safeError(error));
    return;
  }
  try {
    const payload = await fetchJson(`/api/v10/recordings/${encodeURIComponent(recordingId)}/starlink-surrogate-null?${query}`);
    if (requestSequence !== surrogateRequestSequence) return;
    renderSurrogatePayload(payload);
  } catch (error) {
    if (requestSequence !== surrogateRequestSequence) return;
    if (error?.status === 404) {
      byId("surrogate-row-count").textContent = "Unavailable";
      setState("surrogate-state", "unavailable", "Paired surrogate evidence is unavailable for this recording.");
      return;
    }
    byId("surrogate-row-count").textContent = "Error";
    setState("surrogate-state", "error", `Paired surrogate evidence failed: ${safeError(error)}`);
  }
}

const constellationStateColors = ["#80d8ff", "#fff176", "#ff8a80", "#69f0ae"];

function constellationQueryString() {
  const parameters = new URLSearchParams();
  const segment = byId("constellation-segment").value;
  const receiver = byId("constellation-receiver").value;
  const edge = byId("constellation-edge").value;
  if (segment) parameters.set("segment_ids", segment);
  if (receiver) parameters.set("receiver_chain_ids", receiver);
  if (edge) parameters.set("edges", edge);
  parameters.set("maximum_streams", "16");
  parameters.set("maximum_points_per_stream", "2400");
  return parameters.toString();
}

function resetConstellationPresentation(badgeText = "Pending") {
  byId("constellation-warning").hidden = true;
  byId("constellation-streams").replaceChildren();
  setState(
    "constellation-state",
    "pending",
    `Published edge-pilot constellation evidence is ${badgeText.toLowerCase()}…`,
  );
}

function updateConstellationOptions(streams) {
  for (const stream of streams) {
    constellationKnownSegments.add(stream.segment_id);
    constellationKnownReceivers.add(stream.receiver_chain_id);
  }
  for (const [id, values, allLabel] of [
    ["constellation-segment", constellationKnownSegments, "All segments"],
    ["constellation-receiver", constellationKnownReceivers, "All receivers"],
  ]) {
    const picker = byId(id);
    const selected = picker.value;
    picker.replaceChildren();
    const all = document.createElement("option");
    all.value = "";
    all.textContent = allLabel;
    picker.append(all);
    for (const value of [...values].sort()) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      picker.append(option);
    }
    picker.value = values.has(selected) ? selected : "";
  }
}

function drawConstellation(canvas, stream) {
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const padding = 62;
  const plotSize = Math.min(width, height) - 2 * padding;
  const points = stream.display_points || [];
  const magnitude = Math.max(
    1.25,
    ...points.flatMap((point) => [Math.abs(Number(point.i)), Math.abs(Number(point.q))]),
  ) * 1.08;
  const projectX = (value) => padding + ((Number(value) + magnitude) / (2 * magnitude)) * plotSize;
  const projectY = (value) => padding + ((magnitude - Number(value)) / (2 * magnitude)) * plotSize;

  context.clearRect(0, 0, width, height);
  context.fillStyle = "#050907";
  context.fillRect(0, 0, width, height);
  context.strokeStyle = "#294137";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(projectX(-magnitude), projectY(0));
  context.lineTo(projectX(magnitude), projectY(0));
  context.moveTo(projectX(0), projectY(-magnitude));
  context.lineTo(projectX(0), projectY(magnitude));
  context.stroke();

  for (const point of points) {
    const state = Number(point.expected_state);
    context.globalAlpha = point.correct ? 0.58 : 0.9;
    context.fillStyle = constellationStateColors[state];
    context.beginPath();
    context.arc(projectX(point.i), projectY(point.q), point.correct ? 2.1 : 3.2, 0, 2 * Math.PI);
    context.fill();
  }
  context.globalAlpha = 1;
  for (let state = 0; state < 4; state += 1) {
    const angle = 0.5 * Math.PI * (state + 0.5);
    const x = projectX(Math.cos(angle));
    const y = projectY(Math.sin(angle));
    context.strokeStyle = constellationStateColors[state];
    context.lineWidth = 3;
    context.beginPath();
    context.moveTo(x - 8, y - 8);
    context.lineTo(x + 8, y + 8);
    context.moveTo(x + 8, y - 8);
    context.lineTo(x - 8, y + 8);
    context.stroke();
  }
  context.fillStyle = "#9eaaa5";
  context.font = "16px ui-monospace, monospace";
  context.fillText("I", width - padding + 18, projectY(0) + 5);
  context.fillText("Q", projectX(0) + 8, padding - 22);
  context.fillText((-magnitude).toFixed(2), padding - 18, height - padding + 28);
  context.fillText(magnitude.toFixed(2), width - padding - 28, height - padding + 28);
  canvas.dataset.renderedPoints = String(points.length);
  canvas.setAttribute(
    "aria-label",
    `Known Qin synchronization-pilot constellation for ${stream.segment_id}, receiver ${stream.receiver_chain_id}, ${stream.edge} edge; ${points.length} coefficients colored by expected QPSK state with ideal-state crosses`,
  );
}

function constellationLegend() {
  const legend = document.createElement("div");
  legend.className = "constellation-legend";
  legend.setAttribute("aria-label", "Expected Qin state colors");
  for (let state = 0; state < 4; state += 1) {
    const item = document.createElement("span");
    item.className = "constellation-legend-item";
    const swatch = document.createElement("span");
    swatch.className = "constellation-legend-swatch";
    swatch.style.backgroundColor = constellationStateColors[state];
    item.append(swatch);
    appendText(item, "span", `Expected state ${state}`);
    legend.append(item);
  }
  return legend;
}

function assertConstellationSemantics(stream) {
  if (
    !String(stream.evidence_analysis_id || "").startsWith("slqam_")
    || Number(stream.original_point_count) !== 2400
    || !(stream.display_points || []).length
    || (stream.display_points || []).length > 2400
    || (stream.subcarriers || []).length !== 8
    || !["all-canonical-points", "deterministic-even-index-thinning"].includes(stream.display_point_selection)
  ) {
    throw new Error("Dashboard returned unsafe pilot-constellation semantics");
  }
}

function renderConstellationStream(stream, payload) {
  assertConstellationSemantics(stream);
  const card = document.createElement("article");
  card.className = "constellation-card";
  const heading = document.createElement("div");
  heading.className = "decision-heading";
  appendText(heading, "h3", `${stream.segment_id} · ${stream.receiver_chain_id} · ${stream.edge} edge`);
  appendText(heading, "span", "Candidate only", "status-badge").dataset.tone = "warning";
  card.append(heading);

  const layout = document.createElement("div");
  layout.className = "constellation-layout";
  const figure = document.createElement("figure");
  figure.className = "constellation-figure";
  const canvasWrap = document.createElement("div");
  canvasWrap.className = "constellation-canvas-wrap";
  const canvas = document.createElement("canvas");
  canvas.className = "constellation-canvas";
  canvas.width = 720;
  canvas.height = 720;
  canvas.setAttribute("role", "img");
  canvasWrap.append(canvas);
  figure.append(canvasWrap);
  const caption = document.createElement("figcaption");
  caption.append(constellationLegend());
  appendText(
    caption,
    "span",
    `${(stream.display_points || []).length} bounded, equalized coefficients · crosses mark ideal rotated-QPSK states · incorrect hard decisions are drawn larger`,
  );
  figure.append(caption);
  layout.append(figure);

  const facts = document.createElement("dl");
  facts.className = "fact-grid constellation-facts";
  replaceFacts(facts, [
    ["Analysis product", artifactText(payload.analysis_ref)],
    ["Source detector suite", artifactText(payload.source_suite_ref)],
    ["Evidence", `${stream.evidence_analysis_id} · ${digestText(stream.evidence_digest)}`],
    ["Scope", "Known published Qin synchronization pilot · not payload · candidate only"],
    ["Hard-symbol accuracy", formatPercent(stream.hard_symbol_accuracy)],
    ["RMS EVM", Number(stream.rms_evm).toFixed(6)],
    ["Model SNR diagnostic", `${Number(stream.model_snr_db).toFixed(3)} dB`],
    ["Mean confidence", formatPercent(stream.soft_mean_confidence)],
    ["Mean entropy", `${Number(stream.soft_mean_entropy_bits).toFixed(4)} bits / 2`],
    ["Residual CFO refinement", formatHz(Number(stream.residual_cfo_refinement_hz))],
    ["Frame support", `${stream.complete_frame_count} complete frames`],
    ["Point coverage", `${(stream.display_points || []).length} shown of ${stream.original_point_count} known-pilot observations`],
    ["Display selection", stream.display_point_selection],
  ]);
  layout.append(facts);
  card.append(layout);
  drawConstellation(canvas, stream);

  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap constellation-subcarrier-table";
  const table = document.createElement("table");
  const captionNode = document.createElement("caption");
  captionNode.textContent = "Per-subcarrier known-pilot equalization facts";
  table.append(captionNode);
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Subcarrier", "Offset", "Accuracy", "RMS EVM", "Channel magnitude", "Channel phase"]) {
    appendText(headRow, "th", label).scope = "col";
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  for (const subcarrier of stream.subcarriers || []) {
    const row = document.createElement("tr");
    appendText(row, "th", subcarrier.subcarrier_index).scope = "row";
    appendText(row, "td", formatHz(Number(subcarrier.offset_from_edge_center_hz)));
    appendText(row, "td", formatPercent(subcarrier.hard_symbol_accuracy));
    appendText(row, "td", Number(subcarrier.rms_evm).toFixed(6));
    appendText(row, "td", Number(subcarrier.channel_magnitude).toFixed(6));
    appendText(row, "td", `${Number(subcarrier.channel_phase_deg).toFixed(3)}°`);
    body.append(row);
  }
  table.append(body);
  tableWrap.append(table);
  card.append(tableWrap);
  return card;
}

function renderConstellationPayload(payload) {
  if (payload.schema?.schema_id !== "org.leo-flow.dashboard.recording-starlink-pilot-constellation") {
    throw new Error("Dashboard returned an unsupported pilot-constellation view");
  }
  const streams = payload.streams || [];
  updateConstellationOptions(streams);
  if (streams.length === 0) {
    byId("constellation-warning").hidden = true;
    setState("constellation-state", "unavailable", "No published edge-pilot constellation matches the selected segment, receiver, and edge.");
    return;
  }
  for (const stream of streams) assertConstellationSemantics(stream);
  const container = byId("constellation-streams");
  for (const stream of streams) container.append(renderConstellationStream(stream, payload));
  byId("constellation-warning").hidden = false;
  const truncation = payload.truncated
    ? " The bounded response is truncated; select a segment, receiver, or edge to inspect a specific stream."
    : "";
  setState(
    "constellation-state",
    "ready",
    `${streams.length} published edge-pilot candidate constellation${streams.length === 1 ? "" : "s"} loaded.${truncation}`,
  );
}

async function loadPilotConstellation(recordingId) {
  constellationRecordingId = recordingId;
  const requestSequence = ++constellationRequestSequence;
  resetConstellationPresentation();
  try {
    const query = constellationQueryString();
    const payload = await fetchJson(`/api/v11/recordings/${encodeURIComponent(recordingId)}/starlink-pilot-constellation?${query}`);
    if (requestSequence !== constellationRequestSequence) return;
    renderConstellationPayload(payload);
  } catch (error) {
    if (requestSequence !== constellationRequestSequence) return;
    if (error?.status === 404) {
      setState("constellation-state", "unavailable", "Published edge-pilot constellation evidence is unavailable for this recording.");
      return;
    }
    setState("constellation-state", "error", `Published edge-pilot constellation evidence failed: ${safeError(error)}`);
  }
}

function temporalQueryString() {
  const parameters = new URLSearchParams();
  parameters.set("methods", byId("temporal-method").value);
  const mappings = [
    ["temporal-radio", "radio_ids"],
    ["temporal-receiver", "receiver_chain_ids"],
    ["temporal-edge", "edges"],
  ];
  for (const [id, name] of mappings) {
    const value = byId(id).value;
    if (value) parameters.set(name, value);
  }
  parameters.set("maximum_points", "1024");
  return parameters.toString();
}

function temporalOptions(streams) {
  const definitions = [
    ["temporal-radio", streams.map((item) => item.radio_id)],
    ["temporal-receiver", streams.map((item) => item.receiver_chain_id)],
    ["temporal-edge", streams.map((item) => item.edge)],
  ];
  for (const [id, values] of definitions) {
    const select = byId(id);
    const previous = select.value;
    const label = select.options[0].textContent;
    select.replaceChildren(new Option(label, ""));
    for (const value of [...new Set(values)].sort()) select.append(new Option(value, value));
    select.value = values.includes(previous) ? previous : (values[0] || "");
  }
}

function temporalTooltip(point, stream) {
  const start = point.start_sample / stream.sample_rate_hz;
  const stop = point.stop_sample / stream.sample_rate_hz;
  const center = point.center_sample / stream.sample_rate_hz;
  const surrogates = (point.surrogates || []).map((item) => Number(item.winner.score).toFixed(6)).join(", ");
  return `Window ${point.probe_index + 1}: ${start.toFixed(6)}–${stop.toFixed(6)} s (center ${center.toFixed(6)} s) · Qin ${Number(point.qin.score).toFixed(6)} · surrogates [${surrogates}] · finite rank ${point.finite_upper_tail_rank}/${(point.surrogates || []).length + 1} · margin ${Number(point.qin_minus_max_surrogate).toFixed(6)} · winner epoch ${point.qin.winning_epoch_sample}, coarse CFO ${Number(point.qin.winning_coarse_cfo_hz).toFixed(3)} Hz, residual CFO ${Number(point.qin.winning_residual_cfo_hz).toFixed(3)} Hz`;
}

function drawTemporalChart() {
  if (!temporalPayload?.streams?.length) return;
  const radio = byId("temporal-radio").value;
  const receiver = byId("temporal-receiver").value;
  const edge = byId("temporal-edge").value;
  const stream = temporalPayload.streams.find((item) =>
    (!radio || item.radio_id === radio) &&
    (!receiver || item.receiver_chain_id === receiver) &&
    (!edge || item.edge === edge)
  ) || temporalPayload.streams[0];
  const points = stream.points || [];
  temporalChartPoints = points;
  const canvas = byId("temporal-chart");
  canvas.tabIndex = 0;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const margin = {left: 76, right: 28, top: 28, bottom: 58};
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const duration = stream.segment_sample_count / stream.sample_rate_hz;
  const x = (point) => margin.left + (point.center_sample / stream.sample_rate_hz / duration) * plotWidth;
  const y = (score) => margin.top + (1 - Number(score)) * plotHeight;
  context.fillStyle = "#050907";
  context.fillRect(0, 0, width, height);
  context.strokeStyle = "#2b3a34";
  context.fillStyle = "#94a59c";
  context.font = "22px ui-monospace, monospace";
  context.lineWidth = 2;
  for (let score = 0; score <= 1.0001; score += 0.25) {
    const yy = y(score);
    context.beginPath(); context.moveTo(margin.left, yy); context.lineTo(width - margin.right, yy); context.stroke();
    context.fillText(score.toFixed(2), 10, yy + 7);
  }
  for (let fraction = 0; fraction <= 1.0001; fraction += 0.25) {
    const seconds = fraction * duration;
    const xx = margin.left + fraction * plotWidth;
    context.fillText(`${seconds.toFixed(1)} s`, xx - 24, height - 18);
  }
  const display = byId("temporal-surrogate").value;
  if (display === "band") {
    context.fillStyle = "rgba(255, 212, 121, 0.20)";
    context.beginPath();
    points.forEach((point, index) => {
      const high = Math.max(...point.surrogates.map((item) => item.winner.score));
      if (index === 0) context.moveTo(x(point), y(high)); else context.lineTo(x(point), y(high));
    });
    [...points].reverse().forEach((point) => {
      const low = Math.min(...point.surrogates.map((item) => item.winner.score));
      context.lineTo(x(point), y(low));
    });
    context.closePath(); context.fill();
  } else {
    const indexes = display === "all" ? [0, 1, 2, 3] : [Number(display)];
    for (const index of indexes) {
      context.strokeStyle = ["#ffd479", "#d9a6ff", "#75cfff", "#ff9f91"][index];
      context.setLineDash([9, 7]); context.beginPath();
      points.forEach((point, pointIndex) => {
        const item = point.surrogates[index];
        if (!item) return;
        if (pointIndex === 0) context.moveTo(x(point), y(item.winner.score)); else context.lineTo(x(point), y(item.winner.score));
      });
      context.stroke();
    }
  }
  context.setLineDash([]); context.strokeStyle = "#b6ffd2"; context.lineWidth = 4; context.beginPath();
  points.forEach((point, index) => {
    if (index === 0) context.moveTo(x(point), y(point.qin.score)); else context.lineTo(x(point), y(point.qin.score));
  });
  context.stroke();
  context.fillStyle = "#b6ffd2";
  points.forEach((point) => { context.beginPath(); context.arc(x(point), y(point.qin.score), 7, 0, Math.PI * 2); context.fill(); });
  canvas._temporalGeometry = {stream, x, y};
}

function renderTemporalPayload(payload) {
  if (payload.schema?.schema_id !== "org.leo-flow.dashboard.recording-starlink-temporal-pilot") throw new Error("Dashboard returned an unsupported temporal pilot view");
  temporalPayload = payload;
  const streams = payload.streams || [];
  if (!streams.length) {
    byId("temporal-warning").hidden = true;
    byId("temporal-summary").hidden = true;
    setState("temporal-state", "unavailable", "No stratified temporal evidence matches the selected radio, receiver, edge, and method.");
    return;
  }
  temporalOptions(streams);
  const stream = streams[0];
  const plan = payload.plan;
  const summary = stream.dwell_summaries?.[0];
  replaceFacts(byId("temporal-facts"), [
    ["Stream (never pooled)", `${stream.radio_id} · ${stream.receiver_chain_id} · ${stream.edge}`],
    ["Sampling", `${stream.points.length} visible probes across ${(stream.segment_sample_count / stream.sample_rate_hz).toFixed(3)} s dwell`],
    ["Window / nominal stride", `${(plan.window_sample_count / stream.sample_rate_hz * 1000).toFixed(3)} ms / ${(plan.nominal_stride_samples / stream.sample_rate_hz).toFixed(3)} s`],
    ["Window overlap", `${(Math.max(0, 1 - plan.nominal_stride_samples / plan.window_sample_count) * 100).toFixed(3)}% · overlapping points are dependent`],
    ["Analyzed union coverage", `${(stream.analyzed_sample_count / stream.sample_rate_hz * 1000).toFixed(3)} ms · ${(stream.coverage_fraction * 100).toFixed(4)}%`],
    ["Sampled-dwell Qin maximum", summary ? Number(summary.qin_maximum).toFixed(6) : "Unavailable"],
    ["Sampled-dwell finite rank", summary ? `${summary.finite_upper_tail_rank}/${summary.surrogate_maxima.length + 1}` : "Unavailable"],
    ["Candidate occupancy", summary ? `${summary.candidate_window_count}/${summary.probe_count} (${(summary.candidate_window_count / summary.probe_count * 100).toFixed(2)}%) · descriptive only` : "Unavailable"],
    ["Response selection", payload.truncated ? `${payload.original_point_count} original points · extrema-preserving decimation` : `${payload.original_point_count} points · no decimation`],
  ]);
  byId("temporal-warning").hidden = false;
  byId("temporal-summary").hidden = false;
  setState("temporal-state", "ready", `${streams.length} independent stream trace${streams.length === 1 ? "" : "s"} loaded; the plot displays one selected radio/RX/edge only.`);
  drawTemporalChart();
}

async function loadTemporalPilot(recordingId) {
  temporalRecordingId = recordingId;
  const sequence = ++temporalRequestSequence;
  setState("temporal-state", "pending", "Loading stratified temporal candidate evidence…");
  byId("temporal-summary").hidden = true;
  try {
    const payload = await fetchJson(`/api/v13/recordings/${encodeURIComponent(recordingId)}/starlink-temporal-pilot?${temporalQueryString()}`);
    if (sequence !== temporalRequestSequence) return;
    renderTemporalPayload(payload);
  } catch (error) {
    if (sequence !== temporalRequestSequence) return;
    if (error?.status === 404) {
      setState("temporal-state", "unavailable", "Stratified temporal evidence is not yet available for this recording.");
      return;
    }
    setState("temporal-state", "error", `Temporal pilot evidence failed: ${safeError(error)}`);
  }
}

let extendedAnalysisStarted = false;

function loadExtendedRecordingAnalysis(recordingId) {
  if (extendedAnalysisStarted) return;
  extendedAnalysisStarted = true;
  void Promise.all([
    loadWaterfall(recordingId),
    loadDopplerVisualization(recordingId),
    loadSurrogateNull(recordingId),
    loadPilotConstellation(recordingId),
    loadTemporalPilot(recordingId),
    loadStarlinkDecision(recordingId),
    loadAllFeatures(recordingId),
  ]);
}

async function start() {
  const recordingId = recordingIdFromPath();
  if (!recordingId) {
    byId("capture-page-state").dataset.state = "error";
    byId("capture-page-state-text").textContent = "Invalid recording URL";
    return;
  }
  byId("capture-identity").textContent = recordingId;
  for (const [id, message] of [
    ["waterfall-state", "Waterfall projection is available through Load extended analysis."],
    ["doppler-state", "Doppler visualization is available through Load extended analysis."],
    ["surrogate-state", "Surrogate-null evidence is available through Load extended analysis."],
    ["constellation-state", "Pilot constellation evidence is available through Load extended analysis."],
    ["temporal-state", "Temporal pilot evidence is available through Load extended analysis."],
    ["analysis-state", "Diagnostic features are available through Load extended analysis."],
  ]) setState(id, "pending", message);
  byId("starlink-decision-badge").textContent = "On demand";
  byId("diagnostic-features-count").textContent = "On demand";
  document.addEventListener(
    "leo:load-extended-recording-analysis",
    () => loadExtendedRecordingAnalysis(recordingId),
    {once: true},
  );
  const detail = await loadCaptureDetail(recordingId);
  byId("capture-page-state").dataset.state = detail ? "ready" : "error";
  byId("capture-page-state-text").textContent = detail
    ? "Capture projection loaded"
    : "Capture projection unavailable";
}

byId("waterfall-tile").addEventListener("change", (event) => {
  selectWaterfallTile(Number(event.target.value));
});

byId("doppler-tile").addEventListener("change", renderSelectedDopplerTile);
byId("doppler-layer").addEventListener("change", () => {
  if (dopplerRecordingId) loadDopplerVisualization(dopplerRecordingId);
});
byId("doppler-overlays").addEventListener("change", drawDopplerVisualization);

for (const id of ["surrogate-method", "surrogate-radio", "surrogate-channel", "surrogate-edge"]) {
  byId(id).addEventListener("change", () => {
    if (surrogateRecordingId) loadSurrogateNull(surrogateRecordingId);
  });
}
byId("surrogate-apply-time").addEventListener("click", () => {
  if (surrogateRecordingId) loadSurrogateNull(surrogateRecordingId);
});

for (const id of ["constellation-segment", "constellation-receiver", "constellation-edge"]) {
  byId(id).addEventListener("change", () => {
    if (constellationRecordingId) loadPilotConstellation(constellationRecordingId);
  });
}

for (const id of ["temporal-method", "temporal-radio", "temporal-receiver", "temporal-edge"]) {
  byId(id).addEventListener("change", () => {
    if (temporalRecordingId) loadTemporalPilot(temporalRecordingId);
  });
}
byId("temporal-surrogate").addEventListener("change", drawTemporalChart);
byId("temporal-chart").addEventListener("mousemove", (event) => {
  const canvas = event.currentTarget;
  const geometry = canvas._temporalGeometry;
  if (!geometry || temporalChartPoints.length === 0) return;
  const bounds = canvas.getBoundingClientRect();
  const px = (event.clientX - bounds.left) * canvas.width / bounds.width;
  let best = 0;
  for (let index = 1; index < temporalChartPoints.length; index += 1) {
    if (Math.abs(geometry.x(temporalChartPoints[index]) - px) < Math.abs(geometry.x(temporalChartPoints[best]) - px)) best = index;
  }
  temporalFocusedPoint = best;
  byId("temporal-tooltip").textContent = temporalTooltip(temporalChartPoints[best], geometry.stream);
});
byId("temporal-chart").addEventListener("keydown", (event) => {
  const geometry = event.currentTarget._temporalGeometry;
  if (!geometry || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  temporalFocusedPoint = Math.max(0, Math.min(temporalChartPoints.length - 1, temporalFocusedPoint + (event.key === "ArrowRight" ? 1 : -1)));
  byId("temporal-tooltip").textContent = temporalTooltip(temporalChartPoints[temporalFocusedPoint], geometry.stream);
});

byId("diagnostic-features").addEventListener("toggle", (event) => {
  const expanded = event.currentTarget.open;
  byId("diagnostic-features-toggle").setAttribute("aria-expanded", String(expanded));
  byId("diagnostic-features-label").textContent = expanded
    ? "Hide diagnostic features"
    : "Show diagnostic features";
});

start();

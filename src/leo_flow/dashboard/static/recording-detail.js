"use strict";

const byId = (id) => document.getElementById(id);
let waterfallTiles = [];

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

async function start() {
  const recordingId = recordingIdFromPath();
  if (!recordingId) {
    byId("capture-page-state").dataset.state = "error";
    byId("capture-page-state-text").textContent = "Invalid recording URL";
    return;
  }
  byId("capture-identity").textContent = recordingId;
  const [detail] = await Promise.all([
    loadCaptureDetail(recordingId),
    loadWaterfall(recordingId),
    loadStarlinkDecision(recordingId),
    loadAllFeatures(recordingId),
  ]);
  byId("capture-page-state").dataset.state = detail ? "ready" : "error";
  byId("capture-page-state-text").textContent = detail
    ? "Capture projection loaded"
    : "Capture projection unavailable";
}

byId("waterfall-tile").addEventListener("change", (event) => {
  selectWaterfallTile(Number(event.target.value));
});

byId("diagnostic-features").addEventListener("toggle", (event) => {
  const expanded = event.currentTarget.open;
  byId("diagnostic-features-toggle").setAttribute("aria-expanded", String(expanded));
  byId("diagnostic-features-label").textContent = expanded
    ? "Hide diagnostic features"
    : "Show diagnostic features";
});

start();

"use strict";

(() => {
  const node = (id) => document.getElementById(id);
  if (!node("evidence-workspace")) return;

  const recordingId = (() => {
    const parts = window.location.pathname.split("/").filter(Boolean);
    if (parts.length !== 2 || parts[0] !== "recordings") return null;
    try { return decodeURIComponent(parts[1]); } catch (_error) { return null; }
  })();
  if (!recordingId) return;

  let context = null;
  let generation = 0;
  let extendedLoaded = false;
  const analysisProducts = new Map();
  const approachRows = new Map();
  const colors = ["#80d8ff", "#fff176", "#ff8a80", "#69f0ae", "#ce93d8", "#ffb74d", "#90caf9", "#a5d6a7"];

  function state(product, value, message) {
    const target = node(`evidence-${product}-state`);
    target.dataset.state = value;
    target.textContent = message;
    const badge = node(`evidence-${product}-badge`);
    if (badge) {
      badge.textContent = value === "ready" ? (product === "timeline" ? "Full coverage" : product === "approaches" ? "Exact product facts" : "Candidate evidence") : value;
      badge.dataset.tone = value === "error" ? "error" : "warning";
    }
  }

  async function json(path) {
    const response = await fetch(path, {headers: {accept: "application/json"}, credentials: "same-origin"});
    let body = {};
    try { body = await response.json(); } catch (_error) { /* handled below */ }
    if (!response.ok) {
      const error = new Error(body?.error?.message || `request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  const productTargets = {
    approaches: "approaches",
    full_dwell_timeline: "timeline",
    pilot_prescreen: "prescreen",
    qam: "qam",
    adaptive_detector_response: "detector",
    pilot_refinement: "detector",
    legacy_full_dwell: "detector",
    basic_doppler: "doppler",
    advanced_doppler: "doppler",
    pilot_doppler_association: "pilot-doppler",
  };

  function exposeProductState(product, value) {
    const target = node(`evidence-${productTargets[product]}-state`);
    if (target) target.dataset.productState = value;
  }

  function productPayload(product, acceptedSources = []) {
    const envelope = analysisProducts.get(product);
    if (!envelope) return {payload: null, missing: true};
    exposeProductState(product, envelope.state);
    if (envelope.state !== "complete") return {payload: null, missing: true};
    if (acceptedSources.length && !acceptedSources.includes(envelope.source)) return {payload: null, missing: true};
    return {payload: envelope.payload, missing: false};
  }

  function availablePayloads(products) {
    const results = products.map((product) => productPayload(product));
    return {
      payloads: results.flatMap((item) => item.payload ? [item.payload] : []),
      missingCount: results.filter((item) => item.missing).length,
    };
  }

  function preferredPayload(product, sources) {
    const result = productPayload(product, sources);
    return {payload: result.payload, path: result.payload ? analysisProducts.get(product).source : null};
  }

  function facadeParameters(sections) {
    const parameters = new URLSearchParams({sections: sections.join(","), mode: node("evidence-mode").value});
    if (context) queryFilters(parameters);
    parameters.set("channel_numbers", checked("channel").join(","));
    parameters.set("methods", checked("method").join(","));
    parameters.set("qam_maximum_streams", "4");
    parameters.set("qam_maximum_windows", "32");
    parameters.set("qam_maximum_points", "128");
    parameters.set("doppler_maximum_windows", "4096");
    parameters.set("timeline_maximum_windows", "16384");
    parameters.set("maximum_points", "4096");
    return parameters;
  }

  async function fetchAnalysis(sections) {
    const payload = await json(`/api/recordings/${encodeURIComponent(recordingId)}/analysis?${facadeParameters(sections)}`);
    const schema = payload.schema;
    const schemaId = typeof schema === "string" ? schema : schema?.schema_id;
    if (schemaId !== "org.leo-flow.dashboard.recording-analysis-facade" || payload.recording_id !== recordingId) throw new Error("invalid recording-analysis facade response");
    for (const section of payload.sections || []) {
      for (const envelope of section.products || []) {
        if (!["complete", "no_candidate", "pending", "failed", "not_analyzed"].includes(envelope.state)) throw new Error(`invalid ${envelope.product} availability state`);
        analysisProducts.set(envelope.product, envelope);
        exposeProductState(envelope.product, envelope.state);
      }
    }
    document.dispatchEvent(new CustomEvent("leo:recording-analysis", {detail: payload}));
    return payload;
  }

  function checked(name) {
    return [...document.querySelectorAll(`#evidence-controls input[name="${name}"]:checked`)].map((item) => item.value);
  }

  function addChecks(fieldset, name, values, label) {
    fieldset.querySelectorAll("label").forEach((item) => item.remove());
    for (const value of values) {
      const wrapper = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = name;
      input.value = value.value;
      input.checked = true;
      wrapper.append(input, document.createTextNode(` ${label(value)}`));
      fieldset.append(wrapper);
    }
  }

  function assignment(recording, receiver) {
    return (context?.receivers || []).find(
      (item) => item.recording_id === recording && item.receiver_chain_id === receiver,
    ) || null;
  }

  function selectedRecordings() {
    const radios = new Set(checked("radio"));
    return (context?.recordings || []).filter((item) => radios.has(item.recording_id));
  }

  function identity(parts) {
    return parts.filter((item) => item !== undefined && item !== null && item !== "").join(" · ");
  }

  function duration(samples, rate) {
    const seconds = Number(samples) / Number(rate);
    return seconds < 1 ? `${(seconds * 1000).toFixed(3)} ms` : `${seconds.toFixed(3)} s`;
  }

  function percent(value) { return `${(100 * Number(value)).toFixed(4)}%`; }

  function intervalUnionSampleCount(intervals) {
    const ordered = intervals
      .map(([start, stop]) => [Number(start), Number(stop)])
      .filter(([start, stop]) => Number.isFinite(start) && Number.isFinite(stop) && start >= 0 && stop > start)
      .sort((left, right) => left[0] - right[0] || left[1] - right[1]);
    if (!ordered.length) return 0;
    let total = 0;
    let [start, stop] = ordered[0];
    for (const [nextStart, nextStop] of ordered.slice(1)) {
      if (nextStart <= stop) stop = Math.max(stop, nextStop);
      else { total += stop - start; start = nextStart; stop = nextStop; }
    }
    return total + stop - start;
  }

  function setApproachRows(kind, rows) {
    for (const key of [...approachRows.keys()]) if (key.startsWith(`${kind}:`)) approachRows.delete(key);
    rows.forEach((row, index) => approachRows.set(`${kind}:${row.key || index}`, row));
    renderApproachRows();
  }

  function renderApproachRows() {
    const body = node("evidence-approaches-body");
    body.replaceChildren();
    const rows = [...approachRows.values()].sort((left, right) => `${left.approach}:${left.scope}`.localeCompare(`${right.approach}:${right.scope}`));
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.dataset.approach = row.kind;
      for (const value of [row.approach, row.scope, row.window, row.coverage, row.search, row.response, row.status]) {
        const td = document.createElement("td"); td.textContent = value; tr.append(td);
      }
      body.append(tr);
    }
    state("approaches", rows.length ? "ready" : "pending", rows.length ? `${rows.length} exact product-plan row(s). No historical LNB-label correction is inferred.` : "Exact persisted search and windowing facts are pending.");
  }

  async function loadApproaches(current) {
    try {
      const fetched = availablePayloads(["approaches"]);
      if (current !== generation) return;
      const rows = [];
      for (const payload of fetched.payloads) {
        if (payload.candidate_only !== true || payload.calibration_required !== true) throw new Error("unsafe analysis-approach semantics");
        for (const stream of payload.qam_streams || []) rows.push({
          kind: "qam",
          key: identity([payload.recording_id, stream.radio_id, stream.receiver_chain_id, stream.edge]),
          approach: "Acquired pilot QAM v0.3",
          scope: identity([payload.recording_id, stream.radio_id, stream.lnb_id, stream.receiver_chain_id, stream.edge]),
          window: `${stream.window_count} × ${duration(stream.window_sample_count, stream.sample_rate_hz)}; ${stream.sampling_plan}`,
          coverage: `${duration(stream.analyzed_union_sample_count, stream.sample_rate_hz)} / ${duration(stream.segment_sample_count, stream.sample_rate_hz)} (${percent(stream.analyzed_union_fraction)})`,
          search: `${(Number(stream.searched_cfo_min_hz) / 1000).toFixed(1)}…${(Number(stream.searched_cfo_max_hz) / 1000).toFixed(1)} kHz physical CFO; ${Number(stream.coarse_search_cell_count).toLocaleString()} coarse + ${Number(stream.refinement_search_cell_count).toLocaleString()} refinement cells; ${stream.hardware_calibration_state}; profile ${stream.receiver_cfo_profile_ids.join(", ")}`,
          response: `known-pilot constellation, accuracy, EVM, goodness; winning CFO ${(Number(stream.winning_cfo_min_hz) / 1000).toFixed(1)}…${(Number(stream.winning_cfo_max_hz) / 1000).toFixed(1)} kHz`,
          status: `${stream.overall_derivation}; ${stream.retained_candidate_count} retained basins; candidate-only; whole-search calibration required`,
        });
      }
      if (!rows.length && fetched.missingCount) rows.push({kind: "qam", key: "pending", approach: "Acquired pilot QAM v0.3", scope: "selected recording(s)", window: "pending", coverage: "pending", search: "pending", response: "QAM evidence pending", status: "not yet published"});
      setApproachRows("qam", rows);
    } catch (error) {
      if (current !== generation) return;
      setApproachRows("qam", [{kind: "qam", key: "error", approach: "Acquired pilot QAM v0.3", scope: "selected recording(s)", window: "unavailable", coverage: "unavailable", search: "unavailable", response: "QAM approach query failed", status: error.message}]);
    }
  }

  async function loadAdaptiveQamApproaches(current) {
    try {
      const fetched = availablePayloads(["qam"]);
      if (current !== generation) return;
      const rows = [];
      for (const payload of fetched.payloads) {
        if (payload.candidate_only !== true || payload.calibration_required !== true || !payload.source_adaptive_response_ref) throw new Error("unsafe adaptive-QAM approach semantics");
        for (const stream of payload.streams || []) {
          const windows = stream.windows || [];
          if (!windows.length) continue;
          const qamIntervals = windows.map((item) => [item.selection.qam_start_sample, item.selection.qam_stop_sample]);
          const sourceIntervals = windows.map((item) => [item.selection.source_start_sample, item.selection.source_stop_sample]);
          const qamUnion = intervalUnionSampleCount(qamIntervals);
          const sourceUnion = intervalUnionSampleCount(sourceIntervals);
          const windowSizes = [...new Set(qamIntervals.map(([start, stop]) => Number(stop) - Number(start)))].sort((left, right) => left - right);
          const sourceWindowSizes = [...new Set(sourceIntervals.map(([start, stop]) => Number(stop) - Number(start)))].sort((left, right) => left - right);
          const reasons = [...new Set(windows.flatMap((item) => item.selection.reasons || []))].sort();
          const winners = windows.map((item) => Number(item.qam.winning_cfo_hz)).filter(Number.isFinite);
          const winnerSpan = winners.length ? `${(Math.min(...winners) / 1000).toFixed(1)}…${(Math.max(...winners) / 1000).toFixed(1)} kHz` : "winner CFO unavailable";
          const rate = Number(stream.sample_rate_hz);
          const segmentSamples = Number(stream.segment_sample_count);
          const originalCount = Number(stream.original_window_count || windows.length);
          rows.push({
            kind: "qam-adaptive",
            key: identity([payload.recording_id, stream.radio_id, stream.receiver_chain_id, stream.edge]),
            approach: "Adaptive target/control QAM v0.4",
            scope: identity([payload.recording_id, stream.radio_id, stream.lnb_id, stream.receiver_chain_id, stream.edge]),
            window: `${originalCount} × ${windowSizes.map((value) => duration(value, rate)).join("/")} QAM around ${sourceWindowSizes.map((value) => duration(value, rate)).join("/")} exact detector windows`,
            coverage: `${duration(qamUnion, rate)} / ${duration(segmentSamples, rate)} (${percent(qamUnion / segmentSamples)}); source exact union ${duration(sourceUnion, rate)}`,
            search: `each selected window repeats the persisted label-independent time×epoch×CFO search; winner CFO ${winnerSpan}; source ${payload.source_adaptive_response_ref.artifact_id}`,
            response: `per-window known-pilot QAM plus source Qin/control/margin; selection reasons ${reasons.join(", ")}`,
            status: `${stream.overall.derivation}; target/control selection bias disclosed; time×epoch×CFO maximum requires calibration`,
          });
        }
      }
      if (!rows.length && fetched.missingCount) rows.push({kind: "qam-adaptive", key: "pending", approach: "Adaptive target/control QAM v0.4", scope: "selected recording(s)", window: "pending", coverage: "pending", search: "pending", response: "adaptive QAM evidence pending; v0.3 fallback may be shown", status: "not yet published"});
      setApproachRows("qam-adaptive", rows);
    } catch (error) {
      if (current !== generation) return;
      setApproachRows("qam-adaptive", [{kind: "qam-adaptive", key: "error", approach: "Adaptive target/control QAM v0.4", scope: "selected recording(s)", window: "unavailable", coverage: "unavailable", search: "unavailable", response: "adaptive QAM approach query failed", status: error.message}]);
    }
  }

  function seriesLegend(targetId, series) {
    const target = node(targetId);
    target.replaceChildren();
    series.forEach((item, index) => {
      const key = document.createElement("span");
      key.className = "evidence-series-key";
      const swatch = document.createElement("span");
      swatch.className = "evidence-series-swatch";
      swatch.style.backgroundColor = colors[index % colors.length];
      key.append(swatch, document.createTextNode(item.label));
      target.append(key);
    });
  }

  function drawChart(canvasId, series, yLabel, mode, pointLabel) {
    const canvas = node(canvasId);
    const ctx = canvas.getContext("2d");
    const points = series.flatMap((item) => item.points.map((point) => ({...point, series: item})));
    if (!ctx || !points.length) {
      canvas.hidden = true;
      return;
    }
    canvas.hidden = false;
    const pad = {left: 76, right: 22, top: 24, bottom: 52};
    const width = canvas.width - pad.left - pad.right;
    const height = canvas.height - pad.top - pad.bottom;
    const xs = points.map((item) => Number(item.x));
    const ys = points.map((item) => Number(item.y));
    let xmin = Math.min(...xs); let xmax = Math.max(...xs);
    let ymin = Math.min(...ys); let ymax = Math.max(...ys);
    if (xmin === xmax) { xmin -= 0.5; xmax += 0.5; }
    if (ymin === ymax) { ymin -= Math.max(1, Math.abs(ymin) * 0.1); ymax += Math.max(1, Math.abs(ymax) * 0.1); }
    const margin = (ymax - ymin) * 0.08;
    ymin -= margin; ymax += margin;
    const px = (value) => pad.left + ((value - xmin) / (xmax - xmin)) * width;
    const py = (value) => pad.top + ((ymax - value) / (ymax - ymin)) * height;
    ctx.fillStyle = "#050907"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#294137"; ctx.lineWidth = 1;
    for (let index = 0; index <= 4; index += 1) {
      const y = pad.top + (index / 4) * height;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(canvas.width - pad.right, y); ctx.stroke();
      const value = ymax - (index / 4) * (ymax - ymin);
      ctx.fillStyle = "#9eaaa5"; ctx.font = "13px ui-monospace, monospace"; ctx.fillText(value.toPrecision(4), 5, y + 4);
    }
    series.forEach((item, index) => {
      const color = colors[index % colors.length];
      const ordered = [...item.points].sort((a, b) => a.x - b.x);
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.6;
      if (mode === "windows" && ordered.length > 1) {
        ctx.beginPath(); ordered.forEach((point, offset) => offset ? ctx.lineTo(px(point.x), py(point.y)) : ctx.moveTo(px(point.x), py(point.y))); ctx.stroke();
      }
      ordered.forEach((point) => { ctx.beginPath(); ctx.arc(px(point.x), py(point.y), 3.2, 0, 2 * Math.PI); ctx.fill(); });
    });
    ctx.fillStyle = "#c4d0cb"; ctx.font = "14px ui-monospace, monospace";
    ctx.fillText(yLabel, 8, 16);
    ctx.fillText(mode === "windows" ? "UTC window midpoint" : pointLabel, pad.left, canvas.height - 14);
    canvas.dataset.seriesCount = String(series.length);
    canvas.dataset.pointCount = String(points.length);
    canvas.setAttribute("aria-label", `${yLabel}; ${series.length} unpooled series and ${points.length} ${mode === "windows" ? "window" : "overall"} estimates`);
  }

  function drawQam(series, canvasId = "evidence-qam-canvas", description = "Known pilot QAM candidate evidence") {
    const canvas = node(canvasId);
    const ctx = canvas.getContext("2d");
    const all = series.flatMap((item) => item.points.map((point) => ({point, item})));
    if (!ctx || !all.length) { canvas.hidden = true; return; }
    canvas.hidden = false;
    const pad = 48;
    const extent = Math.max(1.25, ...all.flatMap(({point}) => [Math.abs(Number(point.i)), Math.abs(Number(point.q))])) * 1.08;
    const x = (value) => pad + ((Number(value) + extent) / (2 * extent)) * (canvas.width - 2 * pad);
    const y = (value) => pad + ((extent - Number(value)) / (2 * extent)) * (canvas.height - 2 * pad);
    ctx.fillStyle = "#050907"; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "#294137"; ctx.beginPath(); ctx.moveTo(x(-extent), y(0)); ctx.lineTo(x(extent), y(0)); ctx.moveTo(x(0), y(-extent)); ctx.lineTo(x(0), y(extent)); ctx.stroke();
    series.forEach((item, index) => {
      ctx.fillStyle = colors[index % colors.length];
      for (const point of item.points) { ctx.globalAlpha = 0.62; ctx.beginPath(); ctx.arc(x(point.i), y(point.q), 2.2, 0, 2 * Math.PI); ctx.fill(); }
    });
    ctx.globalAlpha = 1;
    canvas.dataset.seriesCount = String(series.length);
    canvas.dataset.pointCount = String(all.length);
    canvas.setAttribute("aria-label", `${description}; ${series.length} series and ${all.length} coefficients`);
  }

  function idealQamPoint(expectedState) {
    const phase = 0.5 * Math.PI * (Number(expectedState) + 0.5);
    return {i: Math.cos(phase), q: Math.sin(phase)};
  }

  function pointIdentity(point, index) {
    if (Number.isInteger(Number(point.symbol_index)) && Number.isInteger(Number(point.subcarrier_index))) {
      return `${Number(point.symbol_index)}:${Number(point.subcarrier_index)}:${Number(point.expected_state)}`;
    }
    return `display:${index}:${Number(point.expected_state)}`;
  }

  function combinedQamSeries(candidates) {
    const groups = new Map();
    for (const candidate of candidates) {
      const key = [candidate.recordingId, candidate.radioId, candidate.segmentId, candidate.edge, candidate.startUtcNs, candidate.stopUtcNs].join("|");
      const group = groups.get(key) || [];
      group.push(candidate);
      groups.set(key, group);
    }
    const combined = [];
    for (const group of groups.values()) {
      group.sort((left, right) => `${left.lnbId}|${left.receiverId}`.localeCompare(`${right.lnbId}|${right.receiverId}`));
      for (let leftIndex = 0; leftIndex < group.length; leftIndex += 1) {
        for (let rightIndex = leftIndex + 1; rightIndex < group.length; rightIndex += 1) {
          const left = group[leftIndex]; const right = group[rightIndex];
          if (left.receiverId === right.receiverId || left.lnbId === right.lnbId) continue;
          const rightByIdentity = new Map(right.points.map((point, index) => [pointIdentity(point, index), point]));
          const leftWeightRaw = 1 / Math.max(left.rmsEvm * left.rmsEvm, 1e-6);
          const rightWeightRaw = 1 / Math.max(right.rmsEvm * right.rmsEvm, 1e-6);
          const weightSum = leftWeightRaw + rightWeightRaw;
          const leftWeight = leftWeightRaw / weightSum; const rightWeight = rightWeightRaw / weightSum;
          const points = [];
          let correct = 0; let squaredError = 0;
          left.points.forEach((leftPoint, index) => {
            const rightPoint = rightByIdentity.get(pointIdentity(leftPoint, index));
            if (!rightPoint || Number(rightPoint.expected_state) !== Number(leftPoint.expected_state)) return;
            const point = {
              ...leftPoint,
              i: leftWeight * Number(leftPoint.i) + rightWeight * Number(rightPoint.i),
              q: leftWeight * Number(leftPoint.q) + rightWeight * Number(rightPoint.q),
            };
            const ideal = idealQamPoint(point.expected_state);
            squaredError += (point.i - ideal.i) ** 2 + (point.q - ideal.q) ** 2;
            const hardState = [0, 1, 2, 3].reduce((best, stateValue) => {
              const statePoint = idealQamPoint(stateValue);
              const distance = (point.i - statePoint.i) ** 2 + (point.q - statePoint.q) ** 2;
              return distance < best.distance ? {state: stateValue, distance} : best;
            }, {state: 0, distance: Number.POSITIVE_INFINITY}).state;
            if (hardState === Number(point.expected_state)) correct += 1;
            points.push(point);
          });
          if (!points.length) continue;
          const accuracy = correct / points.length;
          const rmsEvm = Math.sqrt(squaredError / points.length);
          const goodness = qamGoodness(accuracy, rmsEvm);
          if (goodness === null) continue;
          const label = identity([left.recordingId, left.radioId, left.edge, `${left.lnbId}/${left.receiverId} + ${right.lnbId}/${right.receiverId}`, "time-matched paired QAM"]);
          combined.push({
            label, points, goodness, accuracy, rmsEvm,
            leftWeight, rightWeight,
            startUtcNs: left.startUtcNs, stopUtcNs: left.stopUtcNs,
          });
        }
      }
    }
    return combined;
  }

  function qamGoodness(accuracy, rmsEvm) {
    const measuredAccuracy = Number(accuracy);
    const measuredEvm = Number(rmsEvm);
    if (!Number.isFinite(measuredAccuracy) || !Number.isFinite(measuredEvm) || measuredEvm < 0) return null;
    const chanceCorrectedAccuracy = Math.max(0, Math.min(1, (measuredAccuracy - 0.25) / 0.75));
    // EVM 2.0 is the diagnostic half-quality point.  This keeps the retained
    // RETRO four-cluster examples in the high band while still driving the
    // chance-like, EVM 18--20 controls close to zero.
    const compactness = 1 / (1 + (measuredEvm / 2) * (measuredEvm / 2));
    return Math.sqrt(chanceCorrectedAccuracy * compactness);
  }

  function qamGoodnessBand(goodness) {
    if (goodness >= 0.7) return "high";
    if (goodness >= 0.35) return "moderate";
    return "low";
  }

  function renderQamGoodness(entries) {
    const target = node("evidence-qam-goodness");
    target.replaceChildren();
    for (const entry of entries) {
      const card = document.createElement("div");
      card.className = "qam-goodness-entry";
      card.dataset.goodness = entry.goodness.toFixed(6);
      card.dataset.goodnessBand = qamGoodnessBand(entry.goodness);
      const label = document.createElement("span");
      label.className = "metric-label";
      label.textContent = entry.label;
      const value = document.createElement("strong");
      value.className = "metric-value";
      value.textContent = entry.goodness.toFixed(3);
      const detail = document.createElement("small");
      detail.textContent = `${qamGoodnessBand(entry.goodness)} · accuracy ${(entry.accuracy * 100).toFixed(2)}% · RMS EVM ${entry.rmsEvm.toFixed(3)}${entry.selection ? ` · ${entry.selection}` : ""}`;
      card.append(label, value, detail);
      target.append(card);
    }
    if (entries.length) {
      const explanation = document.createElement("p");
      explanation.className = "availability-note";
      explanation.textContent = "QAM goodness v0.2: geometric mean of chance-corrected known-symbol accuracy and EVM compactness (EVM 2.0 is half quality). RETRO-like separated constellations rank high; random/collapsed constellations rank low. Diagnostic only—not a calibrated detection.";
      target.append(explanation);
    }
  }

  function queryFilters(parameters) {
    const radios = selectedRecordings().map((item) => item.radio_id);
    const lnbs = checked("lnb");
    const receivers = checked("receiver");
    const edges = checked("edge");
    if (radios.length) parameters.set("radio_ids", [...new Set(radios)].join(","));
    if (lnbs.length) parameters.set("lnb_ids", lnbs.join(","));
    if (receivers.length) parameters.set("receiver_chain_ids", receivers.join(","));
    if (edges.length) parameters.set("edges", edges.join(","));
  }

  async function loadTimeline(current) {
    state("timeline", "pending", "Loading complete contiguous IQ tile timelines…");
    try {
      const recordings = selectedRecordings();
      const receivers = new Set(checked("receiver")); const lnbs = new Set(checked("lnb")); const edges = checked("edge"); const channels = new Set(checked("channel").map(Number));
      if (!recordings.length || !receivers.size || !lnbs.size || !edges.length || !channels.size) {
        node("evidence-timeline-canvas").hidden = true; node("evidence-timeline-legend").replaceChildren(); state("timeline", "missing", "Select at least one radio, LNB, receiver, channel, and edge."); return;
      }
      const fetched = availablePayloads(["full_dwell_timeline"]);
      if (current !== generation) return;
      if (!fetched.payloads.length) {
        node("evidence-timeline-canvas").hidden = true; node("evidence-timeline-legend").replaceChildren(); state("timeline", "pending", "The complete IQ tile timeline is pending for every selected recording."); return;
      }
      const series = []; const approach = []; const widths = new Set(); let original = 0; let returned = 0; let truncated = false;
      for (const payload of fetched.payloads) {
        if (payload.candidate_only !== true || payload.calibrated_detection_count !== null) throw new Error("unsafe full-dwell timeline semantics");
        original += Number(payload.original_window_count || 0); returned += Number(payload.returned_window_count || 0); truncated ||= payload.truncated === true;
        for (const stream of payload.streams || []) {
          const lnb = assignment(payload.recording_id, stream.receiver_chain_id)?.lnb_id;
          if (!receivers.has(stream.receiver_chain_id) || !lnbs.has(lnb) || !channels.has(Number(stream.channel_number)) || !edges.includes(stream.edge)) continue;
          widths.add((1000 * Number(payload.prescreen_window_samples) / Number(stream.sample_rate_hz)).toFixed(3));
          const base = identity([payload.recording_id, stream.radio_id, lnb, stream.receiver_chain_id, `CH${stream.channel_number}`, stream.edge]);
          approach.push({
            kind: "timeline", key: base, approach: "Complete IQ power timeline", scope: base,
            window: `${stream.original_window_count} contiguous × ${duration(payload.prescreen_window_samples, stream.sample_rate_hz)}; stride ${duration(payload.prescreen_stride_samples, stream.sample_rate_hz)}; short tail retained`,
            coverage: `${duration(stream.segment_sample_count, stream.sample_rate_hz)} / ${duration(stream.segment_sample_count, stream.sample_rate_hz)} (${percent(stream.prescreen_coverage_fraction)})`,
            search: "pattern-blind mean complex power; deterministic top-power exact-refinement selection",
            response: "power vs UTC for every raw-IQ tile",
            status: `${percent(stream.exact_coverage_fraction)} selected for exact detector refinement; power is not detection`,
          });
          const points = (stream.windows || []).map((window) => ({x: (Number(window.interval_start_utc_ns) + Number(window.interval_stop_utc_ns)) / 2, y: 10 * Math.log10(Math.max(Number(window.mean_complex_power), 1e-30))}));
          series.push({label: `${base} · every IQ tile`, points});
          const selected = (stream.windows || []).filter((window) => window.selected_for_exact_refinement).map((window) => ({x: (Number(window.interval_start_utc_ns) + Number(window.interval_stop_utc_ns)) / 2, y: 10 * Math.log10(Math.max(Number(window.mean_complex_power), 1e-30))}));
          if (selected.length) series.push({label: `${base} · exact-refinement selection`, points: selected});
        }
      }
      setApproachRows("timeline", approach);
      drawChart("evidence-timeline-canvas", series, "mean complex power [dB arb.]", "windows", "UTC tile midpoint");
      seriesLegend("evidence-timeline-legend", series);
      const partial = fetched.missingCount ? ` ${fetched.missingCount} selected recording(s) remain pending.` : "";
      state("timeline", series.length ? "ready" : "missing", series.length ? `${returned.toLocaleString()} of ${original.toLocaleString()} persisted contiguous tile records returned (${[...widths].join("/")} ms windows); source union coverage is 100%.${truncated ? " Display response was extrema-preserving decimated." : " No display decimation."}${partial}` : "No complete IQ tile timelines match the selected hardware scope.");
    } catch (error) {
      if (current !== generation) return;
      node("evidence-timeline-canvas").hidden = true; node("evidence-timeline-legend").replaceChildren();
      state("timeline", error.status === 404 ? "pending" : "error", error.status === 404 ? "The complete IQ tile timeline is pending or unavailable." : `IQ tile timeline failed: ${error.message}`);
      setApproachRows("timeline", [{kind: "timeline", key: "pending", approach: "Complete IQ power timeline", scope: "selected recording(s)", window: "pending", coverage: "pending", search: "pattern-blind", response: "power vs UTC", status: error.status === 404 ? "not yet published" : error.message}]);
    }
  }

  async function loadPilotPrescreen(current) {
    state("prescreen", "pending", "Loading complete-IQ OFDM periodicity timelines…");
    try {
      const recordings = selectedRecordings();
      const receivers = checked("receiver"); const lnbs = checked("lnb"); const edges = checked("edge"); const channels = new Set(checked("channel").map(Number));
      if (!recordings.length || !receivers.length || !lnbs.length || !edges.length || !channels.size) {
        node("evidence-prescreen-canvas").hidden = true; node("evidence-prescreen-legend").replaceChildren(); state("prescreen", "missing", "Select at least one radio, LNB, receiver, channel, and edge."); return;
      }
      const fetched = availablePayloads(["pilot_prescreen"]);
      if (current !== generation) return;
      if (!fetched.payloads.length) {
        node("evidence-prescreen-canvas").hidden = true; node("evidence-prescreen-legend").replaceChildren(); state("prescreen", "pending", "The complete-IQ OFDM prescreen is pending for every selected recording.");
        setApproachRows("prescreen", [{kind: "prescreen", key: "pending", approach: "Complete-IQ OFDM pilot prescreen", scope: "selected recording(s)", window: "pending", coverage: "pending", search: "pattern-blind cyclic-prefix periodicity", response: "periodicity vs UTC", status: "not yet published"}]); return;
      }
      const series = []; const approach = []; let original = 0; let shown = 0; let truncated = false;
      for (const payload of fetched.payloads) {
        if (payload.candidate_only !== true || payload.calibrated_detection_count !== null) throw new Error("unsafe pilot-prescreen semantics");
        original += Number(payload.original_window_count || 0); truncated ||= payload.truncated === true;
        for (const stream of payload.streams || []) {
          const selection = stream.selection; if (!channels.has(Number(selection.channel_number))) continue;
          const base = identity([payload.recording_id, selection.radio_id, selection.lnb_id, selection.receiver_chain_id, `CH${selection.channel_number}`, selection.edge]);
          const points = (stream.windows || []).map((window) => ({x: (Number(window.start_utc_ns) + Number(window.stop_utc_ns)) / 2, y: Number(window.ofdm_periodicity_score)}));
          shown += points.length; series.push({label: `${base} · every IQ tile`, points});
          const seeds = (stream.windows || []).filter((window) => window.periodicity_rank !== null || window.power_rank !== null).map((window) => ({x: (Number(window.start_utc_ns) + Number(window.stop_utc_ns)) / 2, y: Number(window.ofdm_periodicity_score)}));
          if (seeds.length) series.push({label: `${base} · exact-refinement seeds`, points: seeds});
          approach.push({kind: "prescreen", key: base, approach: "Complete-IQ OFDM pilot prescreen", scope: base, window: `${stream.original_window_count} contiguous × ${duration(payload.plan.tile_sample_count, selection.sample_rate_hz)}; short tail retained`, coverage: `${duration(stream.analyzed_sample_count, selection.sample_rate_hz)} / ${duration(selection.segment_sample_count, selection.sample_rate_hz)} (${percent(stream.coverage_fraction)})`, search: `pattern-blind cyclic-prefix periodicity; top ${payload.plan.maximum_periodicity_seeds_per_stream} periodicity + top ${payload.plan.maximum_power_seeds_per_stream} power seeds`, response: "OFDM periodicity vs UTC; selected exact-search seeds highlighted", status: "100% prescreen coverage; exact Qin/surrogate refinement required; candidate-only"});
        }
      }
      setApproachRows("prescreen", approach);
      drawChart("evidence-prescreen-canvas", series, "OFDM cyclic-prefix periodicity [0–1]", "windows", "UTC tile midpoint");
      seriesLegend("evidence-prescreen-legend", series);
      state("prescreen", series.length ? "ready" : "missing", series.length ? `${shown.toLocaleString()} of ${original.toLocaleString()} contiguous periodicity records shown; every source sample was prescreened.${truncated ? " Display response was seeds/extrema/time decimated." : " No display decimation."}` : "No pilot-prescreen streams match the selected scope.");
    } catch (error) {
      if (current !== generation) return;
      node("evidence-prescreen-canvas").hidden = true; node("evidence-prescreen-legend").replaceChildren(); state("prescreen", error.status === 404 ? "pending" : "error", error.status === 404 ? "The complete-IQ OFDM prescreen is pending." : `Pilot prescreen failed: ${error.message}`);
      setApproachRows("prescreen", [{kind: "prescreen", key: "error", approach: "Complete-IQ OFDM pilot prescreen", scope: "selected recording(s)", window: "unavailable", coverage: "unavailable", search: "pattern-blind cyclic-prefix periodicity", response: "periodicity vs UTC", status: error.status === 404 ? "not yet published" : error.message}]);
    }
  }

  async function loadQam(current) {
    state("qam", "pending", "Loading bounded acquired-QAM evidence…");
    try {
      if (!selectedRecordings().length || !checked("lnb").length || !checked("receiver").length || !checked("edge").length) {
        node("evidence-qam-canvas").hidden = true; node("evidence-qam-time-canvas").hidden = true; node("evidence-qam-legend").replaceChildren();
        node("evidence-qam-combined-canvas").hidden = true; node("evidence-qam-combined-legend").replaceChildren();
        renderQamGoodness([]); state("qam", "missing", "Select at least one radio, LNB, receiver, and edge."); state("qam-combined", "missing", "Select at least two receiver ports from one recording."); return;
      }
      const mode = node("evidence-mode").value;
      const fetchedResults = [preferredPayload("qam", ["adaptive-qam-v0.4", "acquired-qam-v0.3"])];
      const fetched = {
        payloads: fetchedResults.flatMap((item) => item.payload ? [item.payload] : []),
        missingCount: fetchedResults.filter((item) => !item.payload).length,
      };
      if (current !== generation) return;
      if (!fetched.payloads.length) {
        node("evidence-qam-canvas").hidden = true; node("evidence-qam-time-canvas").hidden = true; node("evidence-qam-legend").replaceChildren(); renderQamGoodness([]);
        node("evidence-qam-combined-canvas").hidden = true; node("evidence-qam-combined-legend").replaceChildren(); state("qam-combined", "pending", "Per-receiver QAM evidence is pending.");
        state("qam", "pending", "Acquired-QAM evidence is pending for every selected recording."); return;
      }
      const lnbSet = new Set(checked("lnb")); const receiverSet = new Set(checked("receiver")); const edgeSet = new Set(checked("edge"));
      const series = []; const goodnessEntries = []; const goodnessSeries = []; const pairingCandidates = [];
      for (const payload of fetched.payloads) {
        if (payload.candidate_only !== true || payload.calibration_required !== true) throw new Error("unsafe acquired-QAM semantics");
        for (const stream of payload.streams || []) {
          if (!lnbSet.has(stream.lnb_id) || !receiverSet.has(stream.receiver_chain_id) || !edgeSet.has(stream.edge)) continue;
          const windows = stream.windows || [];
          const timePoints = [];
          windows.forEach((window, index) => {
            const qam = window.qam || window;
            const selection = window.selection || null;
            const points = qam.display_points || qam.points || [];
            if (!points.length) return;
            const reasons = (selection?.reasons || []).join(", ");
            const label = identity([stream.recording_id || payload.recording_id, stream.radio_id, stream.lnb_id, stream.receiver_chain_id, stream.edge, mode === "windows" ? `window ${qam.window_index ?? index}` : "overall", reasons]);
            series.push({
              label,
              points,
            });
            // The constellation and its label identify one concrete display
            // window.  Rate that same window; support-weighted dwell metrics
            // describe another aggregation and must not be presented as the
            // selected constellation's goodness.
            const accuracy = qam.hard_symbol_accuracy;
            const rmsEvm = qam.rms_evm;
            const goodness = qamGoodness(accuracy, rmsEvm);
            if (goodness !== null) {
              const selectionDetail = selection ? `${reasons}; source Qin ${Number(selection.source_qin_score).toFixed(4)}, control ${Number(selection.source_max_surrogate_score).toFixed(4)}, margin ${Number(selection.source_qin_minus_max_surrogate).toFixed(4)}` : "legacy dwell-stratified selection";
              goodnessEntries.push({label, goodness, accuracy: Number(accuracy), rmsEvm: Number(rmsEvm), selection: selectionDetail});
              timePoints.push({x: (Number(qam.interval_start_utc_ns) + Number(qam.interval_stop_utc_ns)) / 2, y: goodness});
            }
            pairingCandidates.push({
              recordingId: payload.recording_id,
              radioId: stream.radio_id,
              lnbId: stream.lnb_id,
              segmentId: stream.segment_id,
              receiverId: stream.receiver_chain_id,
              edge: stream.edge,
              startUtcNs: Number(qam.interval_start_utc_ns),
              stopUtcNs: Number(qam.interval_stop_utc_ns),
              rmsEvm: Number(qam.rms_evm),
              points,
            });
          });
          if (timePoints.length) goodnessSeries.push({label: identity([payload.recording_id, stream.radio_id, stream.lnb_id, stream.receiver_chain_id, stream.edge, "QAM goodness"]), points: timePoints});
        }
      }
      const paired = combinedQamSeries(pairingCandidates);
      drawChart("evidence-qam-time-canvas", goodnessSeries, "QAM goodness [0,1]", mode, "selected overall window"); drawQam(series); seriesLegend("evidence-qam-legend", series); renderQamGoodness(goodnessEntries);
      drawQam(paired, "evidence-qam-combined-canvas", "Time-matched dual-receiver known-pilot QAM candidate evidence");
      seriesLegend("evidence-qam-combined-legend", paired.map((item) => ({label: `${item.label} · goodness ${item.goodness.toFixed(3)} · accuracy ${(100 * item.accuracy).toFixed(2)}% · RMS EVM ${item.rmsEvm.toFixed(3)} · weights ${item.leftWeight.toFixed(3)}/${item.rightWeight.toFixed(3)}`})));
      state("qam-combined", paired.length ? "ready" : "missing", paired.length ? `${paired.length} exact-time paired candidate series; weights use measured per-window EVM and no label-derived frequency correction.` : "No two selected receiver streams share an exact analyzed QAM window and known-pilot point identity.");
      const partial = fetched.missingCount ? ` ${fetched.missingCount} selected recording(s) remain pending.` : "";
      const adaptive = fetched.payloads.some((payload) => payload.source_adaptive_response_ref);
      state("qam", series.length ? "ready" : "missing", series.length ? `${series.length} unpooled ${mode === "windows" ? "window" : "selected display-window"} QAM series; goodness always rates the constellation shown, while support-weighted dwell summaries remain separate; ${adaptive ? "adaptive target/control window selection" : "legacy stratified-window fallback"}.${partial}` : "No acquired-QAM series match the selected hardware scope.");
    } catch (error) {
      if (current !== generation) return;
      node("evidence-qam-canvas").hidden = true; node("evidence-qam-time-canvas").hidden = true; node("evidence-qam-legend").replaceChildren(); renderQamGoodness([]);
      node("evidence-qam-combined-canvas").hidden = true; node("evidence-qam-combined-legend").replaceChildren(); state("qam-combined", error.status === 404 ? "pending" : "error", error.status === 404 ? "Per-receiver QAM evidence is pending." : `Paired QAM failed: ${error.message}`);
      state("qam", error.status === 404 ? "pending" : "error", error.status === 404 ? "Acquired-QAM evidence is pending or unavailable for this recording." : `QAM evidence failed: ${error.message}`);
    }
  }

  async function loadDetectors(current) {
    state("detector", "pending", "Loading selected detector windowing approaches…");
    try {
      const methods = checked("method"); const edges = checked("edge"); const channels = new Set(checked("channel").map(Number));
      const radios = new Set(selectedRecordings().map((item) => item.radio_id)); const receivers = new Set(checked("receiver")); const lnbs = new Set(checked("lnb")); const patterns = checked("pattern"); const selectedApproaches = checked("detector-approach");
      if (!radios.size || !receivers.size || !lnbs.size || !channels.size || !edges.length || !methods.length || !patterns.length || !selectedApproaches.length) {
        node("evidence-detector-canvas").hidden = true; node("evidence-detector-legend").replaceChildren(); state("detector", "missing", "Select at least one value in every detector scope, including a windowing approach."); return;
      }
      const definitions = {
        "adaptive-time-diverse": {
          product: "adaptive",
          label: "time-diverse adaptive",
          products: ["adaptive_detector_response"],
        },
        "prescreen-global": {
          product: "prescreen-selected",
          label: "global OFDM / power refinement",
          products: ["pilot_refinement"],
        },
        "legacy-sparse": {
          product: "legacy",
          label: "legacy sparse fallback",
          products: ["legacy_full_dwell"],
        },
      };
      const fetchedApproaches = await Promise.all(selectedApproaches.map(async (name) => {
        const definition = definitions[name];
        if (!definition) throw new Error(`unknown detector windowing approach: ${name}`);
        return {...definition, fetched: availablePayloads(definition.products)};
      }));
      if (current !== generation) return;
      const available = fetchedApproaches.flatMap((item) => item.fetched.payloads.map((payload) => ({...item, payload})));
      if (!available.length) {
        node("evidence-detector-canvas").hidden = true; node("evidence-detector-legend").replaceChildren();
        state("detector", "pending", "The selected detector windowing products are queued or unavailable."); return;
      }
      const grouped = new Map(); const approach = []; const mode = node("evidence-mode").value;
      for (const availableProduct of available) {
        const {payload, product, label: productLabel} = availableProduct;
        for (const stream of payload.streams || []) {
        const selection = stream.selection || stream;
        const radio = stream.radio_id || selection.radio_id;
        const receiver = stream.receiver_chain_id || selection.receiver_chain_id;
        const channel = Number(stream.channel_number || selection.channel_number);
        const edge = stream.edge || selection.edge;
        const lnb = stream.lnb_id || selection.lnb_id || assignment(payload.recording_id, receiver)?.lnb_id;
        if (!radios.has(radio) || !receivers.has(receiver) || !lnbs.has(lnb) || !channels.has(channel) || !edges.includes(edge)) continue;
        const base = identity([payload.recording_id, radio, lnb, receiver, `CH${channel}`, edge]);
        const productBase = identity([base, productLabel]);
        const exactWindows = stream.selection?.seeds || stream.selection?.exact_windows || [];
        const stages = [...new Set(exactWindows.flatMap((item) => item.stage ? [item.stage] : [item.periodicity_rank !== null && item.periodicity_rank !== undefined ? "top-ofdm-periodicity" : null, item.power_rank !== null && item.power_rank !== undefined ? "top-power" : null].filter(Boolean)))];
        const sampleRate = Number(stream.sample_rate_hz || stream.selection?.sample_rate_hz);
        approach.push(product === "prescreen-selected" ? {
          kind: "detector", key: productBase, approach: "Complete-IQ prescreen-selected exact Qin + surrogate search", scope: base,
          window: `${exactWindows.length} exact seeds (${stages.join(" + ")}); ${duration(exactWindows[0] ? Number(exactWindows[0].stop_sample) - Number(exactWindows[0].start_sample) : 0, sampleRate)} probes`,
          coverage: `${percent(stream.exact_coverage_fraction)} exact after 100% contiguous OFDM/power prescreen`,
          search: `same selected windows and full epoch/CFO grid for Qin and all four precommitted surrogates; methods ${methods.join(", ")}`,
          response: "Qin and surrogate algorithm scores vs exact-window UTC; finite paired rank and Qin-minus-max-surrogate margin",
          status: "complete-IQ selection; time/epoch/CFO look-elsewhere calibration required; candidate-only",
        } : product === "adaptive" ? {
          kind: "detector", key: productBase, approach: "Time-diverse symmetric adaptive Qin + surrogate search", scope: base,
          window: `${exactWindows.length} exact windows; stages ${stages.join(", ")}; ${duration(payload.plan.probe_sample_count, sampleRate)} probes`,
          coverage: `${percent(stream.exact_coverage_fraction)} exact; fixed sentinels span the dwell and local windows remain sparse`,
          search: `same union of sentinel, power-seed, Qin-selected, surrogate-selected, and local windows for every pattern; each Qin/surrogate pattern independently searches one full-frame epoch/CFO winner; methods ${methods.join(", ")} are conditioned at that pattern winner`,
          response: "conditioned algorithm score vs exact-window UTC; pattern acquisition winner, selection stage, finite paired rank, Qin-minus-max-surrogate margin",
          status: "time look-elsewhere calibration required; candidate-only; maximum is descriptive",
        } : {
          kind: "detector", key: productBase, approach: "Legacy sparse Qin + paired-surrogate fallback", scope: base,
          window: `${stream.exact_window_count} exact selected windows from ${stream.prescreen_window_count} complete prescreen tiles`,
          coverage: `${percent(stream.exact_coverage_fraction)} exact; ${percent(stream.prescreen_coverage_fraction)} pattern-blind prescreen`,
          search: `identical epoch/CFO grid for Qin and every precommitted surrogate; methods ${methods.join(", ")}`,
          response: "algorithm score vs exact-window UTC; finite paired rank and Qin-minus-surrogate margin",
          status: `${stream.refinement_is_data_adaptive ? "power-selected dependent windows" : "fixed windows"}; adaptive product pending; not calibrated`,
        });
        for (const point of stream.points || []) for (const pattern of patterns) {
          if (!methods.includes(point.method)) continue;
          let score = null;
          if (pattern === "qin") score = point.qin?.score;
          else score = point.surrogates?.[Number(pattern.split("-")[1])]?.winner?.score;
          if (!Number.isFinite(Number(score))) continue;
          const label = identity([payload.recording_id, radio, lnb, receiver, `CH${channel}`, edge, productLabel, point.method, pattern]);
          if (!grouped.has(label)) grouped.set(label, []);
          grouped.get(label).push({x: (Number(point.interval_start_utc_ns) + Number(point.interval_stop_utc_ns)) / 2, y: Number(score)});
        }
        }
      }
      setApproachRows("detector", approach);
      const series = [...grouped].map(([label, points], index) => ({label, points: mode === "windows" ? points : [{x: index, y: Math.max(...points.map((item) => item.y))}]}));
      drawChart("evidence-detector-canvas", series, "score [0,1]", mode, "series (maximum over returned exact windows)");
      seriesLegend("evidence-detector-legend", series);
      const missingCount = fetchedApproaches.reduce((total, item) => total + item.fetched.missingCount, 0);
      const partial = missingCount ? ` ${missingCount} selected recording/product combination(s) remain pending.` : "";
      const productLabels = fetchedApproaches.filter((item) => item.fetched.payloads.length).map((item) => item.label).join(", ");
      state("detector", series.length ? "ready" : "missing", series.length ? `${series.length} unpooled series from: ${productLabels}; ${mode === "overall" ? "overall is the maximum over returned exact windows" : "each point is one exact analyzed window"}.${partial}` : "No detector points match the selected scope.");
    } catch (error) {
      if (current !== generation) return;
      node("evidence-detector-canvas").hidden = true; node("evidence-detector-legend").replaceChildren();
      state("detector", error.status === 404 ? "pending" : "error", error.status === 404 ? "Adaptive detector evidence is pending in the asynchronous queue." : `Detector evidence failed: ${error.message}`);
      setApproachRows("detector", [{kind: "detector", key: "pending", approach: "Symmetric adaptive Qin + surrogate search", scope: "selected recording(s)", window: "pending", coverage: "pending", search: "same candidate/local windows for every pattern; one independent full-frame epoch/CFO winner per Qin/surrogate pattern", response: "conditioned algorithm score vs UTC", status: error.status === 404 ? "queued" : error.message}]);
    }
  }

  async function loadDoppler(current) {
    state("doppler", "pending", "Loading published total fits and bounded server-derived window slopes…");
    node("evidence-pilot-doppler-state").dataset.state = "pending";
    node("evidence-pilot-doppler-state").textContent = "Loading acquired-pilot frequency association…";
    try {
      if (!selectedRecordings().length || !checked("lnb").length || !checked("receiver").length) {
        node("evidence-doppler-canvas").hidden = true; node("evidence-doppler-legend").replaceChildren(); state("doppler", "missing", "Select at least one radio, LNB, and receiver."); return;
      }
      const basic = productPayload("basic_doppler");
      const advanced = productPayload("advanced_doppler");
      const payload = basic.payload || {state: analysisProducts.get("basic_doppler")?.state, series: [], candidate_only: true, calibrated_detection_count: null};
      const advancedPayload = advanced.payload || {state: analysisProducts.get("advanced_doppler")?.state, series: [], candidate_only: true, calibrated_detection_count: null};
      const associationPayloads = checked("edge").length ? availablePayloads(["pilot_doppler_association"]) : {payloads: [], missingCount: 0};
      if (current !== generation) return;
      if (payload.candidate_only !== true || payload.calibrated_detection_count !== null || advancedPayload.candidate_only !== true || advancedPayload.calibrated_detection_count !== null) throw new Error("unsafe Doppler semantics");
      const mode = node("evidence-mode").value;
      const basicSeries = (payload.series || []).map((item, index) => ({
        label: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, `basic candidate ${item.candidate_rank}`]),
        points: mode === "windows" ? (item.windows || []).map((window) => ({x: (Number(window.interval_start_utc_ns) + Number(window.interval_stop_utc_ns)) / 2, y: Number(window.drift_rate_hz_s)})) : [{x: index, y: Number(item.total.drift_rate_hz_s)}],
      }));
      const advancedSeries = (advancedPayload.series || []).map((item, index) => ({
        label: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, "advanced path only"]),
        points: mode === "windows" ? (item.windows || []).map((window) => ({x: (Number(window.point_start_utc_ns) + Number(window.point_stop_utc_ns)) / 2, y: Number(window.drift_rate_hz_s)})) : [{x: basicSeries.length + index, y: Number(item.total.drift_rate_hz_s)}],
      }));
      const series = [...basicSeries, ...advancedSeries];
      const pilotSeries = [];
      const associationFacts = [];
      for (const associationPayload of associationPayloads.payloads) {
        if (associationPayload.candidate_only !== true || associationPayload.calibrated_detection_count !== null) throw new Error("unsafe pilot Doppler association semantics");
        for (const item of associationPayload.series || []) {
          const base = identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, item.edge]);
          pilotSeries.push({
            label: `${base} · acquired pilot CFO`,
            points: (item.qam_windows || []).map((window) => ({x: (Number(window.interval_start_utc_ns) + Number(window.interval_stop_utc_ns)) / 2, y: Number(window.winning_cfo_hz)})),
          });
          for (const comparison of item.comparisons || []) {
            pilotSeries.push({
              label: `${base} · blind path · ${comparison.association_state}`,
              points: (comparison.points || []).map((point) => ({x: Number(point.midpoint_utc_ns), y: Number(point.blind_path_frequency_hz) - Number(item.center_frequency_hz)})),
            });
            associationFacts.push(`${base}: ${comparison.association_state}; median Δf ${Number(comparison.median_frequency_distance_hz).toFixed(1)} Hz; pilot ${Number(comparison.pilot_drift_rate_hz_s).toFixed(1)} Hz/s vs blind ${Number(comparison.blind_path_drift_rate_hz_s).toFixed(1)} Hz/s`);
          }
        }
      }
      const approach = [
        ...(payload.series || []).map((item) => ({
          kind: "doppler", key: identity([item.recording_id, item.radio_id, item.receiver_chain_id, item.segment_id, "basic", item.candidate_rank]), approach: "Basic blind Doppler track", scope: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, `candidate ${item.candidate_rank}`]),
          window: `${(item.windows || []).length} adjacent path intervals with explicit sample/UTC bounds`, coverage: "track-support intervals only; not raw-IQ coverage", search: "blind continuity track then robust total fit", response: "drift rate [Hz/s] total and local windows", status: "candidate-only; uncalibrated",
        })),
        ...(advancedPayload.series || []).map((item) => ({
          kind: "doppler", key: identity([item.recording_id, item.radio_id, item.receiver_chain_id, item.segment_id, "advanced", item.path_digest]), approach: "Advanced-path-only Doppler", scope: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, item.association_state]),
          window: `${(item.windows || []).length} adjacent immutable path-point intervals`, coverage: "path-support intervals only; not raw-IQ coverage", search: "physical-rate bank with held-out/stationary/opposite/time-shuffle controls", response: "published total path rate and local slopes [Hz/s]", status: "candidate-only; no calibrated count",
        })),
        ...associationPayloads.payloads.flatMap((associationPayload) => (associationPayload.series || []).map((item) => ({
          kind: "doppler", key: identity([item.recording_id, item.radio_id, item.receiver_chain_id, item.segment_id, "pilot-association"]), approach: "Acquired-pilot frequency association", scope: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, item.edge]),
          window: `${(item.qam_windows || []).length} adaptive QAM windows with exact UTC/sample scope`, coverage: "selected QAM windows compared only inside blind-path time support", search: `absolute pilot frequency = segment center + acquired CFO; interpolate blind path; diagnostic gate ${Number(associationPayload.frequency_gate_hz).toFixed(0)} Hz`, response: "pilot CFO and blind-path frequency offset vs UTC; Δfrequency and drift-rate comparison", status: (item.comparisons || []).map((value) => value.association_state).join(", ") || "no blind path",
        }))),
      ];
      setApproachRows("doppler", approach);
      drawChart("evidence-doppler-canvas", series, "drift rate [Hz/s]", mode, "published total path rate");
      seriesLegend("evidence-doppler-legend", series);
      const combinedState = series.length ? "ready" : payload.state === "pending" || advancedPayload.state === "pending" ? "pending" : payload.state === "error" || advancedPayload.state === "error" ? "error" : "missing";
      state("doppler", combinedState, series.length ? `${series.length} unpooled series (${basicSeries.length} basic candidate, ${advancedSeries.length} advanced-path-only); ${mode === "overall" ? "published total path rates" : "adjacent immutable path-point slopes with explicit UTC/sample scope"}. Candidate evidence only; no calibrated detection is implied.` : `Doppler evidence is ${combinedState}.`);
      drawChart("evidence-pilot-doppler-canvas", pilotSeries, "frequency offset from segment center [Hz]", "windows", "selected QAM windows");
      seriesLegend("evidence-pilot-doppler-legend", pilotSeries);
      const pilotState = node("evidence-pilot-doppler-state");
      pilotState.dataset.state = pilotSeries.length ? "ready" : associationPayloads.missingCount ? "pending" : "missing";
      pilotState.textContent = associationFacts.length ? associationFacts.join(" | ") : associationPayloads.missingCount ? "Pilot-frequency association is pending with adaptive QAM." : "No pilot-frequency association is available for the selected scope.";
    } catch (error) {
      if (current !== generation) return;
      node("evidence-doppler-canvas").hidden = true; node("evidence-doppler-legend").replaceChildren();
      node("evidence-pilot-doppler-canvas").hidden = true; node("evidence-pilot-doppler-legend").replaceChildren(); node("evidence-pilot-doppler-state").dataset.state = "error"; node("evidence-pilot-doppler-state").textContent = `Pilot-frequency association failed: ${error.message}`;
      state("doppler", error.status === 404 ? "missing" : "error", error.status === 404 ? "Doppler evidence is unavailable." : `Doppler evidence failed: ${error.message}`);
      setApproachRows("doppler", [{kind: "doppler", key: "missing", approach: "Doppler tracking", scope: "selected recording(s)", window: "unavailable", coverage: "unavailable", search: "blind physical-rate paths", response: "drift rate vs UTC", status: error.status === 404 ? "not published" : error.message}]);
    }
  }

  function renderLoadedAnalysis() {
    if (!context) return;
    generation += 1;
    const current = generation;
    approachRows.clear(); renderApproachRows();
    void Promise.all([loadQam(current), loadDetectors(current)]);
    if (extendedLoaded) {
      void Promise.all([loadApproaches(current), loadAdaptiveQamApproaches(current), loadTimeline(current), loadPilotPrescreen(current), loadDoppler(current)]);
    } else {
      state("timeline", "pending", "Complete IQ tile timelines are deferred until extended analysis is requested.");
      state("prescreen", "pending", "Complete-IQ OFDM prescreens are deferred until extended analysis is requested.");
      state("doppler", "pending", "Doppler path composition is deferred until extended analysis is requested.");
      node("evidence-pilot-doppler-state").dataset.state = "pending";
      node("evidence-pilot-doppler-state").textContent = "Pilot-frequency association is deferred until extended analysis is requested.";
    }
  }

  async function reload() {
    if (!context) return;
    try {
      await fetchAnalysis(extendedLoaded ? ["primary", "extended"] : ["primary"]);
      renderLoadedAnalysis();
    } catch (error) {
      const target = node("evidence-context-state"); target.dataset.state = "error"; target.textContent = `Recording analysis refresh failed: ${error.message}`;
    }
  }

  async function loadExtended() {
    if (extendedLoaded) return;
    extendedLoaded = true;
    const button = node("evidence-load-extended");
    button.disabled = true;
    button.textContent = "Extended analysis loading…";
    if (!context) {
      document.dispatchEvent(new CustomEvent("leo:load-extended-recording-analysis"));
      button.textContent = "Extended recording analysis loaded";
      return;
    }
    try {
      await fetchAnalysis(["extended"]);
      const current = generation;
      await Promise.all([loadApproaches(current), loadAdaptiveQamApproaches(current), loadTimeline(current), loadPilotPrescreen(current), loadDoppler(current)]);
      document.dispatchEvent(new CustomEvent("leo:load-extended-recording-analysis"));
      button.textContent = "Extended analysis loaded";
    } catch (error) {
      button.textContent = "Extended analysis failed";
      state("timeline", "error", `Extended recording analysis failed: ${error.message}`);
    }
  }

  async function initialize() {
    try {
      await fetchAnalysis(["primary"]);
      const contextEnvelope = analysisProducts.get("evidence_context");
      exposeProductState("evidence_context", contextEnvelope?.state || "failed");
      if (contextEnvelope?.state !== "complete") {
        const terminal = contextEnvelope?.state || "failed";
        throw new Error(`evidence context is ${terminal.replace("_", " ")}`);
      }
      context = contextEnvelope.payload;
      if (context.candidate_only !== true || context.calibrated_detection_count !== null) throw new Error("unsafe evidence context semantics");
      const recordings = (context.recordings || []).filter((item) => item.recording_id === recordingId);
      const receivers = (context.receivers || []).filter((item) => item.recording_id === recordingId);
      context = {...context, recordings, receivers};
      addChecks(node("evidence-radios"), "radio", recordings, (item) => `${item.radio_id} / ${item.recording_id} (this page)`);
      node("evidence-radios").querySelectorAll("input").forEach((input, index) => {
        input.value = recordings[index].recording_id;
        input.dataset.radioId = recordings[index].radio_id;
        input.checked = true;
      });
      const lnbValues = [...new Set(receivers.map((item) => item.lnb_id))].map((value) => ({value}));
      const receiverValues = [...new Set(receivers.map((item) => item.receiver_chain_id))].map((value) => ({value}));
      addChecks(node("evidence-lnbs"), "lnb", lnbValues, (item) => item.value);
      addChecks(node("evidence-receivers"), "receiver", receiverValues, (item) => item.value);
      const contextState = node("evidence-context-state"); contextState.dataset.state = "ready";
      contextState.dataset.productState = "complete";
      contextState.textContent = `${recordings.length} exact recording scope; ${receivers.length} effective-dated receiver/LNB assignments. Batch companions are excluded.`;
      node("evidence-limitations").textContent = (context.limitations || []).length ? `Limitations: ${context.limitations.join(", ")}. Unresolved assignments are excluded, never inferred from RX names.` : "Hardware selectors use the immutable assignment effective at capture time.";
      node("evidence-controls").addEventListener("change", reload);
      node("evidence-mode").addEventListener("change", reload);
      renderLoadedAnalysis();
    } catch (error) {
      const target = node("evidence-context-state"); target.dataset.state = "error"; target.textContent = `Evidence context unavailable: ${error.message}`;
      ["timeline", "qam", "detector", "doppler"].forEach((product) => state(product, "missing", "Authoritative hardware context is required before evidence can be displayed."));
    }
  }

  node("evidence-load-extended").addEventListener("click", loadExtended);
  void initialize();
})();

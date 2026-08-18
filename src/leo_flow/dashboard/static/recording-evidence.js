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

  async function availablePayloads(paths) {
    const results = await Promise.all(paths.map(async (path) => {
      try { return {payload: await json(path), missing: false}; }
      catch (error) {
        if (error.status === 404) return {payload: null, missing: true};
        throw error;
      }
    }));
    return {
      payloads: results.flatMap((item) => item.payload ? [item.payload] : []),
      missingCount: results.filter((item) => item.missing).length,
    };
  }

  async function preferredPayload(paths) {
    for (const path of paths) {
      try { return {payload: await json(path), path}; }
      catch (error) {
        if (error.status !== 404) throw error;
      }
    }
    return {payload: null, path: null};
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
      const fetched = await availablePayloads(selectedRecordings().map((recording) => `/api/v23/recordings/${encodeURIComponent(recording.recording_id)}/analysis-approaches`));
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

  function drawQam(series) {
    const canvas = node("evidence-qam-canvas");
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
    canvas.setAttribute("aria-label", `Known pilot QAM candidate evidence; ${series.length} unpooled series and ${all.length} coefficients`);
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
      const fetched = await availablePayloads(recordings.map((recording) => {
        const parameters = new URLSearchParams({radio_ids: recording.radio_id, receiver_chain_ids: [...receivers].join(","), edges: edges.join(","), maximum_windows: "16384"});
        return `/api/v20/recordings/${encodeURIComponent(recording.recording_id)}/full-dwell-timeline?${parameters}`;
      }));
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

  async function loadQam(current) {
    state("qam", "pending", "Loading bounded acquired-QAM evidence…");
    try {
      if (!selectedRecordings().length || !checked("lnb").length || !checked("receiver").length || !checked("edge").length) {
        node("evidence-qam-canvas").hidden = true; node("evidence-qam-time-canvas").hidden = true; node("evidence-qam-legend").replaceChildren(); renderQamGoodness([]); state("qam", "missing", "Select at least one radio, LNB, receiver, and edge."); return;
      }
      const mode = node("evidence-mode").value;
      const fetchedResults = await Promise.all(selectedRecordings().map((recording) => {
        const parameters = new URLSearchParams({mode, maximum_streams: "4", maximum_windows_per_stream: "32", maximum_points_per_constellation: "128"});
        queryFilters(parameters);
        const encoded = encodeURIComponent(recording.recording_id);
        return preferredPayload([
          `/api/v25/recordings/${encoded}/starlink-adaptive-qam?${parameters}`,
          `/api/v17/recordings/${encoded}/starlink-acquired-constellation?${parameters}`,
        ]);
      }));
      const fetched = {
        payloads: fetchedResults.flatMap((item) => item.payload ? [item.payload] : []),
        missingCount: fetchedResults.filter((item) => !item.payload).length,
      };
      if (current !== generation) return;
      if (!fetched.payloads.length) {
        node("evidence-qam-canvas").hidden = true; node("evidence-qam-time-canvas").hidden = true; node("evidence-qam-legend").replaceChildren(); renderQamGoodness([]);
        state("qam", "pending", "Acquired-QAM evidence is pending for every selected recording."); return;
      }
      const lnbSet = new Set(checked("lnb")); const receiverSet = new Set(checked("receiver")); const edgeSet = new Set(checked("edge"));
      const series = []; const goodnessEntries = []; const goodnessSeries = [];
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
            const accuracy = mode === "windows" ? qam.hard_symbol_accuracy : stream.overall?.support_weighted_hard_symbol_accuracy;
            const rmsEvm = mode === "windows" ? qam.rms_evm : stream.overall?.support_weighted_rms_evm;
            const goodness = qamGoodness(accuracy, rmsEvm);
            if (goodness !== null) {
              const selectionDetail = selection ? `${reasons}; source Qin ${Number(selection.source_qin_score).toFixed(4)}, control ${Number(selection.source_max_surrogate_score).toFixed(4)}, margin ${Number(selection.source_qin_minus_max_surrogate).toFixed(4)}` : "legacy dwell-stratified selection";
              goodnessEntries.push({label, goodness, accuracy: Number(accuracy), rmsEvm: Number(rmsEvm), selection: selectionDetail});
              timePoints.push({x: (Number(qam.interval_start_utc_ns) + Number(qam.interval_stop_utc_ns)) / 2, y: goodness});
            }
          });
          if (timePoints.length) goodnessSeries.push({label: identity([payload.recording_id, stream.radio_id, stream.lnb_id, stream.receiver_chain_id, stream.edge, "QAM goodness"]), points: timePoints});
        }
      }
      drawChart("evidence-qam-time-canvas", goodnessSeries, "QAM goodness [0,1]", mode, "selected overall window"); drawQam(series); seriesLegend("evidence-qam-legend", series); renderQamGoodness(goodnessEntries);
      const partial = fetched.missingCount ? ` ${fetched.missingCount} selected recording(s) remain pending.` : "";
      const adaptive = fetched.payloads.some((payload) => payload.source_adaptive_response_ref);
      state("qam", series.length ? "ready" : "missing", series.length ? `${series.length} unpooled ${mode === "windows" ? "window" : "overall"} QAM series; ${adaptive ? "adaptive target/control window selection" : "legacy stratified-window fallback"}.${partial}` : "No acquired-QAM series match the selected hardware scope.");
    } catch (error) {
      if (current !== generation) return;
      node("evidence-qam-canvas").hidden = true; node("evidence-qam-time-canvas").hidden = true; node("evidence-qam-legend").replaceChildren(); renderQamGoodness([]);
      state("qam", error.status === 404 ? "pending" : "error", error.status === 404 ? "Acquired-QAM evidence is pending or unavailable for this recording." : `QAM evidence failed: ${error.message}`);
    }
  }

  async function loadDetectors(current) {
    state("detector", "pending", "Loading symmetric adaptive detector evidence…");
    try {
      const methods = checked("method"); const edges = checked("edge"); const channels = new Set(checked("channel").map(Number));
      const radios = new Set(selectedRecordings().map((item) => item.radio_id)); const receivers = new Set(checked("receiver")); const lnbs = new Set(checked("lnb")); const patterns = checked("pattern");
      if (!radios.size || !receivers.size || !lnbs.size || !channels.size || !edges.length || !methods.length || !patterns.length) {
        node("evidence-detector-canvas").hidden = true; node("evidence-detector-legend").replaceChildren(); state("detector", "missing", "Select at least one value in every detector scope."); return;
      }
      const adaptivePaths = selectedRecordings().map((recording) => {
        const parameters = new URLSearchParams({methods: methods.join(","), radio_ids: recording.radio_id, lnb_ids: [...lnbs].join(","), receiver_chain_ids: [...receivers].join(","), edges: edges.join(","), maximum_points: "4096"});
        return `/api/v24/recordings/${encodeURIComponent(recording.recording_id)}/starlink-adaptive-response?${parameters}`;
      });
      let fetched = await availablePayloads(adaptivePaths);
      let adaptive = fetched.payloads.length > 0;
      if (!adaptive) {
        fetched = await availablePayloads(selectedRecordings().map((recording) => {
          const parameters = new URLSearchParams({methods: methods.join(","), radio_ids: recording.radio_id, receiver_chain_ids: [...receivers].join(","), edges: edges.join(","), maximum_points: "4096"});
          return `/api/v15/recordings/${encodeURIComponent(recording.recording_id)}/starlink-full-dwell?${parameters}`;
        }));
      }
      if (current !== generation) return;
      if (!fetched.payloads.length) {
        node("evidence-detector-canvas").hidden = true; node("evidence-detector-legend").replaceChildren();
        state("detector", "pending", "Adaptive detector evidence is queued; no legacy fallback product is available yet."); return;
      }
      const grouped = new Map(); const approach = []; const mode = node("evidence-mode").value;
      for (const payload of fetched.payloads) for (const stream of payload.streams || []) {
        const lnb = stream.lnb_id || assignment(payload.recording_id, stream.receiver_chain_id)?.lnb_id;
        if (!radios.has(stream.radio_id) || !receivers.has(stream.receiver_chain_id) || !lnbs.has(lnb) || !channels.has(Number(stream.channel_number)) || !edges.includes(stream.edge)) continue;
        const base = identity([payload.recording_id, stream.radio_id, lnb, stream.receiver_chain_id, `CH${stream.channel_number}`, stream.edge]);
        const exactWindows = stream.selection?.exact_windows || [];
        const stages = [...new Set(exactWindows.map((item) => item.stage))];
        approach.push(adaptive ? {
          kind: "detector", key: base, approach: "Symmetric adaptive Qin + surrogate search", scope: base,
          window: `${exactWindows.length} exact windows; stages ${stages.join(", ")}; ${duration(payload.plan.probe_sample_count, stream.sample_rate_hz)} probes`,
          coverage: `${percent(stream.exact_coverage_fraction)} exact; fixed sentinels span the dwell and local windows remain sparse`,
          search: `same union of sentinel, power-seed, Qin-selected, surrogate-selected, and local windows for every pattern; identical epoch/CFO grid; methods ${methods.join(", ")}`,
          response: "algorithm score vs exact-window UTC; selection stage, finite paired rank, Qin-minus-max-surrogate margin",
          status: "time look-elsewhere calibration required; candidate-only; maximum is descriptive",
        } : {
          kind: "detector", key: base, approach: "Legacy sparse Qin + paired-surrogate fallback", scope: base,
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
          const label = identity([payload.recording_id, stream.radio_id, lnb, stream.receiver_chain_id, `CH${stream.channel_number}`, stream.edge, point.method, pattern]);
          if (!grouped.has(label)) grouped.set(label, []);
          grouped.get(label).push({x: (Number(point.interval_start_utc_ns) + Number(point.interval_stop_utc_ns)) / 2, y: Number(score)});
        }
      }
      setApproachRows("detector", approach);
      const series = [...grouped].map(([label, points], index) => ({label, points: mode === "windows" ? points : [{x: index, y: Math.max(...points.map((item) => item.y))}]}));
      drawChart("evidence-detector-canvas", series, "score [0,1]", mode, "series (maximum over returned exact windows)");
      seriesLegend("evidence-detector-legend", series);
      const partial = fetched.missingCount ? ` ${fetched.missingCount} selected recording(s) remain pending.` : "";
      state("detector", series.length ? "ready" : "missing", series.length ? `${series.length} unpooled series from the ${adaptive ? "pattern-symmetric adaptive" : "legacy sparse fallback"} product; ${mode === "overall" ? "overall is the maximum over returned exact windows" : "each point is one exact analyzed window"}.${partial}` : "No detector points match the selected scope.");
    } catch (error) {
      if (current !== generation) return;
      node("evidence-detector-canvas").hidden = true; node("evidence-detector-legend").replaceChildren();
      state("detector", error.status === 404 ? "pending" : "error", error.status === 404 ? "Adaptive detector evidence is pending in the asynchronous queue." : `Detector evidence failed: ${error.message}`);
      setApproachRows("detector", [{kind: "detector", key: "pending", approach: "Symmetric adaptive Qin + surrogate search", scope: "selected recording(s)", window: "pending", coverage: "pending", search: "same candidate/local windows and epoch/CFO search for every pattern", response: "algorithm score vs UTC", status: error.status === 404 ? "queued" : error.message}]);
    }
  }

  async function loadDoppler(current) {
    state("doppler", "pending", "Loading published total fits and bounded server-derived window slopes…");
    try {
      if (!selectedRecordings().length || !checked("lnb").length || !checked("receiver").length) {
        node("evidence-doppler-canvas").hidden = true; node("evidence-doppler-legend").replaceChildren(); state("doppler", "missing", "Select at least one radio, LNB, and receiver."); return;
      }
      const parameters = new URLSearchParams({maximum_windows: "4096"}); queryFilters(parameters); parameters.delete("edges");
      const [payload, advancedPayload] = await Promise.all([
        json(`/api/v16/recordings/${encodeURIComponent(recordingId)}/evidence-doppler?${parameters}`),
        json(`/api/v19/recordings/${encodeURIComponent(recordingId)}/evidence-advanced-doppler?${parameters}`),
      ]);
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
      const approach = [
        ...(payload.series || []).map((item) => ({
          kind: "doppler", key: identity([item.recording_id, item.radio_id, item.receiver_chain_id, item.segment_id, "basic", item.candidate_rank]), approach: "Basic blind Doppler track", scope: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, `candidate ${item.candidate_rank}`]),
          window: `${(item.windows || []).length} adjacent path intervals with explicit sample/UTC bounds`, coverage: "track-support intervals only; not raw-IQ coverage", search: "blind continuity track then robust total fit", response: "drift rate [Hz/s] total and local windows", status: "candidate-only; uncalibrated",
        })),
        ...(advancedPayload.series || []).map((item) => ({
          kind: "doppler", key: identity([item.recording_id, item.radio_id, item.receiver_chain_id, item.segment_id, "advanced", item.path_digest]), approach: "Advanced-path-only Doppler", scope: identity([item.recording_id, item.radio_id, item.lnb_id, item.receiver_chain_id, item.segment_id, item.association_state]),
          window: `${(item.windows || []).length} adjacent immutable path-point intervals`, coverage: "path-support intervals only; not raw-IQ coverage", search: "physical-rate bank with held-out/stationary/opposite/time-shuffle controls", response: "published total path rate and local slopes [Hz/s]", status: "candidate-only; no calibrated count",
        })),
      ];
      setApproachRows("doppler", approach);
      drawChart("evidence-doppler-canvas", series, "drift rate [Hz/s]", mode, "published total path rate");
      seriesLegend("evidence-doppler-legend", series);
      const combinedState = series.length ? "ready" : payload.state === "pending" || advancedPayload.state === "pending" ? "pending" : payload.state === "error" || advancedPayload.state === "error" ? "error" : "missing";
      state("doppler", combinedState, series.length ? `${series.length} unpooled series (${basicSeries.length} basic candidate, ${advancedSeries.length} advanced-path-only); ${mode === "overall" ? "published total path rates" : "adjacent immutable path-point slopes with explicit UTC/sample scope"}. Candidate evidence only; no calibrated detection is implied.` : `Doppler evidence is ${combinedState}.`);
    } catch (error) {
      if (current !== generation) return;
      node("evidence-doppler-canvas").hidden = true; node("evidence-doppler-legend").replaceChildren();
      state("doppler", error.status === 404 ? "missing" : "error", error.status === 404 ? "Doppler evidence is unavailable." : `Doppler evidence failed: ${error.message}`);
      setApproachRows("doppler", [{kind: "doppler", key: "missing", approach: "Doppler tracking", scope: "selected recording(s)", window: "unavailable", coverage: "unavailable", search: "blind physical-rate paths", response: "drift rate vs UTC", status: error.status === 404 ? "not published" : error.message}]);
    }
  }

  function reload() {
    if (!context) return;
    generation += 1;
    const current = generation;
    approachRows.clear(); renderApproachRows();
    void Promise.all([loadApproaches(current), loadTimeline(current), loadQam(current), loadDetectors(current), loadDoppler(current)]);
  }

  async function initialize() {
    try {
      context = await json(`/api/v16/recordings/${encodeURIComponent(recordingId)}/evidence-context`);
      if (context.candidate_only !== true || context.calibrated_detection_count !== null) throw new Error("unsafe evidence context semantics");
      addChecks(node("evidence-radios"), "radio", context.recordings || [], (item) => `${item.radio_id} / ${item.recording_id}${item.requested ? " (this page)" : " (batch companion)"}`);
      node("evidence-radios").querySelectorAll("input").forEach((input, index) => { input.value = context.recordings[index].recording_id; });
      const lnbValues = [...new Set((context.receivers || []).map((item) => item.lnb_id))].map((value) => ({value}));
      const receiverValues = [...new Set((context.receivers || []).map((item) => item.receiver_chain_id))].map((value) => ({value}));
      addChecks(node("evidence-lnbs"), "lnb", lnbValues, (item) => item.value);
      addChecks(node("evidence-receivers"), "receiver", receiverValues, (item) => item.value);
      const contextState = node("evidence-context-state"); contextState.dataset.state = "ready";
      contextState.textContent = `${context.recordings.length} recording scope${context.capture_batch_id ? ` from ${context.capture_batch_id}` : " (no authoritative companion batch)"}; ${(context.receivers || []).length} effective-dated receiver/LNB assignments.`;
      node("evidence-limitations").textContent = (context.limitations || []).length ? `Limitations: ${context.limitations.join(", ")}. Unresolved assignments are excluded, never inferred from RX names.` : "Hardware selectors use the immutable assignment effective at capture time.";
      node("evidence-controls").addEventListener("change", reload);
      node("evidence-mode").addEventListener("change", reload);
      reload();
    } catch (error) {
      const target = node("evidence-context-state"); target.dataset.state = "error"; target.textContent = `Evidence context unavailable: ${error.message}`;
      ["timeline", "qam", "detector", "doppler"].forEach((product) => state(product, "missing", "Authoritative hardware context is required before evidence can be displayed."));
    }
  }

  void initialize();
})();

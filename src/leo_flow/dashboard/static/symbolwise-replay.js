"use strict";

(() => {
  const node = (id) => document.getElementById(id);
  if (!node("evidence-symbolwise")) return;
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || parts[0] !== "recordings") return;
  let recordingId;
  try { recordingId = decodeURIComponent(parts[1]); } catch (_error) { return; }

  const colors = ["#80d8ff", "#fff176", "#ff8a80", "#69f0ae", "#ce93d8"];
  let loaded = false;
  let generation = 0;
  let payload = null;
  let context = null;

  function setState(value, message) {
    node("evidence-symbolwise-state").dataset.state = value;
    node("evidence-symbolwise-state").textContent = message;
    node("evidence-symbolwise-badge").textContent = value === "ready" ? "Candidate evidence" : value;
    node("evidence-symbolwise-badge").dataset.tone = value === "error" ? "error" : "warning";
  }

  async function json(path) {
    const response = await fetch(path, {headers: {accept: "application/json"}, credentials: "same-origin"});
    let body = {};
    try { body = await response.json(); } catch (_error) { /* reported below */ }
    if (!response.ok) {
      const error = new Error(body?.error?.message || `request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function checked(name) {
    return [...document.querySelectorAll(`#evidence-controls input[name="${name}"]:checked`)].map((item) => item.value);
  }

  function parameters() {
    const selectedRecordings = new Set(checked("radio"));
    const radios = (context?.recordings || []).filter((item) => selectedRecordings.has(item.recording_id)).map((item) => item.radio_id);
    const query = new URLSearchParams();
    const radioInputs = document.querySelectorAll('#evidence-controls input[name="radio"]');
    const lnbInputs = document.querySelectorAll('#evidence-controls input[name="lnb"]');
    const receiverInputs = document.querySelectorAll('#evidence-controls input[name="receiver"]');
    if ((radioInputs.length && !radios.length) || (lnbInputs.length && !checked("lnb").length) || (receiverInputs.length && !checked("receiver").length)) return null;
    if (radios.length) query.set("radio_ids", [...new Set(radios)].sort().join(","));
    const lnbs = checked("lnb").sort();
    const receivers = checked("receiver").sort();
    if (lnbs.length) query.set("lnb_ids", lnbs.join(","));
    if (receivers.length) query.set("receiver_chain_ids", receivers.join(","));
    return query.toString();
  }

  function selectedPattern(point) {
    const values = new Set(checked("pattern"));
    return point.pattern_role === "qin-exact" ? values.has("qin") : values.has(`surrogate-${point.codebook_index}`);
  }

  function appendCell(row, text, heading = false) {
    const cell = document.createElement(heading ? "th" : "td");
    cell.textContent = String(text);
    if (heading) cell.scope = "row";
    row.append(cell);
  }

  function renderWindow() {
    const body = node("evidence-symbolwise-window-body");
    body.replaceChildren();
    const index = Number(node("evidence-symbolwise-window").value);
    node("evidence-symbolwise-window-output").textContent = String(index);
    for (const stream of payload?.streams || []) {
      const point = stream.windows[index];
      if (!point) continue;
      for (const pattern of point.patterns.filter(selectedPattern)) {
        const row = document.createElement("tr");
        appendCell(row, `${pattern.candidate_label} · ${stream.radio_id} / ${stream.lnb_id} / ${stream.receiver_chain_id}`, true);
        appendCell(row, point.window_index);
        appendCell(row, `[${point.start_sample}, ${point.stop_sample})`);
        appendCell(row, `[${point.start_time_s.toFixed(3)}, ${point.stop_time_s.toFixed(3)}) s`);
        appendCell(row, Number(pattern.selection_score).toFixed(6));
        appendCell(row, `${Number(pattern.winning_cfo_hz).toFixed(3)} Hz`);
        appendCell(row, `${pattern.winning_epoch_sample} samples`);
        body.append(row);
      }
    }
  }

  function draw() {
    const canvas = node("evidence-symbolwise-canvas");
    const series = [];
    for (const stream of payload?.streams || []) {
      for (let patternIndex = 0; patternIndex < 5; patternIndex += 1) {
        const first = stream.windows[0]?.patterns[patternIndex];
        if (!first || !selectedPattern(first)) continue;
        series.push({
          label: `${first.candidate_label} · ${stream.radio_id} / ${stream.lnb_id} / ${stream.receiver_chain_id}`,
          color: colors[patternIndex],
          points: stream.windows.map((window) => ({x: window.start_time_s, y: window.patterns[patternIndex].selection_score})),
        });
      }
    }
    canvas.hidden = series.length === 0;
    const legend = node("evidence-symbolwise-legend"); legend.replaceChildren();
    for (const item of series) { const span = document.createElement("span"); span.textContent = item.label; span.style.color = item.color; legend.append(span); }
    if (!series.length) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.strokeStyle = "#78909c"; ctx.strokeRect(56, 20, canvas.width - 76, canvas.height - 64);
    for (const item of series) {
      ctx.beginPath(); ctx.strokeStyle = item.color; ctx.lineWidth = 1.5;
      item.points.forEach((point, index) => {
        const x = 56 + (point.x / 60) * (canvas.width - 76);
        const y = 20 + (1 - Number(point.y)) * (canvas.height - 64);
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    ctx.fillStyle = "#cfd8dc"; ctx.font = "14px sans-serif"; ctx.fillText("0 s", 56, canvas.height - 18); ctx.fillText("60 s", canvas.width - 62, canvas.height - 18); ctx.fillText("selection score 0–1", 62, 16);
  }

  function render() {
    const streams = payload.streams || [];
    const facts = node("evidence-symbolwise-facts"); facts.replaceChildren();
    const values = [
      ["Scope", `${streams.length} unpooled radio / authoritative LNB / RX stream${streams.length === 1 ? "" : "s"}`],
      ["Window plan", "600 exact 10 ms windows, beginning every 100 ms across 60 s"],
      ["Analyzed union", "6.000 s / 60.000 s = 10% exactly"],
      ["Semantics", "Candidate-only; calibrated detection count unavailable"],
      ["Overall derivation", payload.summary_derivation],
    ];
    for (const [label, value] of values) { const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.textContent = value; facts.append(dt, dd); }
    const overall = node("evidence-symbolwise-overall-body"); overall.replaceChildren();
    for (const stream of streams) for (const item of stream.overall.filter(selectedPattern)) {
      const row = document.createElement("tr");
      appendCell(row, `${item.candidate_label} · ${stream.radio_id} / ${stream.lnb_id} / ${stream.receiver_chain_id}`, true);
      appendCell(row, Number(item.mean_selection_score).toFixed(6)); appendCell(row, Number(item.maximum_selection_score).toFixed(6));
      appendCell(row, `${item.winning_window_index} / ${Number(item.winning_window_start_time_s).toFixed(3)} s`);
      appendCell(row, `${Number(item.winning_cfo_hz).toFixed(3)} Hz / ${item.winning_epoch_sample} samples`); appendCell(row, item.derivation);
      overall.append(row);
    }
    node("evidence-symbolwise-window").disabled = streams.length === 0;
    draw(); renderWindow();
    setState(streams.length ? "ready" : "missing", streams.length ? `${payload.point_count} complete fixed-cadence window points loaded. Curves and summaries remain candidate-only.` : "No durable symbolwise replay stream matches the selected hardware filters.");
  }

  async function load() {
    if (!loaded) return;
    const current = ++generation;
    setState("loading", "Loading all 600 durable response points per selected hardware stream…");
    try {
      context ||= await json(`/api/v16/recordings/${encodeURIComponent(recordingId)}/evidence-context`);
      const suffix = parameters();
      if (suffix === null) {
        payload = {streams: [], point_count: 0, summary_derivation: "no hardware selected"};
        render();
        return;
      }
      const result = await json(`/api/v29/recordings/${encodeURIComponent(recordingId)}/symbolwise-replay${suffix ? `?${suffix}` : ""}`);
      if (current !== generation) return;
      if (result.candidate_only !== true || result.calibrated_detection_count !== null || result.window_count_per_stream !== 600) throw new Error("unsafe symbolwise replay semantics");
      payload = result; render();
    } catch (error) {
      if (current !== generation) return;
      payload = null; node("evidence-symbolwise-canvas").hidden = true; node("evidence-symbolwise-window-body").replaceChildren(); node("evidence-symbolwise-overall-body").replaceChildren();
      setState(error.status === 404 ? "pending" : "error", error.status === 404 ? "Durable symbolwise replay is pending or has not been published for this recording." : `Symbolwise replay failed: ${error.message}`);
    }
  }

  document.addEventListener("leo:load-extended-recording-analysis", () => { loaded = true; void load(); });
  node("evidence-controls").addEventListener("change", () => { if (loaded) void load(); });
  node("evidence-symbolwise-window").addEventListener("input", renderWindow);
})();

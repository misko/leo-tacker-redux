(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const recordingId = () => decodeURIComponent(location.pathname.split("/").filter(Boolean).at(-1) || "");
  let extendedLoaded = false;
  let generation = 0;
  const checked = (name) => [...document.querySelectorAll(`#evidence-controls input[name="${name}"]:checked`)];
  const setState = (text, tone, state) => {
    const panel = byId("evidence-receiver-agnostic-cfo-state");
    const badge = byId("evidence-receiver-agnostic-cfo-badge");
    if (!panel || !badge) return;
    panel.textContent = text; panel.dataset.state = state;
    badge.textContent = tone === "ok" ? "Available" : tone === "error" ? "Unavailable" : "Pending";
    badge.dataset.tone = tone;
  };
  async function load() {
    const id = recordingId();
    if (!id || !byId("evidence-receiver-agnostic-cfo-body")) return;
    generation += 1;
    const current = generation;
    const radioIds = [...new Set(checked("radio").map((item) => item.dataset.radioId).filter(Boolean))].sort();
    const receiverIds = [...new Set(checked("receiver").map((item) => item.value))].sort();
    if (!radioIds.length || !receiverIds.length) {
      byId("evidence-receiver-agnostic-cfo-facts").innerHTML = "";
      byId("evidence-receiver-agnostic-cfo-body").innerHTML = "";
      setState("Select at least one recording radio and receiver port.", "warning", "pending");
      return;
    }
    const parameters = new URLSearchParams({maximum_windows: "6"});
    if (radioIds.length) parameters.set("radio_ids", radioIds.join(","));
    if (receiverIds.length) parameters.set("receiver_chain_ids", receiverIds.join(","));
    setState("Loading bounded v0.6 diagnostic evidence…", "warning", "loading");
    try {
      const response = await fetch(`/api/recordings/${encodeURIComponent(id)}/receiver-agnostic-cfo-qam?${parameters}`, {headers:{Accept:"application/json"}});
      if (current !== generation) return;
      if (response.status === 404) { setState("The bounded offline product has not been published for this recording.", "warning", "pending"); return; }
      if (!response.ok) throw new Error(`request failed (${response.status})`);
      const view = await response.json();
      if (current !== generation) return;
      if (view.candidates_only !== true || view.calibrated_detection_count !== null) throw new Error("unsafe product semantics");
      const streamCount = new Set((view.windows || []).map((item) => `${item.radio_id}/${item.receiver_chain_id}`)).size;
      const patternCount = (view.windows || []).reduce((count, item) => Math.max(count, (item.patterns || []).length), 0);
      const domains = [...new Set((view.windows || []).map((item) => `${Number(item.cfo_min_hz) / 1000} to ${Number(item.cfo_max_hz) / 1000} kHz`))];
      byId("evidence-receiver-agnostic-cfo-facts").innerHTML = `<div><dt>Returned streams</dt><dd>${esc(streamCount)}</dd></div><div><dt>Windows</dt><dd>${esc(view.returned_window_count)} / ${esc(view.total_window_count)}</dd></div><div><dt>Patterns per window</dt><dd>${esc(patternCount)}</dd></div><div><dt>Declared CFO domain</dt><dd>${esc(domains.join(", "))}</dd></div><div><dt>Candidate status</dt><dd>Not calibrated</dd></div>`;
      const rows = [];
      for (const window of view.windows || []) for (const pattern of window.patterns || []) rows.push(`<tr><td>${esc(window.radio_id)} / ${esc(window.receiver_chain_id)}<br>samples ${esc(window.start_sample)}–${esc(window.stop_sample)}</td><td>${pattern.pattern_index === 0 ? "Qin" : `Surrogate ${esc(pattern.pattern_index)}`}</td><td>${Number(pattern.winning_score).toPrecision(6)}</td><td>${esc(pattern.winning_cfo_hz)} Hz / ${esc(pattern.winning_epoch_sample)}</td><td>${(100 * Number(pattern.hard_symbol_accuracy)).toFixed(2)}% / ${Number(pattern.rms_evm).toPrecision(5)}</td></tr>`);
      byId("evidence-receiver-agnostic-cfo-body").innerHTML = rows.join("");
      setState("Durable bounded evidence loaded.", "ok", "ready");
    } catch (error) {
      if (current === generation) setState(`Receiver-agnostic CFO/QAM evidence is unavailable: ${error.message}`, "error", "error");
    }
  }
  document.addEventListener("leo:load-extended-recording-analysis", () => {
    if (extendedLoaded) return;
    extendedLoaded = true;
    void load();
  }, {once: true});
  addEventListener("DOMContentLoaded", () => {
    byId("evidence-controls")?.addEventListener("change", () => {
      if (extendedLoaded) void load();
    });
  });
})();

"use strict";
const byId = (id) => document.getElementById(id);
function add(parent, tag, value, className = "") { const node = document.createElement(tag); node.textContent = String(value); if (className) node.className = className; parent.append(node); return node; }
function goodnessBand(value) { return value >= 0.7 ? "high" : value >= 0.35 ? "moderate" : "low"; }
async function loadCanary() {
  try {
    const response = await fetch("/api/v21/canaries/retro-qam/latest", {headers: {accept: "application/json"}, credentials: "same-origin"}); const payload = await response.json();
    if (!response.ok) throw new Error(payload?.error?.message || `request failed (${response.status})`); if (payload.candidate_only !== true || payload.calibrated_detection !== null) throw new Error("unsafe canary semantics");
    const metrics = byId("canary-metrics"); metrics.hidden = false;
    for (const [label, value] of [["Oracle match", payload.metrics_match_oracle ? "PASS" : "FAIL"], ["Combined QAM goodness", `${Number(payload.combined_qam_goodness).toFixed(3)} · ${goodnessBand(Number(payload.combined_qam_goodness))}`], ["Combined accuracy", `${(100 * Number(payload.combined_hard_symbol_accuracy)).toFixed(2)}%`], ["Combined RMS EVM", Number(payload.combined_rms_evm).toFixed(3)], ["Completed UTC", new Date(Number(payload.completed_utc_ns) / 1_000_000).toISOString()], ["Cadence", `${Number(payload.schedule_interval_seconds) / 60} min`]]) { const item = document.createElement("div"); add(item, "span", label, "metric-label"); add(item, "strong", value, "metric-value"); metrics.append(item); }
    const body = byId("canary-body"); for (const receiver of payload.receivers || []) { const row = document.createElement("tr"); for (const value of [`RX${receiver.receiver_index}`, `${Number(receiver.qam_goodness).toFixed(3)} · ${goodnessBand(Number(receiver.qam_goodness))}`, `${(100 * Number(receiver.hard_symbol_accuracy)).toFixed(2)}%`, Number(receiver.rms_evm).toFixed(3), receiver.winning_epoch_sample, `${Number(receiver.winning_cfo_hz).toFixed(2)} Hz`, `${Number(receiver.held_out_verify_score).toFixed(4)} / ${Number(receiver.conditioned_control_score).toFixed(4)} (Δ ${Number(receiver.verify_minus_control_margin).toFixed(4)})`]) add(row, "td", value); body.append(row); }
    const facts = byId("canary-provenance"); for (const [label, value] of [["Corpus", payload.corpus_id], ["IQ SHA-256", payload.iq_object_digest?.value || "Unavailable"], ["Receipt SHA-256", payload.receipt_digest?.value || "Unavailable"], ["Producer commit", payload.git_commit], ["Semantics", "Candidate-only known-positive regression acceptance; not a calibrated detection"]]) { add(facts, "dt", label); add(facts, "dd", value); }
    byId("canary-state").dataset.state = payload.metrics_match_oracle ? "ready" : "error"; byId("canary-state").textContent = payload.metrics_match_oracle ? "Native Redux matches the frozen leo-tracker oracle." : "Canary mismatch: acquisition or QAM separation regressed.";
  } catch (error) { byId("canary-state").dataset.state = "error"; byId("canary-state").textContent = `Canary unavailable: ${error instanceof Error ? error.message : "request failed"}`; }
}
loadCanary();

# ADR 0040: Prompt full-dwell base product

## Decision

Publish contiguous mean-complex-power tiles as an immutable product independent
of the expensive V15 Qin/surrogate response. A dedicated analysis-only pull
service discovers a bounded newest-first set of exact live recordings, resolves
receiver-to-LNB identity through each recording's authoritative hardware link,
and durably leases one product at a time. Capture and primary analysis never
call, await, drain, or inspect this queue.

The approved base plan uses 20,000-sample contiguous non-overlapping tiles and
retains a short final tile. It covers every IQ sample. Pattern-blind top-power
selection identifies at most 32 optional exact-refinement windows per stream.
The base product is committed before its refinement request enters a separate
durable queue. A failed or saturated refinement lane cannot retract the base.

V20 keeps its published schema and prefers the prompt product. If it has not
yet been produced, V20 falls back to the existing V15-derived timeline. V15 is
unchanged. Selection markers describe requested sparse refinement, not a
calibrated detection or proof that exact refinement has completed.

## Integrity and operations

Migration 0044 closes the exact recording data, metadata, manifest, request,
product, and live CAS identities. Publication and admission accept exact
replay and reject identity conflicts. Work uses expiring generation-fenced
leases, at most eight attempts, newest-first bounded admission, and no
filesystem paths constructed from catalog identities. The service receives no
capture role or radio access and has explicit CPU, memory, task, and shutdown
bounds.

The exact-refinement table is admission-only in this release. Existing V15
remains the approved exact overlay until a separately reviewed consumer is
available.

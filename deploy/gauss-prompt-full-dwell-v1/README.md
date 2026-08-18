# Prompt full-dwell timeline operator

Focused analysis cheaply admits its exact two source-closed recording IDs as
soon as their immutable hardware links exist. This optional pull service remains
the sole IQ producer: it also backfills at most two of the newest unpublished
recordings per cycle and processes one recording at a time. Its
20,000-sample contiguous tiles cover every sample, including a short final
tail. For the supported 2.5/5 Msps dwells this is an 8/4 ms base timeline;
20-second and 60-second inputs remain below the immutable 16,384-window bound.

Migration `0044_prompt_full_dwell_timeline.sql` is required. The service has
analysis credentials and read/write CAS access only; it has no capture port,
radio device, pipeline mode lock, or primary-analysis dependency. A 15-minute
fenced lease, eight-attempt terminal park, 3-CPU quota, 3 GiB memory maximum,
and one-job process bound contain resource use and cancellation. Publication
is CAS-first and exact-replay safe. The V20 dashboard prefers this prompt base
product and retains V15 as a compatibility fallback.

Exact refinements are admitted only after the base product is durably complete
and enter their own bounded durable table. This release does not run an exact
refinement consumer; the existing V15 producer remains the compatible overlay
until a later additive consumer is approved.

The focused-backlog threshold is deliberately 64 rather than zero so an active
analysis queue cannot starve this prompt producer. This does not bypass capture
safety: every claim still requires a fresh local guard outside its sampling
interval, the declared CPU/memory/I/O reserve, and the single shared optional
work slot. Focused analysis only admits PostgreSQL work and never reads timeline
IQ or awaits timeline publication.

The deployment acceptance gate is a complete synthetic or exact real 60-second,
5 Msps, two-receiver run of the production power tiler in less than the
120-second focused-capture cadence. Record elapsed time, covered samples, final
tail, and window count; do not enable this inventory if the gate fails.

On 2026-08-18 the production analyzer completed a synthetic zero-CI16 60-second,
5 Msps, two-receiver timeline in 12.409013 seconds: 15,000 shared tile reads,
15,000 windows per stream, and exactly 300,000,000 covered samples from zero
through the final stop on both streams. This is a compute/geometry acceptance
measurement with a bounded lazy source; it does not include catalog or CAS I/O.
The worker's separate CPU, memory, capture-guard, and I/O-pressure gates remain
required for live claims.

For a bounded operator check or backfill step, append `--once`; each invocation
admits at most two newest recordings and processes at most one lease.

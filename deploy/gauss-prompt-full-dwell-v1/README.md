# Prompt full-dwell timeline operator

This optional pull service admits at most two of the newest unpublished,
source-closed recordings per cycle and processes one recording at a time. Its
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

For a bounded operator check or backfill step, append `--once`; each invocation
admits at most two newest recordings and processes at most one lease.

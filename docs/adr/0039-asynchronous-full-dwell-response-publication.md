# ADR 0039: Asynchronous full-dwell response publication

## Decision

Publish the v0.1 full-dwell detector response as a new CAS-first product and
PostgreSQL catalog. Keep V13 and the temporal pilot product unchanged. The
catalog closes the canonical object into the live-reference view and normalizes
the bounded window points needed by the dashboard. Publication is exact-replay
idempotent and conflicting reuse fails closed.

Full-dwell computation runs only in a bounded asynchronous analysis queue. It
has no capture capability and capture never waits for admission or completion.
The queue reports pending, error, saturation/backlog, and truncation explicitly.
At the approved top-32 setting, capacity planning assumes approximately 505
seconds per RX (about 1,010 worker-seconds for a dual-RX dwell).

The additive V15 API provides fixed, least-privilege bounded reads. Its point
identity is recording, segment, radio, receiver chain, edge, method, and window.
Each point retains interval, Qin and surrogate search maxima, winner epoch/CFO,
finite rank, and paired margin. Radio and receiver filters remain independent.

## Scientific disclosure

The cheap pattern-blind prescreen has 100% interval-union coverage. Exact
detector evaluation covers only selected windows, normally top 32 or about
1.28%. The latter is sparse exact coverage and is never “full detector
coverage.” API transport truncation is a third, independent quantity.

## Consequences

The dashboard can render time traces without CAS or filesystem access. A failed
catalog transaction can leave an unreachable CAS leaf, but cannot expose a
partial product. Queue latency is expected and visible; it cannot degrade radio
capture continuity.

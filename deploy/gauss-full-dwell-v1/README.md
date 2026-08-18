# Independent full-dwell producer v1

This optional service discovers recent eligible detector-suite products and
publishes V15 full-dwell responses through a separate bounded lane. It has no
capture capability, mode lock, or dependency from capture. Capture readiness
and drain routines intentionally ignore this optional backlog.

Migration 0042 must be applied before the service starts. Admission is
transactionally serialized, admits at most two new products per cycle by
default, and caps active ready/leased/retry work at eight. One service instance
claims one product at a time with a two-hour fenced lease. Exact product and
work identities make restarts idempotent; invalid work parks, transient work
retries at most three attempts, and stale workers cannot complete.

The same bounded discovery path provides backfill: start the service after
migration and it prioritizes the newest unpublished suites, gradually admitting
older suites as capacity becomes available. `--once` performs one bounded
admission/processing cycle for qualification. There is no migration-time bulk
backfill.

The default top-32 exact plan is expensive (about 1,010 worker-seconds for a
dual-RX recording on the measured host), so this service remains independent
of continuous capture and the capture-critical staged analysis receipt.

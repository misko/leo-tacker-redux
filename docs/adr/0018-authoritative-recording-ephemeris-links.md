# ADR 0018: Authoritative recording-to-ephemeris links

Status: accepted

## Decision

Backfill jobs pin a recording ID, provider, scope, temporal policy artifact, and
`as_of` knowledge boundary. Preparation obtains the exact published recording
object and derives its capture interval from the digest-verified manifest. The
PostgreSQL committer then locks the active lease, verifies the exact recording
identity, stabilizes the ephemeris history, applies the frozen temporal rule,
inserts one immutable link, and completes the job in one transaction.

History stabilization uses a transaction advisory lock keyed by provider and
scope. An `ephemeris_snapshot` insert trigger acquires the same key, creating a
selection linearization point without granting the analysis role `UPDATE` on
immutable recording or ephemeris rows.

`available_then` and `first_after` are supported. `best_ephemeris` remains
fail-closed. A failed or stale transaction exposes neither a link nor successful
job. The executor contains no provider transport or credentials and retries
failures through the existing generation-fenced job repository.

## Scientific boundary

This link proves which archived TLE catalog was selected for a recording. It is
not satellite association and is not ground truth. SGP4/association requires
additional frozen contracts before implementation:

- station position/reference frame and time-dependent Earth-orientation inputs;
- propagation library/version, gravity constants, time scale, and error policy;
- receiver/LNB frequency model and satellite transmitter/channel hypotheses;
- observation uncertainty, visibility/gating rules, association objective, and
  deterministic tie-breaking;
- an independently reviewed or injected label source for accuracy claims.

Normalized TLE objects already preserve exact provider, NORAD ID, element lines,
and epoch inputs, so they are sufficient inputs once these missing contracts are
defined. Model fitting and dashboards consume links through later, separate
interfaces; neither is coupled to the backfill worker.

# ADR 0030: Operator storage-capacity qualification

- Status: accepted
- Date: 2026-08-13

## Decision

Storage capacity is observed by a stateless operator maintenance command and a
systemd oneshot/timer, not by capture, analysis, dashboard, or an additional
application service. Configuration names every root explicitly and supplies
provisional byte/fraction thresholds plus optional estimated acquisition rate
and time-to-full thresholds. The command emits deterministic JSON evidence and
fails closed for inaccessible filesystem views.

Capacity is grouped by device identity. Exact inode aliases do not multiply
estimated rate; distinct configured roots on one device do. No directory walk,
catalog mutation, path-derived scientific state, retention decision, or GC
operation belongs at this boundary.

## Consequences

The check is cheap enough for a periodic timer and does not add NFS/CAS listing
pressure. systemd and the site's monitoring bridge own repetition and routing.
Operators must install real paths and thresholds from measured production
rates; repository examples are not production claims. Estimated time to full
is unavailable when no rate is configured, while mandatory bytes and fraction
alarms still apply.

## Verification

Unit tests inject the clock, stat, statvfs, and path resolver. They cover all
three statuses, alert-floor exit behavior, same-inode aliases, shared-device
rate aggregation, inaccessible roots, and closed config
validation. An installation exercise must additionally prove the monitoring
route and intervention window on the target host.

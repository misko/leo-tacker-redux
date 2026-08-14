# Operator storage-capacity qualification

`leo_flow.maintenance.capacity` is a scheduled operator check, not a fourth
product application service. It inspects only roots named in a closed JSON
configuration by calling `stat` and `statvfs`. It does not enumerate files,
derive scientific state from paths, query a catalog, delete data, or trigger
capture/analysis/dashboard work.

The checked roots should cover every filesystem that can constrain operation:
the capture spool/state area, immutable CAS, and backup destination where those
are locally mounted. Paths and thresholds in
[`capacity.example.json`](../../deploy/storage-capacity/capacity.example.json)
are deliberately provisional examples. An operator must replace them using
measured acquisition/publication rates, outage tolerance, backup size, and
available response time before installation. The example does not describe a
production host.

## Evidence and alert semantics

Every invocation writes exactly one canonical compact JSON object to stdout.
Each configured root includes its resolved path, device/inode identity,
available bytes (`f_bavail`, so reserved blocks do not disguise operator
capacity), total bytes, free fraction, and applicable reasons. When a positive
estimated rate is configured, it also reports estimated seconds to full.
Thresholds are inclusive and status is the worst of:

- available bytes;
- available fraction;
- optional time to full.

Exit status is `0` below the configured `fail_on` floor, `2` for a warning that
meets that floor, `3` for critical, and `4` for an invalid/unreadable
configuration. A missing, inaccessible, or zero-capacity root fails closed as
critical. The message intentionally exposes no exception text.

Roots on one physical device share a single capacity observation and a summed
estimated write rate. Exact device/inode aliases are identified by
`duplicate_of`; their rate is counted only once using the maximum configured
alias rate. One `statvfs` snapshot is taken per device per invocation, avoiding
false disagreement when usage changes between root checks. This also avoids
multiplying capacity or rates for symlink/bind aliases.

The rate is an operational estimate, not measured history. Omit it when it is
unknown; byte and fraction checks remain mandatory. Distinct logical writers
configured on the same device are assumed independent and their rates are
summed. Do not configure both a broad aggregate rate and its component rates on
that device.

## Installation and routing

Validate the local configuration against
[`capacity.schema.json`](../../deploy/storage-capacity/capacity.schema.json),
then run a foreground check before enabling the timer:

```console
/opt/leo-flow/bin/python -m leo_flow.maintenance.capacity \
  --config /etc/leo-flow/storage-capacity.json
systemctl enable --now leo-storage-capacity.timer
systemctl list-timers leo-storage-capacity.timer
```

The example oneshot and timer run every five minutes. systemd records the JSON
in the journal and marks the oneshot failed on a configured alert. Route
`Unit=leo-storage-capacity.service` failures through the site's existing
systemd/Prometheus/journald alert bridge. This repository intentionally does
not invent an email, webhook, paging endpoint, or monitoring credentials.
Repeated failures remain visible in `systemctl status` and
`journalctl -u leo-storage-capacity.service`.

The unit needs search/stat access to every configured root but no write access.
If a root is under a home directory, revise `ProtectHome` narrowly rather than
turning off the other sandbox controls. Keep alert routing outside this command;
the JSON report is its stable monitoring boundary.

## Qualification

Before production, record a capacity exercise that verifies healthy, warning,
critical, inaccessible-root, and estimated-time-to-full outcomes against the
installed filesystem layout. Confirm the monitoring bridge pages on both exit
2 and exit 3, and tune thresholds so the response window precedes operational
fullness by the measured intervention time. This check does not replace a
measured spool-outage test or retention/GC safety qualification.

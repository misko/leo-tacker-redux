# Authoritative hardware metadata

This one-shot operator workflow creates, validates, and publishes an exact
effective-dated hardware snapshot. It is administration around the shared CAS
and PostgreSQL catalog, not a fourth long-running pipeline process. Capture does
not import or call it; capture receives only the exact snapshot ID selected by
an operator.

## Author before publishing

Copy
[`operator-config.example.json`](../../deploy/hardware-metadata/operator-config.example.json)
and validate it against
[`operator-config.schema.json`](../../deploy/hardware-metadata/operator-config.schema.json).
The runtime parser also rejects every unknown or missing field, duplicate JSON
keys, invalid contract IDs, overlapping receiver-chain intervals, and relative
CAS roots.

The example is deliberately fictional and contains `example` and
`not_authoritative` identities. **Do not publish it.** In particular, it is not
metadata for the V5 bench radio at `192.168.1.15`; no LNB is connected to that
radio, so this repository does not invent an LNB asset, polarization, or science
receiver chain for it.

One `receiver_chains` interval is the effective-dated binding of a receiver
identity to all of the following:

| Field | Meaning |
|---|---|
| `radio_id` / `radio_channel` | exact V5 radio asset and physical RX channel |
| `lnb_id` | exact inventoried LNB asset, not a model or mutable nickname |
| `cable_id` | exact wiring/cable epoch; use a new interval after rewiring |
| `polarization` | polarization for that assembled path, or `null` only when unknown |
| `valid_from_utc_ns` / `valid_until_utc_ns` | half-open UTC nanosecond interval; `null` means still installed |

Thus a radio/LNB/cable change closes the old interval and starts a new one. A
radio can be identified as V5 in its stable inventory ID, such as
`radio_v5_<asset>`, but firmware attestations and network addresses are not
hardware-inventory truth in this schema. Preserve those separately in capture
provenance. Every radio used by a receiver path must appear in `radio_ids`.

Choose a new immutable `snapshot_id` for changed content. The catalog forbids
reusing an ID or deterministic publication key for different bytes; there is no
`latest` lookup or mutable alias.

## Create and inspect exact identity

The output bundle is canonical JSON. `create` will not overwrite different
bytes; rerunning it with the same config and destination is idempotent.

```console
python -m leo_flow.hardware create \
  --config /etc/leo-flow/hardware/station-a-v5.json \
  --output /var/lib/leo-flow/hardware/hw_station_a_v5_20260813.json

python -m leo_flow.hardware validate \
  --bundle /var/lib/leo-flow/hardware/hw_station_a_v5_20260813.json
```

Both commands emit one JSON object containing `snapshot_id`,
`digest_algorithm`, `digest_value`, and `byte_count`. Record that exact
ID/digest pair in the change ticket and use it in downstream analysis inputs.

## Dry-run and publish

The config contains only the systemd credential name, never a DSN or password.
Supply the named credential through `CREDENTIALS_DIRECTORY`. A dry-run validates
that the bundle exactly equals the authored snapshot; it performs no credential
lookup and creates neither a CAS directory nor a database connection.

```console
python -m leo_flow.hardware publish \
  --config /etc/leo-flow/hardware/station-a-v5.json \
  --bundle /var/lib/leo-flow/hardware/hw_station_a_v5_20260813.json \
  --dry-run

sudo systemd-run --wait --pipe --collect \
  -p LoadCredential=hardware-catalog-dsn:/etc/leo-flow/secrets/hardware-catalog-dsn \
  -p Environment=PYTHONPATH=/opt/leo-flow/src \
  /opt/leo-flow/.venv/bin/python -m leo_flow.hardware publish \
  --config /etc/leo-flow/hardware/station-a-v5.json \
  --bundle /var/lib/leo-flow/hardware/hw_station_a_v5_20260813.json
```

Real publication writes canonical bytes to content-addressed storage first,
then atomically records normalized PostgreSQL projections through the existing
hardware catalog port. It reads the result back by the exact ID/digest pair
before reporting success. Repeating the same command is idempotent. Diagnostics
never include configuration, credential names, DSNs, paths, or exception text;
failure is a single `hardware_operator_failed` JSON event on stderr.

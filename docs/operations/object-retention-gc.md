# Object retention and garbage collection

Garbage collection is an operator-only maintenance operation. Do not use a
capture, analysis, or dashboard database service for it, and do not grant those
processes remote-delete credentials.

## Policy workflow

1. Insert an explicit `object_retention_policy`. Use
   `allow_remote_delete = false` for indefinite holds. While detector methods
   remain unsettled, raw recording objects should remain held.
2. Assign the policy to exact `(digest_algorithm, digest_value)` identities.
   `assigned_at + retain_for_seconds + grace_period_seconds` is the queryable
   deadline. Multiple policies are conservative: all must permit deletion and
   the latest deadline wins.
3. Review `object_retention_status` and `object_gc_candidate`. A candidate must
   show zero live references. Never select objects by locator, date directory,
   media type, or NFS placement.
4. Run the maintenance composition with its private libpq service file and the
   delete-capable CAS credential. Keep ordinary blob-reader credentials
   read-only.
5. Review `object_gc_attempt` and all `gc_delete_failed`/expired `gc_claimed`
   rows. A missing remote object on retry is treated as an idempotent delete;
   the fenced catalog transition still has to complete.

Example queries:

```sql
SELECT * FROM object_retention_status
ORDER BY eligible_after NULLS LAST, digest_value;

SELECT * FROM object_gc_candidate
ORDER BY eligible_after, digest_value;

SELECT * FROM object_gc_attempt
ORDER BY attempt_id DESC LIMIT 100;
```

The catalog keeps `gc_deleted` tombstones. Do not delete or revive them by
hand. Remote failure details are intentionally sanitized; use restricted
maintenance logs for infrastructure diagnostics.

## Failure invariants

- Database claim failure performs no remote delete.
- A new live reference and a GC claim cannot both commit.
- Remote failure leaves a retryable, non-live catalog object and an audit row.
- Process death after remote deletion leaves an expiring claim; retry is safe.
- Successful deletion creates a tombstone, preventing runtime resurrection.
- Unregistered leftover bytes are acceptable and remain visible to a separate
  storage reconciliation process; missing bytes behind a live reference are
  never acceptable.

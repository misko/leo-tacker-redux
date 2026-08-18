# Gauss pilot-refinement worker

This optional analysis-only service consumes the durable work ledger created by
migration 0049. It never participates in capture admission. One bounded worker
opens the immutable recording once, evaluates every complete-IQ prescreen seed
with the same Qin and precommitted-surrogate search, publishes the canonical
candidate-only bundle, and then completes its fenced lease.

Run one worker initially. Scale only after capture continuity and backlog
measurements show sufficient CPU headroom; every worker is independently
idempotent and fenced by PostgreSQL.

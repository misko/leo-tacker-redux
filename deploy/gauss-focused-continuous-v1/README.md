# Continuous focused CH4-lower collection

This user service replaces the ten-minute one-shot timer.  Its Python
supervisor continuously plans and captures synchronized 20-second CH4-lower
radio pairs.  As soon as a pair closes terminally, the supervisor dispatches
its six exact FeatureSet, waterfall/Doppler, and Starlink suite/null/QAM jobs in
a child process and immediately begins preparing the next capture.

Up to eight pair analyses may be in flight.  PostgreSQL migration 0037 records
each terminal pair and its exact six jobs before any lease is claimed; the
capture gate permits only those registered jobs to overlap radio work.  An
unregistered lease, low-capacity gate, uncertain capture, failed analysis, or
conflicting journal transition halts the service and creates
`failure-latch.json`.  Capture is never replayed.  After a clean service
restart, terminal captured pairs and idempotent analysis are resumed from the
full-sync SQLite journal.

The 30-second lead is active capture preparation, not a ten-minute scheduling
pause.  The service has no timer and remains continuously active until stopped
or a fail-closed condition occurs.

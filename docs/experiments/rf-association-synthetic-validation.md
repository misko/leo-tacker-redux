# Reproducible SGP4 and RF association validation slice

This experiment exercises the pinned offline SGP4 adapter and the RF
association decision together. It is a bounded software-validation corpus, not
a satellite-identification accuracy claim.

## Frozen inputs

| Input | Frozen choice |
|---|---|
| Orbit implementation | `sgp4==2.25`, Vallado improved mode, WGS72 |
| Frame/time profile | `teme-pef-gmst82-no-eop-v1`, UTC used as UT1 |
| TLE source | Compact cases copied from the Vallado SGP4 verification distribution |
| Station | ITRF `(6378135, 0, 0)` metres |
| RF carrier | 1 GHz for every candidate |
| Receiver calibration | +1250 Hz bias, -0.15 Hz/s drift, pinned variances |
| Injection | SHA-256 counter noise, seed `20260813`, ±0.25 Hz and ±0.002 Hz/s |
| Specification fixture | `tests/model_analysis/fixtures/rf_association_synthetic_v1.json` |

The 33333 error case retains the published orbital fields while correcting only
the two legacy checksum digits. That derivative is identified in the fixture
and exists only so the strict archive parser can reach the published SGP4 error
path.

## Independent paths

The experiment has two code paths:

1. `inject_synthetic_rf_measurement` generates frequency and drift directly
   from a pinned state, carrier, calibration, light speed, offsets, and
   counter-based noise. It neither calls nor imports the association scorer or
   a prediction helper.
2. `associate_rf_measurement` independently propagates every candidate through
   `Sgp4OrbitPropagator`, applies the production gates, and scores the injected
   measurement.

The counter-based noise is a SHA-256 function of seed, case ID, and axis. It is
independent of test order and Python pseudo-random-number implementation.

## Expected matrix

| Scenario | Times/candidates | Expected observed class |
|---|---|---|
| `match_early` | -650 minutes from 6251 epoch; 6251 and 8195 | match 6251 |
| `match_late` | +140 minutes; 6251 and 8195 | match 6251 |
| `ambiguous_policy` | +140 minutes; both accepted under an explicit broad ambiguity policy | ambiguous |
| `residual_no_match` | +45 minutes; independent +50 kHz injection; 6251 and 8195 | no match |
| `below_elevation` | epoch; both candidates below the 5° gate | below elevation |
| `propagation_error` | +25 minutes from 33333 epoch | propagation error `sgp4:4` |

The report emits ordered per-case results and a sparse confusion matrix. Its
digest closes over the experiment reference, decisions, expected classes, and
matrix. Repeated execution must produce an identical report and six passing
cases.

## Truth boundary and limitations

Digital injection makes the injected RF values and intended software outcome
exact synthetic truth. It does **not** make the chosen TLE, station geometry,
carrier assignment, RF calibration, or satellite identity observational ground
truth. In particular, this is partially a self-consistency test because the
injection state and candidate state use the same pinned SGP4 profile. Published
Vallado TEME vectors independently cover the adapter's native propagation, but
they do not validate the simplified TEME/PEF/ITRF observation model against a
real pass.

Claims about real association performance require separately established
labels, such as a controlled transmitter injection, coordinated beacon truth,
or another reviewed observation source that is not derived from this scoring
path. Such observations should be partitioned by pass/session before model
selection. TLE proximity alone must remain an association hypothesis.

This slice intentionally excludes multi-recording tracking, orbit fitting,
Kalman filtering, model training, network retrieval, mutable ephemeris lookup,
and dashboard publication.

## Reproduction

Run the focused experiment with the optional orbit dependency installed:

```console
uv run --extra orbit pytest -q tests/model_analysis/test_rf_association_validation.py
```

The fixture is an input specification, not a captured golden output. Do not
rewrite it merely because a future implementation changes a result; investigate
the numerical or policy difference and create a new experiment identity when a
scientific choice changes.

# Gauss 36-slot continuous supercycle canary

This is the mandatory finite gate between v8 qualification and the 936-slot
continuous main campaign. It exercises each of the nine rate/dwell cells under
each of `L/L`, `L/U`, `U/U`, `U/L` exactly once. It cannot authorize or share
state with main collection.

## Checkpoints

| Checkpoint | Exact pass evidence | Failure action |
|---|---|---|
| Offline identity | sealed release hashes; migration head 0030; exact v8 receipt digest; `.20/.21` station/runtime identities | do not arm |
| Admission | no capture/analysis owner; both radios passive with both TX/DDS/constant-IQ disabled; capacity at least `2 × 1,254,400,000 + margin` | do not arm |
| Capture | 36 fresh terminal eligible pairs; 72 unique recordings; every skew `<100 ms`; 40 ms 1/2/4 contiguous refills; no miss/catch-up | halt and preserve; never replay |
| Stage barriers | capture service succeeded first; feature, waterfall and Starlink suite compute at 8 workers, projections at 4; migration-0030 scoped identities exact | halt and preserve |
| Product closure | exactly 72 FeatureSets, waterfalls, terminal Starlink v0.2 suites and dashboard recording/detail projections; no 5xx | no receipt |
| Benchmark | immutable wall, process CPU, peak RSS for each of six stages plus capture latency/skew distributions | review before main936 |
| Receipt | atomically written canary-v1 receipt bound to exact definition and v8 receipt, with `main_campaign_authorized=false` | never promote from partial evidence |

The Starlink suite output is candidate-only. The 1.25 MS/s streams are terminal
`not_evaluated`; 2.5/5 MS/s streams are eligible for all eight methods. Neither
the operator nor dashboard may turn candidates into a detection count.

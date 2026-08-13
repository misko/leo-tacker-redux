# Wave 0 execution

Wave 0 freezes the smallest coherent contract and validates the recording
format before production implementation begins.

| Workstream | Branch | Output | Gate |
|---|---|---|---|
| Contracts/platform | `wave0/contracts` | Contract v0.1, narrow ports, golden fixtures and conformance tests | Schema, hash, unit, time and import rules pass |
| Recording format | `wave0/format-spike` | Measured HDF5/SigMF comparison and repeatable spike | One format chosen with remaining Pi gates explicit |
| Benchmark/test data | `wave0/benchmark` | Deterministic corpus manifest, split/label rules and oracle schema | Membership and hashes validate without copying raw IQ |
| Integration steward | `main` | ADRs, dependency lock, CI, merge and integration gates | All three branches reviewed before contract freeze |

No production detector, model, dashboard, or hardware deployment begins until
the Wave 0 gate is recorded in an ADR.

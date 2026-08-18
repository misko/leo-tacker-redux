# Receiver-agnostic residual-CFO search v0.6

Status: additive offline candidate-evidence component. The immutable legacy
symbolwise replay, acquisition v0.3, adaptive QAM v0.4, and pattern-symmetric
QAM v0.5 contracts remain unchanged. This component defines no persistence,
dashboard, deployment, live-radio access, detection threshold, or calibration
claim.

## Contract and search strategy

One immutable plan is applied unchanged to every radio and receiver. The API
accepts no LNB identifier, receiver label, frequency center, or correction
table. Its default declared residual domain includes both endpoints from
-700,000 through +700,000 Hz. An endpoint-preserving coarse CFO grid is crossed
with an endpoint-preserving epoch grid. Every Qin and precommitted surrogate
gets the same basin quota. Their retained basins are unioned, then every pattern
is evaluated on every cell in the union of coarse and local grids.

Local epoch radius is required to bridge every residue between adjacent coarse
epochs. Local CFO radius is required to bridge adjacent coarse CFO cells.
Multiple separated basins survive until the symmetric local pass; there is no
Qin-only center, target-only refinement, or receiver-specific offset.

The receipt stores every unique `(epoch_sample, cfo_hz)` cell, its coarse/local
stage, all patterns that selected its local neighborhood, and the score for
every pattern. It also stores exact coarse, local, unique-cell, total
pattern-evaluation, and look-elsewhere counts. The declared look-elsewhere
family is exactly `unique cells × patterns`; no unreported trial factor exists
inside this component.

## Strategy, cost, and output comparison

| Component | Residual CFO domain | Search | Cost accounting | Output |
|---|---:|---|---|---|
| Legacy explicit-center replay v0.1 | receiver center ±350 kHz | nine timing CFOs, four retained epochs, clipped three-cell symbolwise and ±2 kHz conditioned refine | per-pattern coarse/refinement counts; center determines reachable domain | candidate-only pattern winner |
| Current acquisition v0.3 / QAM v0.4-v0.5 | at least ±400 kHz, normally receiver-profile bound | all coarse epochs, eight basins, 500 Hz local CFO, held-out exact/control | coarse and refinement counts per acquisition run | candidate-only acquisition and known-pattern QAM |
| Receiver-agnostic v0.6 | at least **±700 kHz**, identical for every radio/RX | endpoint-preserving coarse grid, equal basins per Qin/surrogate, all-pattern union local refine | exact cells and exact pattern-by-cell look-elsewhere family, rejected before local scoring if any ceiling is exceeded | one candidate winner per pattern plus complete cell provenance; never a verdict |

For a 64-sample epoch modulus, the default v0.6 coarse stage has 15 CFOs × 9
epochs = 135 unique cells. Its hard ceilings are 100,000 coarse cells, 100,000
local cells, 150,000 total unique cells, and 1,000,000 pattern evaluations.
The component fails closed rather than truncating patterns, basins, endpoints,
or cells. Cost is intentionally explicit because symmetric controls can be
more expensive than a single-pattern predecessor search.

## Numerical matrix and scientific limits

Component tests cover exact ±700 kHz endpoints, every epoch residue, competing
alias basins, flat noise, a stronger wrong pattern, surrogate-label
permutation, bounded-cost rejection, J1 early/late receiver-CFO extrema, and
both frozen RETRO receiver winners. J1 and RETRO values are conditioned
numerical coverage canaries only. They are not training observations,
calibration members, empirical nulls, threshold evidence, or a claimed sample
size. A later independently frozen calibration study must include the complete
time/epoch/CFO/pattern search and its recorded look-elsewhere family before any
detection operating point can be claimed.

# 0007: Fail-closed V5 startup attestation

Status: proposed

## Context

V5 continuity metadata requires a matched patched Python binding, native
libiio, SPF parser, and radio firmware.  An ordinary PyPI `pylibiio` binding
can still read IQ, but it cannot establish source continuity.  Configuration
defaults are claims, not observations, and must not become recording
provenance.

## Decision

The production V5 composition performs preflight before exposing a radio to
the capture engine.  It compares immutable expected facts from the separately
built runtime manifest with immutable observations supplied by the runtime
boundary.  The capture-owned comparison covers:

- Python `iio` module path, exact version and source commit, and
  `iio.MetadataBuffer`;
- loaded native libiio path, exact version and source commit;
- standard `ip` and `usb` backends, exact pyadi version, and exact SPF module,
  revision, and metadata protocol;
- observed radio serial and V5 firmware string, metadata capability, paired
  scan mask, channel count, and native `I0,Q0,I1,Q1` CI16 layout.

Any mismatch refuses startup.  The accepted observations produce the
`CaptureProvenance`; caller-supplied firmware and host-version defaults do not.
Only standard libiio `ip:` and `usb:` contexts are accepted.  Custom direct-IP
and direct-USB protocols need separate adapters and qualification.

The comparison layer imports no libiio, pyadi, or SPF packages.  Runtime
packaging owns collection and validation of observations, allowing offline
fakes without coupling capture contracts to vendor types.

## Consequences

- Dependency replacement, binding/native skew, wrong firmware, and wrong
  two-receiver layout fail before capture.
- An immutable runtime can be upgraded by supplying a new manifest and
  qualification evidence without changing recording contracts.
- Merely constructing the low-level `PlutoPairedRadio` remains possible for
  tests and explicitly unverified capture.  Production composition must use
  `create_attested_v5_radio`.
- Live-host acceptance still must prove that the runtime observation provider
  reads the loaded library rather than an unrelated file and that radio facts
  are observed from the selected context.

## Compatibility and fixtures

No public serialized contract changes.  Existing recording fixtures remain
valid.  New mismatch tests are capture-adapter fixtures only.

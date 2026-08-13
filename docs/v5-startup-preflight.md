# V5 startup preflight

Production V5 capture is composed through
`leo_flow.capture.drivers.create_attested_v5_radio`.  It takes a qualified
runtime expectation, an expected radio identity, injected host/radio
observers, a standard libiio device factory, and the SPF V3 metadata reader.

The runtime builder owns `deploy/v5-runtime/manifest.json`, schema
`leo-flow.v5-runtime/v1`.  Capture mirrors its qualified facts into
`ExpectedV5Runtime`; it does not construct paths or import deployment code.
The runtime observation provider must report the module and native library
that the current process actually loaded, not merely files present on disk.
The runtime verifier's `--json` result maps directly to `ObservedV5Runtime`:
`manifest_schema` becomes `schema`, `libiio_binding_path` becomes
`iio_module_path`, `libiio_native_paths` becomes an immutable tuple, and the
source-commit, backend, pyadi, and SPF fields retain their meanings.  A
successful verifier result establishes `metadata_buffer_present=True`; a
failed verifier must never be converted into observations.

Startup order is fail-closed:

1. Reject every URI except non-empty standard `ip:` and `usb:` contexts.
2. Observe and attest the host runtime before opening the radio.
3. Open exactly the selected context and observe its radio facts.
4. Attest V5 firmware, metadata capability, serial, scan mask, paired channel
   count, and native CI16 component order.
5. Construct the adapter with provenance derived from accepted observations.

The low-level `PlutoPairedRadio` constructor remains injectable for tests.  It
is not the production startup boundary.

The initial qualified expectation is:

| Item | Exact value |
|---|---|
| Runtime schema | `leo-flow.v5-runtime/v1` |
| libiio tag | `spf-frame-metadata-source/v0.25-final-v3` |
| libiio commit | `c26258bfa33098c2b215e19cf85d448e89499b1a` |
| libiio version tuple | `(0, 25, c26258b)` |
| Python capability | `iio.MetadataBuffer` |
| pyadi-iio | `0.0.21` |
| SPF revision | `c40ee4116546889effd72056115adaaa1bc3fd40` |
| SPF parser | `spf.direct_radio.iio_metadata:IioMetadataRx` |
| Metadata protocol | `spf-radio-metadata-v3` |
| Standard transports | `ip:`, `usb:` |

## Live-host acceptance gates

- Prove a stock PyPI `pylibiio` environment is refused.
- Prove a patched binding resolving the system native libiio is refused.
- Prove both `ip:` and `usb:` enumerate the required context and backends.
- Read serial, `fw_version`, `iio,buffer-metadata`, and scan layout from the
  selected radio rather than configuration.
- Confirm the first metadata record repeats paired scan mask `0x0f`, two
  channels, and eight bytes per paired CI16 sample.
- Record the successful attestation alongside capture-service startup logs;
  do not serialize local deployment paths into scientific recording contracts.

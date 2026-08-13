# Repository collaboration rules

- `leo-tracker` is a reference and numerical oracle, never a runtime dependency.
- Public contracts are immutable within a published major version.
- Components communicate through contracts and narrow ports, never private ORM
  models, constructed storage paths, or another component's implementation.
- Do not add shell workflow engines or NFS control-plane files.
- Every component change includes component-owned tests.
- Cross-component integration tests, dependency files, deployment files, and
  ADR approval are owned by the integration steward.
- Do not regenerate golden fixtures merely because a test fails.

# Site deployment readiness

The site-readiness bundle turns the remaining operator-owned choices into one
closed, reviewable manifest before any file is installed or service is started.
It binds candidate bytes to exact destination paths and checks cross-component
consistency. It is an offline planning gate, not evidence that the candidate
host, radio, CAS, PostgreSQL cluster, reverse proxy, or alert bridge works.

The checker reads only:

- the manifest passed with `--manifest`; and
- candidate files beneath the explicit `--repository-root`.

It does not resolve a systemd credential, read a credential source, inspect a
mount, open a socket, contact PostgreSQL or a radio, call `systemctl`, write an
installation tree, or start a process. Its receipt records every external-access
category as false.

## Manifest boundary

Start from
[`site-readiness.example.json`](../../deploy/site-readiness-v1/site-readiness.example.json)
and validate its closed shape against
[`site-readiness.schema.json`](../../deploy/site-readiness-v1/site-readiness.schema.json).
The checked example is deliberately not deployable: `REPLACE_WITH_` fields,
the provisional capacity roots, and the placeholder off-host identities make
qualification fail closed.

Prepare site-specific copies of the referenced configuration and unit files
under the reviewed repository root. Do not edit a digest to bless an unreviewed
change. Review the candidate first, then record its exact SHA-256. A source path
must be relative to the repository root; a destination must be an exact,
normalized absolute path. The checker refuses traversal outside the repository
and emits a sorted install plan but never applies it.

The manifest binds these inputs:

| Boundary | Required site assertion | Consistency check |
| --- | --- | --- |
| Capture | reviewed config bytes, plan source reference, plan ID/digest, radio adapter reference, radio ID, and serial | strict capture loader and equality with the immutable V5 deployment constants |
| Analysis | approved `module:PLUGIN`, worker roster, config bytes, destination, and unique runtime instance IDs | strict analysis loader, template `%i` path, plugin argument, target worker inventory, and unique IDs |
| Ephemeris | reviewed canary config and receipt retention | strict canary loader; `mode=offline`, `network_approved=false`, and no credential capability |
| CAS/capacity | off-host config, exact mount/source/filesystem/mount-root/group, `2770` group policy, service access, and capacity thresholds | existing off-host no-contact preflight plus exactly one capacity root equal to the CAS root |
| PostgreSQL | host, port, database, TLS mode, four scoped credential names and four source paths | database identity and credential names match the off-host config; source basenames match credential names |
| Dashboard | loopback application config, HTTPS public origin, proxy kind, authentication policy reference, certificate reference, and private-key reference | strict dashboard loader, loopback-only bind, and unit credential wiring |
| Operations | health config, receipt path, incident directory, alert route, and health/off-host/ephemeris retention days | strict health loader and exact health-unit receipt/config arguments |
| systemd | ten pinned candidate units and their exact `/etc/systemd/system` names | static unit text checks for configs, plugin, credentials, CAS, target workers/timers, and receipt path |

Credential entries contain only a systemd credential name and the path from
which an authorized installer will later load it. They never contain a DSN,
username/password pair, token, certificate, or private-key bytes. The checker
does not open those paths. PostgreSQL endpoint fields contain no userinfo.
Dashboard authentication and TLS entries are inventory references to separately
managed policy/key material, not that material itself.

## Offline qualification

After every placeholder is replaced and every digest is recorded, run:

```console
/opt/leo-flow/bin/python -m leo_flow.deployments.site_readiness \
  --manifest deploy/site-readiness-v1/site-readiness.site.json \
  --repository-root /path/to/reviewed/leo-tracker-redux
```

Exit `0` means the manifest and pinned candidates are complete and mutually
consistent. Exit `2` means qualification ran and at least one gate failed. Exit
`3` means the manifest or candidate set could not be safely read. Stdout is one
canonical JSON receipt; stderr is sanitized. A passing receipt contains:

- the manifest digest and site identity;
- reviewed capture, worker, endpoint, proxy, and retention identities;
- a destination-sorted install plan with pinned source digests;
- every static consistency gate; and
- explicit confirmation that no external access occurred.

Save the receipt in the operator evidence system named by site policy. Do not
put it in the CAS, use it as queue state, or have a service watch it. Any change
to a candidate file, destination, worker roster, policy reference, endpoint, or
credential binding requires review and a new manifest digest.

The checker qualifies the analysis plugin reference in the unit, but cannot
import a site-owned package that is intentionally absent from this repository.
Package provenance and the exported `PLUGIN` attribute remain separate release
evidence.

## Static unit verification

The component test materializes the pinned units under their exact unit names in
a temporary search path and runs `systemd-analyze verify`. It neither installs
units nor contacts a service manager:

```console
.venv/bin/pytest -q tests/services/test_site_readiness.py
```

Repeat static verification for any site-modified target or unit candidate before
accepting its new digest. A static pass proves unit syntax and dependency
resolution only; it does not execute start, crash, restart, stop, or reset
lifecycle behavior.

## Promotion boundary

Review the install plan and receipt before a separately authorized installer
copies any file. This checker intentionally has no install or launch command.
After installation, the operator must still complete the existing gates:

1. read-only off-host CAS/PostgreSQL inspection and cross-host comparison;
2. the explicitly armed immutable cross-host byte probe, if required by site
   acceptance;
3. dashboard proxy authentication rejection and TLS verification;
4. capacity warning/critical alert routing and retention exercises;
5. offline ephemeris and systemd health receipt production/retention; and
6. separately authorized radio/hardware qualification for the exact reviewed
   capture plan and radio identity.

Do not treat this offline receipt as authorization to contact the radio, mount
storage, query the database, load secrets, or start `leo-flow.target`.

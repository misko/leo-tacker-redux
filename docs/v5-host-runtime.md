# Pluto V5 host runtime

Verified V5 capture runs in the image defined by
`deploy/v5-runtime/Dockerfile`. The image is the deployment boundary: it holds
the patched native libiio, its matching generated Python binding, exactly
pyadi-iio 0.0.21, and only the three SPF modules needed to parse metadata and
fit sample time. None are loaded from `/tmp`, a checkout, `PYTHONPATH`, or the
host linker search path.

The reviewed inputs and installed paths are recorded in
`deploy/v5-runtime/manifest.json`. `verify_runtime.py` checks the binding
version and `MetadataBuffer`, the native library's loaded path, Python package
versions, the SPF import, and every installed SPF source digest. It runs while
the image is built and should run as the capture process's startup preflight.
An ordinary PyPI pylibiio cannot satisfy this build: pyadi is installed without
dependencies and the generated binding is installed last from the exact
libiio source commit. A separate image-build stage deliberately replaces that
binding with PyPI pylibiio and requires verification to reject the result; the
tainted stage is never copied into the runtime image.

## Build and inspect

From the repository root:

```bash
docker build --pull \
  --file deploy/v5-runtime/Dockerfile \
  --tag leo-flow-v5:c26258b-c40ee41 .

docker run --rm leo-flow-v5:c26258b-c40ee41
docker run --rm leo-flow-v5:c26258b-c40ee41 \
  /opt/leo-v5/bin/verify-runtime --json
docker image inspect leo-flow-v5:c26258b-c40ee41 \
  --format '{{json .RepoDigests}}'
```

Publish and deploy an image by digest, never by a mutable tag. The first build
needs network access to the two Git repositories, Debian repositories, and
PyPI. Archive the resulting multi-architecture OCI image and its digest for
offline rebuild-independent deployment.

## Standard transports

The runtime supports ordinary libiio contexts and V5 metadata buffers:

| Transport | URI example | Container access |
|---|---|---|
| Standard network | `ip:192.168.1.15` | Route to the radio; no host library mount |
| Standard USB | URI reported by `iio_info -S` | Pass the one resolved USB device; do not mount every host USB device by default |

The image entrypoint verifies the runtime before executing any supplied
command. For an IP discovery check:

```bash
docker run --rm --network host \
  leo-flow-v5:c26258b-c40ee41 \
  /opt/leo-v5/bin/iio_attr -T 2000 \
  -u ip:192.168.1.15 -C iio,buffer-metadata
```

SPF custom `direct-ip` and `direct-usb` are different wire protocols. They are
explicitly outside this image and require separate adapters; their names must
not be used for standard `ip:` or `usb:` libiio contexts.

## Dependency refresh rule

Do not run pip in a deployed container. Rebuild from the Dockerfile, run the
verification entrypoint, run Redux tests in CI, then publish a new image by
digest. A pyadi or numpy change requires a manifest change and fresh hardware
qualification. A libiio or SPF change additionally requires updating immutable
commits and reviewed source hashes.

## Reproducibility limits

The source commits, base-image index, and application versions are pinned, but
Debian packages and PyPI wheels are fetched from live repositories and are not
yet mirrored in a checksummed source bundle. Docker output can also vary by
architecture and builder. Production promotion therefore requires retaining
the built OCI digest/SBOM. A later hardening step should create a signed,
checksummed amd64/arm64 wheel-and-deb bundle in an immutable release.

# syntax=docker/dockerfile:1.7
ARG DEBIAN_IMAGE=docker.io/library/debian:12.11-slim@sha256:b1a741487078b369e78119849663d7f1a5341ef2768798f7b7406c4240f86aef

FROM ${DEBIAN_IMAGE} AS libiio-build
ARG LIBIIO_REPOSITORY=https://github.com/misko/libiio.git
ARG LIBIIO_REF=spf-frame-metadata-source/v0.25-final-v3
ARG LIBIIO_COMMIT=c26258bfa33098c2b215e19cf85d448e89499b1a
RUN apt-get update && apt-get install -y --no-install-recommends \
      bison ca-certificates cmake flex g++ git libaio-dev libusb-1.0-0-dev \
      libxml2-dev make pkg-config python3-dev python3-pip && \
    rm -rf /var/lib/apt/lists/*
RUN git -c advice.detachedHead=false clone --depth 1 --branch "${LIBIIO_REF}" \
      "${LIBIIO_REPOSITORY}" /src/libiio && \
    test "$(git -C /src/libiio rev-parse HEAD)" = "${LIBIIO_COMMIT}"
RUN cmake -S /src/libiio -B /build/libiio \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/leo-v5 \
      -DINSTALL_UDEV_RULE=OFF \
      -DPYTHON_BINDINGS=ON \
      -DPYTHON_EXECUTABLE=/usr/bin/python3 \
      -DHAVE_DNS_SD=OFF \
      -DWITH_DOC=OFF \
      -DWITH_EXAMPLES=OFF \
      -DWITH_LOCAL_BACKEND=ON \
      -DWITH_NETWORK_BACKEND=ON \
      -DWITH_SERIAL_BACKEND=OFF \
      -DWITH_TESTS=ON \
      -DWITH_USB_BACKEND=ON && \
    cmake --build /build/libiio --parallel && \
    cmake --install /build/libiio && \
    python3 -m pip wheel --no-deps --wheel-dir /wheels /build/libiio/bindings/python

FROM ${DEBIAN_IMAGE} AS spf-source
ARG SPF_REPOSITORY=https://github.com/misko/spf.git
ARG SPF_COMMIT=c40ee4116546889effd72056115adaaa1bc3fd40
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none --no-checkout "${SPF_REPOSITORY}" /src/spf && \
    git -C /src/spf fetch --depth 1 origin "${SPF_COMMIT}" && \
    git -C /src/spf checkout --detach "${SPF_COMMIT}" && \
    test "$(git -C /src/spf rev-parse HEAD)" = "${SPF_COMMIT}"
RUN mkdir -p /minimal/spf/direct_radio && \
    cp /src/spf/spf/__init__.py /minimal/spf/__init__.py && \
    cp /src/spf/spf/direct_radio/__init__.py \
      /src/spf/spf/direct_radio/iio_metadata.py \
      /src/spf/spf/direct_radio/sample_clock.py \
      /src/spf/spf/direct_radio/usb_protocol.py /minimal/spf/direct_radio/

FROM ${DEBIAN_IMAGE} AS runtime
ARG PYADI_VERSION=0.0.21
ARG NUMPY_VERSION=2.4.6
ARG PSYCOPG_VERSION=3.3.4
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libaio1 libusb-1.0-0 libxml2 python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*
COPY --from=libiio-build /opt/leo-v5 /opt/leo-v5
COPY --from=libiio-build /wheels /wheels
COPY --from=spf-source /minimal /opt/leo-v5/python
COPY deploy/v5-runtime/manifest.json /opt/leo-v5/runtime-manifest.json
COPY deploy/v5-runtime/verify_runtime.py /opt/leo-v5/bin/verify-runtime
COPY deploy/v5-runtime/entrypoint.py /opt/leo-v5/bin/runtime-entrypoint
COPY deploy/v5-canary/capture.json /opt/leo-v5/deploy/v5-canary-capture.json
COPY deploy/v5-scan/capture.json /opt/leo-v5/deploy/v5-scan-capture.json
COPY . /src/leo-tracker-redux
RUN printf '%s\n' /opt/leo-v5/lib /opt/leo-v5/lib64 \
      > /etc/ld.so.conf.d/leo-v5.conf && ldconfig && \
    python3 -m pip install --no-cache-dir --break-system-packages \
      "numpy==${NUMPY_VERSION}" "psycopg[binary]==${PSYCOPG_VERSION}" && \
    python3 -m pip install --no-cache-dir --break-system-packages --no-deps \
      "pyadi-iio==${PYADI_VERSION}" && \
    python3 -m pip install --no-cache-dir --break-system-packages --no-deps \
      /wheels/pylibiio-*.whl /src/leo-tracker-redux && \
    cp -R /opt/leo-v5/python/spf /usr/local/lib/python3.11/dist-packages/ && \
    rm -rf /wheels /src/leo-tracker-redux && \
    chmod 0555 /opt/leo-v5/bin/verify-runtime \
      /opt/leo-v5/bin/runtime-entrypoint && \
    /opt/leo-v5/bin/verify-runtime && \
    python3 -c 'import psycopg; assert psycopg.__version__ == "3.3.4"' && \
    python3 -m pip check

FROM runtime AS dependency-refresh-test
RUN python3 -m pip install --no-cache-dir --break-system-packages --no-deps \
      --force-reinstall pylibiio==0.25 && \
    if /opt/leo-v5/bin/verify-runtime; then \
      echo "ordinary PyPI pylibiio escaped runtime verification" >&2; exit 1; \
    fi && \
    touch /dependency-refresh-detected

FROM runtime AS final
COPY --from=dependency-refresh-test /dependency-refresh-detected \
  /opt/leo-v5/dependency-refresh-detected
ENTRYPOINT ["/opt/leo-v5/bin/runtime-entrypoint"]

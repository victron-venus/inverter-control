# Python 3.12.13, Debian Bookworm (immutable build image).
FROM python@sha256:3cd9086bdb30f7c9bc08a3fa621d9842e0d3f6f9291aeb4677e0547817c10b12 AS build
ARG SOURCE_DATE_EPOCH=1791417600
ARG CC=gcc
ARG CXX=g++
ARG CPPFLAGS=""
ARG CFLAGS="-g -O2 -fstack-protector-strong"
ARG CXXFLAGS="-g -O2 -fstack-protector-strong"
ARG LDFLAGS="-Wl,-z,relro,-z,now"
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    CC=${CC} CXX=${CXX} CPPFLAGS=${CPPFLAGS} CFLAGS=${CFLAGS} \
    CXXFLAGS=${CXXFLAGS} LDFLAGS=${LDFLAGS}
ARG SOURCE_REVISION
ENV SOURCE_REVISION=${SOURCE_REVISION}
WORKDIR /build
COPY build-support/tls-armv7/requirements-*.txt ./
RUN python -m venv /tools && \
    /tools/bin/pip install --require-hashes --only-binary=:all: -r requirements-build.txt && \
    /tools/bin/pip download --require-hashes --no-deps --only-binary=:all: \
      --platform manylinux_2_31_armv7l --python-version 312 --implementation cp --abi cp312 \
      -r requirements-wheels.txt -d /bundle/wheels && \
    /tools/bin/pip download --require-hashes --no-deps --no-binary=:all: \
      --no-build-isolation -r requirements-source.txt -d /sources
# A fixed source directory preserves useful debug paths without random pip paths.
RUN --network=none mkdir /build/cffi && \
    tar -xzf /sources/cffi-2.1.1.tar.gz --strip-components=1 -C /build/cffi
COPY scripts/build_tls_wheel.py ./
RUN --network=none /tools/bin/python build_tls_wheel.py
COPY scripts/finalize_tls_bundle.py ./
RUN --network=none python finalize_tls_bundle.py && \
    cp requirements-*.txt /bundle/ && \
    dpkg-query -W > /bundle/builder-packages.txt && \
    cp /sources/cffi-2.1.1.tar.gz /bundle/

# A separate native CI target instruments CFFI; these binaries are never shipped.
FROM build AS sanitizer
ENV CFLAGS="-g -O1 -fsanitize=address -fno-omit-frame-pointer" \
    LDFLAGS="-fsanitize=address"
RUN --network=none rm -rf /build/cffi/build /build/cffi/src/cffi.egg-info && \
    /tools/bin/pip wheel --no-index --no-deps --no-build-isolation /build/cffi -w /asan-wheels && \
    /tools/bin/pip install --no-index --no-deps /asan-wheels/*.whl /bundle/wheels/pycparser-*.whl
COPY scripts/check_cffi_sanitizer.py /check/check_cffi_sanitizer.py
COPY tests/fuzz_cffi_sanitized.py /check/fuzz_cffi_sanitized.py
RUN --network=none /tools/bin/python /check/check_cffi_sanitizer.py

FROM scratch AS sanitizer-export
USER 65532:65532
COPY --from=sanitizer /sanitizer-result.json /

# Python 3.12.13, Debian Bookworm slim (immutable verification image).
FROM python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS verify
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /bundle /bundle
COPY inverter_control/tls_policy.py /check/tls_policy.py
COPY tests/tls_bundle_probe.py /check/tls_bundle_probe.py
RUN --network=none mkdir /verify && chown -R 65532:65532 /verify /bundle
USER 65532:65532
RUN --network=none python -m venv /verify && \
    /verify/bin/pip install --no-index --require-hashes --only-binary=:all: \
      --find-links=/bundle/wheels -r /bundle/requirements.txt && \
    /verify/bin/pip check && \
    /verify/bin/python /check/tls_bundle_probe.py --armv7 > /bundle/verification.json

FROM scratch AS export
USER 65532:65532
COPY --from=verify /bundle/ /

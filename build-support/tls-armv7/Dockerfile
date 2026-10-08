FROM python:3.12.13-bookworm@sha256:3cd9086bdb30f7c9bc08a3fa621d9842e0d3f6f9291aeb4677e0547817c10b12 AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 SOURCE_DATE_EPOCH=1791417600
ARG SOURCE_REVISION
ENV SOURCE_REVISION=${SOURCE_REVISION}
WORKDIR /build
COPY build-support/tls-armv7/requirements-*.txt ./
RUN python -m venv /tools && \
    /tools/bin/pip install --require-hashes --only-binary=:all: -r requirements-build.txt && \
    /tools/bin/pip download --require-hashes --no-deps --only-binary=:all: -r requirements-wheels.txt -d /bundle/wheels && \
    /tools/bin/pip download --require-hashes --no-deps --no-binary=:all: --no-build-isolation -r requirements-source.txt -d /sources
RUN --network=none /tools/bin/pip wheel --no-index --no-deps --no-build-isolation /sources/cffi-2.1.1.tar.gz -w /bundle/wheels
COPY scripts/finalize_tls_bundle.py ./
RUN --network=none python finalize_tls_bundle.py /bundle && \
    cp requirements-*.txt /bundle/ && \
    dpkg-query -W > /bundle/builder-packages.txt && \
    cp /sources/cffi-2.1.1.tar.gz /bundle/

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS verify
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /bundle /bundle
COPY inverter_control/tls_policy.py /check/tls_policy.py
COPY scripts/check_tls_bundle.py /check/check_tls_bundle.py
RUN --network=none mkdir /verify && chown -R 65532:65532 /verify /bundle
USER 65532:65532
RUN --network=none python -m venv /verify && \
    /verify/bin/pip install --no-index --require-hashes --only-binary=:all: --find-links=/bundle/wheels -r /bundle/requirements.txt && \
    /verify/bin/pip check && \
    /verify/bin/python /check/check_tls_bundle.py --armv7 > /bundle/verification.json

FROM scratch AS export
USER 65532:65532
COPY --from=verify /bundle/ /

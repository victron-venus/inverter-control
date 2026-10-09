# Offline TLS dependencies on ARMv7

The TLS key check adds `cryptography==50.0.2`, `cffi==2.1.1` and `pycparser==3.1`
to the runtime. The native source archive does not contain or install dependencies. New signed
releases also provide a separate versioned TLS bundle; verify both payloads using
[the release signature instructions](release-signatures.md).
The ARMv7 bundle contains exactly this new dependency closure; existing Requests,
MQTT, D-Bus and other application requirements still apply. Do not stop a working
controller before validating the interpreter and dependencies for the upgrade.

## Supported ABI

The bundle targets CPython 3.12, Linux ARMv7 hard-float, glibc >= 2.36 and
`libffi.so.8`. The upstream cryptography wheel requires glibc >= 2.31; the CFFI
wheel is compiled on Debian Bookworm, so the complete bundle has the higher
baseline. It does not support older Python, musl, ARMv6 or arbitrary Venus forks.

The official [Venus v3.60 configuration](https://github.com/victronenergy/venus/blob/v3.60/Makefile)
selects Scarthgap; its [glibc recipe](https://github.com/victronenergy/openembedded-core/blob/v3.60/meta/recipes-core/glibc/glibc-version.inc)
uses glibc 2.39. The official Scarthgap ARMv7 [package feed](https://updates.victronenergy.com/feeds/venus/release/packages/scarthgap/cortexa7hf-neon-vfpv4/)
provides CPython 3.12 and libffi8. Check the actual target rather than inferring
compatibility from a device model. No hardware installation or physical control
validation is implied by the container checks.

## Build and inspect

Use the successful **ARMv7 offline TLS dependency bundle** CI job for the exact
reviewed commit. Download `tls-dependencies-cp312-armv7` from that run, or reproduce
it on a Linux Docker host with ARMv7 execution enabled:

```sh
docker buildx build --platform linux/arm/v7 \
  --file build-support/tls-armv7/Dockerfile \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --output type=local,dest=release-dist/tls-armv7 --progress plain .
```

Two immutable official Python image digests pin compiler and verification
environments. All input Python artifacts have explicit SHA-256 hashes. CFFI is
built from its verified source archive with pinned build tools, without network
or build isolation. The final stage installs all three wheels into a fresh
environment **with networking disabled**, runs `pip check`, imports the native
extensions and exercises exact RSA/EC and Edwards certificate-key checks. This
tests binary compatibility in the container, not a live Venus deployment.

Inspect `provenance.json` (commit, image digests, platform, wheel hashes),
`verification.json` (native imports and key boundaries), `builder-packages.txt`
and the input requirement files. The locally compiled wheel hash is recorded in
the generated `requirements.txt`; it must match the artifact from the reviewed
successful build. Every build now performs two clean CFFI compilations and fails unless the wheel
bytes match. `repeatability.json` records that digest, build flags and retained
debug information. This is repeatability in the pinned environment, not a claim
that arbitrary toolchains reproduce the same binary. See the
[build and installation guide](build-and-install.md).

## Operator-controlled installation

Back up the existing installation and confirm the service's exact interpreter:

```sh
python3 -c 'import sys, platform, ctypes; print(sys.version, platform.machine(), platform.libc_ver()); ctypes.CDLL("libffi.so.8")'
```

Transfer the verified bundle to the target without including site credentials.
Using the supported package installer for that interpreter (install `python3-pip`
from the matching Venus feed if it is absent), install only from this bundle:

```sh
python3 -m pip install --no-index --only-binary=:all: --require-hashes \
  --find-links=/path/to/tls-armv7/wheels -r /path/to/tls-armv7/requirements.txt
python3 -c 'import cryptography, cffi, pycparser; from cryptography import x509; print(cryptography.__version__, cffi.__version__, pycparser.__version__)'
```

Do not replace firmware Python or bypass an externally-managed-environment
restriction. If the image does not permit this supported installation path,
prepare a matching application environment with the device maintainer before
upgrading. Test imports using the same account/interpreter as **both** the
controller and log-forwarder service. `update.sh` validates imports before it
stops services; a missing or incompatible extension leaves the existing
installation running. Keep the source and dependency upgrade together, then
perform the documented target-device acceptance checks.

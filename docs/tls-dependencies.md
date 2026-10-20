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

Venus services share the firmware's Python packages. Prefer an application
environment over replacing those packages with system-wide pip. Starting with
1.23.9, an optional device-local selector at
`/data/setupOptions/inverter-control/runtime-python` chooses the interpreter for
both application services and the live installer checks. Its contents must be one
absolute path of the form
`/data/inverter-control-runtimes/<id>/bin/python3`. Keep the selector root-owned,
regular (not a link), and writable only by root. Its parent directories `/data`,
`/data/setupOptions` and `/data/setupOptions/inverter-control` must also be real,
root-owned directories without group or other write permission. A missing selector retains the
system Python default; an invalid or broken selection is an error, not a request
to fall back.

Create a new environment directly at its final, versioned path; do not move or
modify an environment used by running services. For example:

```sh
python3 -m venv --copies --system-site-packages /data/inverter-control-runtimes/control-1.23.9
```

The system-site option deliberately retains the firmware's native D-Bus/GI
modules. It is not a fully isolated or immutable operating system. Verify
`sys.prefix` is the intended private directory and differs from `sys.base_prefix`
before installing anything. Transfer the authenticated TLS bundle and any other
required wheels without site credentials. Assemble an explicit, hash-locked
runtime requirement list from the reviewed `uv.lock` and the signed TLS bundle;
the TLS archive alone does not contain Requests, MQTT or the other application
dependencies. Install that closure only into the new environment:

```sh
/data/inverter-control-runtimes/control-1.23.9/bin/python3 -m pip install \
  --no-index --no-deps --ignore-installed --only-binary=:all: --require-hashes \
  --find-links=/path/to/verified-wheels -r /path/to/verified-runtime-requirements.txt
```

Here `--ignore-installed` ensures inherited system packages do not satisfy the
private installation. Do not use this command with the system interpreter.
Verify versions, native imports, TLS key checks and configuration loading with the
exact private interpreter before the controlled update. Confirm system package
files and opkg metadata remain unchanged.

During an exclusive maintenance window, save the current selector (including its
absence), active configuration and control modes. Stop the existing application
services before atomically selecting the prepared environment, then use the
ordinary `update.sh` procedure. The new installer validates the selected
interpreter before its own service interruption. Never leave a changed selector
in place after a failed preflight without restoring the prior selection. Preserve
the old environment and accepted package for rollback; restore both package and
selector together. Runtime control flags still require explicit restoration and
readback after a restart.

Do not replace firmware Python or bypass externally-managed-environment
restrictions. If the image lacks compatible venv/ensurepip support, prepare a
supported application environment before upgrading. A firmware update may
change the underlying interpreter or inherited native modules, so revalidate the
private runtime after that change. Packaging with `DESTDIR` never reads the live
selector. Keep source and dependencies together, then perform the documented
target-device acceptance checks.

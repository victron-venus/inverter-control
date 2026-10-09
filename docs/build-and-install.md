# Building and installing the native package

The controller uses Python source directly. Its supported device integration is
Venus OS with the runtime documented in [offline TLS dependencies](tls-dependencies.md).
Packaging also compiles the CFFI dependency for the optional ARMv7 bundle; native
build requirements therefore apply even though the controller itself is Python.

## Developer environment

Install Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/),
then, from a clean checkout:

```sh
uv sync --locked --all-extras
make check
bash scripts/ci.sh integration
```

The lock file records Python runtime and development dependencies. The integration
command uses the pinned shared mock environment. It does not require a live
inverter and must not be interpreted as hardware acceptance. Dependency changes
update `pyproject.toml`, `uv.lock`, and the ARMv7 input locks where applicable.

## Package installation and removal

On the supported device use the existing SetupHelper/PackageManager install and
uninstall actions; `make install` and `make uninstall` delegate to the same
`setup install auto` / `setup uninstall auto` entry points. Live installation
validates required runtime imports before interrupting services. Uninstall stops
the services and removes their activation hooks while retaining operator settings
and data for deliberate cleanup.

Package builders can stage the exact native payload using the POSIX `DESTDIR`
convention, without root, SetupHelper, runtime dependencies, or a connected device:

```sh
make install DESTDIR="$PWD/build/stage"
# Inspect build/stage/data/inverter-control before packaging it.
make uninstall DESTDIR="$PWD/build/stage"
```

`DESTDIR` may also be passed as an environment variable to `setup install auto`,
`setup uninstall auto`, or `sh update.sh`. Staging runs before device helpers are
loaded. It writes only under `DESTDIR/data/inverter-control`; it never touches
live services, D-Bus, device settings, certificates, or boot hooks. It does not
activate services inside the staged filesystem either. Root `/`, links in the
staging path, linked payload destinations, and invalid uninstall manifests are
rejected. Use a staging directory owned by the invoking user and not concurrently
modified by another process. The installation manifest records owned payload
files so uninstall preserves added operator files such as `local_config.py`.
The application prefix remains `/data/inverter-control`, as required by its
Venus service integration; `DESTDIR` relocates packaging, not the runtime prefix.

## Repeatable builds and native flags

`bash scripts/package-release.sh 1.23.5 rc --output /path/to/empty-output` builds
from tracked, present inputs. Use the committed base version in place of the
example. Published builds additionally require the frozen release plan supplied
by the release pipeline. Tar entries have normalized ownership and timestamps,
gzip embeds no filename or current time, file order is sorted, and executable
bits are retained. Tests rebuild after changing source mtimes and compare all
resulting bytes.

The ARMv7 builder pins its two base images and all downloaded Python artifacts.
Every invocation compiles CFFI twice with clean object and metadata directories,
then refuses to produce a bundle if the wheel bytes differ. `repeatability.json`
records the resulting digest, the compiler/linker flags, epoch, and whether
requested debugging information survived into the wheel. This is a same-input,
same-environment repeatability claim; signatures, release timestamps and remote
service receipts are not deterministic build products.

The Docker build accepts and forwards `CC`, `CXX`, `CPPFLAGS`, `CFLAGS`,
`CXXFLAGS`, and `LDFLAGS`; caller values replace documented defaults rather than
being silently discarded. For example:

```sh
docker buildx build --platform linux/arm/v7 \
  --file build-support/tls-armv7/Dockerfile \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --build-arg CC=gcc --build-arg CFLAGS='-g -O1 -fstack-protector-strong' \
  --build-arg LDFLAGS='-Wl,-z,relro,-z,now' \
  --output type=local,dest=build/tls-armv7 .
```

The builder never strips the native extension and checks `.debug_info` when
`-g` is requested. Source staging does not compile or strip files. There is no
recursive build graph with cross-directory dependencies: one Python packaging
operation creates the source tarball and one native dependency build creates the
CFFI wheel. The already-built cryptography and pycparser wheels are hash-verified
inputs, not independently rebuilt by this project. The offline verification
stage installs the exact dependency closure and exercises native imports and TLS
key checks. Read [release signature verification](release-signatures.md) before
using downloaded release payloads.

## Native memory-safety checks

The required CI workflow also rebuilds CFFI with AddressSanitizer on native
x86-64. It verifies sanitizer linkage and first requires an intentionally invalid
standalone canary to be detected, then runs 2,000 deterministic fuzz iterations
of CFFI declaration parsing, arrays, structs, buffers and owned pointer casts.
The sanitizer image has no network during the test. Instrumented binaries are
never distributed. Leak detection is disabled for Python process-lifetime
allocations; invalid memory access detection remains enabled and aborts on an
error. This tests CFFI's native API, not instrumented execution of the downloaded
cryptography/OpenSSL binaries. ARMv7 compatibility remains a separate offline
installation/import/TLS test.

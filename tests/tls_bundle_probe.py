"""Exercise the shipped certificate parser and exact key boundary after offline install."""

import argparse
import ctypes
import importlib.metadata
import json
import platform
import ssl
import struct
import sys
from datetime import UTC, datetime, timedelta

import _cffi_backend
import cffi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from cryptography.x509.oid import NameOID
from tls_policy import verify_key_lengths


def check(armv7: bool = False) -> dict:
    if armv7 and (platform.machine() not in ("armv7l", "armv8l") or struct.calcsize("P") != 4):
        raise RuntimeError("The bundle must be built and tested on 32-bit ARMv7")
    ctypes.CDLL("libffi.so.8")
    if cffi.FFI().sizeof("char") != 1:
        raise RuntimeError("CFFI native backend is unavailable")
    results = []
    for label, key, accepted in (
        ("rsa2047", rsa.generate_private_key(65537, 2047), False),  # nosec B505 - rejection fixture
        ("rsa2048", rsa.generate_private_key(65537, 2048), True),
        ("ec192", ec.generate_private_key(ec.SECP192R1()), False),
        ("ec224", ec.generate_private_key(ec.SECP224R1()), True),
        ("ed25519", ed25519.Ed25519PrivateKey.generate(), True),
        ("ed448", ed448.Ed448PrivateKey.generate(), True),
    ):
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-bundle-test")])
        now = datetime.now(UTC)
        algorithm = None if label.startswith("ed") else hashes.SHA256()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=1))
            .sign(key, algorithm)
        )

        class VerifiedSocket:
            context = ssl.create_default_context()

            def get_verified_chain(self, certificate=cert):
                return [certificate.public_bytes(serialization.Encoding.DER)]

        try:
            verify_key_lengths(VerifiedSocket())
            actual = True
        except ssl.SSLError:
            actual = False
        if actual != accepted:
            raise RuntimeError(f"Wrong certificate-key boundary: {label}")
        results.append({"key": label, "accepted": actual})
    return {
        "python": sys.version,
        "cffi_backend": _cffi_backend.__version__,
        "machine": platform.machine(),
        "libc": platform.libc_ver(),
        "versions": {
            name: importlib.metadata.version(name) for name in ("cryptography", "cffi", "pycparser")
        },
        "key_checks": results,
        "scope": "Native imports and certificate-key checks; not device or TLS handshake certification",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--armv7", action="store_true")
    print(json.dumps(check(parser.parse_args().armv7), indent=2))

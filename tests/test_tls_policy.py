"""Actual loopback clients; synthetic keys never enter system/user trust stores."""

import http.client
import os
import select
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from inverter_control import homeassistant as ha
from inverter_control import log_forwarder as lf
from inverter_control.requests_tls import VerifiedHTTPAdapter
from inverter_control.tls_policy import VerifiedHTTPSHandler, _key_is_strong, verify_key_lengths

Key = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
CHAIN_CASES = (
    "strong",
    "strong-ec",
    "weak-leaf",
    "weak-intermediate",
    "weak-root",
    "weak-2047-root",
    "weak-ec-root",
)


@pytest.fixture(autouse=True)
def clean_tls_environment(monkeypatch):
    for name in list(os.environ):
        if name.upper().endswith("_PROXY") or name in (
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
        ):
            monkeypatch.delenv(name)
    monkeypatch.setattr(ha, "VUESensorDBusClient", lambda *a, **k: None)
    monkeypatch.setattr(ha, "HA_TOKEN", "synthetic-only")


def certificate(
    key: Key, name: str, issuer: x509.Certificate | None, issuer_key: Key, *, ca: bool
) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=not ca,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
        )
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), False
        ).add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def chains(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path, Path]]:
    directory = tmp_path_factory.mktemp("otlp-synthetic-pki")
    result = {}
    for case in CHAIN_CASES:
        root_bits = {"weak-root": 1024, "weak-2047-root": 2047}.get(case, 2048)
        root_key: Key
        if case in ("strong-ec", "weak-ec-root"):
            curve = ec.SECP192R1() if case == "weak-ec-root" else ec.SECP256R1()
            root_key = ec.generate_private_key(curve)
        else:
            root_key = rsa.generate_private_key(65537, root_bits)
        root = certificate(root_key, case, None, root_key, ca=True)
        issuer, issuer_key = root, root_key
        intermediate = b""
        if case == "weak-intermediate":
            issuer_key = rsa.generate_private_key(65537, 1024)  # nosec B505 - rejected synthetic chain fixture
            issuer = certificate(issuer_key, "intermediate", root, root_key, ca=True)
            intermediate = issuer.public_bytes(serialization.Encoding.PEM)
        leaf_key: Key = (
            ec.generate_private_key(ec.SECP256R1())
            if case == "strong-ec"
            else rsa.generate_private_key(65537, 1024 if case == "weak-leaf" else 2048)
        )
        leaf = certificate(leaf_key, "localhost", issuer, issuer_key, ca=False)
        cert_file, key_file, ca_file = (
            directory / f"{case}.{suffix}" for suffix in ("pem", "key", "ca")
        )
        cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM) + intermediate)
        key_file.write_bytes(
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        ca_file.write_bytes(root.public_bytes(serialization.Encoding.PEM))
        result[case] = cert_file, key_file, ca_file
    return result


@contextmanager
def peer(
    chain: tuple[Path, Path, Path],
    version: ssl.TLSVersion,
    *,
    redirect: str | None = None,
    client_auth: bool = False,
) -> Iterator[tuple[int, dict[str, Any]]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = version
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    context.load_cert_chain(chain[0], chain[1])
    if client_auth:
        context.load_verify_locations(chain[2])
        context.verify_mode = ssl.CERT_REQUIRED
    result: dict[str, Any] = {"application_bytes": b""}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def worker() -> None:
            try:
                raw, _ = listener.accept()
                with raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as connection:
                        result["tls"] = connection.version()
                        data = b""
                        while b"\r\n\r\n" not in data:
                            part = connection.recv(8192)
                            if not part:
                                if not data:
                                    result["closed_before_http"] = True
                                    return
                                raise EOFError("Expected HTTP headers")
                            data += part
                            result["application_bytes"] = data
                        headers, body = data.split(b"\r\n\r\n", 1)
                        length = next(
                            (
                                int(line.split(b":", 1)[1])
                                for line in headers.split(b"\r\n")
                                if line.lower().startswith(b"content-length:")
                            ),
                            0,
                        )
                        while len(body) < length:
                            part = connection.recv(8192)
                            if not part:
                                raise EOFError("Expected complete OTLP body")
                            body += part
                        result["application_bytes"] = headers + b"\r\n\r\n" + body
                        response = (
                            f"HTTP/1.1 307 Temporary Redirect\r\nLocation: {redirect}\r\n"
                            if redirect
                            else "HTTP/1.1 200 OK\r\n"
                        )
                        connection.sendall(
                            (
                                response
                                + 'Content-Length: 14\r\nConnection: close\r\n\r\n{"state":"42"}'
                            ).encode()
                        )
            except ssl.SSLError as error:
                result["handshake_error"] = str(error)
            except (ConnectionResetError, BrokenPipeError) as error:
                if result["application_bytes"]:
                    result["unexpected_error"] = repr(error)
                else:
                    result["closed_before_http"] = True
            except Exception as error:
                result["unexpected_error"] = repr(error)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], result
        finally:
            thread.join(6)
            assert not thread.is_alive()
            assert "unexpected_error" not in result, result


def calibrate(chain: tuple[Path, Path, Path], version: ssl.TLSVersion) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    context.load_verify_locations(chain[2])
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    with peer(chain, version) as (port, observed):
        connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=5)
        try:
            connection.request("POST", "/oracle", body=b"calibration")
            assert connection.getresponse().status == 200
        finally:
            connection.close()
    assert observed["application_bytes"].endswith(b"calibration")


@contextmanager
def proxy(
    target_port: int, chain: tuple[Path, Path, Path] | None
) -> Iterator[tuple[int, list[bytes]]]:
    """A bounded CONNECT relay whose only permitted destination is our loopback peer."""
    observed: list[bytes] = []
    errors: list[Exception] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def worker() -> None:
            try:
                raw, _ = listener.accept()
                with raw:
                    raw.settimeout(5)
                    if chain:
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        context.minimum_version = ssl.TLSVersion.TLSv1_2
                        context.set_ciphers("DEFAULT:@SECLEVEL=0")
                        context.load_cert_chain(chain[0], chain[1])
                        incoming: socket.socket = context.wrap_socket(raw, server_side=True)
                    else:
                        incoming = raw
                    with incoming:
                        request = b""
                        while b"\r\n\r\n" not in request:
                            data = incoming.recv(8192)
                            if not data:
                                return
                            request += data
                        observed.append(request)
                        assert request.startswith(f"CONNECT localhost:{target_port} ".encode())
                        with socket.create_connection(("127.0.0.1", target_port), timeout=5) as out:
                            incoming.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                            while True:
                                ready, _, _ = select.select([incoming, out], [], [], 5)
                                if not ready:
                                    raise TimeoutError("Synthetic proxy did not finish")
                                for source in ready:
                                    data = source.recv(8192)
                                    if not data:
                                        return
                                    destination = out if source is incoming else incoming
                                    destination.sendall(data)
            except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
                # Rejected TLS peers can close while the proxy relays handshake bytes.
                # Positive cases separately require their full response and request.
                pass
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], observed
        finally:
            thread.join(6)
            assert not thread.is_alive()
            assert not errors, errors


def run_client(path, url, monkeypatch):
    if path == "ha":
        monkeypatch.setattr(ha, "HA_URL", url)
        client = ha.HomeAssistantClient()
        try:
            return client._get_state("sensor.synthetic") == "42"
        finally:
            client.stop()
    monkeypatch.setattr(lf, "LOKI_URL", url)
    monkeypatch.setattr(lf, "USE_REQUESTS", path == "loki_requests")
    return lf.push_to_loki({"marker": "synthetic-log-payload"})


@pytest.mark.parametrize("path", ["ha", "loki_requests", "loki_urllib"])
@pytest.mark.parametrize("version", [ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3])
@pytest.mark.parametrize("case", [*CHAIN_CASES, "untrusted", "wrong-host"])
def test_actual_client_full_chain_before_http(chains, monkeypatch, path, version, case):
    chain = chains.get(case, chains["strong"])
    calibrate(chain, version)
    ca = chains["strong-ec"][2] if case == "untrusted" else chain[2]
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    with peer(chain, version) as (port, received):
        host = "127.0.0.1" if case == "wrong-host" else "localhost"
        accepted = run_client(path, f"https://{host}:{port}", monkeypatch)
    assert accepted is case.startswith("strong")
    if accepted:
        assert received["application_bytes"]
        if path == "ha":
            assert b"Authorization: Bearer synthetic-only" in received["application_bytes"]
        else:
            assert b"synthetic-log-payload" in received["application_bytes"]
    else:
        assert received["application_bytes"] == b""


@pytest.mark.parametrize("path", ["ha", "loki_requests", "loki_urllib"])
@pytest.mark.parametrize("case", ["strong", "weak-2047-root"])
def test_http_connect_preserves_origin_guard(chains, monkeypatch, path, case):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(chains[case][2]))
    monkeypatch.setenv("SSL_CERT_FILE", str(chains[case][2]))
    with (
        peer(chains[case], ssl.TLSVersion.TLSv1_3) as (port, received),
        proxy(port, None) as (proxy_port, seen),
    ):
        monkeypatch.setenv("HTTPS_PROXY", f"http://localhost:{proxy_port}")
        assert run_client(path, f"https://localhost:{port}", monkeypatch) is (case == "strong")
    assert len(seen) == 1
    assert bool(received["application_bytes"]) is (case == "strong")


@pytest.mark.parametrize("case", ["strong", "weak-2047-root"])
def test_https_proxy_preserves_inner_chain(chains, monkeypatch, tmp_path, case):
    bundle = tmp_path / "roots.pem"
    bundle.write_bytes(chains["strong"][2].read_bytes() + chains[case][2].read_bytes())
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    with (
        peer(chains[case], ssl.TLSVersion.TLSv1_3) as (port, received),
        proxy(port, chains["strong"]) as (proxy_port, seen),
    ):
        monkeypatch.setenv("HTTPS_PROXY", f"https://localhost:{proxy_port}")
        assert run_client("ha", f"https://localhost:{port}", monkeypatch) is (case == "strong")
    assert len(seen) == 1
    assert bool(received["application_bytes"]) is (case == "strong")


def test_weak_https_proxy_before_connect(chains, monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(chains["weak-2047-root"][2]))
    with proxy(1, chains["weak-2047-root"]) as (proxy_port, seen):
        monkeypatch.setenv("HTTPS_PROXY", f"https://localhost:{proxy_port}")
        assert not run_client("ha", "https://localhost:1", monkeypatch)
    assert seen == []


@pytest.mark.parametrize("transport", ["requests", "urllib"])
def test_explicit_mtls_and_ca_configuration(chains, transport):
    cert, key, ca = chains["strong"]
    with peer(chains["strong"], ssl.TLSVersion.TLSv1_3, client_auth=True) as (port, seen):
        url = f"https://localhost:{port}"
        if transport == "requests":
            with requests.Session() as session:
                session.mount("https://", VerifiedHTTPAdapter())
                response = session.post(
                    url, data=b"mutual-tls", verify=str(ca), cert=(str(cert), str(key)), timeout=5
                )
                assert response.status_code == 200
        else:
            ctx = ssl.create_default_context(cafile=ca)
            ctx.load_cert_chain(cert, key)
            opener = lf.urllib.request.build_opener(VerifiedHTTPSHandler(context=ctx))
            with opener.open(
                lf.urllib.request.Request(url, data=b"mutual-tls"), timeout=5
            ) as response:
                assert response.status == 200
    assert seen["application_bytes"].endswith(b"mutual-tls")


@pytest.mark.parametrize("chain", [None, [], [b"not-a-certificate"]])
def test_unavailable_or_invalid_verified_chain_fails_closed(chain):
    sock = SimpleNamespace(context=SimpleNamespace(verify_mode=ssl.CERT_REQUIRED))
    if chain is not None:
        sock.get_verified_chain = lambda: chain
    with pytest.raises((ssl.SSLError, ValueError)):
        verify_key_lengths(sock)


@pytest.mark.parametrize("case", ["strong", "weak-2047-root"])
def test_public_der_api_has_same_policy(chains, case):
    cert = x509.load_pem_x509_certificate(chains[case][2].read_bytes())
    sock = SimpleNamespace(
        context=SimpleNamespace(verify_mode=ssl.CERT_REQUIRED),
        get_verified_chain=lambda: [cert.public_bytes(serialization.Encoding.DER)],
    )
    if case == "strong":
        verify_key_lengths(sock)
    else:
        with pytest.raises(ssl.SSLError, match="security minimum"):
            verify_key_lengths(sock)


def test_key_algorithms_and_exact_minimums():
    cases = [
        (rsa.generate_private_key(65537, 2047).public_key(), False),  # nosec B505 - rejection boundary
        (rsa.generate_private_key(65537, 2048).public_key(), True),
        (ec.generate_private_key(ec.SECP192R1()).public_key(), False),
        (ec.generate_private_key(ec.SECP224R1()).public_key(), True),
        (dsa.generate_private_key(1024).public_key(), False),  # nosec B505 - rejection boundary
        (dsa.generate_private_key(2048).public_key(), True),
        (ed25519.Ed25519PrivateKey.generate().public_key(), True),
        (ed448.Ed448PrivateKey.generate().public_key(), True),
        (object(), False),
    ]
    for key, expected in cases:
        assert _key_is_strong(SimpleNamespace(public_key=lambda key=key: key)) is expected


@pytest.mark.parametrize("path", ["ha", "loki_requests", "loki_urllib"])
def test_plain_http_remains_available_without_crypto_import(path, monkeypatch):
    import builtins
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.reply()

        def reply(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Length", "14")
            self.end_headers()
            self.wfile.write(b'{"state":"42"}')

        def log_message(self, *args):
            pass

    original = builtins.__import__

    def no_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("fixture omits optional-at-import crypto dependency")
        return original(name, *args, **kwargs)

    with HTTPServer(("127.0.0.1", 0), Handler) as listener:
        listener.timeout = 5
        thread = threading.Thread(target=listener.handle_request)
        thread.start()
        try:
            with patch("builtins.__import__", no_crypto):
                assert run_client(path, f"http://127.0.0.1:{listener.server_port}", monkeypatch)
        finally:
            thread.join(6)
    assert not thread.is_alive()
    assert len(seen) == 1


def test_missing_crypto_fails_https_validation(chains):
    import builtins

    cert = x509.load_pem_x509_certificate(chains["strong"][2].read_bytes())
    der = cert.public_bytes(serialization.Encoding.DER)
    sock = SimpleNamespace(
        context=SimpleNamespace(verify_mode=ssl.CERT_REQUIRED), get_verified_chain=lambda: [der]
    )
    original = builtins.__import__

    def no_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("fixture unavailable")
        return original(name, *args, **kwargs)

    with patch("builtins.__import__", no_crypto):
        with pytest.raises(ssl.SSLError, match="cryptography dependency"):
            verify_key_lengths(sock)


def test_direct_script_import_without_requests():
    import subprocess  # nosec B404 - fixed interpreter and local import-only test
    import sys

    source = """import builtins,runpy,sys
sys.path.insert(0,sys.argv[1])
original=builtins.__import__
def omit_requests(name,*args,**kwargs):
    if name == "requests": raise ImportError("synthetic minimal runtime")
    return original(name,*args,**kwargs)
builtins.__import__=omit_requests
module=runpy.run_path(sys.argv[2],run_name="probe")
assert module["USE_REQUESTS"] is False
assert module["VerifiedHTTPSHandler"].__module__ == "tls_policy"
"""
    directory = Path(lf.__file__).parent
    result = subprocess.run(  # nosec B603 - fixed interpreter, test source and repository paths
        [sys.executable, "-c", source, str(directory), str(directory / "log_forwarder.py")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_requests_adapter_preserves_pool_configuration():
    adapter = VerifiedHTTPAdapter(pool_connections=2, pool_maxsize=5, max_retries=0)
    assert adapter._pool_connections == 2
    assert adapter._pool_maxsize == 5
    assert adapter.max_retries.total == 0
    default = requests.adapters.HTTPAdapter()
    assert (
        default.poolmanager.pool_classes_by_scheme["https"]
        is not adapter.poolmanager.pool_classes_by_scheme["https"]
    )
    adapter.close()
    default.close()


def test_requests_distinct_ca_selections_do_not_share_trust(chains):
    """An earlier connection must not add roots to a later explicit CA selection."""
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", VerifiedHTTPAdapter())
        with peer(chains["strong"], ssl.TLSVersion.TLSv1_2) as (port, first):
            response = session.get(
                f"https://localhost:{port}", verify=str(chains["strong"][2]), timeout=5
            )
            assert response.status_code == 200
        assert first["application_bytes"]
        with peer(chains["strong"], ssl.TLSVersion.TLSv1_2) as (port, second):
            with pytest.raises(requests.exceptions.SSLError):
                session.get(
                    f"https://localhost:{port}", verify=str(chains["strong-ec"][2]), timeout=5
                )
        assert not second["application_bytes"]


def test_urllib_rejects_https_proxy_before_connection(monkeypatch):
    """Do not disclose CONNECT or Proxy-Authorization to a plaintext socket."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.1)
        port = listener.getsockname()[1]
        monkeypatch.setenv("HTTPS_PROXY", f"https://fixture:synthetic@127.0.0.1:{port}")
        assert not run_client("loki_urllib", "https://localhost:1", monkeypatch)
        with pytest.raises(TimeoutError):
            listener.accept()


def test_urllib_https_proxy_no_proxy_bypass_stays_direct(chains, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://fixture:synthetic@127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("SSL_CERT_FILE", str(chains["strong"][2]))
    with peer(chains["strong"], ssl.TLSVersion.TLSv1_3) as (port, observed):
        assert run_client("loki_urllib", f"https://localhost:{port}", monkeypatch)
    assert observed["application_bytes"].startswith(b"POST ")
    assert b"Proxy-Authorization" not in observed["application_bytes"]


def test_certificate_parser_failure_has_static_tls_error(chains):
    from cryptography.exceptions import UnsupportedAlgorithm

    cert = x509.load_pem_x509_certificate(chains["strong"][2].read_bytes())
    sock = SimpleNamespace(
        context=SimpleNamespace(verify_mode=ssl.CERT_REQUIRED),
        get_verified_chain=lambda: [cert.public_bytes(serialization.Encoding.DER)],
    )
    for error in (ValueError("synthetic secret detail"), UnsupportedAlgorithm("synthetic detail")):
        with patch(
            "inverter_control.tls_policy._certificate_from_verified_item", side_effect=error
        ):
            with pytest.raises(
                ssl.SSLError, match="^.*HTTPS certificate key cannot be validated.*$"
            ) as raised:
                verify_key_lengths(sock)
        assert "synthetic" not in str(raised.value)


@pytest.mark.parametrize("level", [0, 2, 3])
def test_proxy_context_security_floor_preserves_cipher_allowlist(level, monkeypatch):
    from inverter_control import requests_tls

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.set_ciphers(f"ECDHE-RSA-AES128-GCM-SHA256:@SECLEVEL={level}")
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    before = context.get_ciphers()
    monkeypatch.setattr(
        requests_tls,
        "ssl",
        SimpleNamespace(
            SSLContext=lambda protocol: context,
            PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT,
            TLSVersion=ssl.TLSVersion,
        ),
    )
    result = requests_tls._tls_context()
    assert result is context
    assert result.security_level == max(level, 2)
    assert result.minimum_version == ssl.TLSVersion.TLSv1_3
    assert result.verify_mode == ssl.CERT_REQUIRED
    assert result.check_hostname
    assert result.get_ciphers() == before

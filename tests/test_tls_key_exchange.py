"""Real TLS1.2 DHE minimum, calibrated independently of client rejection."""

import http.client
import socket
import ssl
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_tls_policy as tls_cases

# Accepted test-only deprecation: cryptography 50 deprecates all FFDH APIs.
# Keep this public loader to independently verify the exact 2047/2048-bit fixtures;
# changing to ECDHE would remove the finite-field minimum regression. The warning
# stays visible. When the API is removed, replace only fixture parsing, not the
# real handshake or no-private-bytes-before-rejection assertions below.
from cryptography.hazmat.primitives.serialization import load_pem_parameters

chains = tls_cases.chains
clean_tls_environment = tls_cases.clean_tls_environment


@contextmanager
def dhe_peer(chain, bits):
    parameters = Path(__file__).parent / "fixtures" / "tls" / f"dh{bits}.txt"
    assert load_pem_parameters(parameters.read_bytes()).parameter_numbers().p.bit_length() == bits
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("DHE-RSA-AES256-GCM-SHA384:@SECLEVEL=0")
    context.load_dh_params(parameters)
    context.load_cert_chain(chain[0], chain[1])
    seen = {"application_bytes": b""}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                raw, _ = listener.accept()
                with raw:
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as tls:
                        seen["cipher"] = tls.cipher()[0]
                        while b"\r\n\r\n" not in seen["application_bytes"]:
                            part = tls.recv(8192)
                            if not part:
                                return
                            seen["application_bytes"] += part
                        headers, body = seen["application_bytes"].split(b"\r\n\r\n", 1)
                        length = next(
                            (
                                int(line.split(b":", 1)[1])
                                for line in headers.split(b"\r\n")
                                if line.lower().startswith(b"content-length:")
                            ),
                            0,
                        )
                        while len(body) < length:
                            part = tls.recv(8192)
                            if not part:
                                raise EOFError("incomplete synthetic body")
                            body += part
                            seen["application_bytes"] += part
                        tls.sendall(
                            b'HTTP/1.1 200 OK\r\nContent-Length: 14\r\nConnection: close\r\n\r\n{"state":"42"}'
                        )
            except ssl.SSLError as exc:
                seen["tls_error"] = str(exc)
            except Exception as exc:
                seen["unexpected_error"] = repr(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], seen
        finally:
            thread.join(6)
            assert not thread.is_alive()
            assert "unexpected_error" not in seen, seen


@pytest.mark.parametrize("bits", [2047, 2048])
@pytest.mark.parametrize("path", ["ha", "loki_requests", "loki_urllib"])
def test_finite_field_dh_exact_minimum(chains, monkeypatch, bits, path):
    chain = chains["strong"]
    context = ssl.create_default_context(cafile=chain[2])
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("DHE-RSA-AES256-GCM-SHA384:@SECLEVEL=0")
    with dhe_peer(chain, bits) as (port, observed):
        connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=5)
        try:
            connection.request("GET", "/oracle")
            assert connection.getresponse().status == 200
        finally:
            connection.close()
    assert observed["cipher"] == "DHE-RSA-AES256-GCM-SHA384"
    assert observed["application_bytes"].startswith(b"GET /oracle ")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(chain[2]))
    monkeypatch.setenv("SSL_CERT_FILE", str(chain[2]))
    # Observe and re-raise the real client-side OpenSSL failure. A trust,
    # hostname, network or server-configuration failure must not pass as DH policy.
    original_handshake = ssl.SSLSocket.do_handshake
    client_errors = []

    def observed_handshake(stream, *args, **kwargs):
        try:
            return original_handshake(stream, *args, **kwargs)
        except ssl.SSLError as error:
            if not stream.server_side:
                client_errors.append(error.reason)
            raise

    monkeypatch.setattr(ssl.SSLSocket, "do_handshake", observed_handshake)
    with dhe_peer(chain, bits) as (port, observed):
        accepted = tls_cases.run_client(path, f"https://localhost:{port}", monkeypatch)
    assert accepted is (bits == 2048), (bits, path, observed)
    if bits == 2047:
        assert client_errors == ["DH_KEY_TOO_SMALL"]
        assert observed["application_bytes"] == b""
    else:
        assert client_errors == []
        assert observed["cipher"] == "DHE-RSA-AES256-GCM-SHA384"
        assert observed["application_bytes"]
        if path == "ha":
            assert b"Authorization: Bearer synthetic-only" in observed["application_bytes"]
        else:
            assert b"synthetic-log-payload" in observed["application_bytes"]

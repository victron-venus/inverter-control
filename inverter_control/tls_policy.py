"""Check the verified peer chain before an HTTPS connection sends application data.

The supported CPython 3.12 SSL object exposes the verified chain privately;
Python 3.13+ also provides a public API. Unsupported runtimes fail closed for
HTTPS. Plain HTTP does not need the optional-at-import cryptography module.
"""

import http.client
import ssl
import urllib.request
from urllib.parse import urlsplit


def _certificate_from_verified_item(item):
    from cryptography import x509

    if isinstance(item, bytes):
        return x509.load_der_x509_certificate(item)
    encode = getattr(item, "public_bytes", None)
    if callable(encode):
        pem = encode()
        if isinstance(pem, str):
            return x509.load_pem_x509_certificate(pem.encode("ascii"))
    raise ssl.SSLError("HTTPS runtime cannot expose a verified certificate")


def _key_is_strong(certificate):
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa

    key = certificate.public_key()
    if isinstance(key, rsa.RSAPublicKey):
        return key.public_numbers().n.bit_length() >= 2048
    if isinstance(key, ec.EllipticCurvePublicKey):
        return key.key_size >= 224
    if isinstance(key, dsa.DSAPublicKey):
        parameters = key.public_numbers().parameter_numbers
        return parameters.p.bit_length() >= 2048 and parameters.q.bit_length() >= 224
    return isinstance(key, ed25519.Ed25519PublicKey | ed448.Ed448PublicKey)


def verify_key_lengths(sock):
    """Enforce key minima on this connection's complete, already verified chain."""
    # urllib3 SSLTransport wraps HTTPS-proxy tunnels in an inner SSLObject.
    tls = getattr(sock, "sslobj", sock)
    if tls.context.verify_mode != ssl.CERT_REQUIRED:
        raise ssl.SSLError("HTTPS certificate verification is required")
    get_chain = getattr(tls, "get_verified_chain", None)
    if not callable(get_chain):
        get_chain = getattr(getattr(tls, "_sslobj", None), "get_verified_chain", None)
    if not callable(get_chain):
        raise ssl.SSLError("HTTPS requires a runtime exposing its verified TLS chain")
    chain = get_chain()
    if not isinstance(chain, list) or not chain:
        raise ssl.SSLError("HTTPS requires a nonempty verified TLS chain")
    try:
        from cryptography.exceptions import UnsupportedAlgorithm

        strong = all(_key_is_strong(_certificate_from_verified_item(item)) for item in chain)
    except ImportError as error:
        raise ssl.SSLError("HTTPS key validation requires the cryptography dependency") from error
    except (ValueError, UnsupportedAlgorithm) as error:
        raise ssl.SSLError("HTTPS certificate key cannot be validated") from error
    if not strong:
        raise ssl.SSLError("HTTPS certificate key is below the supported security minimum")


class VerifiedHTTPSConnection(http.client.HTTPSConnection):
    """Retain stdlib roots, hostname checks and CONNECT before validating keys."""

    def connect(self):
        try:
            super().connect()
            verify_key_lengths(self.sock)
        except Exception:
            self.close()
            raise


class VerifiedHTTPSHandler(urllib.request.HTTPSHandler):
    """Use the guarded connection without replacing urllib's proxy handlers."""

    def https_open(self, req):
        return self.do_open(VerifiedHTTPSConnection, req, context=self._context)


class VerifiedProxyHandler(urllib.request.ProxyHandler):
    """Reject TLS-to-proxy URLs that stdlib would silently treat as plaintext."""

    def proxy_open(self, req, proxy, type):
        if urlsplit(proxy).scheme == "https":
            # Match ProxyHandler's bypass before touching the request or socket.
            if req.host and urllib.request.proxy_bypass(req.host):
                return None
            raise ssl.SSLError("The urllib transport does not support HTTPS-scheme proxies")
        return super().proxy_open(req, proxy, type)

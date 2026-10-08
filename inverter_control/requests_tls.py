"""Session-local Requests adapter; ordinary Requests globals are never modified."""

import socket
import ssl
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

if __package__:
    from .tls_policy import verify_key_lengths
else:  # log_forwarder.py is also a supported directly executed service entry point.
    from tls_policy import verify_key_lengths


def _tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    if context.security_level < 2:
        context.set_ciphers("DEFAULT:@SECLEVEL=2")
    return context


class _VerifiedConnection(HTTPSConnection):
    def connect(self):
        try:
            super().connect()
            verify_key_lengths(self.sock)
        except Exception:
            self.close()
            raise

    def _connect_tls_proxy(self, hostname: str, sock: socket.socket) -> ssl.SSLSocket:
        # Use the same selected roots as the destination, including Requests CA env.
        context = _tls_context()
        if self.ca_certs or self.ca_cert_dir or self.ca_cert_data:
            context.load_verify_locations(self.ca_certs, self.ca_cert_dir, self.ca_cert_data)
        else:
            context.load_default_certs()
        if self.proxy_config is None:
            raise ssl.SSLError("HTTPS proxy configuration is missing")
        self.proxy_config = self.proxy_config._replace(ssl_context=context)
        connection = super()._connect_tls_proxy(hostname, sock)
        try:
            verify_key_lengths(connection)
        except Exception:
            connection.close()
            raise
        return connection


class _VerifiedPool(HTTPSConnectionPool):
    ConnectionCls = _VerifiedConnection


class VerifiedHTTPAdapter(HTTPAdapter):
    """Keep caller pooling/retry settings and enforce HTTPS peer-key minima."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "https": _VerifiedPool,
        }

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        if urlsplit(proxy).scheme not in ("http", "https"):
            raise ValueError("HTTPS key validation supports HTTP and HTTPS CONNECT proxies only")
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        manager.pool_classes_by_scheme = {
            **manager.pool_classes_by_scheme,
            "https": _VerifiedPool,
        }
        return manager

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        if verify is False:
            raise ValueError("HTTPS certificate verification cannot be disabled")
        # Let urllib3 create a context for this connection. Reusing one context
        # across CA selections would accumulate roots from earlier requests.
        return super().build_connection_pool_key_attributes(request, verify, cert)

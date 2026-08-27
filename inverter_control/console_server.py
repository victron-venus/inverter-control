#!/usr/bin/env python3
"""
TCP Console Server for Inverter Control
Streams console output to connected clients on port 9999
Uses threading for compatibility with synchronous main loop
"""

import logging
import socket
import threading
from collections import deque

logger = logging.getLogger("inverter-control")

TCP_CONSOLE_PORT = 9999
_clients: set[socket.socket] = set()
_clients_lock = threading.Lock()
_server_socket = None
_server_thread = None
_running = False
_console_buffer: deque = deque(maxlen=100)


def _accept_clients():
    """Accept loop running in background thread"""
    global _running
    while _running and _server_socket:
        try:
            client, addr = _server_socket.accept()
            client.setblocking(False)
            logger.info(f"Console client connected: {addr}")

            with _clients_lock:
                _clients.add(client)

            # Send buffered lines
            try:
                for line in _console_buffer:
                    client.sendall((line + "\n").encode("utf-8"))
            except Exception:
                pass

        except socket.timeout:
            continue
        except Exception as e:
            if _running:
                logger.debug(f"Accept error: {e}")
            break


def broadcast_line(line: str):
    """Send a line to all connected console clients (thread-safe)"""
    # Buffer the line
    _console_buffer.append(line)

    if not _clients:
        return

    data = (line + "\n").encode("utf-8")
    dead_clients = set()

    with _clients_lock:
        for client in _clients.copy():
            try:
                client.sendall(data)
            except Exception:
                dead_clients.add(client)

        # Clean up dead clients
        for client in dead_clients:
            _clients.discard(client)
            try:
                client.close()
            except Exception:
                pass


def start_server():
    """Start the TCP console server in background thread"""
    global _server_socket, _server_thread, _running

    if _running:
        return

    try:
        _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server_socket.settimeout(1.0)  # For clean shutdown
        _server_socket.bind(("0.0.0.0", TCP_CONSOLE_PORT))
        _server_socket.listen(5)

        _running = True
        _server_thread = threading.Thread(target=_accept_clients, daemon=True)
        _server_thread.start()

        logger.info(f"TCP console server started on port {TCP_CONSOLE_PORT}")
        print(f"  TCP console: port {TCP_CONSOLE_PORT} (nc Cerbo {TCP_CONSOLE_PORT})")
    except Exception:
        logging.exception("Failed to start TCP console server")


def stop_server():
    """Stop the TCP console server"""
    global _server_socket, _server_thread, _running

    _running = False

    if _server_socket:
        try:
            _server_socket.close()
        except Exception:
            pass
        _server_socket = None

    # Close all clients
    with _clients_lock:
        for client in _clients.copy():
            try:
                client.close()
            except Exception:
                pass
        _clients.clear()

    if _server_thread:
        _server_thread.join(timeout=2)
        _server_thread = None

#!/usr/bin/env python3
"""
TCP Console Server for Inverter Control
Streams console output to connected clients on port 9999
Uses threading for compatibility with synchronous main loop
"""

import logging
import os
import queue
import socket
import threading
import time
from collections import deque

logger = logging.getLogger("inverter-control")

TCP_CONSOLE_PORT = 9999
_clients: set[socket.socket] = set()
_clients_lock = threading.Lock()
_server_socket = None
_server_thread = None
_sender_thread = None
_sender_queue: queue.Queue[str] = queue.Queue(maxsize=200)
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
                if not _running:
                    client.close()
                    return
                _clients.add(client)

            # Send buffered lines on a non-blocking socket; a full send buffer
            # raises immediately and is swallowed. This runs on the accept
            # thread (never the control main thread).
            try:
                for line in tuple(_console_buffer):
                    client.sendall((line + "\n").encode("utf-8"))
            except OSError:
                # sendall can have written only part of a line. Drop this client
                # instead of continuing with a corrupt stream or retaining it.
                with _clients_lock:
                    _clients.discard(client)
                _close_client(client)

        except TimeoutError:
            continue
        except Exception as e:
            if _running:
                logger.debug(f"Accept error: {e}")
            break


def _close_client(client: socket.socket) -> None:
    """Best-effort socket cleanup after disconnect or shutdown."""
    try:
        client.close()
    except OSError as exc:
        logger.debug("Console socket close failed: %s", type(exc).__name__)


def _send_loop():
    """Background thread that drains the send queue and streams to clients.

    All socket I/O happens here so broadcast_line never blocks the control
    main thread on a slow client or on _clients_lock contention."""
    while True:
        if _next_line_done():
            return


def _next_line_done() -> bool:
    """Wait for a console line and stream it; True when the sender should exit."""
    try:
        line = _sender_queue.get(timeout=0.5)
    except queue.Empty:
        return bool(not _running and _sender_queue.empty())
    except Exception:
        return True

    try:
        _send_to_clients(line)
    finally:
        _sender_queue.task_done()
    return False


def _send_to_clients(line: str) -> None:
    """Stream one line to all connected clients and drop dead sockets."""
    data = (line + "\n").encode("utf-8")
    dead_clients = set()

    with _clients_lock:
        for client in _clients.copy():
            try:
                client.sendall(data)
            except Exception:
                dead_clients.add(client)

        for client in dead_clients:
            _clients.discard(client)
            _close_client(client)


def broadcast_line(line: str):
    """Enqueue a line for all connected console clients (never blocks main thread)"""
    _console_buffer.append(line)
    try:
        _sender_queue.put_nowait(line)
    except queue.Full:
        pass  # Drop console lines silently when the sender queue is full


def start_server():
    """Start the TCP console server in background thread"""
    global _server_socket, _server_thread, _sender_thread, _running

    if _running:
        return

    try:
        _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _server_socket.settimeout(1.0)  # For clean shutdown
        _server_socket.bind(
            (os.environ.get("INVERTER_CONSOLE_HOST", "127.0.0.1"), TCP_CONSOLE_PORT)
        )
        _server_socket.listen(5)

        _running = True
        _server_thread = threading.Thread(target=_accept_clients, daemon=True)
        _server_thread.start()
        _sender_thread = threading.Thread(target=_send_loop, daemon=True)
        _sender_thread.start()

        logger.info(f"TCP console server started on port {TCP_CONSOLE_PORT}")
        print(f"  TCP console: port {TCP_CONSOLE_PORT} (nc Cerbo {TCP_CONSOLE_PORT})")
    except Exception:
        logging.exception("Failed to start TCP console server")


def request_stop_server():
    """Stop accepting without waiting for the accept thread."""
    global _server_socket, _running
    _running = False
    if _server_socket:
        _close_client(_server_socket)
        _server_socket = None


def stop_server(timeout: float = 2.0) -> bool:
    """Stop the TCP console server within the caller's remaining join budget."""
    global _server_thread, _sender_thread
    deadline = time.monotonic() + max(0.0, timeout)
    request_stop_server()
    # The sender owns this lock during sends; waiting is part of the budget.
    if not _clients_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        return False
    try:
        for client in _clients.copy():
            _close_client(client)
        _clients.clear()
    finally:
        _clients_lock.release()

    stopped = True
    if _server_thread:
        _server_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        stopped = not _server_thread.is_alive()
        if stopped:
            _server_thread = None

    # The sender drains queued lines and exits on its next idle timeout. Keep
    # a live handle when the shared budget cannot confirm that it has exited.
    if _sender_thread:
        _sender_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        sender_stopped = not _sender_thread.is_alive()
        if sender_stopped:
            _sender_thread = None
        stopped = sender_stopped and stopped
    return stopped

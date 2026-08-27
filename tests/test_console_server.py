"""
Unit tests for Console Server
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import console_server


class TestConsoleServer:
    """Test console server functionality"""

    def teardown_method(self):
        """Reset globals after each test"""
        console_server._clients.clear()
        console_server._server_socket = None
        console_server._server_thread = None
        console_server._sender_thread = None
        console_server._running = False
        console_server._console_buffer.clear()
        while not console_server._sender_queue.empty():
            console_server._sender_queue.get_nowait()

    def test_globals_initialized(self):
        """Test global variables are initialized"""
        assert console_server._clients == set()
        assert console_server._server_socket is None
        assert console_server._server_thread is None
        assert console_server._running is False
        assert console_server.TCP_CONSOLE_PORT == 9999
        assert len(console_server._console_buffer) == 0
        assert console_server._sender_queue.empty()

    @patch("inverter_control.console_server.socket.socket")
    @patch("inverter_control.console_server.threading.Thread")
    def test_start_server(self, mock_thread, mock_socket):
        """Test starting server"""
        mock_sock = MagicMock()
        mock_socket.return_value = mock_sock

        console_server.start_server()

        mock_sock.setsockopt.assert_called_once()
        mock_sock.settimeout.assert_called_once_with(1.0)
        mock_sock.bind.assert_called_once_with(("0.0.0.0", 9999))
        mock_sock.listen.assert_called_once_with(5)
        assert console_server._running is True
        assert console_server._server_socket == mock_sock
        # One thread for accept loop, one for the send loop
        assert mock_thread.call_count == 2
        assert mock_thread.return_value.start.call_count == 2

    def test_start_server_already_running(self):
        """Test start_server doesn't start twice"""
        console_server._running = True
        console_server._server_socket = MagicMock()

        console_server.start_server()  # Should return early

    @patch("inverter_control.console_server.socket.socket")
    def test_start_server_bind_error(self, mock_socket):
        """Test start_server handles bind error"""
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = OSError("Address already in use")
        mock_socket.return_value = mock_sock

        console_server.start_server()  # Should not raise

        assert console_server._running is False

    def test_stop_server(self):
        """Test stopping server closes connections"""
        mock_server = MagicMock()
        mock_client1 = MagicMock()
        mock_client2 = MagicMock()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True

        console_server._server_socket = mock_server
        console_server._clients = {mock_client1, mock_client2}
        console_server._server_thread = mock_thread
        console_server._running = True

        console_server.stop_server()

        mock_client1.close.assert_called_once()
        mock_client2.close.assert_called_once()
        mock_server.close.assert_called_once()
        assert console_server._running is False
        mock_thread.join.assert_called_once_with(timeout=2)
        assert len(console_server._clients) == 0

    def test_stop_server_no_server(self):
        """Test stop_server when no server running"""
        console_server._server_socket = None
        console_server._clients = set()
        console_server._server_thread = None
        console_server._running = False

        console_server.stop_server()  # Should not raise

    def test_broadcast_line_no_clients(self):
        """Test broadcast_line with no clients returns early"""
        console_server._clients = set()
        initial_buffer_len = len(console_server._console_buffer)

        console_server.broadcast_line("test line")

        # Line should still be buffered
        assert len(console_server._console_buffer) == initial_buffer_len + 1

    def test_broadcast_line_enqueues(self):
        """Test broadcast_line enqueues the line without blocking on clients"""
        console_server._clients = set()
        console_server.broadcast_line("hello")
        assert console_server._sender_queue.get_nowait() == "hello"
        assert console_server._sender_queue.empty()

    def test_send_loop_streams_to_clients(self):
        """Test _send_loop sends lines to clients and drops dead ones"""
        mock_client1 = MagicMock()
        mock_client2 = MagicMock()
        mock_client2.sendall.side_effect = Exception("Broken pipe")

        console_server._clients = {mock_client1, mock_client2}
        console_server._running = False  # let _send_loop exit after draining

        console_server.broadcast_line("hello")
        console_server._send_loop()  # drains the single queued line synchronously

        mock_client1.sendall.assert_called_once_with(b"hello\n")
        mock_client2.sendall.assert_called_once_with(b"hello\n")
        # Dead client should be removed
        assert mock_client2 not in console_server._clients
        assert mock_client1 in console_server._clients

    def test_broadcast_line_buffers(self):
        """Test broadcast_line buffers lines"""
        console_server._clients = set()

        console_server.broadcast_line("line1")
        console_server.broadcast_line("line2")

        assert list(console_server._console_buffer) == ["line1", "line2"]

    def test_buffer_maxlen(self):
        """Test console buffer has maxlen of 100"""
        console_server._clients = set()

        for i in range(150):
            console_server.broadcast_line(f"line{i}")

        assert len(console_server._console_buffer) == 100
        assert list(console_server._console_buffer)[0] == "line50"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

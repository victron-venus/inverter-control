"""Tests for log_forwarder module."""

import importlib
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

log_forwarder = importlib.import_module("inverter_control.log_forwarder")


class TestLogForwarder:
    """Tests for log forwarder functions."""

    def setup_method(self):
        """Set up test environment."""
        self.temp_state_file = tempfile.NamedTemporaryFile(delete=False).name
        self.temp_log_file = tempfile.NamedTemporaryFile(delete=False).name

        # Patch the global constants
        self.state_patcher = patch.object(log_forwarder, "STATE_FILE", self.temp_state_file)
        self.state_patcher.start()

        # Create test log file
        with open(self.temp_log_file, "w", encoding="utf-8") as f:
            f.write("@4000000067890abcdef12345 message 1\n")
            f.write("@4000000067890abcdef12346 message 2\n")
            f.write("plain message without timestamp\n")

    def teardown_method(self):
        """Clean up test environment."""
        self.state_patcher.stop()
        if os.path.exists(self.temp_state_file):
            os.unlink(self.temp_state_file)
        if os.path.exists(self.temp_log_file):
            os.unlink(self.temp_log_file)

    def test_load_state_no_file(self):
        """Test loading state when file doesn't exist."""
        result = log_forwarder.load_state()
        assert result == {}

    def test_load_state_empty_file(self):
        """Test loading state from empty file."""
        with open(self.temp_state_file, "w", encoding="utf-8") as f:
            f.write("")
        result = log_forwarder.load_state()
        assert result == {}

    def test_load_state_invalid_json(self):
        """Test loading state with invalid JSON."""
        with open(self.temp_state_file, "w", encoding="utf-8") as f:
            f.write("invalid json")
        result = log_forwarder.load_state()
        assert result == {}

    def test_load_state_valid_json(self):
        """Test loading valid state."""
        state_data = {"service1": {"position": 100, "inode": 12345}}
        with open(self.temp_state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f)
        result = log_forwarder.load_state()
        assert result == state_data

    def test_save_state(self):
        """Test saving state to file."""
        state = {"service1": {"position": 100, "inode": 12345}}
        log_forwarder.save_state(state)

        with open(self.temp_state_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved == state

    def test_save_state_os_error(self):
        """Test save_state handles OSError gracefully."""
        # Make state file a directory to cause OSError
        os.unlink(self.temp_state_file)
        os.makedirs(self.temp_state_file)
        try:
            log_forwarder.save_state({"test": "data"})
        except Exception:
            pytest.fail("save_state should handle OSError gracefully")
        finally:
            # Cleanup the directory
            import shutil

            shutil.rmtree(self.temp_state_file, ignore_errors=True)

    def test_parse_multilog_timestamp_valid(self):
        """Test parsing valid TAI64N timestamp."""
        # Use a known TAI64N timestamp: @4000000067890abcdef12345
        # TAI64N: 8 bytes seconds + 4 bytes nanoseconds = 24 hex chars
        line = "@4000000067890abcdef12345 test message"
        ts, msg = log_forwarder.parse_multilog_timestamp(line)

        assert ts is not None
        # Message starts after timestamp + space (25 chars + 1 = position 26), so no leading space
        assert msg == "test message"

    def test_parse_multilog_timestamp_invalid_format(self):
        """Test parsing invalid timestamp format."""
        line = "no timestamp here"
        ts, msg = log_forwarder.parse_multilog_timestamp(line)
        assert ts is None
        assert msg == line

    def test_parse_multilog_timestamp_too_short(self):
        """Test parsing timestamp that's too short."""
        line = "@short"
        ts, msg = log_forwarder.parse_multilog_timestamp(line)
        assert ts is None
        assert msg == line

    def test_parse_multilog_timestamp_invalid_hex(self):
        """Test parsing timestamp with invalid hex."""
        line = "@4000000067890abcdef1234g invalid hex"
        ts, msg = log_forwarder.parse_multilog_timestamp(line)
        assert ts is None
        assert msg == line

    def test_parse_multilog_timestamp_no_message(self):
        """Test parsing timestamp with no message."""
        line = "@4000000067890abcdef12345"
        ts, msg = log_forwarder.parse_multilog_timestamp(line)
        assert ts is not None
        assert msg == ""

    def test_read_new_lines_basic(self):
        """Test reading new lines from file."""
        lines, pos, inode = log_forwarder.read_new_lines(self.temp_log_file, 0, None)

        assert len(lines) >= 3
        assert pos > 0
        assert inode is not None

    def test_read_new_lines_with_position(self):
        """Test reading from specific position returns new lines only."""
        # Read all lines first
        lines1, pos1, inode1 = log_forwarder.read_new_lines(self.temp_log_file, 0, None)

        # Reading from the returned position should return no new lines
        lines2, pos2, inode2 = log_forwarder.read_new_lines(self.temp_log_file, pos1, inode1)
        assert lines2 == []

    def test_read_new_lines_appended_content(self):
        """Test reading appended content."""
        # Read initial content
        lines1, pos1, inode1 = log_forwarder.read_new_lines(self.temp_log_file, 0, None)

        # Append more content
        with open(self.temp_log_file, "a") as f:
            f.write("@4000000067890abcdef12347 appended line\n")

        # Read again from same position
        lines2, pos2, inode2 = log_forwarder.read_new_lines(self.temp_log_file, pos1, inode1)
        assert len(lines2) >= 1
        assert any("appended line" in line for line in lines2)

    def test_read_new_lines_nonexistent_file(self):
        """Test reading from nonexistent file."""
        lines, pos, inode = log_forwarder.read_new_lines("/nonexistent/file.log", 0, None)
        assert lines == []
        assert pos == 0
        assert inode is None

    def test_read_new_lines_batch_limit(self):
        """Test batch size limit."""
        many_lines_file = tempfile.NamedTemporaryFile(delete=False).name
        try:
            with open(many_lines_file, "w") as f:
                for i in range(200):
                    f.write(f"line {i}\n")

            lines, _, _ = log_forwarder.read_new_lines(many_lines_file, 0, None)
            assert len(lines) == log_forwarder.BATCH_SIZE
        finally:
            if os.path.exists(many_lines_file):
                os.unlink(many_lines_file)

    def test_format_loki_payload(self):
        """Test formatting Loki payload."""
        lines = [
            "@4000000067890abcdef12345 message one",
            "plain message two",
        ]
        payload = log_forwarder.format_loki_payload("test-service", lines)

        assert "streams" in payload
        assert len(payload["streams"]) == 1
        stream = payload["streams"][0]

        assert stream["stream"]["job"] == "cerbo"
        assert stream["stream"]["service"] == "test-service"

        assert "values" in stream
        assert len(stream["values"]) == 2

        # Check first value has timestamp and message
        assert len(stream["values"][0]) == 2
        assert "message one" in stream["values"][0][1]

        # Check second value uses current time for timestamp
        assert len(stream["values"][1]) == 2
        assert "plain message two" in stream["values"][1][1]

    def test_format_loki_payload_empty_lines(self):
        """Test formatting with empty lines list."""
        payload = log_forwarder.format_loki_payload("test-service", [])
        assert payload["streams"][0]["values"] == []

    @patch("inverter_control.log_forwarder.USE_REQUESTS", True)
    @patch("inverter_control.log_forwarder.requests.post")
    def test_push_to_loki_success_requests(self, mock_post):
        """Test successful push with requests library."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        payload = {"streams": [{"stream": {"job": "test"}, "values": [["123", "msg"]]}]}
        result = log_forwarder.push_to_loki(payload)

        assert result is True
        mock_post.assert_called_once()

    @patch("inverter_control.log_forwarder.USE_REQUESTS", True)
    @patch("inverter_control.log_forwarder.requests.post")
    def test_push_to_loki_failure_requests(self, mock_post):
        """Test failed push with requests library."""
        import requests

        mock_post.side_effect = requests.RequestException("Connection error")

        payload = {"streams": [{"stream": {"job": "test"}, "values": [["123", "msg"]]}]}
        result = log_forwarder.push_to_loki(payload)

        assert result is False

    @patch("inverter_control.log_forwarder.USE_REQUESTS", False)
    @patch("inverter_control.log_forwarder.urllib.request.urlopen")
    def test_push_to_loki_success_urllib(self, mock_urlopen):
        """Test successful push with urllib."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        payload = {"streams": [{"stream": {"job": "test"}, "values": [["123", "msg"]]}]}
        result = log_forwarder.push_to_loki(payload)

        assert result is True

    @patch("inverter_control.log_forwarder.USE_REQUESTS", False)
    @patch("inverter_control.log_forwarder.urllib.request.urlopen")
    def test_push_to_loki_failure_http_error(self, mock_urlopen):
        """Test failed push with HTTP error."""
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        payload = {"streams": [{"stream": {"job": "test"}, "values": [["123", "msg"]]}]}
        result = log_forwarder.push_to_loki(payload)

        assert result is False

    @patch("inverter_control.log_forwarder.USE_REQUESTS", False)
    @patch("inverter_control.log_forwarder.urllib.request.urlopen")
    def test_push_to_loki_exception_urllib(self, mock_urlopen):
        """Test push exception handling with urllib."""
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        payload = {"streams": [{"stream": {"job": "test"}, "values": [["123", "msg"]]}]}
        result = log_forwarder.push_to_loki(payload)

        assert result is False

    @patch.object(log_forwarder, "LOG_SOURCES", {})
    def test_process_logs_no_sources(self):
        """Test process_logs with no sources."""
        log_forwarder.process_logs()
        # Should not crash

    @patch.object(log_forwarder, "LOG_SOURCES", {"test-service": "/nonexistent.log"})
    def test_process_logs_nonexistent_source(self):
        """Test process_logs with nonexistent log file."""
        log_forwarder.process_logs()
        # Should not crash

    def test_parse_multilog_timestamp_tai64n_conversion(self):
        """Test TAI64N to Unix timestamp conversion."""
        # Known TAI64N: @400000000000000000000000
        # TAI64 epoch = 2^62 seconds before Unix epoch
        # Unix timestamp = TAI64 - 2^62 - 10
        line = "@400000000000000000000000 test"
        ts, msg = log_forwarder.parse_multilog_timestamp(line)

        assert ts is not None
        assert msg == "test"
        # Verify it's a reasonable timestamp (not the TAI64 raw value)
        assert ts < 10**20  # Should be in nanoseconds but reasonable

    def test_constant_definitions(self):
        """Test module constants are defined."""
        assert log_forwarder.POLL_INTERVAL == 5
        assert log_forwarder.BATCH_SIZE == 100
        assert log_forwarder.JOB_LABEL == "cerbo"
        assert "inverter-control" in log_forwarder.LOG_SOURCES
        assert "dbus-mqtt-chain1" in log_forwarder.LOG_SOURCES
        assert "dbus-mqtt-chain2" in log_forwarder.LOG_SOURCES
        assert "dbus-virtual-chain" in log_forwarder.LOG_SOURCES


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
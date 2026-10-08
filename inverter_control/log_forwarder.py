#!/usr/bin/env python3
"""
Log forwarder for Cerbo GX to Loki.

Reads multilog directories and forwards logs to Loki via HTTP push API.
Designed for Venus OS with minimal dependencies.
"""

import json
import os
import re
import sys
import tempfile
import time
import traceback

# Import urllib at module level for test patching
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests

    if __package__:
        from .requests_tls import VerifiedHTTPAdapter
    else:
        from requests_tls import VerifiedHTTPAdapter

    USE_REQUESTS = True
except ImportError:
    USE_REQUESTS = False

if __package__:
    from .tls_policy import VerifiedHTTPSHandler, VerifiedProxyHandler
else:
    from tls_policy import VerifiedHTTPSHandler, VerifiedProxyHandler

# Configuration
LOKI_URL = os.environ.get("LOKI_URL", "")
LOKI_URL_FILE = "/data/setupOptions/inverter-control/loki_url"
STATE_FILE = os.environ.get("STATE_FILE", "/run/inverter-control/log-forwarder-state.json")  # nosec B310 # nosonar — single-user embedded device
POLL_INTERVAL = 5  # seconds
CONFIG_RETRY_INTERVAL = 60  # seconds, while configuration is missing or invalid
BATCH_SIZE = 100  # max lines per push
JOB_LABEL = "cerbo"
ROTATED_LOG = re.compile(r"^@[0-9a-fA-F]{24}\.[su]$")

# Log sources: service_name -> log file path
LOG_SOURCES = {
    "inverter-control": "/var/log/inverter-control/current",
    "dbus-mqtt-chain1": "/var/log/dbus-mqtt-chain1/current",
    "dbus-mqtt-chain2": "/var/log/dbus-mqtt-chain2/current",
    "dbus-virtual-chain": "/var/log/dbus-virtual-chain/current",
}


def validate_loki_url(url):
    """Reject malformed endpoints before sending any log content."""
    endpoint = urllib.parse.urlsplit(url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.fragment
        or any(char.isspace() for char in url)
        or "\\" in url
    ):
        raise ValueError(
            "Loki endpoint must be an absolute HTTP(S) URL without whitespace, backslashes or a fragment"
        )
    # Only literal loopback addresses may use plaintext, without DNS resolution
    # or alternate address spellings that HTTP clients may interpret differently.
    if endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("Remote Loki endpoints require HTTPS")
    # Accessing port validates its syntax and range, even without a network call.
    if endpoint.port == 0:
        raise ValueError("Loki endpoint port must be greater than zero")


def load_loki_url():
    """Load the operator endpoint outside the replaceable release directory."""
    if "LOKI_URL" in os.environ:
        url = os.environ["LOKI_URL"].strip()
    else:
        filename = os.environ.get("LOKI_URL_FILE", LOKI_URL_FILE)
        try:
            with open(filename, encoding="utf-8") as config:
                url = config.read(8193).strip()
        except FileNotFoundError:
            return ""
    if len(url) > 8192:
        raise ValueError("Loki endpoint exceeds 8192 characters")
    if url:
        validate_loki_url(url)
    return url


def wait_for_loki_url():
    """Retry absent or invalid configuration without a supervisor restart loop."""
    last_problem = None
    while True:
        try:
            url = load_loki_url()
            if url:
                return url
            problem = "Loki endpoint is not configured; forwarding is idle"
        except (OSError, ValueError) as error:
            # Do not print URL contents, which can include authentication data.
            problem = (
                f"Loki endpoint configuration is unreadable or invalid ({type(error).__name__})"
            )
        if problem != last_problem:
            print(
                f"{problem}; set LOKI_URL or {os.environ.get('LOKI_URL_FILE', LOKI_URL_FILE)}. "
                f"Retrying in {CONFIG_RETRY_INTERVAL}s",
                file=sys.stderr,
            )
            last_problem = problem
        time.sleep(CONFIG_RETRY_INTERVAL)


def load_state():
    """Load file positions from state file."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return {}
        # A partially corrupted entry must not prevent all services forwarding.
        return {
            service: cursor
            for service, cursor in state.items()
            if isinstance(cursor, dict)
            and type(cursor.get("position")) is int
            and cursor["position"] >= 0
            and (cursor.get("inode") is None or type(cursor["inode"]) is int)
        }
    except (OSError, ValueError):
        return {}


def save_state(state):
    """Atomically replace the cursor; a crash may replay, but cannot skip logs."""
    temporary = None
    try:
        directory = os.path.dirname(os.path.abspath(STATE_FILE))
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, prefix=".log-forwarder-", delete=False
        ) as f:
            temporary = f.name
            json.dump(state, f)
        os.replace(temporary, STATE_FILE)
    except OSError as e:
        print(f"Warning: Could not save state: {e}", file=sys.stderr)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def parse_multilog_timestamp(line):
    """
    Parse multilog @timestamp format.

    Multilog timestamps are in TAI64N format: @4000000067890abcdef12345
    The @ prefix followed by 24 hex characters.

    Returns (timestamp_ns, message) or (None, line) if no timestamp.
    """
    if not line.startswith("@") or len(line) < 25:
        return None, line

    try:
        # TAI64N: 8 bytes seconds + 4 bytes nanoseconds = 24 hex chars
        tai64n_hex = line[1:25]
        tai64_secs = int(tai64n_hex[:16], 16)
        nanosecs = int(tai64n_hex[16:24], 16)

        # TAI64 epoch is 2^62 seconds before Unix epoch
        # Unix timestamp = TAI64 - 2^62 - 10 (TAI-UTC offset, approximate)
        unix_secs = tai64_secs - (1 << 62) - 10
        timestamp_ns = unix_secs * 1_000_000_000 + nanosecs

        # Message is everything after the timestamp and space
        message = line[26:] if len(line) > 26 else ""
        return timestamp_ns, message
    except (ValueError, IndexError):
        return None, line


def retained_log_files(filepath):
    """List retained files in order; return None if rotation raced the scan."""
    directory = os.path.dirname(os.path.abspath(filepath))
    current_name = os.path.basename(filepath)

    def relevant_names():
        return {
            name
            for name in os.listdir(directory)
            if ROTATED_LOG.fullmatch(name) or name in {"previous", current_name}
        }

    names = relevant_names()
    paths = [os.path.join(directory, name) for name in sorted(names) if ROTATED_LOG.fullmatch(name)]
    paths.extend([os.path.join(directory, "previous"), filepath])
    files = []
    seen = set()
    for path in paths:
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            continue  # multilog may rename an archive while this list is built
        if stat.st_ino not in seen:
            files.append((path, stat.st_ino))
            seen.add(stat.st_ino)
    if relevant_names() != names:
        # In particular, do not declare the saved inode lost when current was
        # renamed after listdir but before stat and its new name was not seen.
        return None
    return files


def read_new_lines(filepath, position, inode):
    """Drain the saved inode and newer retained files, advancing only whole lines."""
    try:
        files = retained_log_files(filepath)
        if not files:
            return [], position, inode
        start = next((index for index, (_, number) in enumerate(files) if number == inode), None)
        if inode is not None and start is None:
            print(
                f"Warning: Retention loss for {filepath}: saved inode {inode} is no longer "
                "available; replaying all retained logs (duplicates are possible)",
                file=sys.stderr,
            )
        new_position, new_inode = position, inode
        for path, expected_inode in files[start or 0 :]:
            with open(path, "rb") as f:
                stat = os.fstat(f.fileno())
                if stat.st_ino != expected_inode:
                    # A second rotation raced our directory scan. Retry the same
                    # acknowledged cursor next poll rather than skip an archive.
                    return [], position, inode
                new_inode = stat.st_ino
                new_position = position if inode == new_inode else 0
                if stat.st_size < new_position:
                    print(f"Warning: Log truncated: {path}; replaying it", file=sys.stderr)
                    new_position = 0
                f.seek(new_position)
                lines = []
                while len(lines) < BATCH_SIZE:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n") and path == filepath:
                        # The writer has not finished this line. Keep its bytes
                        # pending, including split UTF-8 characters.
                        f.seek(line_start)
                        break
                    message = line.rstrip(b"\n\r").decode("utf-8", errors="replace")
                    if message:
                        lines.append(message)
                new_position = f.tell()
            if lines or path == filepath:
                return lines, new_position, new_inode
            # Exhausted archive: continue to the next file, even if it is empty.
        return [], new_position, new_inode
    except OSError as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)
        return [], position, inode


def format_loki_payload(service_name, lines):
    """
    Format lines for Loki push API.

    Returns JSON payload for /loki/api/v1/push
    """
    values = []
    now_ns = int(time.time() * 1_000_000_000)

    for line in lines:
        timestamp_ns, message = parse_multilog_timestamp(line)
        if timestamp_ns is None:
            timestamp_ns = now_ns

        # Loki expects [timestamp_string, log_line]
        values.append([str(timestamp_ns), message])

    payload = {
        "streams": [
            {
                "stream": {
                    "job": JOB_LABEL,
                    "service": service_name,
                },
                "values": values,
            }
        ]
    }

    return payload


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Keep log payloads on the explicitly configured Loki endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def push_to_loki(payload):
    """Push logs to Loki via HTTP."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    try:
        validate_loki_url(LOKI_URL)
        if USE_REQUESTS:
            with requests.Session() as session:
                session.mount("https://", VerifiedHTTPAdapter())
                resp = session.post(
                    LOKI_URL, data=data, headers=headers, timeout=10, allow_redirects=False
                )
                if 300 <= resp.status_code < 400:
                    raise ValueError("Loki redirects are not allowed")
                resp.raise_for_status()
        else:
            req = urllib.request.Request(  # pylint: disable=used-before-assignment
                LOKI_URL, data=data, headers=headers, method="POST"
            )
            opener = urllib.request.build_opener(
                _RejectRedirects(), VerifiedHTTPSHandler(), VerifiedProxyHandler()
            )
            with opener.open(req, timeout=10) as resp:
                if resp.status >= 400:
                    raise urllib.error.HTTPError(
                        LOKI_URL, resp.status, f"HTTP {resp.status}", {}, None
                    )
        return True
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None) or getattr(
            e, "code", None
        )
        detail = f"HTTP {status}" if status is not None else type(e).__name__
        print(f"Error pushing to Loki ({detail}); retaining the cursor for retry", file=sys.stderr)
        return False


def process_logs():
    """Main processing loop iteration."""
    state = load_state()
    state_changed = False

    for service_name, filepath in LOG_SOURCES.items():
        # Get current position and inode from state
        file_state = state.get(service_name, {})
        position = file_state.get("position", 0)
        inode = file_state.get("inode", None)

        # Read new lines
        lines, new_position, new_inode = read_new_lines(filepath, position, inode)

        if lines:
            payload = format_loki_payload(service_name, lines)
            if push_to_loki(payload):
                # Update state only on successful push
                state[service_name] = {"position": new_position, "inode": new_inode}
                state_changed = True
                print(f"Forwarded {len(lines)} lines from {service_name}")
            else:
                # Keep old position to retry
                print(
                    f"Failed to forward {len(lines)} lines from {service_name}",
                    file=sys.stderr,
                )
        elif (new_position, new_inode) != (position, inode):
            # Empty files and blank lines need acknowledgement as well.
            state[service_name] = {"position": new_position, "inode": new_inode}
            state_changed = True

    if state_changed:
        save_state(state)


def main():
    """Main entry point."""
    global LOKI_URL
    print("Log forwarder starting...")
    LOKI_URL = wait_for_loki_url()
    print(f"Loki endpoint configured ({urllib.parse.urlsplit(LOKI_URL).hostname})")
    print(f"State file: {STATE_FILE}")
    print(f"Poll interval: {POLL_INTERVAL}s")
    print(f"Monitoring: {', '.join(LOG_SOURCES.keys())}")
    print(f"Using: {'requests' if USE_REQUESTS else 'urllib'}")

    while True:
        try:
            process_logs()
        except Exception as e:
            print(f"Error in main loop: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

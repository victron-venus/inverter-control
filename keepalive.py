#!/usr/bin/env python3
"""
Keepalive script - maintains last setpoint during main process restart.
Runs for a short time, sending cached setpoint to prevent Victron standby.
"""

import subprocess
import time
import sys
import os

# How long to run (seconds)
DURATION = int(os.environ.get("KEEPALIVE_DURATION", "30"))
INTERVAL = 1.0


def dbus_get(service, path):
    """Get value from D-Bus"""
    try:
        result = subprocess.run(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=" + service,
                path,
                "com.victronenergy.BusItem.GetValue",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "int32" in line or "double" in line:
                    return int(line.split()[-1])
        return None
    except Exception:
        return None


def dbus_set(service, path, value):
    """Set value on D-Bus"""
    try:
        subprocess.run(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=" + service,
                path,
                "com.victronenergy.BusItem.SetValue",
                f"variant:int32:{value}",
            ],
            capture_output=True,
            timeout=2,
        )
        return True
    except Exception:
        return False


def find_vebus_service():
    """Find vebus service name"""
    try:
        result = subprocess.run(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=com.victronenergy.dbusmonitor",
                "/",
                "com.victronenergy.dbusmonitor.GetServices",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in result.stdout.split("\n"):
            if "com.victronenergy.vebus" in line:
                # Extract service name
                import re

                match = re.search(r"(com\.victronenergy\.vebus\.\w+)", line)
                if match:
                    return match.group(1)
    except Exception:
        pass
    # Fallback to common name
    return "com.victronenergy.vebus.ttyUSB2"


def main():
    print(f"[Keepalive] Starting, will run for {DURATION}s")

    vebus = find_vebus_service()
    print(f"[Keepalive] Using vebus: {vebus}")

    # Get current setpoint
    setpoint = dbus_get(vebus, "/Hub4/L1/AcPowerSetpoint")
    if setpoint is None:
        setpoint = 0
        print("[Keepalive] Could not read setpoint, using 0")
    else:
        print(f"[Keepalive] Current setpoint: {setpoint}W")

    start = time.time()
    count = 0

    while time.time() - start < DURATION:
        # Check if main process is back
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "http://localhost:8080/api/state",  # nosec B310 — local API
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.stdout.strip() == "200":
                print("[Keepalive] Main process is back, exiting")
                return 0
        except Exception:
            pass

        # Send setpoint
        if dbus_set(vebus, "/Hub4/L1/AcPowerSetpoint", setpoint):
            count += 1
            if count % 5 == 0:
                print(f"[Keepalive] Sent setpoint {setpoint}W ({count} times)")

        time.sleep(INTERVAL)

    print(f"[Keepalive] Timeout after {DURATION}s, exiting")
    return 1


if __name__ == "__main__":
    sys.exit(main())

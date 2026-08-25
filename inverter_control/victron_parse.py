"""D-Bus tree output parsers and battery SOC calculation."""

import logging
import re
from typing import Any

logger = logging.getLogger("inverter-control")

# Pre-compiled regexes for hot-path parsing (called per-service per-poll cycle)
MPPT_POWER_RE = re.compile(r"Yield/Power[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)")
MPPT_CURRENT_RE = re.compile(r"Dc/0/Current[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)")
TASMOTA_POWER_RE = re.compile(r"Ac/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)")
VARIANT_RE = re.compile(r"variant\s+\S+\s+(-?[\d.]+)")
VARIANT_Typed_RE = re.compile(r"(?:double|int32|variant\s+(?:double|int32))\s+([-\d\.]+)")
VARIANT_STR_RE = re.compile(r"variant\s+(\S.*)")
ACLOAD_POWER_RE = re.compile(r"(?:double|int32|variant\s+(?:double|int32))\s+([-\d.]+)")


def extract_power_from_tree(output: str | None) -> float:
    """Extract Ac/Power value from a D-Bus output.

    Handles both tree query format (with path) and literal format (variant only).
    """
    if not output:
        return 0.0
    match = TASMOTA_POWER_RE.search(output)
    if not match:
        match = VARIANT_RE.search(output)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            logger.debug("Power parse failed: %s", match.group(1))
    return 0.0


def extract_acload_name_power(
    service_output: tuple[str | None, str | None],
) -> tuple[str, float] | None:
    """Extract (key, power) from CustomName + Ac/Power D-Bus outputs. Returns None on failure."""
    name_output, power_output = service_output
    if not name_output or not power_output:
        return None
    name_match = VARIANT_STR_RE.search(name_output.strip())
    power_match = ACLOAD_POWER_RE.search(power_output)
    if not name_match or not power_match:
        return None
    try:
        name = name_match.group(1).strip()
        power = float(power_match.group(1))
        key = name.lower().replace(" ", "_")
        return key, power
    except (ValueError, TypeError) as e:
        logger.debug("acload parse failed: %s", e)
        return None


# System data regex patterns - shared between background poll and sync fallback.
# Bank V/I/P are NOT here on purpose: they come from the SmartShunt service
# (parse_shunt_data_output), never from the system aggregate.
SYSTEM_DATA_PATTERNS = {
    "g1": r"Ac/Grid/L1/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "g2": r"Ac/Grid/L2/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "t1": r"Ac/Consumption/L1/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "t2": r"Ac/Consumption/L2/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "pv_total": r"Dc/Pv/Power[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)",
}

# SmartShunt service tree paths (com.victronenergy.battery.<port>)
SHUNT_DATA_PATTERNS = {
    "bv": r"Dc/0/Voltage[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)",
    "bc": r"Dc/0/Current[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "bp": r"Dc/0/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
}


def parse_system_data_output(output: str) -> dict[str, Any]:
    """Parse system data tree output using shared patterns. Returns dict with g1,g2,gt,t1,t2,tt,pv_total."""
    data: dict[str, Any] = {
        "g1": 0,
        "g2": 0,
        "t1": 0,
        "t2": 0,
        "pv_total": 0,
    }
    for key, pattern in SYSTEM_DATA_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            try:
                val = float(match.group(1))
                data[key] = int(val)
            except (ValueError, TypeError) as e:
                logger.debug("System data parse failed for %s: %s", key, e)
    data["gt"] = data["g1"] + data["g2"]
    data["tt"] = data["t1"] + data["t2"]
    return data


def parse_shunt_data_output(output: str) -> dict[str, Any]:
    """Parse SmartShunt tree output. Returns dict with bv, bc, bp.

    Missing keys stay absent so a partial tree never wipes good values with
    zeros; the shunt is the only source for these keys by design."""
    data: dict[str, Any] = {}
    for key, pattern in SHUNT_DATA_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            try:
                val = float(match.group(1))
                data[key] = val if key != "bp" else int(val)
            except (ValueError, TypeError) as e:
                logger.debug("Shunt data parse failed for %s: %s", key, e)
    return data


def parse_variant_value(output: str | None) -> float:
    """Parse a variant value from D-Bus output, return 0.0 on failure"""
    if not output:
        return 0.0
    match = VARIANT_RE.search(output)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except (ValueError, TypeError):
        logger.debug("D-Bus value parse failed: %s", match.group(1))
        return 0.0


def parse_mppt_output(output: str) -> dict[str, float]:
    """Parse MPPT power and current from tree query output"""
    mppt_data = {"w": 0.0, "a": 0.0}
    match = MPPT_POWER_RE.search(output)
    if match:
        try:
            mppt_data["w"] = float(match.group(1))
        except (ValueError, TypeError):
            logger.debug("MPPT power parse failed: %s", match.group(1))
    match = MPPT_CURRENT_RE.search(output)
    if match:
        try:
            mppt_data["a"] = float(match.group(1))
        except (ValueError, TypeError):
            logger.debug("MPPT current parse failed: %s", match.group(1))
    return mppt_data


# =============================================================================
# BATTERY SOC CALCULATION (ported from HA template sensors)
# =============================================================================

# Bank SOC paradigm copied from the Home Assistant "Battery %" template
# sensor: a plain linear map of pack voltage onto 0-100%, clamped and rounded.
BATTERY_VOLTAGE_MIN = 40.0  # V -> 0%
BATTERY_VOLTAGE_MAX = 54.4  # V -> 100% (absorption voltage)


def calculate_battery_soc_from_voltage(voltage: float) -> float:
    """
    Calculate bank SOC from pack voltage the same way the HA "Battery %" sensor
    does: linear between min and max voltage, clamped to 0-100, rounded.

    Args:
        voltage: Battery voltage in volts (SmartShunt Dc/0/Voltage)

    Returns:
        SOC percentage as whole number 0-100
    """
    try:
        pct = (
            (float(voltage) - BATTERY_VOLTAGE_MIN) / (BATTERY_VOLTAGE_MAX - BATTERY_VOLTAGE_MIN)
        ) * 100.0
    except (ValueError, TypeError):
        return 0.0
    return float(round(min(100.0, max(0.0, pct))))

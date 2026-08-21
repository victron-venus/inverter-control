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


# System data regex patterns - shared between background poll and sync fallback
SYSTEM_DATA_PATTERNS = {
    "g1": r"Ac/Grid/L1/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "g2": r"Ac/Grid/L2/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "t1": r"Ac/Consumption/L1/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "t2": r"Ac/Consumption/L2/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "bv": r"Dc/Battery/Voltage[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)",
    "bc": r"Dc/Battery/Current[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "bp": r"Dc/Battery/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
    "pv_total": r"Dc/Pv/Power[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)",
}


def parse_system_data_output(output: str) -> dict[str, Any]:
    """Parse system data tree output using shared patterns. Returns dict with g1,g2,gt,t1,t2,tt,bv,bc,bp,pv_total."""
    data: dict[str, Any] = {
        "g1": 0,
        "g2": 0,
        "t1": 0,
        "t2": 0,
        "bv": 0.0,
        "bc": 0.0,
        "bp": 0,
        "pv_total": 0,
    }
    for key, pattern in SYSTEM_DATA_PATTERNS.items():
        match = re.search(pattern, output)
        if match:
            try:
                val = float(match.group(1))
                data[key] = int(val) if key not in ("bv", "bc") else val
            except (ValueError, TypeError) as e:
                logger.debug("System data parse failed for %s: %s", key, e)
    data["gt"] = data["g1"] + data["g2"]
    data["tt"] = data["t1"] + data["t2"]
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

# Polynomial coefficients for voltage -> SOC conversion (5th degree, highest first)
# From: sensor.compensation_sensor_battery_voltage coefficients attribute
# SOC = c0*V^5 + c1*V^4 + c2*V^3 + c3*V^2 + c4*V + c5
BATTERY_VOLTAGE_TO_SOC_COEFFS = (
    0.004273352289848183,  # V^5
    -1.1946101528489494,  # V^4
    131.15278553768547,  # V^3
    -7086.612266200085,  # V^2
    188790.53434597014,  # V^1
    -1986209.3055883816,  # V^0 (constant)
)

# Battery parameters for load correction (Coulomb counting approximation)
BATTERY_CAPACITY_CHARGE_AH = 280.0  # Ah when charging
BATTERY_CAPACITY_DISCHARGE_AH = 180.0  # Ah when discharging
BATTERY_ROUNDTRIP_EFFICIENCY = 0.95  # 95%


def _voltage_to_soc(voltage: float) -> float:
    """Convert battery voltage to SOC using 5th-degree polynomial."""
    try:
        v = float(voltage)
        if v < 40.0 or v > 58.4:
            return 0.0
        soc = BATTERY_VOLTAGE_TO_SOC_COEFFS[0]
        for coeff in BATTERY_VOLTAGE_TO_SOC_COEFFS[1:]:
            soc = soc * v + coeff
        return max(0.0, min(100.0, soc))
    except (ValueError, TypeError):
        return 0.0


def _apply_load_correction(base_soc: float, power_w: float) -> float:
    """Apply load correction to SOC based on battery power."""
    try:
        p = float(power_w)
        soc = float(base_soc)

        if p < 0:  # Discharging
            capacity = BATTERY_CAPACITY_DISCHARGE_AH
            correction = (abs(p) / capacity) * 100.0 * (1.0 - BATTERY_ROUNDTRIP_EFFICIENCY)
            return min(soc + correction, 100.0)
        elif p > 0:  # Charging
            capacity = BATTERY_CAPACITY_CHARGE_AH
            correction = (p / capacity) * 100.0 * (1.0 - BATTERY_ROUNDTRIP_EFFICIENCY)
            return max(soc - correction, 0.0)
        else:
            return soc
    except (ValueError, TypeError):
        return base_soc


def calculate_battery_soc_from_voltage(voltage: float, power_w: float) -> float:
    """
    Calculate corrected battery SOC from voltage and power.
    This is the local replacement for HA's corrected_battery_soc sensor.

    Args:
        voltage: Battery voltage in volts (from D-Bus Dc/Battery/Voltage)
        power_w: Battery power in watts (from D-Bus Dc/Battery/Power,
                 positive=charging, negative=discharging)

    Returns:
        SOC percentage (0-100)
    """
    base_soc = _voltage_to_soc(voltage)
    corrected_soc = _apply_load_correction(base_soc, power_w)
    return round(corrected_soc, 2)

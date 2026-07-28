"""
DVCC (Dynamic Voltage and Current Control) Calculator
=======================================================

Calculates dynamic Charge Current Limit (CCL), Discharge Current Limit (DCL),
and Charge Voltage Limit (CVL) based on battery cell data from D-Bus.

Pure Python module - no D-Bus or MQTT dependencies for easy testing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("inverter-control")


@dataclass
class DvccConfig:
    """Configuration for DVCC calculator"""

    cell_count: int = 16
    max_charge_current: float = 100.0
    max_discharge_current: float = 120.0
    cell_max_voltage: float = 3.65
    cell_balance_start: float = 3.45
    cell_balance_full: float = 3.55
    ccl_change_rate: float = 10.0
    dcl_change_rate: float = 15.0

    # Cell voltage thresholds
    cell_full_current: float = 3.40
    cell_start_limit: float = 3.45
    cell_balance_voltage: float = 3.50
    cell_near_full: float = 3.55
    cell_cutoff: float = 3.60

    # Imbalance thresholds
    imbalance_start: float = 0.05
    imbalance_aggressive: float = 0.10
    imbalance_critical: float = 0.20

    # Temperature thresholds (°C)
    temp_charge_min: float = 0.0
    temp_charge_reduced: float = 5.0
    temp_charge_optimal: float = 10.0
    temp_charge_limit: float = 45.0
    temp_charge_stop: float = 50.0
    temp_discharge_min: float = -20.0
    temp_discharge_reduced: float = -10.0

    # SoC thresholds
    soc_reduce_start: float = 95.0
    soc_reduce_factor: float = 0.5
    soc_discharge_stop: float = 5.0
    soc_discharge_reduced: float = 10.0

    min_charge_current: float = 2.0


@dataclass
class DvccLimits:
    """Result of DVCC calculation"""

    ccl: float
    dcl: float
    cvl: float
    ccl_reason: str
    dcl_reason: str
    max_cell_voltage: float | None
    max_cell_id: int | None
    min_cell_voltage: float | None
    min_cell_id: int | None
    cell_delta: float | None
    min_temp: float | None
    max_temp: float | None
    soc: float | None


class DvccCalculator:
    """
    DVCC Calculator for dynamic battery current limits.

    Calculates CCL (Charge Current Limit), DCL (Discharge Current Limit),
    and CVL (Charge Voltage Limit) based on:
    - Highest cell voltage (charge protection / balancing)
    - Lowest cell voltage (discharge protection)
    - Cell voltage imbalance (delta between min/max)
    - Temperature limits (LiFePO4 safe range)
    - SoC (optional, for battery longevity)

    The goal is to protect cells BEFORE BMS triggers emergency cutoff,
    allowing balancers time to work and preventing system shutdowns.
    """

    def __init__(self, config: DvccConfig | None = None):
        """
        Initialize DVCC calculator.

        Args:
            config: DvccConfig object with all parameters
        """
        self.config = config or DvccConfig()
        self.cell_count = self.config.cell_count
        self._max_charge_current = self.config.max_charge_current
        self._max_discharge_current = self.config.max_discharge_current

        # Rate limiting state
        self._last_ccl = self._max_charge_current
        self._last_dcl = self._max_discharge_current
        # None until first calculate() call to avoid stale dt from construction
        self._last_update_time: float | None = None

        logger.info(
            "DVCC initialized: %d cells, max_charge=%.1fA, max_discharge=%.1fA",
            self.cell_count,
            self._max_charge_current,
            self._max_discharge_current,
        )

    def set_battery_limits(self, max_charge: float, max_discharge: float):
        """Update battery current limits (e.g., from config changes)"""
        self._max_charge_current = max_charge
        self._max_discharge_current = max_discharge

    def calculate_ccl_from_cell_voltage(self, max_cell: float | None) -> tuple[float, str]:
        """Calculate CCL based on highest cell voltage (balancing protection)"""
        if max_cell is None:
            return self._max_charge_current, "no_cell_data"

        v = max_cell
        max_cc = self._max_charge_current
        min_cc = self.config.min_charge_current
        c: DvccConfig = self.config

        # Cell cutoff - stop charging
        if v >= c.cell_cutoff:
            ccl, reason = 0.0, f"cell_overvoltage_{v:.3f}V"
        # Near full - minimal current for balancing
        elif v >= c.cell_near_full:
            factor = 1.0 - (v - c.cell_near_full) / (c.cell_cutoff - c.cell_near_full)
            ccl, reason = max(0.0, min_cc * factor), f"tail_charge_{v:.3f}V"
        # Balance voltage - aggressive reduction
        elif v >= c.cell_balance_voltage:
            factor = 1.0 - (v - c.cell_balance_voltage) / (
                c.cell_near_full - c.cell_balance_voltage
            )
            ccl, reason = min_cc + (max_cc * 0.20 - min_cc) * factor, f"balancing_{v:.3f}V"
        # Start limiting - gradual reduction
        elif v >= c.cell_start_limit:
            factor = 1.0 - (v - c.cell_start_limit) / (c.cell_balance_voltage - c.cell_start_limit)
            ccl, reason = max_cc * (0.20 + 0.80 * factor), f"reducing_{v:.3f}V"
        # Below start limit - full current (but above full_current threshold)
        elif v >= c.cell_full_current:
            ccl, reason = max_cc, "normal"
        # Well below balance start - full current
        else:
            ccl, reason = max_cc, "normal"

        return ccl, reason

    def calculate_ccl_from_imbalance(self, cell_delta: float | None) -> tuple[float, str]:
        """Calculate CCL based on cell voltage imbalance (delta min-max)"""
        if cell_delta is None or cell_delta < 0:
            return self._max_charge_current, "no_delta"

        max_cc = self._max_charge_current
        min_cc = self.config.min_charge_current
        c: DvccConfig = self.config

        if cell_delta <= c.imbalance_start:
            ccl, reason = max_cc, "balanced"
        elif cell_delta >= c.imbalance_critical:
            ccl, reason = min_cc, f"critical_imbalance_{cell_delta:.3f}V"
        elif cell_delta >= c.imbalance_aggressive:
            factor = 1.0 - (cell_delta - c.imbalance_aggressive) / (
                c.imbalance_critical - c.imbalance_aggressive
            )
            ccl, reason = min_cc + (max_cc * 0.30 - min_cc) * factor, f"imbalance_{cell_delta:.3f}V"
        else:
            # Start limiting zone
            factor = 1.0 - (cell_delta - c.imbalance_start) / (
                c.imbalance_aggressive - c.imbalance_start
            )
            ccl, reason = max_cc * (0.30 + 0.70 * factor), f"slight_imbalance_{cell_delta:.3f}V"

        return ccl, reason

    def calculate_ccl_from_temperature(
        self, min_temp: float | None, max_temp: float | None
    ) -> tuple[float, str]:
        """Calculate CCL based on temperature limits (LiFePO4 safe range)"""
        # Use max temp for charging (hottest cell limits)
        temp = max_temp if max_temp is not None else min_temp
        if temp is None:
            return self._max_charge_current, "no_temp_data"

        c: DvccConfig = self.config

        if temp <= c.temp_charge_min:
            ccl, reason = 0.0, f"too_cold_{temp:.1f}C"
        elif temp >= c.temp_charge_stop:
            ccl, reason = 0.0, f"too_hot_{temp:.1f}C"
        # Cold but chargeable - reduce current
        elif temp < c.temp_charge_reduced:
            factor = (temp - c.temp_charge_min) / (c.temp_charge_reduced - c.temp_charge_min)
            factor = max(0.0, min(1.0, factor))
            ccl, reason = (
                max(self._max_charge_current * factor * 0.5, c.min_charge_current),
                f"cold_{temp:.1f}C",
            )
        # Cool - reduce to ~50%
        elif temp < c.temp_charge_optimal:
            factor = (temp - c.temp_charge_reduced) / (
                c.temp_charge_optimal - c.temp_charge_reduced
            )
            factor = max(0.0, min(1.0, factor))
            ccl, reason = (
                max(self._max_charge_current * (0.1 + 0.4 * factor), c.min_charge_current),
                f"cool_{temp:.1f}C",
            )
        # Hot - reduce linearly
        elif temp > c.temp_charge_limit:
            factor = 1.0 - (temp - c.temp_charge_limit) / (c.temp_charge_stop - c.temp_charge_limit)
            factor = max(0.0, min(1.0, factor))
            ccl, reason = self._max_charge_current * max(0.2, factor), f"hot_{temp:.1f}C"
        else:
            ccl, reason = self._max_charge_current, f"temp_ok_{temp:.1f}C"

        return ccl, reason

    def calculate_ccl_from_soc(self, soc: float | None) -> tuple[float, str]:
        """Calculate CCL based on State of Charge (battery longevity)"""
        if soc is None or soc < self.config.soc_reduce_start:
            return self._max_charge_current, "soc_ok"

        c: DvccConfig = self.config

        if soc >= 100.0:
            return self._max_charge_current * c.soc_reduce_factor, "soc_100"

        # Linear reduction from 100% to REDUCE_FACTOR
        factor = 1.0 - (soc - c.soc_reduce_start) / (100.0 - c.soc_reduce_start) * (
            1.0 - c.soc_reduce_factor
        )
        return self._max_charge_current * factor, f"soc_{soc:.0f}"

    def calculate_dcl_from_cell_voltage(self, min_cell: float | None) -> tuple[float, str]:
        """Calculate DCL based on lowest cell voltage (discharge protection)"""
        if min_cell is None:
            return self._max_discharge_current, "no_cell_data"

        v = min_cell
        max_dc = self._max_discharge_current

        if v >= 3.0:
            return max_dc, "normal"

        if v <= 2.7:
            return 0.0, f"cell_undervoltage_{v:.3f}V"

        if v <= 2.9:
            factor = (v - 2.7) / (2.9 - 2.7)
            return max(max_dc * factor * 0.5, 0.0), f"low_cell_{v:.3f}V"

        # v is between 2.9 and 3.0 here
        factor = (v - 2.9) / (3.0 - 2.9)
        return max(max_dc * (0.5 + 0.5 * factor), 0.0), f"reducing_{v:.3f}V"

    def calculate_dcl_from_temperature(
        self, min_temp: float | None, max_temp: float | None
    ) -> tuple[float, str]:
        """Calculate DCL based on temperature (cold limits discharge)"""
        temp = min_temp if min_temp is not None else max_temp
        if temp is None:
            return self._max_discharge_current, "no_temp_data"

        c: DvccConfig = self.config

        if temp <= c.temp_discharge_min:
            return 0.0, f"too_cold_discharge_{temp:.1f}C"

        if temp < c.temp_discharge_reduced:
            factor = (temp - c.temp_discharge_min) / (
                c.temp_discharge_reduced - c.temp_discharge_min
            )
            factor = max(0.0, min(1.0, factor))
            return max(
                self._max_discharge_current * 0.3 * factor, 0.0
            ), f"cold_discharge_{temp:.1f}C"

        return self._max_discharge_current, f"temp_discharge_ok_{temp:.1f}C"

    def calculate_dcl_from_soc(self, soc: float | None) -> tuple[float, str]:
        """Calculate DCL based on SoC (protect from deep discharge)"""
        if soc is None or soc > self.config.soc_discharge_reduced:
            return self._max_discharge_current, "soc_discharge_ok"

        if soc <= self.config.soc_discharge_stop:
            return 0.0, f"soc_{soc:.0f}_deep_discharge"

        factor = (soc - self.config.soc_discharge_stop) / (
            self.config.soc_discharge_reduced - self.config.soc_discharge_stop
        )
        factor = max(0.0, min(1.0, factor))
        return max(self._max_discharge_current * factor * 0.5, 0.0), f"soc_{soc:.0f}_low"

    def _compute_ccl(
        self,
        max_cell: float | None,
        cell_delta: float | None,
        min_temp: float | None,
        max_temp: float | None,
        soc: float | None,
    ) -> tuple[float, str]:
        """Calculate CCL from all sources - take minimum (most restrictive)."""
        ccl_values = [
            self.calculate_ccl_from_cell_voltage(max_cell),
            self.calculate_ccl_from_imbalance(cell_delta),
            self.calculate_ccl_from_temperature(min_temp, max_temp),
            self.calculate_ccl_from_soc(soc),
        ]
        return min(ccl_values, key=lambda x: x[0])

    def _compute_dcl(
        self,
        min_cell: float | None,
        min_temp: float | None,
        max_temp: float | None,
        soc: float | None,
    ) -> tuple[float, str]:
        """Calculate DCL from all sources - take minimum (most restrictive)."""
        dcl_values = [
            self.calculate_dcl_from_cell_voltage(min_cell),
            self.calculate_dcl_from_temperature(min_temp, max_temp),
            self.calculate_dcl_from_soc(soc),
        ]
        return min(dcl_values, key=lambda x: x[0])

    def _rate_limit(
        self, ccl: float, dcl: float, allow_charge: bool, allow_discharge: bool
    ) -> tuple[float, float]:
        """Apply rate limiting for smooth transitions (skip for hard safety cutoffs)."""
        now = time.time()

        if self._last_update_time is None:
            # First call - initialize timestamp, no rate limiting
            self._last_update_time = now
            self._last_ccl = ccl
            self._last_dcl = dcl
            return ccl, dcl

        dt = now - self._last_update_time
        self._last_update_time = now

        max_ccl_change = self.config.ccl_change_rate * dt
        max_dcl_change = self.config.dcl_change_rate * dt

        if not (ccl <= 0.0 or not allow_charge):
            ccl = self._rate_limit_value(ccl, self._last_ccl, max_ccl_change)
        # else: hard safety cutoff - ccl is already 0.0, apply immediately

        if not (dcl <= 0.0 or not allow_discharge):
            dcl = self._rate_limit_value(dcl, self._last_dcl, max_dcl_change)
        # else: hard safety cutoff - dcl is already 0.0, apply immediately

        self._last_ccl = ccl
        self._last_dcl = dcl
        return ccl, dcl

    @staticmethod
    def _rate_limit_value(value: float, last_value: float, max_change: float) -> float:
        if value > last_value:
            return min(value, last_value + max_change)
        if value < last_value:
            # Allow faster reduction for safety
            return max(value, last_value - max_change * 2)
        return value

    def calculate(self, data: dict[str, Any]) -> DvccLimits:
        """
        Calculate all DVCC parameters from battery data.

        Args:
            data: Dict with keys:
                - max_cell: Highest cell voltage (V)
                - max_cell_id: Cell ID of highest voltage
                - min_cell: Lowest cell voltage (V)
                - min_cell_id: Cell ID of lowest voltage
                - max_temp: Highest cell temperature (°C)
                - min_temp: Lowest cell temperature (°C)
                - soc: State of charge (%)
                - allow_charge: True if BMS allows charging
                - allow_discharge: True if BMS allows discharging

        Returns:
            DvccLimits with ccl, dcl, cvl, and diagnostic reasons
        """
        max_cell = data.get("max_cell")
        min_cell = data.get("min_cell")
        max_cell_id = data.get("max_cell_id")
        min_cell_id = data.get("min_cell_id")
        max_temp = data.get("max_temp")
        min_temp = data.get("min_temp")
        soc = data.get("soc")
        allow_charge = data.get("allow_charge", True)
        allow_discharge = data.get("allow_discharge", True)

        # Calculate cell delta (imbalance)
        cell_delta = None
        if max_cell is not None and min_cell is not None:
            cell_delta = max_cell - min_cell

        ccl, ccl_reason = self._compute_ccl(max_cell, cell_delta, min_temp, max_temp, soc)
        dcl, dcl_reason = self._compute_dcl(min_cell, min_temp, max_temp, soc)

        # Apply BMS block signals AFTER all calculations
        if not allow_charge:
            ccl = 0.0
            ccl_reason = "bms_blocked"

        if not allow_discharge:
            dcl = 0.0
            dcl_reason = "bms_blocked"

        ccl, dcl = self._rate_limit(ccl, dcl, allow_charge, allow_discharge)

        # Calculate CVL (Charge Voltage Limit)
        cvl = self.config.cell_max_voltage * self.cell_count

        logger.debug(
            "DVCC: CCL=%.1fA (%s), DCL=%.1fA (%s), CVL=%.2fV", ccl, ccl_reason, dcl, dcl_reason, cvl
        )

        return DvccLimits(
            ccl=round(ccl, 1),
            dcl=round(dcl, 1),
            ccl_reason=ccl_reason,
            dcl_reason=dcl_reason,
            cvl=round(cvl, 2),
            max_cell_voltage=max_cell,
            max_cell_id=max_cell_id,
            min_cell_voltage=min_cell,
            min_cell_id=min_cell_id,
            cell_delta=cell_delta,
            min_temp=min_temp,
            max_temp=max_temp,
            soc=soc,
        )


# Convenience function for testing
def create_dvcc_from_config(config: dict[str, Any]) -> DvccCalculator:
    """Create DVCC calculator from config dict"""
    return DvccCalculator(
        DvccConfig(
            cell_count=config.get("DVCC_CELL_COUNT", 16),
            max_charge_current=config.get("DVCC_MAX_CHARGE_CURRENT", 100.0),
            max_discharge_current=config.get("DVCC_MAX_DISCHARGE_CURRENT", 120.0),
            cell_max_voltage=config.get("DVCC_CELL_MAX_VOLTAGE", 3.65),
            cell_balance_start=config.get("DVCC_CELL_START_LIMIT", 3.45),
            cell_balance_full=config.get("DVCC_CELL_BALANCE_VOLTAGE", 3.55),
            ccl_change_rate=config.get("DVCC_CCL_CHANGE_RATE", 10.0),
            dcl_change_rate=config.get("DVCC_DCL_CHANGE_RATE", 15.0),
            # Cell voltage thresholds
            cell_full_current=config.get("DVCC_CELL_FULL_CURRENT", 3.40),
            cell_start_limit=config.get("DVCC_CELL_START_LIMIT", 3.45),
            cell_balance_voltage=config.get("DVCC_CELL_BALANCE_VOLTAGE", 3.50),
            cell_near_full=config.get("DVCC_CELL_NEAR_FULL", 3.55),
            cell_cutoff=config.get("DVCC_CELL_CUTOFF", 3.60),
            # Imbalance thresholds
            imbalance_start=config.get("DVCC_IMBALANCE_START_LIMIT", 0.05),
            imbalance_aggressive=config.get("DVCC_IMBALANCE_AGGRESSIVE", 0.10),
            imbalance_critical=config.get("DVCC_IMBALANCE_CRITICAL", 0.20),
            # Temperature thresholds (°C) - LiFePO4 safe range
            temp_charge_min=config.get("DVCC_TEMP_STOP_CHARGE", 0.0),
            temp_charge_reduced=config.get("DVCC_TEMP_REDUCED", 5.0),
            temp_charge_optimal=config.get("DVCC_TEMP_FULL_CURRENT_MIN", 10.0),
            temp_charge_limit=config.get("DVCC_TEMP_FULL_CURRENT_MAX", 40.0),
            temp_charge_stop=config.get("DVCC_TEMP_STOP_CHARGE_HIGH", 50.0),
            temp_discharge_min=config.get("DVCC_TEMP_DISCHARGE_MIN", -20.0),
            temp_discharge_reduced=config.get("DVCC_TEMP_DISCHARGE_REDUCED", -10.0),
            # SoC thresholds
            soc_reduce_start=config.get("DVCC_SOC_REDUCE_START", 95.0),
            soc_reduce_factor=config.get("DVCC_SOC_REDUCE_FACTOR", 0.5),
            soc_discharge_stop=config.get("DVCC_SOC_DISCHARGE_STOP", 5.0),
            soc_discharge_reduced=config.get("DVCC_SOC_DISCHARGE_REDUCED", 15.0),
            min_charge_current=config.get("DVCC_MIN_CHARGE_CURRENT", 2.0),
        )
    )

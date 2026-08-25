"""
Inverter Control Logic
Separated from I/O for stability and testability.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("inverter-control")


@dataclass
class SystemState:
    """Snapshot of the system state for setpoint calculation"""

    # Grid data
    g1: int
    g2: int
    gt: int

    # Consumption data
    t1: int
    t2: int
    tt: int

    # Inverter data
    inv_power: int

    # Solar data
    mppt_total: float
    tasmota_total: float
    pv_total: float

    # External data
    ev_power: float
    garage_power: float

    # Control switches
    only_charging: bool
    no_feed: bool
    house_support: bool
    charge_battery: bool
    do_not_supply_charger: bool
    limit_to_ev: bool

    # Persistence (required fields before optional ones)
    previous_setpoint: int

    # Home grid smoothing (optional, when ENABLE_GRID_SMOOTHING_WITH_HOME)
    home_total: float = 0.0  # Total house consumption from Vue
    derived_gt: float | None = None  # Grid derived from home_total - production
    filtered_gt: float | None = None


@dataclass
class ControlResult:
    """Result of the setpoint calculation"""

    setpoint: int
    flags: str
    filtered_gt: float


# Strategy functions - each takes (state, current_vanew) -> (new_vanew, flags)
# Ordered by priority (first = lowest priority / base strategy)

StrategyFn = Callable[[SystemState, int], tuple[int, str]]


def normal_strategy(
    state: SystemState,
    current_vanew: int,
    *,
    damping_factor: float,
    deadband_low: int,
    deadband_high: int,
    creep_rate: float,
    creep_max: float,
    export_damping: float,
    _state: dict,
) -> tuple[int, str]:
    """Base strategy: Target grid zero with creep correction in deadband.
    Asymmetric response: export (gt < 0) corrected more aggressively than import.
    """
    effective_gt = state.gt
    flags = ""

    # Tests can override deadband via _state dict
    _deadband_low = _state.get("deadband_low", deadband_low)
    _deadband_high = _state.get("deadband_high", deadband_high)
    _creep_rate = _state.get("creep_rate", creep_rate)
    _creep_max = _state.get("creep_max", creep_max)

    # Adjust Grid for EV Exclusion Mode
    if state.do_not_supply_charger and state.ev_power > 100:
        effective_gt = state.gt - state.ev_power
        flags += f"[EV:{int(state.ev_power)}] "

    # Target Grid Zero
    smoothed_gt = state.filtered_gt if state.filtered_gt is not None else float(effective_gt)

    if _deadband_low < smoothed_gt < _deadband_high:
        _state["stable_count"] = _state.get("stable_count", 0) + 1
        # Creep: accumulate error to push toward zero
        # Faster creep for export (we never want to export)
        if smoothed_gt > 0:
            _state["creep_accumulator"] = min(
                _creep_max, _state.get("creep_accumulator", 0.0) + _creep_rate
            )
        else:
            # Export creep: 2x faster accumulation
            _state["creep_accumulator"] = max(
                -_creep_max, _state.get("creep_accumulator", 0.0) - _creep_rate * 2
            )
        # Use stronger damping for export direction
        damping = export_damping if smoothed_gt < 0 else damping_factor
        creep_correction = int(_state["creep_accumulator"] * damping)
        vanew = state.previous_setpoint - creep_correction
        flags += f"[~{int(_state['creep_accumulator'])}] "
    else:
        _state["stable_count"] = 0
        _state["creep_accumulator"] = 0.0
        # Asymmetric damping: export corrected more aggressively
        damping = export_damping if smoothed_gt < 0 else damping_factor
        correction = -smoothed_gt * damping
        vanew = state.inv_power + correction

    return int(vanew), flags


def only_charging_strategy(
    state: SystemState,
    current_vanew: int,
    *,
    efficiency: float,
    solar_offset: int,
) -> tuple[int, str]:
    """Don't discharge battery - output only what MPPT produces"""
    if not state.only_charging:
        return current_vanew, ""

    max_ac_output = int(state.mppt_total * efficiency) - solar_offset
    min_setpoint = -max(0, max_ac_output)

    if current_vanew < min_setpoint:
        return min_setpoint, f"[OC:{max_ac_output}] "
    return current_vanew, "[OC~] "


def do_not_supply_charger_strategy(
    state: SystemState,
    current_vanew: int,
    *,
    efficiency: float,
    solar_offset: int,
) -> tuple[int, str]:
    """Don't let battery power the EV charger"""
    if state.do_not_supply_charger and state.ev_power > 100:
        max_ac_output = max(0, int(state.mppt_total * efficiency) - solar_offset)
        min_setpoint = -max_ac_output
        if current_vanew < min_setpoint:
            return min_setpoint, f"[NoEV:{max_ac_output}] "
    return current_vanew, ""


def limit_to_ev_strategy(
    state: SystemState,
    current_vanew: int,
    *,
    efficiency: float,
    reserve: int = 500,
) -> tuple[int, str]:
    """Export most solar to grid when EV is charging, keep reserve for battery"""
    if not state.limit_to_ev:
        return current_vanew, ""

    ev_charging_detected = state.garage_power > 1000 or state.ev_power > 1000
    if ev_charging_detected:
        ac_output = int(state.mppt_total * efficiency)
        export_power = max(0, ac_output - reserve)
        return -export_power, f"[LimEV:{ac_output}-{reserve}] "

    return current_vanew, ""


def no_feed_strategy(
    state: SystemState,
    current_vanew: int,
) -> tuple[int, str]:
    """Match Tasmota microinverter output exactly"""
    if state.no_feed:
        return int(state.tasmota_total), "[NF] "
    return current_vanew, ""


def house_support_strategy(
    state: SystemState,
    current_vanew: int,
) -> tuple[int, str]:
    """Tasmota solar minus offset for house loads"""
    if state.house_support:
        return int(state.tasmota_total - 300), "[HS] "
    return current_vanew, ""


def charge_battery_strategy(
    state: SystemState,
    current_vanew: int,
) -> tuple[int, str]:
    """Force battery charging at maximum rate"""
    if state.charge_battery:
        return 2200, "[CHG] "
    return current_vanew, ""


# Strategy list in priority order (first = lowest priority)
STRATEGIES: list[StrategyFn] = [
    normal_strategy,
    only_charging_strategy,
    do_not_supply_charger_strategy,
    limit_to_ev_strategy,
    no_feed_strategy,
    house_support_strategy,
    charge_battery_strategy,
]


class SetpointCalculator:
    """Orchestrates strategies and applies safety limits"""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.ema_alpha = config.get("EMA_ALPHA", 0.3)
        self.power_limit_min = config.get("POWER_LIMIT_MIN", -2300)
        self.power_limit_max = config.get("POWER_LIMIT_MAX", 2250)
        self.delta_limit = config.get("SETPOINT_DELTA_LIMIT", 2000)
        self.burst_threshold = config.get("BURST_THRESHOLD", 150)
        self.burst_gain = config.get("BURST_GAIN", 0.8)

        # D-term: prevent overshoot when gt is converging to zero fast
        self.d_brake_zone = config.get("D_BRAKE_ZONE", 100)
        self.d_threshold = config.get("D_THRESHOLD", 50)
        self.d_gain = config.get("D_GAIN", 0.3)
        self.prev_effective_gt: float | None = None

        # Normal strategy state (creep accumulator, etc.)
        self._normal_state: dict = {}
        # EMA-smoothed derived_gt (persists across calculate() calls)
        self._filtered_derived_gt: float | None = None

    # Backwards compatibility for tests - expose normal strategy state
    @property
    def strategies(self):
        class MockNormalStrategy:
            def __init__(self, state):
                self._state = state

            @property
            def creep_accumulator(self):
                return self._state.get("creep_accumulator", 0.0)

            @creep_accumulator.setter
            def creep_accumulator(self, value):
                self._state["creep_accumulator"] = value

            @property
            def stable_count(self):
                return self._state.get("stable_count", 0)

            @stable_count.setter
            def stable_count(self, value):
                self._state["stable_count"] = value

            @property
            def deadband_low(self):
                return self._state.get("deadband_low", -50)

            @deadband_low.setter
            def deadband_low(self, value):
                self._state["deadband_low"] = value

            @property
            def deadband_high(self):
                return self._state.get("deadband_high", 30)

            @deadband_high.setter
            def deadband_high(self, value):
                self._state["deadband_high"] = value

            @property
            def creep_rate(self):
                return self._state.get("creep_rate", 0.5)

            @property
            def creep_max(self):
                return self._state.get("creep_max", 100.0)

        return [MockNormalStrategy(self._normal_state)]

    def _apply_burst_correction(
        self, raw_vanew: int, effective_gt: float, old_filtered_gt: float | None
    ) -> tuple[int, str, bool]:
        """Apply burst correction for sudden load spikes. Returns (vanew, flags, fired)."""
        if old_filtered_gt is None:
            return raw_vanew, "", False
        spike = effective_gt - old_filtered_gt
        if abs(spike) <= self.burst_threshold:
            return raw_vanew, "", False
        correction = int(-spike * self.burst_gain)
        return raw_vanew + correction, f"[B:{correction:+d}] ", True

    def _apply_d_term(self, raw_vanew: int, effective_gt: float) -> tuple[int, str]:
        """Apply D-term braking to prevent overshoot. Returns (vanew, flags)."""
        if self.prev_effective_gt is None:
            return raw_vanew, ""
        d_gt = effective_gt - self.prev_effective_gt
        if abs(effective_gt) >= self.d_brake_zone or abs(d_gt) <= self.d_threshold:
            return raw_vanew, ""
        brake = -int(d_gt * self.d_gain)
        return raw_vanew + brake, f"[D:{brake:+d}] "

    def _run_strategies(self, state: SystemState) -> tuple[int, str]:
        """Run all strategies with their config kwargs. Returns (raw_vanew, flags)."""
        raw_vanew = state.previous_setpoint
        total_flags = ""
        for strategy in STRATEGIES:
            # Pass config params via kwargs for each strategy
            if strategy is normal_strategy:
                raw_vanew, flags = strategy(
                    state,
                    raw_vanew,
                    damping_factor=self.config.get("DAMPING_FACTOR", 0.7),
                    deadband_low=self.config.get("GRID_ZERO_DEADBAND_LOW", -50),
                    deadband_high=self.config.get("GRID_ZERO_DEADBAND_HIGH", 30),
                    creep_rate=self.config.get("CREEP_RATE", 0.5),
                    creep_max=self.config.get("CREEP_MAX", 100.0),
                    export_damping=self.config.get("EXPORT_DAMPING", 1.0),
                    _state=self._normal_state,
                )
            elif strategy is only_charging_strategy or strategy is do_not_supply_charger_strategy:
                raw_vanew, flags = strategy(
                    state,
                    raw_vanew,
                    efficiency=self.config.get("INVERTER_EFFICIENCY", 0.94),
                    solar_offset=self.config.get("SOLAR_OUTPUT_OFFSET", 60),
                )
            elif strategy is limit_to_ev_strategy:
                raw_vanew, flags = strategy(
                    state,
                    raw_vanew,
                    efficiency=self.config.get("INVERTER_EFFICIENCY", 0.94),
                )
            else:
                raw_vanew, flags = strategy(state, raw_vanew)
            total_flags += flags
        return raw_vanew, total_flags

    def calculate(self, state: SystemState) -> ControlResult:
        """Execute the control logic pipeline"""

        # Update EMA filter
        effective_gt = state.gt
        if state.do_not_supply_charger and state.ev_power > 100:
            effective_gt = state.gt - state.ev_power

        # Grid smoothing with Home total (derived_gt = home_total - pv_total)
        # Blend instantaneous CT with derived grid for stability.
        if state.derived_gt is not None:
            smoothing_weight = self.config.get("GRID_SMOOTHING_HOME_WEIGHT", 0.7)
            if self.config.get("GRID_SMOOTHING_DERIVED_TAU", 3.2) > 0:
                # Pre-smoothed by the background GridFilter thread (time-based
                # tau, same notion of "smoothed grid" as the CT filter); use
                # the value directly - no per-cycle EMA here.
                effective_gt = (
                    smoothing_weight * float(state.derived_gt)
                    + (1 - smoothing_weight) * effective_gt
                )
            else:
                # Legacy path (GRID_SMOOTHING_DERIVED_TAU=0): per-cycle EMA on
                # the raw derived value using GRID_SMOOTHING_DERIVED_ALPHA.
                derived_alpha = self.config.get("GRID_SMOOTHING_DERIVED_ALPHA", 0.1)
                raw_derived = float(state.derived_gt)
                if self._filtered_derived_gt is None:
                    self._filtered_derived_gt = raw_derived
                else:
                    self._filtered_derived_gt = (
                        derived_alpha * raw_derived
                        + (1 - derived_alpha) * self._filtered_derived_gt
                    )
                effective_gt = (
                    smoothing_weight * self._filtered_derived_gt
                    + (1 - smoothing_weight) * effective_gt
                )

        old_filtered_gt = state.filtered_gt
        new_filtered_gt = (
            float(effective_gt)
            if old_filtered_gt is None
            else (self.ema_alpha * effective_gt + (1 - self.ema_alpha) * old_filtered_gt)
        )
        state.filtered_gt = new_filtered_gt

        # Run strategies
        raw_vanew, total_flags = self._run_strategies(state)

        # Apply corrections
        raw_vanew, burst_flags, burst_fired = self._apply_burst_correction(
            raw_vanew, effective_gt, old_filtered_gt
        )
        raw_vanew, d_flags = self._apply_d_term(raw_vanew, effective_gt)
        self.prev_effective_gt = effective_gt

        # Rate limit: apply 9/10 of the change (fast convergence)
        diff = raw_vanew - state.previous_setpoint
        convergence = 1.0 if burst_fired else 0.9
        vanew = state.previous_setpoint + int(diff * convergence)

        # Apply safety limits and software fuse
        vanew = max(self.power_limit_min, min(self.power_limit_max, vanew))
        delta = vanew - state.previous_setpoint
        if abs(delta) > self.delta_limit:
            limited_delta = self.delta_limit if delta > 0 else -self.delta_limit
            vanew = state.previous_setpoint + limited_delta
            total_flags += f"[!Δ{int(abs(delta))}] "

        return ControlResult(
            setpoint=int(vanew),
            flags=burst_flags + d_flags + total_flags.strip(),
            filtered_gt=new_filtered_gt,
        )

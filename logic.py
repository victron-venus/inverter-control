"""
Inverter Control Logic
Separated from I/O for stability and testability.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger('inverter-control')

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
    
    # Persistence
    previous_setpoint: int
    filtered_gt: Optional[float] = None

@dataclass
class ControlResult:
    """Result of the setpoint calculation"""
    setpoint: int
    flags: str
    filtered_gt: float

class BaseStrategy(ABC):
    """Abstract base class for control strategies"""
    
    @abstractmethod
    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        """
        Calculate setpoint and return (new_setpoint, flags)
        current_vanew: the setpoint calculated by previous (lower priority) strategies
        """
        pass

class NormalStrategy(BaseStrategy):
    """Base strategy: Target grid zero"""
    
    def __init__(self, damping_factor: float, deadband_low: int, deadband_high: int):
        self.damping_factor = damping_factor
        self.deadband_low = deadband_low
        self.deadband_high = deadband_high

    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        effective_gt = state.gt
        flags = ""
        
        # Step 4: Adjust Grid for EV Exclusion Mode
        if state.do_not_supply_charger and state.ev_power > 100:
            effective_gt = state.gt - state.ev_power
            flags += f"[EV:{int(state.ev_power)}] "
            
        # Step 5: Base Calculation - Target Grid Zero
        # Use filtered_gt passed from the calculator
        smoothed_gt = state.filtered_gt if state.filtered_gt is not None else effective_gt
        
        if self.deadband_low < smoothed_gt < self.deadband_high:
            vanew = state.previous_setpoint
            flags += "[~] "
        else:
            correction = -smoothed_gt * self.damping_factor
            vanew = state.inv_power + correction
            
        return int(vanew), flags

class OnlyChargingStrategy(BaseStrategy):
    """Don't discharge battery - output only what MPPT produces"""
    
    def __init__(self, efficiency: float, solar_offset: int):
        self.efficiency = efficiency
        self.solar_offset = solar_offset

    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        if not state.only_charging:
            return current_vanew, ""
            
        max_ac_output = int(state.mppt_total * self.efficiency) - self.solar_offset
        min_setpoint = -max(0, max_ac_output)
        
        if current_vanew < min_setpoint:
            return min_setpoint, f"[OC:{max_ac_output}] "
        return current_vanew, "[OC~] "

class DoNotSupplyChargerStrategy(BaseStrategy):
    """Don't let battery power the EV charger"""
    
    def __init__(self, efficiency: float, solar_offset: int):
        self.efficiency = efficiency
        self.solar_offset = solar_offset

    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        if state.do_not_supply_charger and state.ev_power > 100:
            max_ac_output = max(0, int(state.mppt_total * self.efficiency) - self.solar_offset)
            min_setpoint = -max_ac_output
            if current_vanew < min_setpoint:
                return min_setpoint, f"[NoEV:{max_ac_output}] "
        return current_vanew, ""

class LimitToEvStrategy(BaseStrategy):
    """Export most solar to grid when EV is charging, keep reserve for battery"""
    
    def __init__(self, efficiency: float, reserve: int = 500):
        self.efficiency = efficiency
        self.reserve = reserve

    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        if not state.limit_to_ev:
            return current_vanew, ""
            
        ev_charging_detected = state.garage_power > 1000 or state.ev_power > 1000
        if ev_charging_detected:
            ac_output = int(state.mppt_total * self.efficiency)
            export_power = max(0, ac_output - self.reserve)
            return -export_power, f"[LimEV:{ac_output}-{self.reserve}] "
            
        return current_vanew, ""

class NoFeedStrategy(BaseStrategy):
    """Match Tasmota microinverter output exactly"""
    
    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        if state.no_feed:
            return int(state.tasmota_total), "[NF] "
        return current_vanew, ""

class HouseSupportStrategy(BaseStrategy):
    """Tasmota solar minus offset for house loads"""
    
    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        if state.house_support:
            return int(state.tasmota_total - 300), "[HS] "
        return current_vanew, ""

class ChargeBatteryStrategy(BaseStrategy):
    """Force battery charging at maximum rate"""
    
    def calculate(self, state: SystemState, current_vanew: int) -> Tuple[int, str]:
        if state.charge_battery:
            return 2200, "[CHG] "
        return current_vanew, ""

class SetpointCalculator:
    """Orchestrates strategies and applies safety limits"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.ema_alpha = config.get('EMA_ALPHA', 0.3)
        self.power_limit_min = config.get('POWER_LIMIT_MIN', -2300)
        self.power_limit_max = config.get('POWER_LIMIT_MAX', 2250)
        self.delta_limit = config.get('SETPOINT_DELTA_LIMIT', 2000)
        
        # Strategies in priority order (as in main.py)
        self.strategies = [
            NormalStrategy(
                config.get('DAMPING_FACTOR', 0.7),
                config.get('GRID_ZERO_DEADBAND_LOW', -50),
                config.get('GRID_ZERO_DEADBAND_HIGH', 80)
            ),
            OnlyChargingStrategy(
                config.get('INVERTER_EFFICIENCY', 0.94),
                config.get('SOLAR_OUTPUT_OFFSET', 60)
            ),
            DoNotSupplyChargerStrategy(
                config.get('INVERTER_EFFICIENCY', 0.94),
                config.get('SOLAR_OUTPUT_OFFSET', 60)
            ),
            LimitToEvStrategy(config.get('INVERTER_EFFICIENCY', 0.94)),
            NoFeedStrategy(),
            HouseSupportStrategy(),
            ChargeBatteryStrategy()
        ]

    def calculate(self, state: SystemState) -> ControlResult:
        """Execute the control logic pipeline"""
        
        # Update EMA filter
        effective_gt = state.gt
        if state.do_not_supply_charger and state.ev_power > 100:
            effective_gt = state.gt - state.ev_power
            
        if state.filtered_gt is None:
            new_filtered_gt = float(effective_gt)
        else:
            new_filtered_gt = (self.ema_alpha * effective_gt) + ((1 - self.ema_alpha) * state.filtered_gt)
            
        state.filtered_gt = new_filtered_gt
        
        vanew = state.previous_setpoint
        total_flags = ""
        
        # Run strategies
        for strategy in self.strategies:
            vanew, flags = strategy.calculate(state, vanew)
            total_flags += flags
            
        # Apply safety limits
        vanew = max(self.power_limit_min, min(self.power_limit_max, vanew))
        
        # Apply software fuse (delta limit)
        delta = vanew - state.previous_setpoint
        if abs(delta) > self.delta_limit:
            limited_delta = self.delta_limit if delta > 0 else -self.delta_limit
            vanew = state.previous_setpoint + limited_delta
            total_flags += f"[!Δ{int(abs(delta))}] "
            
        return ControlResult(
            setpoint=int(vanew),
            flags=total_flags.strip(),
            filtered_gt=new_filtered_gt
        )

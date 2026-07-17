"""
Console UI for Inverter Control
Handles formatting and terminal updates
"""

from datetime import datetime
from typing import Dict, Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from .config import (
    TIMEZONE,
    Colors as C,
    ENABLE_EV,
    ENABLE_WATER,
    ENABLE_HA_LOADS,
    ENABLE_DISHWASHER,
)


class ConsoleUI:
    """Handles terminal output formatting"""

    def __init__(self, ha_client, victron_interface):
        self.ha = ha_client
        self.victron = victron_interface
        self.title_update_counter = 0

    def format_line(
        self,
        sys_data: Dict[str, Any],
        setpoint: int,
        previous_setpoint: int,
        flags: str,
        filtered_gt: float,
    ) -> str:
        """Format console output matching original style"""
        now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M:%S")

        g1, g2, gt = sys_data["g1"], sys_data["g2"], sys_data["gt"]
        t1, t2, tt = sys_data["t1"], sys_data["t2"], sys_data["tt"]
        bv = sys_data.get("bv", 0)

        # Grid/Usage section
        net_usage = int(self.ha.get_sensor("net_usage", gt))
        home_total = int(self.ha.get_sensor("home_total", tt))
        smooth_str = f"[{int(filtered_gt)}]" if abs(gt - filtered_gt) > 10 else ""

        grid_str = f"{C.GREEN}g:{gt}{smooth_str}({g1}+{g2}){net_usage}{C.RESET}"
        usage_str = f"{tt}({t1}+{t2}) tt:{home_total}"

        # Extracted sections
        battery_str = self._fmt_battery_section(sys_data)
        solar_str = self._fmt_solar_section(sys_data)
        loads_str = self._fmt_loads_section()
        extra_str = self._fmt_extra_info()

        line = (
            f"{now}{flags}>{C.CYAN}{setpoint}{C.RESET}({previous_setpoint}) "
            f"{grid_str}\t{usage_str} {battery_str} "
            f"{solar_str} {loads_str} {extra_str} {bv:.2f}"
        )

        return line

    def _fmt_battery_section(self, sys_data: Dict[str, Any]) -> str:
        bp = sys_data.get("bp", 0)
        battery_socs = sys_data.get("battery_socs", [])
        soc1 = int(battery_socs[0]) if len(battery_socs) > 0 else 0
        soc2 = int(battery_socs[1]) if len(battery_socs) > 1 else 0
        comp_v = int(self.ha.get_sensor("compensation_voltage", 0))
        _, inv_state_name = self.victron.get_inverter_state()

        return f"{C.YELLOW}[{inv_state_name}]{bp}W,{comp_v}%,{soc1}%,{soc2}%{C.RESET}"

    def _fmt_solar_section(self, sys_data: Dict[str, Any]) -> str:
        mppt_data = sys_data.get("mppt_data", {})
        tasmota_powers = sys_data.get("tasmota_powers", [])

        mppt_total = sum(m["w"] for m in mppt_data.values())
        tasmota_total = sum(tasmota_powers)
        solar_total = mppt_total + tasmota_total

        if solar_total == 0:
            return f"{C.CYAN}0{C.RESET}"

        def fmt_current(a):
            return "0A" if a < 0.05 else f"{a:.1f}A"

        mppt_str = "+".join(
            f"{int(m['w'])}[{fmt_current(m['a'])}]" for m in mppt_data.values()
        )
        tas_str = "+".join(str(int(p)) for p in tasmota_powers if p > 0)

        solar_str = f"{C.CYAN}{int(solar_total)}("
        if tas_str:
            solar_str += f"{tas_str}+"
        solar_str += f"{int(mppt_total)}({mppt_str})){C.RESET}"
        return solar_str

    def _fmt_loads_section(self) -> str:
        if not ENABLE_HA_LOADS:
            return ""
        loads_parts = []
        for name, key in [
            ("g", "garage"),
            ("f", "fridge"),
            ("h", "furnace"),
            ("s", "stove"),
            ("m", "microwave"),
            ("k", "kitchen_fridge_side"),
            ("d", "dishwasher"),
            ("l", "lost"),
        ]:
            val = int(self.ha.get_sensor(key, 0))
            if val > 19:
                loads_parts.append(f"{val}{name}")
        return " ".join(loads_parts)

    def _fmt_extra_info(self) -> str:
        parts = []

        # Water
        if ENABLE_WATER:
            water_level = int(self.ha.get_sensor("water_level", 0))
            color = C.RED if self.ha.water_valve_on else C.YELLOW
            parts.append(f"{color}{water_level}cm{C.RESET}")

        # Car
        if ENABLE_EV:
            car_soc = int(self.ha.get_sensor("car_soc", 0))
            parts.append(f"{C.YELLOW}{car_soc}%{C.RESET}")

        # Appliances
        parts.append(fmt_appliance_time(self.ha.get_sensor("washer_time", "")))
        parts.append(fmt_appliance_time(self.ha.get_sensor("dryer_time", "")))

        if ENABLE_DISHWASHER and self.ha.get_binary_sensor("dishwasher_running"):
            parts.append(
                fmt_appliance_time(self.ha.get_sensor("dishwasher_duration", ""))
            )

        return "".join(parts)

    def update_terminal_title(self):
        """Update terminal title with daily stats"""
        self.title_update_counter += 1
        if self.title_update_counter < 10:
            return
        self.title_update_counter = 0

        produced = self.ha.get_sensor("produced_today", 0)
        dollars = self.ha.get_sensor("produced_dollars", 0)
        grid_kwh = self.ha.get_sensor("grid_kwh_today", 0)
        bin_kwh = self.ha.get_sensor("battery_in_today", 0)
        bout_kwh = self.ha.get_sensor("battery_out_today", 0)

        title = (
            f"{produced}kW(${dollars})[G:{grid_kwh}kW] B.I:{bin_kwh}kWh,O:{bout_kwh}kWh"
        )
        print(f"\033]2;{title}\007", end="", flush=True)


def fmt_appliance_time(t):
    """Format appliance time (strip leading zeros)"""
    if not t or t == "0":
        return ""
    t = str(t).lstrip("0:")
    if t.endswith(":00"):
        t = t[:-3]
    return t

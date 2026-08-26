"""
Console UI for Inverter Control
Handles formatting and terminal updates
"""

from datetime import datetime
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from .config import (
    ENABLE_DISHWASHER,
    ENABLE_EV,
    ENABLE_HA_LOADS,
    ENABLE_WATER,
    TIMEZONE,
)
from .config import (
    Colors as C,
)


class ConsoleUI:
    """Handles terminal output formatting"""

    def __init__(self, ha_client, victron_interface, water_reader=None):
        self.ha = ha_client
        self.victron = victron_interface
        self.water = water_reader  # WaterSystemReader or None (test mode)
        self.title_update_counter = 0

    def format_line(  # pylint: disable=too-many-arguments
        self,
        sys_data: dict[str, Any],
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
        home_total = int(self.ha.get_vue_sensor("total", tt))
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

    def _fmt_battery_section(self, sys_data: dict[str, Any]) -> str:
        bp = sys_data.get("bp", 0)
        battery_socs = sys_data.get("battery_socs", [])
        soc1 = int(battery_socs[0]) if len(battery_socs) > 0 else 0
        soc2 = int(battery_socs[1]) if len(battery_socs) > 1 else 0
        # Calculate compensation voltage locally from D-Bus (was HA sensor)
        # Approximation: voltage sag/rise under load as % of nominal
        bv = sys_data.get("bv", 48.0)
        bc = sys_data.get("bc", 0.0)
        comp_v = 0
        if bv > 0 and bc != 0:
            # Internal resistance estimate * current / nominal voltage * 100
            # Typical LiFePO4 IR ~ 0.01-0.02 ohm per cell * 16 cells = 0.16-0.32 ohm
            ir_est = 0.24  # ohm
            comp_v = int(abs(bc * ir_est) / bv * 100)
            comp_v = min(comp_v, 100)
        _, inv_state_name = self.victron.get_inverter_state()

        return f"{C.YELLOW}[{inv_state_name}]{bp}W,{comp_v}%,{soc1}%,{soc2}%{C.RESET}"

    def _fmt_solar_section(self, sys_data: dict[str, Any]) -> str:
        mppt_data = sys_data.get("mppt_data", {})
        pv_inverter_powers = sys_data.get("pv_inverter_powers", [])

        mppt_total = sum(m["w"] for m in mppt_data.values())
        pv_inverter_total = sum(pv_inverter_powers)
        solar_total = mppt_total + pv_inverter_total

        if solar_total == 0:
            return f"{C.CYAN}0{C.RESET}"

        def fmt_current(a):
            return "0A" if a < 0.05 else f"{a:.1f}A"

        mppt_str = "+".join(f"{int(m['w'])}[{fmt_current(m['a'])}]" for m in mppt_data.values())
        tas_str = "+".join(str(int(p)) for p in pv_inverter_powers if p > 0)

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

        # Water (dbus-pump via D-Bus; level in %)
        if ENABLE_WATER and self.water is not None:
            wstate = self.water.read()
            level = wstate["water_level"]
            if level is None:
                parts.append(f"{C.YELLOW}--%{C.RESET}")
            else:
                color = C.RED if wstate["water_valve"] else C.YELLOW
                parts.append(f"{color}{int(level)}%{C.RESET}")

        # Car
        if ENABLE_EV:
            car_soc = int(self.ha.get_sensor("car_soc", 0))
            parts.append(f"{C.YELLOW}{car_soc}%{C.RESET}")

        # Appliances
        parts.append(fmt_appliance_time(self.ha.get_sensor("washer_time", "")))
        parts.append(fmt_appliance_time(self.ha.get_sensor("dryer_time", "")))

        if ENABLE_DISHWASHER and self.ha.get_binary_sensor("dishwasher_running"):
            parts.append(fmt_appliance_time(self.ha.get_sensor("dishwasher_duration", "")))

        return "".join(parts)

    def update_terminal_title(self):
        """Update terminal title with daily stats from D-Bus"""
        self.title_update_counter += 1
        if self.title_update_counter < 10:
            return
        self.title_update_counter = 0

        produced = self.victron.get_total_solar_yield_today()
        bin_kwh, bout_kwh = self.victron.get_battery_daily_energy()

        title = f"{produced:.1f}kWh B.I:{bin_kwh:.1f}kWh O:{bout_kwh:.1f}kWh"
        print(f"\033]2;{title}\007", end="", flush=True)


def fmt_appliance_time(t):
    """Format appliance time (strip leading zeros)"""
    if not t or t == "0":
        return ""
    t = str(t).lstrip("0:")
    t = t.removesuffix(":00")
    return t

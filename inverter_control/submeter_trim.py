"""Slow, bounded grid correction using distinct delayed submeter reports.

This helper never replaces the primary controller. It can move a held command
only after an undisturbed measurement window. Proposals are transactional:
``commit`` must follow an accepted actuator write, including base commands.
"""

import math
from dataclasses import dataclass, field
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> bool:
    number = _number(value)
    return number is not None and number.is_integer()


@dataclass(frozen=True)
class TrimDecision:
    """One command proposal; callers must commit the exact returned object."""

    setpoint: int
    delta: int
    measurement_time: float | None
    reason: str
    _previous: int = field(repr=False)
    _kind: str = field(repr=False)
    _proposed_at: float = field(repr=False)


@dataclass(frozen=True)
class _Report:
    timestamp: float
    power: float
    observed_at: float


class SubmeterTrim:
    """A small correction per fresh, settled window, without a running ramp.

    A configured service is preferred. Otherwise the first valid service and
    device instance are pinned for this object's lifetime. A service restart
    (backup generation) or primary source change requires a new warmup.
    ``reset`` clears active history, but never permits another meter identity.
    """

    MAX_AGE = 8.0
    MAX_FUTURE = 1.0
    SETTLE_SECONDS = 10.0
    MEASUREMENT_DELAY = 6.0
    WINDOW_SECONDS = 20.0
    MIN_SPAN = 10.0
    MIN_REPORTS = 5
    SUBMETER_RANGE = 40.0
    PRIMARY_RANGE = 30.0
    RAW_DEVIATION = 80.0
    DEADZONE = 5.0
    GAIN = 0.25
    MAX_STEP = 4
    MAX_TOTAL = 60

    def __init__(
        self,
        enabled: bool = False,
        *,
        expected_service: str = "",
        expected_device_instance: int | None = None,
    ):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if not isinstance(expected_service, str) or (
            expected_service and not expected_service.startswith("com.victronenergy.acload.")
        ):
            raise ValueError("expected_service must be an AC load service name")
        if expected_device_instance is not None and (
            not _integer(expected_device_instance) or expected_device_instance < 0
        ):
            raise ValueError("expected_device_instance must be a nonnegative integer")
        self.enabled = enabled
        self._service = expected_service or None
        self._instance = expected_device_instance
        self._last_accepted_delta: int | None = None
        self._last_accepted_time: float | None = None
        self._last_trim_time: float | None = None
        self._last_now: float | None = None
        self._context: tuple | None = None
        self._accepted_setpoint: int | None = None
        self._command_time: float | None = None
        self._command_wall: float | None = None
        self._last_measurement: float | None = None
        self._last_power: float | None = None
        self._limits: tuple[float, float] | None = None
        self._total_trim = 0
        self.reset("warming_up" if enabled else "disabled")

    def reset(self, reason: str = "reset") -> None:
        """Discard pending/history and warm up again without renewing the budget.

        An outage or failed write must not grant another 60 W allowance. Only
        a verified new source or accepted fast-control baseline does that.
        The high-watermark also survives suspension so a report stays unique.
        """
        self._needs_warmup = True
        self._issued: TrimDecision | None = None
        self._pending: TrimDecision | None = None
        self._clear_window()
        self._reason = reason

    def _clear_window(self) -> None:
        self._reports: list[_Report] = []
        self._primary: list[tuple[float, float]] = []
        self._average: float | None = None
        self._pending = None

    def _decision(
        self,
        base: int,
        previous: int,
        now: float,
        reason: str,
        *,
        delta: int = 0,
        timestamp: float | None = None,
    ) -> TrimDecision:
        self._reason = reason
        self._issued = TrimDecision(
            setpoint=base + delta,
            delta=delta,
            measurement_time=timestamp,
            reason=reason,
            _previous=previous,
            _kind="trim" if delta else "base" if base != previous else "hold",
            _proposed_at=now,
        )
        if delta:
            self._pending = self._issued
        return self._issued

    def _restart_baseline(self, previous: int, now: float, wall_time: float) -> None:
        self._accepted_setpoint = previous
        self._command_time = now
        self._command_wall = wall_time
        self._total_trim = 0
        self._last_measurement = None
        self._last_power = None
        self._needs_warmup = False
        self._clear_window()

    def _sample(self, sample: Any, wall_time: float) -> tuple[tuple, float, float] | str:
        if not isinstance(sample, dict) or sample.get("available") is not True:
            return "submeter_unavailable"
        service = sample.get("service")
        instance = sample.get("device_instance")
        generation = sample.get("generation", 0)
        if (
            not isinstance(service, str)
            or not service.startswith("com.victronenergy.acload.")
            or not _integer(instance)
            or instance < 0
            or not _integer(generation)
            or generation < 0
        ):
            return "invalid_identity"
        if (self._service is not None and service != self._service) or (
            self._instance is not None and instance != self._instance
        ):
            return "foreign_identity"
        timestamp = _number(sample.get("measurement_time"))
        power = _number(sample.get("power"))
        age = _number(sample.get("age_seconds"))
        if timestamp is None or timestamp <= 0 or power is None or age is None or age < 0:
            return "invalid_submeter"
        source_age = wall_time - timestamp
        if source_age < -self.MAX_FUTURE:
            return "future_submeter"
        if source_age > self.MAX_AGE or age > self.MAX_AGE:
            return "stale_submeter"
        self._service = service
        self._instance = int(instance)
        return (service, int(instance), int(generation)), timestamp, power

    def propose(
        self,
        *,
        now: float,
        wall_time: float,
        previous_setpoint: int,
        base_setpoint: int,
        sample: dict[str, Any],
        eligible: bool,
        reason: str,
        raw_grid: float,
        filtered_grid: float,
        min_setpoint: int,
        max_setpoint: int,
        source_key: tuple,
    ) -> TrimDecision:
        """Return a held/base command or a bounded correction, without applying it.

        Caller eligibility must exclude every special control mode, manual
        override, fallback source, derivative/burst action and derived filter.
        Fast command changes always take precedence over the slow correction.
        """
        if not _integer(previous_setpoint):
            raise ValueError("previous_setpoint must be an accepted integer command")
        previous = int(previous_setpoint)
        base = int(base_setpoint) if _integer(base_setpoint) else previous
        clock = _number(now)
        wall = _number(wall_time)
        if clock is None or wall is None or clock < 0 or wall <= 0:
            self.reset("invalid_clock")
            return self._decision(base, previous, 0.0, "invalid_clock")
        if self._last_now is not None and clock < self._last_now:
            self.reset("clock_reversed")
            self._last_now = clock
            return self._decision(base, previous, clock, "clock_reversed")
        self._last_now = clock
        if not self.enabled or eligible is not True:
            message = "disabled" if not self.enabled else reason or "ineligible"
            self.reset(message)
            return self._decision(base, previous, clock, message)
        if not _integer(base_setpoint):
            self.reset("invalid_base_command")
            return self._decision(previous, previous, clock, "invalid_base_command")
        if base != previous:
            self._clear_window()
            return self._decision(base, previous, clock, "fast_command")
        low, high = _number(min_setpoint), _number(max_setpoint)
        if low is None or high is None or low >= high or not low <= base <= high:
            self.reset("invalid_limits")
            return self._decision(base, previous, clock, "invalid_limits")
        if self._limits is not None and self._limits != (low, high):
            self.reset("limits_changed")
        self._limits = low, high
        raw, filtered = _number(raw_grid), _number(filtered_grid)
        if raw is None or filtered is None:
            self.reset("invalid_primary")
            return self._decision(base, previous, clock, "invalid_primary")
        if (
            not isinstance(source_key, tuple)
            or len(source_key) < 2
            or not isinstance(source_key[0], str)
            or not source_key[0].startswith("com.victronenergy.")
            or any(not _integer(item) or item < 0 for item in source_key[1:])
        ):
            self.reset("invalid_source")
            return self._decision(base, previous, clock, "invalid_source")
        report = self._sample(sample, wall)
        if isinstance(report, str):
            self.reset(report)
            return self._decision(base, previous, clock, report)
        identity, timestamp, power = report
        context = source_key, identity
        if context != self._context or previous != self._accepted_setpoint:
            self._restart_baseline(previous, clock, wall)
            self._context = context
        elif self._needs_warmup:
            self._command_time = clock
            self._command_wall = wall
            self._needs_warmup = False
        if abs(raw - filtered) > self.RAW_DEVIATION:
            self._clear_window()
            return self._decision(base, previous, clock, "primary_transient")
        self._primary.append((clock, filtered))
        self._primary = [
            point for point in self._primary if clock - point[0] <= self.WINDOW_SECONDS
        ]
        if self._last_measurement is not None and timestamp < self._last_measurement:
            self._clear_window()
            return self._decision(base, previous, clock, "out_of_order_submeter")
        is_new = self._last_measurement is None or timestamp > self._last_measurement
        if not is_new and self._last_power != power:
            self._clear_window()
            return self._decision(base, previous, clock, "changed_submeter_report")
        if is_new:
            self._pending = None
            self._last_measurement = timestamp
            self._last_power = power
        if timestamp < self._command_wall + self.MEASUREMENT_DELAY:
            self._pending = None
            return self._decision(base, previous, clock, "measurement_predates_settle")
        if is_new:
            self._reports.append(_Report(timestamp, power, clock))
        self._reports = [
            item for item in self._reports if timestamp - item.timestamp <= self.WINDOW_SECONDS
        ]
        if self._reports:
            powers = [item.power for item in self._reports]
            primary = [value for t, value in self._primary if t >= self._reports[0].observed_at]
            if max(powers) - min(powers) > self.SUBMETER_RANGE:
                self._clear_window()
                return self._decision(base, previous, clock, "submeter_unstable")
            if primary and max(primary) - min(primary) > self.PRIMARY_RANGE:
                self._clear_window()
                return self._decision(base, previous, clock, "primary_unstable")
            self._average = sum(powers) / len(powers)
        if clock - self._command_time < self.SETTLE_SECONDS:
            return self._decision(base, previous, clock, "settling")
        if self._pending is not None:
            # The same source report and command are still fresh and eligible.
            self._issued = self._pending
            self._reason = self._pending.reason
            return self._pending
        if not is_new:
            return self._decision(base, previous, clock, "repeated_submeter")
        if len(self._reports) < self.MIN_REPORTS or (
            self._reports[-1].timestamp - self._reports[0].timestamp < self.MIN_SPAN
        ):
            return self._decision(base, previous, clock, "collecting")
        if abs(self._average) <= self.DEADZONE:
            return self._decision(base, previous, clock, "within_deadzone", timestamp=timestamp)
        magnitude = min(self.MAX_STEP, max(1, round(abs(self._average) * self.GAIN)))
        delta = -magnitude if self._average > 0 else magnitude
        if abs(self._total_trim + delta) > self.MAX_TOTAL:
            return self._decision(base, previous, clock, "trim_budget", timestamp=timestamp)
        if not low < base + delta < high:
            return self._decision(base, previous, clock, "setpoint_limit", timestamp=timestamp)
        return self._decision(base, previous, clock, "trim", delta=delta, timestamp=timestamp)

    def commit(self, decision: TrimDecision, now: float, wall_time: float) -> bool:
        """Record only the current decision after its hardware write succeeds."""
        clock, wall = _number(now), _number(wall_time)
        if (
            decision is not self._issued
            or clock is None
            or wall is None
            or clock < decision._proposed_at
            or wall <= 0
        ):
            return False
        changed = decision.setpoint != decision._previous
        if changed or self._accepted_setpoint is None:
            self._command_time = clock
            self._command_wall = wall
            self._accepted_setpoint = decision.setpoint
        if changed:
            self._last_accepted_delta = decision.setpoint - decision._previous
            self._last_accepted_time = wall
            if decision._kind == "trim":
                self._total_trim += decision.delta
                self._last_trim_time = wall
            else:
                self._total_trim = 0
            self._clear_window()
            self._needs_warmup = False
        self._pending = None
        self._issued = None
        return True

    def status(self) -> dict[str, Any]:
        """Return finite, JSON-safe observability without exposing internal tokens."""
        return {
            "enabled": self.enabled,
            "reason": self._reason,
            "avg_error": self._average,
            "sample_count": len(self._reports),
            "total_trim": self._total_trim,
            "max_total_trim": self.MAX_TOTAL,
            "last_measurement_time": self._last_measurement,
            "last_accepted_setpoint": self._accepted_setpoint,
            "last_accepted_delta": self._last_accepted_delta,
            "last_accepted_time": self._last_accepted_time,
            "last_trim_time": self._last_trim_time,
            "pending_delta": self._pending.delta if self._pending else 0,
            "service": self._service,
            "device_instance": self._instance,
        }

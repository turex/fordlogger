import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .models import Position

log = logging.getLogger("fordlogger")


class VehicleState(str, Enum):
    DRIVING = "driving"
    CHARGING = "charging"
    PARKED = "parked"
    SLEEPING = "sleeping"


CHARGING_STATUSES = {
    "Charging",
    "ChargingAC",
    "ChargingDC",
    "CHARGING",
}

PLUG_CONNECTED = {"CONNECTED", "Connected"}
IGNITION_ACTIVE = {"Run", "RUN", "On", "ON", "START"}


class StateMachine:
    def __init__(
        self,
        vin: str,
        sleep_after_minutes: int = 30,
        stop_debounce_seconds: int = 120,
    ):
        self.vin = vin
        self.state = VehicleState.PARKED
        self.last_change_ts: datetime = datetime.now(timezone.utc)
        self._last_significant_change: datetime = datetime.now(timezone.utc)
        self.sleep_after_s = sleep_after_minutes * 60
        self.stop_debounce_s = stop_debounce_seconds

        # Prior position tracking to detect telemetry deltas while parked
        self._last_pos: Optional[Position] = None
        self._last_motion_ts: Optional[datetime] = None

    def transition(self, pos: Position) -> tuple[VehicleState, bool]:
        """Returns (new_state, changed). Updates internal state."""
        old = self.state
        now = pos.ts or datetime.now(timezone.utc)

        speed = pos.speed_kmh or 0.0
        ignition_on = pos.ignition_status in IGNITION_ACTIVE
        has_speed = speed > 1.0

        if has_speed or ignition_on:
            self._last_motion_ts = now

        # 1. Driving Detection with Debounce
        # If speed drops to 0 at a traffic light, don't flap to PARKED immediately
        is_driving = False
        if has_speed or ignition_on:
            is_driving = True
        elif old == VehicleState.DRIVING and self._last_motion_ts:
            time_stopped = (now - self._last_motion_ts).total_seconds()
            if time_stopped < self.stop_debounce_s:
                is_driving = True

        # 2. Charging Detection
        is_charging = (
            (pos.charge_status in CHARGING_STATUSES)
            or ((pos.charge_power_kw or 0.0) > 0.1 and pos.plug_status in PLUG_CONNECTED)
        )

        # 3. Base State Resolution
        if is_driving:
            new = VehicleState.DRIVING
        elif is_charging:
            new = VehicleState.CHARGING
        elif old == VehicleState.SLEEPING:
            # Wake up if doors unlock, ignition turns on, or charging starts
            if self._has_wake_trigger(pos):
                new = VehicleState.PARKED
            else:
                new = VehicleState.SLEEPING
        else:
            new = VehicleState.PARKED

        # 4. Sleep Transition (Parked -> Sleeping)
        if new == VehicleState.PARKED and old in (VehicleState.PARKED, VehicleState.SLEEPING):
            idle_duration = (now - self._last_significant_change).total_seconds()
            if idle_duration > self.sleep_after_s:
                new = VehicleState.SLEEPING

        # 5. Handle State Changes
        changed = new != old
        if changed:
            log.info("State change %s: %s -> %s", self.vin, old.value, new.value)
            self.last_change_ts = now
            self._last_significant_change = now

        # Update activity timestamp if parked data is actively fluctuating
        if new == VehicleState.PARKED and self._has_parked_activity(pos):
            self._last_significant_change = now

        self.state = new
        self._last_pos = pos
        return new, changed

    def _has_wake_trigger(self, pos: Position) -> bool:
        """Determines if a sleeping vehicle should wake into PARKED."""
        if not self._last_pos:
            return False

        # Door unlocked
        if (
            self._last_pos.door_lock_status in ("LOCKED", "Locked")
            and pos.door_lock_status in ("UNLOCKED", "Unlocked")
        ):
            return True

        # Pre-conditioning or HVAC turned on (high 12V / traction discharge while parked)
        if pos.bat_current_a and pos.bat_current_a < -5.0:
            return True

        # Plug connected state changed
        if self._last_pos.plug_status != pos.plug_status:
            return True

        return False

    def _has_parked_activity(self, pos: Position) -> bool:
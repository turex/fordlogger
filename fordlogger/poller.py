import logging
import time
from datetime import datetime, timezone
from typing import Optional

from . import db
from .charge_detector import finalize_charge_session
from .config import INTERVALS
from .models import Position
from .state_machine import StateMachine, VehicleState
from .trip_detector import finalize_trip

log = logging.getLogger("fordlogger")


class Poller:
    def __init__(self, cfg: dict, conn, apis: list):
        self.cfg = cfg
        self.conn = conn
        self.apis = apis if isinstance(apis, list) else [apis]
        self._db_failures = 0
        self.machines: dict[str, StateMachine] = {}

        # In-memory start tracking with DB fallback
        self._drive_start: dict[str, datetime] = {}
        self._charge_start: dict[str, datetime] = {}
        self._last_ts: dict[str, datetime] = {}
        self._last_garage_update: float = 0
        self.garage_interval_s = 3600

    def _get_machine(self, vin: str) -> StateMachine:
        if vin not in self.machines:
            sleep_min = self.cfg.get("sleep_after_minutes", 30)
            sm = StateMachine(vin, sleep_after_minutes=sleep_min)

            saved_state, state_ts = db.get_latest_state(self.conn, vin)
            if saved_state and saved_state in VehicleState.__members__.values():
                sm.state = VehicleState(saved_state)
                log.info("Restored state for %s from DB: %s (at %s)", vin, saved_state, state_ts)

                # Recover ongoing sessions across daemon reboots
                if sm.state == VehicleState.DRIVING and state_ts:
                    self._drive_start[vin] = state_ts
                elif sm.state == VehicleState.CHARGING and state_ts:
                    self._charge_start[vin] = state_ts

            self.machines[vin] = sm
        return self.machines[vin]

    def _ensure_db(self) -> bool:
        """Verify DB connection or reconnect if dropped."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
            self._db_failures = 0
            return True
        except Exception:
            self._db_failures += 1
            log.warning("DB connection lost — attempting reconnect (%d)", self._db_failures)
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                self.conn = db.connect(self.cfg)
                db.ensure_schema(self.conn)
                log.info("DB reconnected successfully")
                self._db_failures = 0
                return True
            except Exception as e:
                log.error("DB reconnect failed: %s", e)
                return False

    def poll_once(self):
        """Execute a single poll cycle for all vehicles."""
        if not self._ensure_db():
            return

        now_mono = time.time()
        now_dt = datetime.now(timezone.utc)

        # Periodic garage update
        if now_mono - self._last_garage_update > self.garage_interval_s:
            for api in self.apis:
                try:
                    vehicles = api.get_garage()
                    for v in vehicles:
                        db.upsert_vehicle(self.conn, v)
                    log.info("Garage updated: %d vehicle(s)", len(vehicles))
                except Exception as e:
                    log.error("Garage update error: %s", e)
            self._last_garage_update = now_mono

        # Telemetry cycle
        for api in self.apis:
            try:
                positions = api.get_telemetry()
            except Exception as e:
                log.error("Telemetry fetch error: %s", e, exc_info=True)
                continue

            for pos in positions:
                try:
                    self._process_position(pos, now_dt)
                except Exception as e:
                    log.error("Failed processing position for VIN %s: %s", getattr(pos, "vin", "unknown"), e, exc_info=True)

    def _process_position(self, pos: Position, wall_clock_now: datetime):
        vin = pos.vin
        sm = self._get_machine(vin)
        old_state = sm.state

        # Check if API returned identical timestamp
        is_stale = (vin in self._last_ts and pos.ts == self._last_ts[vin])

        if is_stale:
            # When API data is stationary, pass wall clock time to evaluate sleep timeouts
            if old_state == VehicleState.PARKED:
                idle_s = (wall_clock_now - sm._last_significant_change).total_seconds()
                if idle_s > sm.sleep_after_s:
                    new_state = VehicleState.SLEEPING
                    sm.state = new_state
                    log.info("State change %s: %s -> %s (idle timeout via heartbeat)", vin, old_state.value, new_state.value)
                    db.insert_state(self.conn, vin, wall_clock_now, new_state.value, old_state.value)
                    self._handle_transition(vin, old_state, new_state, wall_clock_now)
            return

        self._last_ts[vin] = pos.ts

        if not self.cfg.get("store_raw_json", True):
            pos.raw_json = None

        new_state, changed = sm.transition(pos)
        pos.state = new_state.value

        pos_id = db.insert_position(self.conn, pos)
        log.info(
            "Position #%s – %s | SoC=%s%% | Range=%skm | Odo=%skm | State=%s",
            pos_id, vin, pos.soc_pct, pos.range_km, pos.odometer_km, new_state.value,
        )

        if changed:
            db.insert_state(self.conn, vin, pos.ts, new_state.value, old_state.value)
            self._handle_transition(vin, old_state, new_state, pos.ts)

    def _handle_transition(self, vin: str, old: VehicleState, new: VehicleState, ts: datetime):
        # 1. Driving transitions
        if new == VehicleState.DRIVING:
            self._drive_start[vin] = ts
        elif old == VehicleState.DRIVING and vin in self._drive_start:
            start_ts = self._drive_start.pop(vin)
            try:
                finalize_trip(
                    self.conn,
                    vin,
                    start_ts,
                    ts,
                    geocoding=self.cfg.get("geocoding", True),
                )
            except Exception as e:
                log.error("Trip finalization failed for %s: %s", vin, e, exc_info=True)

        # 2. Charging transitions
        if new == VehicleState.CHARGING:
            self._charge_start[vin] = ts
        elif old == VehicleState.CHARGING and vin in self._charge_start:
            start_ts = self._charge_start.pop(vin)
            try:
                finalize_charge_session(
                    self.conn,
                    vin,
                    start_ts,
                    ts,
                    geocoding=self.cfg.get("geocoding", True),
                )
            except Exception as e:
                log.error("Charge session finalization failed for %s: %s", vin, e, exc_info=True)

    def get_interval(self) -> int:
        """Return the shortest interval across tracked vehicles."""
        if not self.machines:
            return INTERVALS.get("parked", 60)
        return min(INTERVALS.get(sm.state.value, 60) for sm in self.machines.values())

    def run_forever(self):
        log.info("Polling daemon started")
        self._last_garage_update = 0

        while True:
            try:
                self.poll_once()
            except Exception as e:
                log.critical("Unexpected error in poll_once: %s", e, exc_info=True)

            interval = self.get_interval()
            log.debug("Next poll in %ds", interval)
            time.sleep(interval)
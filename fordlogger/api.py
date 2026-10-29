import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .auth import get_valid_token
from .models import Position, Vehicle

log = logging.getLogger("fordlogger")

DEFAULT_TIMEOUT = 15  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = [30, 60, 120]


class FordAPI:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base = cfg["api_base"].rstrip("/")
        self.session = requests.Session()

    def _get(self, path: str) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"

        for attempt in range(MAX_RETRIES + 1):
            token = get_valid_token(self.cfg)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }

            try:
                r = self.session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)

                if r.status_code == 429:
                    if attempt < MAX_RETRIES:
                        # Prefer server-specified Retry-After if available
                        retry_after = r.headers.get("Retry-After")
                        try:
                            wait = int(retry_after) if retry_after else RETRY_BACKOFF[attempt]
                        except ValueError:
                            wait = RETRY_BACKOFF[attempt]

                        log.warning(
                            "429 Rate Limited on /%s — retrying in %ds (attempt %d/%d)",
                            path, wait, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue

                    log.error("429 Rate Limited on /%s — exhausted %d retries", path, MAX_RETRIES)

                r.raise_for_status()
                return r.json()

            except (requests.RequestException) as e:
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF[attempt]
                    log.warning("Request failed (%s) on /%s — retrying in %ds", e, path, wait)
                    time.sleep(wait)
                    continue
                log.error("Request failed on /%s after %d retries: %s", path, MAX_RETRIES, e)
                raise

        return {}

    def get_garage(self) -> list[Vehicle]:
        data = self._get("garage")
        items = data if isinstance(data, list) else data.get("vehicles", [data])
        vehicles = []

        for v in items:
            if not isinstance(v, dict):
                continue
            vin = v.get("vin") or v.get("vehicleId")
            if not vin:
                continue

            vehicles.append(
                Vehicle(
                    vin=vin,
                    make=v.get("make"),
                    model=v.get("modelName") or v.get("model"),
                    model_year=v.get("modelYear"),
                    color=v.get("color"),
                    nickname=v.get("nickName") or v.get("nickname"),
                    engine_type=v.get("engineType"),
                )
            )
        return vehicles

    def get_telemetry(self) -> list[Position]:
        data = self._get("telemetry")
        items = data if isinstance(data, list) else [data]
        positions = []

        for t in items:
            if not isinstance(t, dict):
                continue
            if "metrics" not in t:
                log.warning("Telemetry payload missing 'metrics' key: %s", list(t.keys()))
                continue

            pos = self._parse_telemetry(t)
            if pos:
                positions.append(pos)
        return positions

    @staticmethod
    def _extract_latest_update_time(metrics: dict) -> Optional[datetime]:
        latest = None
        for entry in metrics.values():
            items = entry if isinstance(entry, list) else [entry]
            for item in items:
                if not isinstance(item, dict):
                    continue
                ut = item.get("updateTime")
                if not ut:
                    continue
                try:
                    parsed = datetime.fromisoformat(ut.replace("Z", "+00:00"))
                    if latest is None or parsed > latest:
                        latest = parsed
                except (ValueError, TypeError):
                    continue
        return latest

    def _parse_telemetry(self, t: dict) -> Optional[Position]:
        metrics = t.get("metrics", {})

        def m(key: str) -> Any:
            entry = metrics.get(key)
            if entry is None:
                return None
            if isinstance(entry, list):
                return entry[0].get("value") if (entry and isinstance(entry[0], dict)) else None
            if isinstance(entry, dict):
                # Returns nested dict/val if 'value' exists, else the dict itself (e.g. position)
                return entry.get("value", entry)
            return entry

        def m_list(key: str) -> list[dict]:
            entry = metrics.get(key)
            return entry if isinstance(entry, list) else []

        vin = t.get("vin") or t.get("vehicleId")
        if not vin:
            return None

        ts = self._extract_latest_update_time(metrics) or datetime.now(timezone.utc)

        # Explicit None checks to avoid dropping 0 values
        soc = m("xevBatteryStateOfCharge")
        if soc is None:
            soc = m("xevBatteryActualStateOfCharge")

        rng = m("xevBatteryRange")
        odo = m("odometer")
        speed = m("speed")

        # Location extraction
        pos_data = m("position")
        lat = lon = None
        if isinstance(pos_data, dict):
            loc = pos_data.get("location", pos_data)
            lat = loc.get("lat")
            lon = loc.get("lon")

        # Heading extraction
        heading_data = m("heading")
        heading = None
        if isinstance(heading_data, dict):
            heading = heading_data.get("heading")
        elif isinstance(heading_data, (int, float)):
            heading = heading_data

        charger_voltage = m("xevBatteryChargerVoltageOutput")
        charger_current = m("xevBatteryChargerCurrentOutput")
        charge_power = None
        if charger_voltage is not None and charger_current is not None:
            try:
                charge_power = round(float(charger_current) * float(charger_voltage) / 1000.0, 3)
            except (ValueError, TypeError):
                pass

        # Tire pressures
        tire_pressures = {"FL": None, "FR": None, "RL": None, "RR": None}
        for tp in m_list("tirePressure"):
            wheel = tp.get("vehicleWheel", "")
            val = tp.get("value")
            if "FRONT_LEFT" in wheel:
                tire_pressures["FL"] = val
            elif "FRONT_RIGHT" in wheel:
                tire_pressures["FR"] = val
            elif "REAR_LEFT" in wheel:
                tire_pressures["RL"] = val
            elif "REAR_RIGHT" in wheel:
                tire_pressures["RR"] = val

        # Door lock status
        door_lock = None
        door_entries = m_list("doorLockStatus")
        for d in door_entries:
            if d.get("vehicleDoor") == "ALL_DOORS":
                door_lock = d.get("value")
                break
        if door_lock is None and door_entries:
            door_lock = door_entries[0].get("value")

        return Position(
            ts=ts,
            vin=vin,
            soc_pct=soc,
            range_km=rng,
            odometer_km=odo,
            speed_kmh=speed,
            lat=lat,
            lon=lon,
            heading=heading,
            bat_temp_c=m("xevBatteryTemperature"),
            outside_temp_c=m("outsideTemperature"),
            bat_voltage=m("xevBatteryVoltage"),
            bat_current_a=m("xevBatteryIoCurrent"),
            energy_remaining_kwh=m("xevBatteryEnergyRemaining"),
            bat_capacity_kwh=m("xevBatteryCapacity"),
            plug_status=m("xevPlugChargerStatus"),
            charge_status=m("xevBatteryChargeDisplayStatus"),
            charge_power_kw=charge_power,
            charger_voltage=charger_voltage,
            charger_current_a=charger_current,
            tire_pressure_fl=tire_pressures["FL"],
            tire_pressure_fr=tire_pressures["FR"],
            tire_pressure_rl=tire_pressures["RL"],
            tire_pressure_rr=tire_pressures["RR"],
            door_lock_status=door_lock,
            ignition_status=m("ignitionStatus"),
            raw_json=t,
        )
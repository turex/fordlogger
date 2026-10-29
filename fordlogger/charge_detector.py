import logging
from datetime import datetime
from typing import Optional

from . import db
from .geocoder import reverse_geocode
from .models import ChargeSession

log = logging.getLogger("fordlogger")


def finalize_charge_session(
    conn,
    vin: str,
    charge_start_ts: datetime,
    charge_end_ts: datetime,
    geocoding: bool = True,
) -> Optional[ChargeSession]:
    """Create a charge session summary from positions between start and end timestamps."""
    # Preferably query positions with both start and end bounds
    if hasattr(db, "get_positions_between"):
        charge_positions = db.get_positions_between(conn, vin, charge_start_ts, charge_end_ts)
    else:
        positions = db.get_positions_since(conn, vin, charge_start_ts)
        charge_positions = [p for p in positions if p["ts"] <= charge_end_ts]

    if len(charge_positions) < 2:
        log.warning("Too few positions for charge session: %d", len(charge_positions))
        return None

    first = charge_positions[0]
    last = charge_positions[-1]

    duration_s = int((last["ts"] - first["ts"]).total_seconds())
    if duration_s < 60:
        log.debug("Charge session too short (%ds), skipping", duration_s)
        return None

    # SoC calculation
    start_soc = first.get("soc_pct")
    end_soc = last.get("soc_pct")
    soc_added = None
    if start_soc is not None and end_soc is not None:
        soc_added = round(end_soc - start_soc, 1)

    # Energy calculation
    energy_start = first.get("energy_remaining_kwh")
    energy_end = last.get("energy_remaining_kwh")
    energy_added = None
    if energy_start is not None and energy_end is not None:
        energy_added = round(energy_end - energy_start, 3)

    # Power metrics & time-weighted power calculation
    powers = [
        p["charge_power_kw"]
        for p in charge_positions
        if p.get("charge_power_kw") is not None and p["charge_power_kw"] > 0
    ]
    max_power = round(max(powers), 2) if powers else None

    # Time-weighted average power calculation (Riemann step sum)
    avg_power = None
    total_energy_kwh_integral = 0.0
    valid_duration_s = 0.0

    for i in range(len(charge_positions) - 1):
        curr_p = charge_positions[i]
        next_p = charge_positions[i + 1]
        kw = curr_p.get("charge_power_kw")

        if kw is not None and kw > 0:
            dt = (next_p["ts"] - curr_p["ts"]).total_seconds()
            if dt > 0:
                total_energy_kwh_integral += kw * (dt / 3600.0)
                valid_duration_s += dt

    if valid_duration_s > 0:
        avg_power = round(total_energy_kwh_integral / (valid_duration_s / 3600.0), 2)
    elif powers:
        avg_power = round(sum(powers) / len(powers), 2)

    # If telemetry didn't provide direct delta energy, estimate from power integral
    if energy_added is None and total_energy_kwh_integral > 0:
        energy_added = round(total_energy_kwh_integral, 3)

    # Charging type heuristic: standard 3-phase AC caps at 22 kW (32A)
    charge_type = None
    if max_power is not None:
        charge_type = "DC" if max_power > 22.0 else "AC"

    # Ambient temperature
    temps = [p["outside_temp_c"] for p in charge_positions if p.get("outside_temp_c") is not None]
    outside_temp = round(sum(temps) / len(temps), 1) if temps else None

    # Reverse geocoding (with coordinate presence guard)
    lat = first.get("lat")
    lon = first.get("lon")
    address = None
    if geocoding and lat is not None and lon is not None:
        try:
            address = reverse_geocode(lat, lon)
        except Exception as e:
            log.warning("Geocoding failed for charge session: %s", e)

    cs = ChargeSession(
        vin=vin,
        start_ts=first["ts"],
        end_ts=last["ts"],
        duration_s=duration_s,
        start_pos_id=first.get("id"),
        end_pos_id=last.get("id"),
        lat=lat,
        lon=lon,
        address=address,
        start_soc_pct=start_soc,
        end_soc_pct=end_soc,
        soc_added_pct=soc_added,
        energy_added_kwh=energy_added,
        max_power_kw=max_power,
        avg_power_kw=avg_power,
        charge_type=charge_type,
        outside_temp_c=outside_temp,
    )

    cs_id = db.insert_charge_session(conn, cs)
    if hasattr(cs, "id"):
        cs.id = cs_id

    log.info(
        "Charge session saved #%s: %.1f%% -> %.1f%% (+%.1f kWh), %dmin, max %.1f kW (%s)",
        cs_id,
        start_soc if start_soc is not None else 0.0,
        end_soc if end_soc is not None else 0.0,
        energy_added if energy_added is not None else 0.0,
        duration_s // 60,
        max_power if max_power is not None else 0.0,
        charge_type or "Unknown",
    )
    return cs
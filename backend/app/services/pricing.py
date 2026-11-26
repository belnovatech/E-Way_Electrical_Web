# Tariff pick: active plan for the vehicle type covering today, zone or global.
# Lowest priority wins, zone plan wins a tie.
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from .. import db
from ..errors import ApiError
from ..utils import money

# use as: LEFT JOIN LATERAL (TARIFF_SQL % ("<zone expr>", "<vehicle_type expr>")) tr ON true
TARIFF_SQL = """
    SELECT t.* FROM tariff_plans t
    WHERE t.status = 'active' AND t.vehicle_type = {vt}
      AND (t.zone_id = {zone} OR t.zone_id IS NULL)
      AND current_date BETWEEN t.effective_from AND COALESCE(t.effective_to, 'infinity'::date)
    ORDER BY t.priority, (t.zone_id IS NULL), t.id
    LIMIT 1
"""


def tariff_lateral(zone_expr: str, vt_expr: str) -> str:
    return TARIFF_SQL.format(zone=zone_expr, vt=vt_expr)


def resolve_tariff(cur, zone_id: int, vehicle_type: str) -> dict:
    row = db.one(cur, tariff_lateral("%(zone)s", "%(vt)s::vehicle_type"), {"zone": zone_id, "vt": vehicle_type})
    if row is None:
        raise ApiError(409, f"No active tariff for {vehicle_type} charging in this zone.", "no_tariff")
    return row


def rate_at(tariff: dict, at: datetime | None = None) -> Decimal:
    if tariff["pricing_model"] == "time_of_use" and tariff["peak_start"] and tariff["peak_end"]:
        now: time = (at or datetime.now()).time()
        start, end = tariff["peak_start"], tariff["peak_end"]
        in_peak = start <= now < end if start <= end else (now >= start or now < end)
        if in_peak:
            return money(tariff["peak_rate_per_kwh"])
        if tariff["off_peak_rate_per_kwh"] is not None:
            return money(tariff["off_peak_rate_per_kwh"])
    return money(tariff["rate_per_kwh"])


def session_cost(tariff: dict | None, energy_kwh: Decimal, rate: Decimal) -> dict[str, Decimal]:
    # energy + service fee (bumped up to the minimum) + GST. 0 kWh = free
    energy_kwh = Decimal(energy_kwh)
    if energy_kwh <= 0:
        zero = money(0)
        return {"energy_cost": zero, "service_fee": zero, "gst_amount": zero, "total": zero}
    energy_cost = money(energy_kwh * rate)
    service_fee = money(tariff["service_fee"]) if tariff else money(0)
    min_charge = money(tariff["min_charge"]) if tariff else money(0)
    if energy_cost + service_fee < min_charge:
        service_fee = min_charge - energy_cost
    gst_pct = Decimal(tariff["gst_percent"]) if tariff else Decimal(18)
    gst = money((energy_cost + service_fee) * gst_pct / 100)
    return {"energy_cost": energy_cost, "service_fee": service_fee, "gst_amount": gst,
            "total": energy_cost + service_fee + gst}


def max_affordable_kwh(tariff: dict, rate: Decimal, balance: Decimal) -> Decimal:
    """kWh whose full cost (energy + service fee + GST) fits in the wallet balance."""
    gst_factor = 1 + Decimal(tariff["gst_percent"]) / 100
    budget = Decimal(balance) / gst_factor - Decimal(tariff["service_fee"])
    if budget <= 0 or rate <= 0:
        return Decimal(0)
    return (budget / rate).quantize(Decimal("0.001"))


def estimate_reservation_cost(duration_minutes: int, rate: Decimal) -> Decimal:
    # same as the frontend: ~10 kWh/hr, min Rs 30
    est_kwh = round(duration_minutes / 60 * 10, 1)
    return money(max(30, round(Decimal(str(est_kwh)) * rate)))

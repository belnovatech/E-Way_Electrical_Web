from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .. import db
from ..config import get_settings
from ..errors import ApiError, not_found
from ..utils import money
from . import ledger
from .pricing import max_affordable_kwh, session_cost

LIVE = ("starting", "charging", "stopping")
DEFAULT_CAPACITY = Decimal("40.5")


def _tariff(cur, tariff_id: int | None) -> dict | None:
    return None if tariff_id is None else db.one(cur, "SELECT * FROM tariff_plans WHERE id = %s", (tariff_id,))


def refresh_live(cur, session_id: int) -> str | None:
    # simulation only: energy = power * time * efficiency, capped at the auto-stop
    # battery target and at what the wallet can pay. returns which cap hit, if any
    s = get_settings()
    row = db.one(cur, """
        SELECT cs.*, cp.power_kw, v.battery_capacity_kwh, w.balance, c.auto_stop_battery_percent
        FROM charging_sessions cs
        JOIN charging_points cp ON cp.id = cs.charging_point_id
        JOIN vehicles v ON v.id = cs.vehicle_id
        JOIN wallets w ON w.customer_id = cs.customer_id
        JOIN customer_profiles c ON c.user_id = cs.customer_id
        WHERE cs.id = %s
    """, (session_id,))
    if row is None or row["status"] not in LIVE or not s.charging_simulation:
        return None

    tariff = _tariff(cur, row["tariff_plan_id"])
    rate = Decimal(row["rate_per_kwh"])
    capacity = Decimal(row["battery_capacity_kwh"] or DEFAULT_CAPACITY)
    start_soc = Decimal(row["start_soc"] if row["start_soc"] is not None else 20)
    target = Decimal(row["auto_stop_battery_percent"]) if start_soc < row["auto_stop_battery_percent"] else Decimal(100)

    hours = Decimal((datetime.now(timezone.utc) - row["started_at"]).total_seconds()) / 3600
    simulated = Decimal(row["power_kw"]) * hours * Decimal(str(s.simulation_speedup)) * Decimal(str(s.charging_efficiency))
    battery_room = max(Decimal(0), capacity * (target - start_soc) / 100)
    affordable = max_affordable_kwh(tariff, rate, Decimal(row["balance"])) if tariff else battery_room

    energy = min(simulated, battery_room, affordable)
    energy = max(energy, Decimal(row["energy_delivered_kwh"])).quantize(Decimal("0.001"))  # never goes backwards
    soc = min(100, int(start_soc + energy / capacity * 100))
    cur.execute("""
        UPDATE charging_sessions
           SET energy_delivered_kwh = %s, current_soc = %s, energy_cost = %s,
               peak_power_kw = COALESCE(peak_power_kw, %s)
         WHERE id = %s
    """, (energy, soc, money(energy * rate), row["power_kw"], session_id))

    if energy >= battery_room - Decimal("0.001"):
        return "battery_target"
    if energy >= affordable - Decimal("0.001"):
        return "wallet_limit"
    return None


def session_payload(cur, session_id: int) -> dict:
    # ChargingSession shape from src/types/charging.ts + costBreakdown
    r = db.one(cur, """
        SELECT cs.*, cp.point_code, cp.connector_type, cp.power_kw, s.id AS station_id, s.name AS station_name,
               s.address, s.city, v.nickname, v.manufacturer, v.model, v.license_plate, v.battery_capacity_kwh,
               w.balance, r.reservation_number
        FROM charging_sessions cs
        JOIN charging_points cp ON cp.id = cs.charging_point_id
        JOIN stations s ON s.id = cp.station_id
        JOIN vehicles v ON v.id = cs.vehicle_id
        JOIN wallets w ON w.customer_id = cs.customer_id
        LEFT JOIN reservations r ON r.id = cs.reservation_id
        WHERE cs.id = %s
    """, (session_id,))
    if r is None:
        raise not_found("Charging session")
    live = r["status"] in LIVE
    if live:
        cost = session_cost(_tariff(cur, r["tariff_plan_id"]), r["energy_delivered_kwh"], Decimal(r["rate_per_kwh"]))
        current_cost = cost["total"]
    else:
        current_cost = r["total_amount"]
    end = r["ended_at"] or datetime.now(timezone.utc)
    balance = Decimal(r["balance"])
    return {
        "sessionId": str(r["id"]),
        "sessionNumber": r["session_number"],
        "reservationId": str(r["reservation_id"]) if r["reservation_id"] else None,
        "reservationNumber": r["reservation_number"],
        "chargerId": r["point_code"],
        "stationId": str(r["station_id"]),
        "stationName": r["station_name"],
        "location": f'{r["address"]}, {r["city"]}',
        "vehicleId": str(r["vehicle_id"]),
        "vehicleName": r["nickname"] or f'{r["manufacturer"] or ""} {r["model"]}'.strip(),
        "vehiclePlate": r["license_plate"],
        "vehicleBatteryCapacityKWh": float(r["battery_capacity_kwh"] or DEFAULT_CAPACITY),
        "vehicleStartBatteryPercentage": r["start_soc"],
        "connectorType": r["connector_type"],
        "powerKw": float(r["power_kw"]),
        "ratePerKwh": float(r["rate_per_kwh"]),
        "energyDeliveredKwh": float(r["energy_delivered_kwh"]),
        "durationSeconds": int((end - r["started_at"]).total_seconds()),
        "currentCost": float(current_cost),
        "walletRemaining": float(max(Decimal(0), balance - current_cost) if live else balance),
        "initialWalletBalance": float(balance + (0 if live or r["payment_status"] != "paid" else r["total_amount"])),
        "batteryPercentage": r["current_soc"],
        "status": r["status"].upper(),
        "paymentStatus": r["payment_status"],
        "costBreakdown": {
            "energyCost": float(r["energy_cost"]), "serviceFee": float(r["service_fee"]),
            "idleFee": float(r["idle_fee"]), "gstAmount": float(r["gst_amount"]), "total": float(current_cost),
        },
        "stopReason": r["stop_reason"],
        "startedAt": r["started_at"].isoformat(),
        "stoppedAt": r["ended_at"].isoformat() if r["ended_at"] else None,
    }


def summary_payload(cur, session_id: int) -> dict:
    p = session_payload(cur, session_id)
    secs = p["durationSeconds"]
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return {
        **{k: p[k] for k in ("sessionId", "sessionNumber", "chargerId", "stationName", "location", "vehicleName",
                             "vehiclePlate", "energyDeliveredKwh", "durationSeconds", "ratePerKwh",
                             "walletRemaining", "startedAt", "stoppedAt", "paymentStatus", "costBreakdown", "status")},
        "durationFormatted": f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s",
        "totalCost": p["currentCost"],
        "finalBatteryPercentage": p["batteryPercentage"],
    }


def finalize_session(cur, session_id: int, stop_reason: str) -> dict:
    row = db.one(cur, "SELECT * FROM charging_sessions WHERE id = %s FOR UPDATE", (session_id,))
    if row is None:
        raise not_found("Charging session")
    if row["status"] not in LIVE:
        raise ApiError(409, "This charging session has already ended.")
    refresh_live(cur, session_id)
    row = db.one(cur, "SELECT * FROM charging_sessions WHERE id = %s", (session_id,))

    tariff = _tariff(cur, row["tariff_plan_id"])
    cost = session_cost(tariff, row["energy_delivered_kwh"], Decimal(row["rate_per_kwh"]))
    status = "completed" if Decimal(row["energy_delivered_kwh"]) > 0 else "cancelled"
    cur.execute("""
        UPDATE charging_sessions
           SET status = %s, ended_at = now(), energy_cost = %s, service_fee = %s, gst_amount = %s, stop_reason = %s
         WHERE id = %s
    """, (status, cost["energy_cost"], cost["service_fee"], cost["gst_amount"], stop_reason, session_id))

    payment_status = "paid"
    if cost["total"] > 0:
        wallet = ledger.wallet_of(cur, row["customer_id"], lock=True)
        if Decimal(wallet["balance"]) >= cost["total"]:
            station = db.scalar(cur, """
                SELECT s.name FROM charging_points cp JOIN stations s ON s.id = cp.station_id WHERE cp.id = %s
            """, (row["charging_point_id"],))
            ledger.post_wallet_txn(cur, row["customer_id"], "charging_fee", "debit", cost["total"],
                                   title=f"Charging at {station}",
                                   description=f'{row["energy_delivered_kwh"]} kWh @ Rs {row["rate_per_kwh"]}/kWh',
                                   session_id=session_id)
        else:
            payment_status = "pending"
            ledger.notify(cur, row["customer_id"], "wallet", "Payment pending",
                          f'Rs {cost["total"]} for session {row["session_number"]} could not be taken from your wallet. '
                          "Please top up to clear the due amount.", route="/customer/wallet",
                          entity_type="charging_session", entity_id=session_id)
    cur.execute("UPDATE charging_sessions SET payment_status = %s WHERE id = %s", (payment_status, session_id))
    cur.execute("UPDATE vehicles SET battery_percentage = %s WHERE id = %s", (row["current_soc"], row["vehicle_id"]))
    if row["reservation_id"]:
        cur.execute("UPDATE reservations SET status = 'completed' WHERE id = %s", (row["reservation_id"],))
    if status == "completed":
        ledger.notify(cur, row["customer_id"], "charging", "Charging complete",
                      f'{row["energy_delivered_kwh"]} kWh delivered. Total Rs {cost["total"]}.',
                      route="/customer/history", entity_type="charging_session", entity_id=session_id)
    return summary_payload(cur, session_id)


def cancel_reservation(cur, reservation_id: int, reason: str) -> None:
    # refund the slot fee only if the slot hasn't started yet
    r = db.one(cur, "SELECT * FROM reservations WHERE id = %s FOR UPDATE", (reservation_id,))
    if r is None:
        raise not_found("Reservation")
    if r["status"] not in ("upcoming", "confirmed"):
        raise ApiError(409, f'A {r["status"]} reservation cannot be cancelled.')
    cur.execute("""
        UPDATE reservations SET status = 'cancelled', cancelled_at = now(), cancellation_reason = %s WHERE id = %s
    """, (reason, reservation_id))
    fee = db.one(cur, """
        SELECT t.amount FROM wallet_transactions t
        WHERE t.reservation_id = %s AND t.txn_type = 'reservation_fee' AND t.status = 'success'
          AND NOT EXISTS (SELECT 1 FROM wallet_transactions x WHERE x.reservation_id = t.reservation_id AND x.txn_type = 'refund')
    """, (reservation_id,))
    refunded = fee is not None and r["scheduled_start"] > datetime.now(timezone.utc)
    if refunded:
        ledger.post_wallet_txn(cur, r["customer_id"], "refund", "credit", fee["amount"],
                               title="Reservation slot fee refund", description=reason, reservation_id=reservation_id)
    ledger.notify(cur, r["customer_id"], "reservation", "Reservation cancelled",
                  f'Booking {r["reservation_number"]} was cancelled.' + (" Slot fee refunded to your wallet." if refunded else ""),
                  route="/customer/reservations", entity_type="reservation", entity_id=reservation_id)

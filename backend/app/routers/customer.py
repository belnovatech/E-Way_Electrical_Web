from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from .. import db
from ..errors import ApiError, not_found
from ..security import CurrentUser, require_customer
from ..services import ledger
from ..services.charging import LIVE, cancel_reservation, refresh_live, session_payload
from ..services.pricing import estimate_reservation_cost, rate_at, resolve_tariff, tariff_lateral
from ..utils import CamelModel, initials, new_code, normalize_plate, parse_connector, parse_vehicle_type
from .public import _station_rows, resolve_point

router = APIRouter(prefix="/customer", tags=["Customer"])
legacy = APIRouter(tags=["Customer"])          # paths already called by src/services/*.ts

# --- SQL fragments ---

VEHICLE_SQL = f"""
    SELECT v.id::text AS id, COALESCE(v.nickname, concat_ws(' ', v.manufacturer, v.model)) AS name,
           v.manufacturer, v.model, initcap(v.vehicle_type::text) AS type, v.license_plate AS "licensePlate",
           v.battery_capacity_kwh::float AS "batteryCapacityKWh", v.connector_type::text AS connector,
           v.manufacture_year AS year,
           (SELECT COUNT(*) FROM charging_sessions s WHERE s.vehicle_id = v.id)::int AS "chargingSessionsCount",
           COALESCE(v.battery_percentage, 0) AS "batteryPercentage", v.is_default AS "isDefault",
           v.is_verified AS "isVerified", tr.rate_per_kwh::float AS "ratePerKWh", v.created_at AS "createdAt"
    FROM vehicles v
    JOIN customer_profiles c ON c.user_id = v.owner_id
    LEFT JOIN LATERAL ({tariff_lateral("c.zone_id", "v.vehicle_type")}) tr ON true
    WHERE v.owner_id = %(uid)s AND v.deleted_at IS NULL AND (%(vid)s::bigint IS NULL OR v.id = %(vid)s)
    ORDER BY v.is_default DESC, v.created_at
"""

RESERVATION_SQL = """
    SELECT r.id::text AS id, r.reservation_number AS "reservationNumber", s.id::text AS "stationId",
           s.name AS "stationName", s.city, cp.id::text AS "chargerId", cp.point_code AS "chargerCode",
           concat(cp.connector_type, ' ', cp.current_type) AS "chargerType", cp.power_kw::float AS "powerKw",
           v.id::text AS "vehicleId", COALESCE(v.nickname, concat_ws(' ', v.manufacturer, v.model)) AS "vehicleName",
           v.license_plate AS "vehiclePlate", initcap(v.vehicle_type::text) AS "vehicleType",
           to_char(r.scheduled_start, 'YYYY-MM-DD') AS date,
           to_char(r.scheduled_start, 'FMHH12:MI AM') || ' - ' || to_char(r.scheduled_end, 'FMHH12:MI AM') AS "timeSlot",
           to_char(r.scheduled_start, 'HH24:MI') AS "startTime",
           (EXTRACT(EPOCH FROM r.scheduled_end - r.scheduled_start) / 60)::int AS "durationMinutes",
           r.estimated_cost::float AS "estimatedCostINR", r.slot_fee::float AS "slotFeeINR",
           CASE r.status WHEN 'confirmed' THEN 'upcoming' ELSE r.status::text END AS status,
           r.rate_per_kwh::float AS "ratePerKWh", r.scheduled_start AS "scheduledStart",
           r.scheduled_end AS "scheduledEnd", r.created_at AS "createdAt"
    FROM reservations r
    JOIN charging_points cp ON cp.id = r.charging_point_id
    JOIN stations s ON s.id = cp.station_id
    JOIN vehicles v ON v.id = r.vehicle_id
    WHERE r.customer_id = %(uid)s AND (%(rid)s::bigint IS NULL OR r.id = %(rid)s)
      AND (%(status)s::text IS NULL OR r.status::text = %(status)s OR (%(status)s = 'upcoming' AND r.status = 'confirmed'))
    ORDER BY r.scheduled_start DESC
"""

HISTORY_SQL = """
    SELECT cs.id::text AS id, cs.session_number AS "sessionNumber", s.name AS "stationName",
           s.address AS "stationAddress", s.city, cp.point_code AS "chargerId",
           to_char(cs.started_at, 'YYYY-MM-DD') AS date, to_char(cs.started_at, 'FMHH12:MI AM') AS "startTime",
           to_char(cs.ended_at, 'FMHH12:MI AM') AS "endTime", cs.energy_delivered_kwh::float AS "energyConsumedKWh",
           (EXTRACT(EPOCH FROM COALESCE(cs.ended_at, now()) - cs.started_at) / 60)::int AS "durationMinutes",
           cs.rate_per_kwh::float AS "ratePerKWh", cs.total_amount::float AS "totalCostINR",
           COALESCE(v.nickname, concat_ws(' ', v.manufacturer, v.model)) AS "vehicleName",
           v.license_plate AS "vehiclePlate", initcap(v.vehicle_type::text) AS "vehicleType", v.id::text AS "vehicleId",
           CASE WHEN cs.status IN ('starting','charging','stopping') THEN 'active'
                WHEN cs.status IN ('failed','faulted') THEN 'failed' ELSE cs.status::text END AS status,
           cs.payment_status AS "paymentStatus", cs.started_at AS "startedAt", cs.ended_at AS "endedAt"
    FROM charging_sessions cs
    JOIN charging_points cp ON cp.id = cs.charging_point_id
    JOIN stations s ON s.id = cp.station_id
    JOIN vehicles v ON v.id = cs.vehicle_id
    WHERE cs.customer_id = %(uid)s
      AND (%(status)s::text IS NULL OR (CASE WHEN cs.status IN ('starting','charging','stopping') THEN 'active'
                WHEN cs.status IN ('failed','faulted') THEN 'failed' ELSE cs.status::text END) = %(status)s)
      AND (%(from)s::date IS NULL OR cs.started_at >= %(from)s::date)
      AND (%(to)s::date IS NULL OR cs.started_at < %(to)s::date + 1)
      AND (%(vid)s::bigint IS NULL OR cs.vehicle_id = %(vid)s)
      AND (%(q)s::text IS NULL OR s.name ILIKE %(q)s OR s.city ILIKE %(q)s OR cp.point_code ILIKE %(q)s)
    ORDER BY cs.started_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

SUPPORT_STATUS_UI = """CASE t.status WHEN 'open' THEN 'Open' WHEN 'in_progress' THEN 'In Progress'
    WHEN 'waiting_for_customer' THEN 'Waiting for Customer' WHEN 'resolved' THEN 'Resolved' ELSE 'Closed' END"""

TICKET_SQL = f"""
    SELECT t.id::text AS id, t.ticket_number AS "ticketNumber", t.subject, t.category, t.description,
           s.name AS "stationName", cp.point_code AS "chargerId", {SUPPORT_STATUS_UI} AS status,
           t.created_at AS "createdAt", t.updated_at AS "lastUpdate",
           COALESCE((SELECT json_agg(json_build_object('id', r.id::text, 'sender', r.sender, 'message', r.message,
                                                       'timestamp', r.created_at) ORDER BY r.created_at)
                     FROM support_ticket_replies r WHERE r.ticket_id = t.id), '[]'::json) AS replies
    FROM support_tickets t
    LEFT JOIN stations s ON s.id = t.station_id
    LEFT JOIN charging_points cp ON cp.id = t.charging_point_id
    WHERE t.customer_id = %(uid)s AND (%(tid)s::bigint IS NULL OR t.id = %(tid)s)
    ORDER BY t.updated_at DESC
"""


def duration_text(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return " ".join(p for p in (f"{h} hr" if h else "", f"{m} min" if m else "") if p) or "0 min"


def _vehicles(cur, uid: int, vid: int | None = None) -> list[dict]:
    return db.all_rows(cur, VEHICLE_SQL, {"uid": uid, "vid": vid})


def _vehicle(cur, uid: int, vid: int) -> dict:
    rows = _vehicles(cur, uid, vid)
    if not rows:
        raise not_found("Vehicle")
    return rows[0]


def _reservations(cur, uid: int, rid: int | None = None, status: str | None = None) -> list[dict]:
    rows = db.all_rows(cur, RESERVATION_SQL, {"uid": uid, "rid": rid, "status": status})
    for r in rows:
        r["durationText"] = duration_text(r["durationMinutes"])
    return rows


def _profile(cur, uid: int) -> dict:
    r = db.one(cur, """
        SELECT u.id, u.full_name, u.phone_number, u.email, u.is_verified, u.created_at, u.public_code,
               c.city, c.state, c.is_profile_complete, w.balance, z.code AS zone_code, z.name AS zone_name
        FROM users u JOIN customer_profiles c ON c.user_id = u.id JOIN wallets w ON w.customer_id = u.id
        LEFT JOIN zones z ON z.id = c.zone_id WHERE u.id = %s
    """, (uid,))
    return {"id": str(r["id"]), "customerCode": r["public_code"], "name": r["full_name"] or "",
            "phoneNumber": r["phone_number"], "email": r["email"], "city": r["city"] or "", "state": r["state"] or "",
            "walletBalance": float(r["balance"]), "avatarInitials": initials(r["full_name"]),
            "isProfileComplete": r["is_profile_complete"], "memberSince": r["created_at"].isoformat(),
            "isVerified": r["is_verified"], "zoneCode": r["zone_code"], "zoneName": r["zone_name"]}


def link_home_zone(cur, uid: int, city: str | None) -> None:
    # zone comes from the city, and that zone's admin gets assigned
    current = db.one(cur, "SELECT zone_id FROM customer_profiles WHERE user_id = %s", (uid,))
    zone = db.one(cur, """
        SELECT id, state FROM zones WHERE lower(city) = lower(%s) AND status <> 'inactive' ORDER BY code LIMIT 1
    """, (city,)) if city else None
    zone_id = zone["id"] if zone else None
    if zone_id == current["zone_id"]:
        return
    admin_id = db.scalar(cur, """
        SELECT admin_id FROM admin_zone_assignments WHERE zone_id = %s AND is_active ORDER BY assigned_at LIMIT 1
    """, (zone_id,)) if zone_id else None
    cur.execute("UPDATE customer_profiles SET zone_id = %s, assigned_admin_id = %s, state = COALESCE(state, %s) "
                "WHERE user_id = %s", (zone_id, admin_id, zone["state"] if zone else None, uid))


# --- profile & settings ---

class ProfileIn(CamelModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    email: Optional[str] = Field(None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    city: Optional[str] = Field(None, max_length=80)
    state: Optional[str] = Field(None, max_length=80)


class SettingsIn(CamelModel):
    sms_notifications: Optional[bool] = None
    push_notifications: Optional[bool] = None
    email_invoices: Optional[bool] = None
    auto_stop_battery_percent: Optional[int] = Field(None, ge=50, le=100)
    language: Optional[str] = Field(None, max_length=30)
    company_name: Optional[str] = Field(None, max_length=150)
    gstin: Optional[str] = Field(None, pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


@router.get("/profile", summary="CustomerProfile")
def get_profile(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _profile(cur, user.id)


@router.patch("/profile", summary="Update profile (also used by Complete Profile)")
def update_profile(body: ProfileIn, user: CurrentUser = Depends(require_customer)):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        if "name" in data or "email" in data:
            cur.execute("""
                UPDATE users SET full_name = COALESCE(%s, full_name),
                                 email = CASE WHEN %s THEN NULLIF(lower(%s::text), '') ELSE email END
                WHERE id = %s
            """, (data.get("name"), "email" in data, data.get("email"), user.id))
        if "city" in data or "state" in data:
            cur.execute("UPDATE customer_profiles SET city = COALESCE(%s, city), state = COALESCE(%s, state) "
                        "WHERE user_id = %s", (data.get("city"), data.get("state"), user.id))
            if "city" in data:
                link_home_zone(cur, user.id, data["city"])
        cur.execute("""
            UPDATE customer_profiles SET is_profile_complete = (SELECT full_name IS NOT NULL FROM users WHERE id = %s)
            WHERE user_id = %s
        """, (user.id, user.id))
        return _profile(cur, user.id)


@router.get("/settings")
def get_settings_(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return db.one(cur, """
            SELECT sms_notifications AS "smsNotifications", push_notifications AS "pushNotifications",
                   email_invoices AS "emailInvoices", auto_stop_battery_percent AS "autoStopBatteryPercent",
                   preferred_language AS language, company_name AS "companyName", gstin
            FROM customer_profiles WHERE user_id = %s
        """, (user.id,))


@router.put("/settings")
def update_settings(body: SettingsIn, user: CurrentUser = Depends(require_customer)):
    cols = {"sms_notifications": "sms_notifications", "push_notifications": "push_notifications",
            "email_invoices": "email_invoices", "auto_stop_battery_percent": "auto_stop_battery_percent",
            "language": "preferred_language", "company_name": "company_name", "gstin": "gstin"}
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        for key, value in data.items():
            cur.execute(f"UPDATE customer_profiles SET {cols[key]} = %s WHERE user_id = %s", (value, user.id))
    return get_settings_(user)


# --- dashboard ---

@router.get("/dashboard", summary="CustomerDashboardData")
def dashboard(lat: Optional[float] = None, lng: Optional[float] = None, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        live_id = db.scalar(cur, "SELECT id FROM charging_sessions WHERE customer_id = %s AND status = ANY(%s)",
                            (user.id, list(LIVE)))
        if live_id:
            refresh_live(cur, live_id)
        profile = _profile(cur, user.id)
        active = None
        if live_id:
            s = session_payload(cur, live_id)
            active = {"sessionId": s["sessionId"], "stationName": s["stationName"], "energyKWh": s["energyDeliveredKwh"],
                      "durationMin": s["durationSeconds"] // 60, "currentCostINR": s["currentCost"],
                      "batteryLevel": s["batteryPercentage"]}
        last = db.one(cur, """
            SELECT total_amount::float AS "amountINR", energy_delivered_kwh::float AS "energyKWh",
                   (EXTRACT(EPOCH FROM ended_at - started_at) / 60)::int AS "durationMin"
            FROM charging_sessions WHERE customer_id = %s AND status = 'completed' ORDER BY ended_at DESC LIMIT 1
        """, (user.id,))
        month_kwh = db.scalar(cur, """
            SELECT COALESCE(SUM(energy_delivered_kwh), 0)::float FROM charging_sessions
            WHERE customer_id = %s AND started_at >= date_trunc('month', now())
        """, (user.id,))
        unread = db.scalar(cur, "SELECT COUNT(*)::int FROM notifications WHERE user_id = %s AND NOT is_read", (user.id,))
        vehicles = _vehicles(cur, user.id)
        default_vt = parse_vehicle_type(vehicles[0]["type"]) if vehicles else None
        stations = _station_rows(cur, lat=lat if lat is not None else 16.5062, lng=lng if lng is not None else 80.6480,
                                 vehicle_type=default_vt, limit=5)
    return {
        "profile": profile, "walletBalance": profile["walletBalance"], "activeSession": active, "lastCharging": last,
        "totalEnergyThisMonthKWh": month_kwh, "vehicles": vehicles,
        "nearbyStations": [{"id": s["id"], "name": s["name"], "city": s["city"], "distanceKm": s["distanceKm"],
                            "availablePoints": s["availableChargers"], "totalPoints": s["totalChargers"],
                            "powerKw": s["powerKw"], "ratePerKWh": s["ratePerKWh"]} for s in stations],
        "unreadNotificationsCount": unread,
    }


ICONS = {"Car": "🚗", "Bike": "🛵", "Bus": "🚌"}


def _dashboard_vehicle(v: dict) -> dict:
    return {"id": v["id"], "name": v["name"], "type": v["type"], "ratePerKWh": v["ratePerKWh"],
            "batteryPercentage": v["batteryPercentage"], "iconEmoji": ICONS.get(v["type"], "🚗"),
            "licensePlate": v["licensePlate"]}


@legacy.get("/vehicles", summary="Vehicle[] (src/services/vehicleService.ts)")
def legacy_vehicles(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return [_dashboard_vehicle(v) for v in _vehicles(cur, user.id)]


@legacy.get("/dashboard/summary", summary="DashboardSummary (src/services/dashboardService.ts)")
def legacy_dashboard_summary(user: CurrentUser = Depends(require_customer)):
    d = dashboard(user=user)
    return {
        "walletBalance": d["walletBalance"],
        "availablePointsNearby": sum(s["availablePoints"] for s in d["nearbyStations"]),
        "currentVehicle": _dashboard_vehicle(d["vehicles"][0]) if d["vehicles"] else None,
        "nearbyZones": [{"id": s["id"], "name": s["name"], "city": s["city"], "distanceKm": s["distanceKm"],
                         "etaMinutes": round((s["distanceKm"] or 0) / 30 * 60) + 2,
                         "status": "available" if s["availablePoints"] else "busy",
                         "availablePointsCount": s["availablePoints"]} for s in d["nearbyStations"]],
    }


# --- vehicles ---

class VehicleIn(CamelModel):
    name: Optional[str] = Field(None, max_length=80)
    manufacturer: Optional[str] = Field(None, max_length=60)
    model: str = Field(min_length=1, max_length=80)
    type: str
    license_plate: str = Field(min_length=4, max_length=20)
    battery_capacity_kwh: Optional[float] = Field(None, alias="batteryCapacityKWh", gt=0, le=1000)
    connector: Optional[str] = None
    battery_percentage: Optional[int] = Field(None, ge=0, le=100)
    is_default: bool = False
    year: Optional[int] = Field(None, ge=2000, le=2100)


class VehicleUpdateIn(CamelModel):
    name: Optional[str] = Field(None, max_length=80)
    manufacturer: Optional[str] = Field(None, max_length=60)
    model: Optional[str] = Field(None, min_length=1, max_length=80)
    type: Optional[str] = None
    license_plate: Optional[str] = Field(None, min_length=4, max_length=20)
    battery_capacity_kwh: Optional[float] = Field(None, alias="batteryCapacityKWh", gt=0, le=1000)
    connector: Optional[str] = None
    battery_percentage: Optional[int] = Field(None, ge=0, le=100)
    is_default: Optional[bool] = None
    year: Optional[int] = Field(None, ge=2000, le=2100)


def _set_default_vehicle(cur, uid: int, vid: int) -> None:
    cur.execute("UPDATE vehicles SET is_default = false WHERE owner_id = %s AND is_default AND id <> %s", (uid, vid))
    cur.execute("UPDATE vehicles SET is_default = true WHERE id = %s", (vid,))


@router.get("/vehicles", summary="CustomerVehicle[]")
def list_vehicles(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _vehicles(cur, user.id)


@router.post("/vehicles", status_code=201)
def add_vehicle(body: VehicleIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        first = not db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM vehicles WHERE owner_id = %s AND deleted_at IS NULL)",
                              (user.id,))
        row = db.one(cur, """
            INSERT INTO vehicles (owner_id, nickname, manufacturer, model, manufacture_year, vehicle_type, license_plate,
                                  battery_capacity_kwh, connector_type, battery_percentage)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (user.id, body.name, body.manufacturer, body.model, body.year, parse_vehicle_type(body.type),
              normalize_plate(body.license_plate), body.battery_capacity_kwh, parse_connector(body.connector),
              body.battery_percentage))
        if first or body.is_default:
            _set_default_vehicle(cur, user.id, row["id"])
        return _vehicle(cur, user.id, row["id"])


@router.patch("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: int, body: VehicleUpdateIn, user: CurrentUser = Depends(require_customer)):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        _vehicle(cur, user.id, vehicle_id)
        mapping = {
            "name": ("nickname", lambda v: v), "manufacturer": ("manufacturer", lambda v: v), "model": ("model", lambda v: v),
            "year": ("manufacture_year", lambda v: v), "type": ("vehicle_type", parse_vehicle_type),
            "license_plate": ("license_plate", normalize_plate), "battery_capacity_kwh": ("battery_capacity_kwh", lambda v: v),
            "connector": ("connector_type", parse_connector), "battery_percentage": ("battery_percentage", lambda v: v),
        }
        for key, value in data.items():
            if key in mapping:
                col, conv = mapping[key]
                cur.execute(f"UPDATE vehicles SET {col} = %s WHERE id = %s", (None if value is None else conv(value), vehicle_id))
        if data.get("is_default"):
            _set_default_vehicle(cur, user.id, vehicle_id)
        return _vehicle(cur, user.id, vehicle_id)


@router.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        v = _vehicle(cur, user.id, vehicle_id)
        if db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM charging_sessions WHERE vehicle_id = %s AND status = ANY(%s))",
                     (vehicle_id, list(LIVE))):
            raise ApiError(409, "This vehicle is charging right now. Stop the session first.")
        if db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM reservations WHERE vehicle_id = %s AND status IN ('upcoming','confirmed'))",
                     (vehicle_id,)):
            raise ApiError(409, "This vehicle has upcoming reservations. Cancel them first.")
        cur.execute("UPDATE vehicles SET deleted_at = now(), is_default = false WHERE id = %s", (vehicle_id,))
        if v["isDefault"]:
            nxt = db.scalar(cur, "SELECT id FROM vehicles WHERE owner_id = %s AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
                            (user.id,))
            if nxt:
                _set_default_vehicle(cur, user.id, nxt)
    return {"success": True}


@router.post("/vehicles/{vehicle_id}/default")
def set_default_vehicle(vehicle_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        _vehicle(cur, user.id, vehicle_id)
        _set_default_vehicle(cur, user.id, vehicle_id)
        return _vehicle(cur, user.id, vehicle_id)


# --- reservations ---

class ReservationIn(CamelModel):
    charger_id: str = Field(min_length=1, description="Charging point id or point code")
    vehicle_id: int
    date: Date
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(ge=15, le=240)


def check_vehicle_fits(point: dict, vehicle: dict) -> None:
    vt = parse_vehicle_type(vehicle["type"])
    if vt not in point["supported_vehicle_types"]:
        raise ApiError(422, f'{point["station_name"]} does not support {vehicle["type"].lower()} charging.')
    if vehicle["connector"] and vehicle["connector"] != point["connector_type"]:
        raise ApiError(422, f'Your vehicle uses {vehicle["connector"]} but this charger has a {point["connector_type"]} connector.')


@router.get("/reservations", summary="CustomerReservation[]")
def list_reservations(status: Optional[Literal["upcoming", "active", "completed", "cancelled"]] = None,
                      user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _reservations(cur, user.id, status=status)


@router.post("/reservations", status_code=201)
def create_reservation(body: ReservationIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        point = resolve_point(cur, body.charger_id, lock=True)
        vehicle = _vehicle(cur, user.id, body.vehicle_id)
        check_vehicle_fits(point, vehicle)
        times = db.one(cur, """
            SELECT start_at, start_at + make_interval(mins => %(dur)s) AS end_at,
                   start_at < now() - interval '5 minutes' AS in_past, start_at > now() + interval '30 days' AS too_far
            FROM (SELECT (%(d)s::date + %(t)s::time) AT TIME ZONE 'Asia/Kolkata' AS start_at) x
        """, {"d": body.date, "t": body.start_time, "dur": body.duration_minutes})
        if times["in_past"]:
            raise ApiError(422, "The selected slot is in the past.")
        if times["too_far"]:
            raise ApiError(422, "Reservations can be made up to 30 days ahead.")

        tariff = resolve_tariff(cur, point["zone_id"], parse_vehicle_type(vehicle["type"]))
        rate = rate_at(tariff, times["start_at"])
        estimate = estimate_reservation_cost(body.duration_minutes, rate)
        slot_fee = Decimal(tariff["reservation_slot_fee"])
        balance = Decimal(ledger.wallet_of(cur, user.id, lock=True)["balance"])
        if balance < estimate + slot_fee:
            raise ApiError(402, f"Insufficient wallet balance (Rs {balance}). Please top up at least "
                                f"Rs {estimate + slot_fee - balance} to book this slot.", "insufficient_balance")
        row = db.one(cur, """
            INSERT INTO reservations (reservation_number, customer_id, vehicle_id, charging_point_id, tariff_plan_id,
                                      scheduled_start, scheduled_end, rate_per_kwh, slot_fee, estimated_cost, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmed') RETURNING id, reservation_number
        """, (new_code("RSV"), user.id, body.vehicle_id, point["id"], tariff["id"], times["start_at"], times["end_at"],
              rate, slot_fee, estimate))
        if slot_fee > 0:
            ledger.post_wallet_txn(cur, user.id, "reservation_fee", "debit", slot_fee, title="Reservation slot fee",
                                   description=f'{point["station_name"]} - {point["point_code"]}', reservation_id=row["id"])
        res = _reservations(cur, user.id, row["id"])[0]
        ledger.notify(cur, user.id, "reservation", "Reservation confirmed",
                      f'{res["stationName"]} on {res["date"]}, {res["timeSlot"]}.', route="/customer/reservations",
                      entity_type="reservation", entity_id=row["id"])
        return res


@router.get("/reservations/{reservation_id}")
def get_reservation(reservation_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        rows = _reservations(cur, user.id, reservation_id)
    if not rows:
        raise not_found("Reservation")
    return rows[0]


@router.post("/reservations/{reservation_id}/cancel")
def cancel_my_reservation(reservation_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        if not _reservations(cur, user.id, reservation_id):
            raise not_found("Reservation")
        cancel_reservation(cur, reservation_id, "Cancelled by customer")
        return _reservations(cur, user.id, reservation_id)[0]


# --- history ---

@router.get("/charging-history", summary="ChargingHistoryItem[]")
def charging_history(status: Optional[str] = None, from_date: Optional[Date] = Query(None, alias="from"),
                     to_date: Optional[Date] = Query(None, alias="to"), vehicle_id: Optional[int] = Query(None, alias="vehicleId"),
                     search: Optional[str] = None, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return db.all_rows(cur, HISTORY_SQL, {"uid": user.id, "status": status, "from": from_date, "to": to_date,
                                              "vid": vehicle_id, "q": f"%{search}%" if search else None,
                                              "limit": limit, "offset": offset})


# --- support ---

class TicketIn(CamelModel):
    subject: str = Field(min_length=3, max_length=200)
    category: str = Field(min_length=2, max_length=60)
    description: str = Field(min_length=5, max_length=5000)
    station_name: Optional[str] = Field(None, max_length=150)
    station_id: Optional[int] = None
    charger_id: Optional[str] = Field(None, max_length=30)


class ReplyIn(CamelModel):
    message: str = Field(min_length=1, max_length=5000)


class TicketStatusIn(CamelModel):
    status: Literal["Resolved", "Closed", "resolved", "closed"]


def _tickets(cur, uid: int, tid: int | None = None) -> list[dict]:
    return db.all_rows(cur, TICKET_SQL, {"uid": uid, "tid": tid})


def _ticket(cur, uid: int, tid: int) -> dict:
    rows = _tickets(cur, uid, tid)
    if not rows:
        raise not_found("Support ticket")
    return rows[0]


@router.get("/support-tickets", summary="SupportTicket[]")
def list_tickets(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _tickets(cur, user.id)


@router.post("/support-tickets", status_code=201)
def create_ticket(body: TicketIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        station_id, point_id, description = body.station_id, None, body.description
        if station_id is None and body.station_name:
            station_id = db.scalar(cur, "SELECT id FROM stations WHERE lower(name) = lower(%s) LIMIT 1", (body.station_name,))
            if station_id is None:
                description += f"\n\nStation: {body.station_name}"
        if body.charger_id:
            p = db.one(cur, "SELECT id, station_id FROM charging_points WHERE upper(point_code) = upper(%s)", (body.charger_id,))
            if p and (station_id is None or station_id == p["station_id"]):
                point_id, station_id = p["id"], p["station_id"]
            else:
                description += f"\n\nCharger: {body.charger_id}"
        row = db.one(cur, """
            INSERT INTO support_tickets (ticket_number, customer_id, category, subject, description, station_id, charging_point_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (new_code("TKT"), user.id, body.category, body.subject, description, station_id, point_id))
        return _ticket(cur, user.id, row["id"])


@router.post("/support-tickets/{ticket_id}/replies")
def reply_ticket(ticket_id: int, body: ReplyIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        t = _ticket(cur, user.id, ticket_id)
        if t["status"] == "Closed":
            raise ApiError(409, "This ticket is closed. Please open a new ticket.")
        cur.execute("INSERT INTO support_ticket_replies (ticket_id, sender, sender_user_id, message) VALUES (%s, 'customer', %s, %s)",
                    (ticket_id, user.id, body.message))
        cur.execute("""
            UPDATE support_tickets SET status = CASE WHEN status IN ('waiting_for_customer', 'resolved') THEN 'in_progress'
                                                     ELSE status END, resolved_at = NULL, updated_at = now()
            WHERE id = %s
        """, (ticket_id,))
        return _ticket(cur, user.id, ticket_id)


@router.patch("/support-tickets/{ticket_id}")
def close_ticket(ticket_id: int, body: TicketStatusIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        _ticket(cur, user.id, ticket_id)
        cur.execute("UPDATE support_tickets SET status = %s, resolved_at = COALESCE(resolved_at, now()) WHERE id = %s",
                    (body.status.lower(), ticket_id))
        return _ticket(cur, user.id, ticket_id)

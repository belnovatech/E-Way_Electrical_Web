# Admin console endpoints (src/admin).
# Zone managers and field engineers only see their own zones, see security.load_scope.
from __future__ import annotations

import csv
import json
import io
import re
from datetime import date as Date
from decimal import Decimal
from typing import Literal, Optional, Union

import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from .. import db
from ..errors import ApiError, not_found
from ..security import CurrentUser, require_permission, require_staff
from ..services import ledger
from ..services.charging import LIVE, cancel_reservation, finalize_session, refresh_live
from ..services.pricing import tariff_lateral
from ..utils import CamelModel, mask_email, mask_phone, money, parse_connector, parse_enum, parse_vehicle_type
from .public import resolve_point

router = APIRouter(prefix="/admin", tags=["Admin"])


def zf(col: str) -> str:
    return f"(%(zones)s::bigint[] IS NULL OR {col} = ANY(%(zones)s::bigint[]))"


def _check_zone(user: CurrentUser, zone_id: int | None, what: str = "Zone") -> None:
    if not user.in_scope(zone_id):
        raise not_found(what)


def _refresh_live_in_scope(cur, user: CurrentUser) -> None:
    for row in db.all_rows(cur, f"""
        SELECT cs.id FROM charging_sessions cs JOIN charging_points cp ON cp.id = cs.charging_point_id
        JOIN stations s ON s.id = cp.station_id WHERE cs.status = ANY(%(live)s) AND {zf("s.zone_id")}
    """, {"live": list(LIVE), "zones": user.zone_ids}):
        refresh_live(cur, row["id"])


# --- dashboard ---

ZONE_SQL = f"""
    SELECT z.id::text AS id, z.code, z.name, z.address, z.city, z.state,
           v.total_chargers::int AS "totalChargers", v.available_chargers::int AS "availableChargers",
           v.charging_count::int AS "chargingCount", v.reserved_count::int AS "reservedCount",
           v.maintenance_count::int AS "maintenanceCount", v.offline_count::int AS "offlineCount",
           z.power_capacity_kw::float AS "powerCapacityKw", z.current_load_kw::float AS "currentLoadKw",
           v.today_revenue::float AS "todayRevenueINR", z.operating_hours AS "operatingHours",
           z.contact_number AS "contactNumber", initcap(z.status::text) AS status,
           z.latitude::float AS lat, z.longitude::float AS lng,
           (SELECT COUNT(*) FROM stations st WHERE st.zone_id = z.id)::int AS "stationsCount",
           v.assigned_admins AS "assignedAdmins"
    FROM zones z JOIN v_zone_overview v ON v.id = z.id
    WHERE {zf("z.id")} AND (%(zid)s::bigint IS NULL OR z.id = %(zid)s)
    ORDER BY z.code
"""


def _zones(cur, user: CurrentUser, zone_id: int | None = None) -> list[dict]:
    return db.all_rows(cur, ZONE_SQL, {"zones": user.zone_ids, "zid": zone_id})


SESSIONS_CTE = f"""
    WITH ss AS (
        SELECT cs.*, s.zone_id, v.vehicle_type FROM charging_sessions cs
        JOIN charging_points cp ON cp.id = cs.charging_point_id JOIN stations s ON s.id = cp.station_id
        JOIN vehicles v ON v.id = cs.vehicle_id
        WHERE {zf("s.zone_id")}
    )
"""


@router.get("/dashboard", summary="AdminDashboardOverview")
def dashboard(user: CurrentUser = Depends(require_permission("VIEW_ZONES", "VIEW_REPORTS", "VIEW_REVENUE"))):
    p = {"zones": user.zone_ids, "live": list(LIVE)}
    with db.tx() as cur:
        _refresh_live_in_scope(cur, user)
        zones = _zones(cur, user)
        m = db.one(cur, SESSIONS_CTE + """
            SELECT COALESCE(SUM(total_amount) FILTER (WHERE started_at >= date_trunc('day', now())), 0) AS today,
                   COALESCE(SUM(total_amount) FILTER (WHERE started_at >= date_trunc('day', now()) - interval '1 day'
                                                        AND started_at < date_trunc('day', now())), 0) AS yesterday,
                   COUNT(*) FILTER (WHERE status = ANY(%(live)s)) AS active,
                   COALESCE(SUM(energy_cost) FILTER (WHERE status = ANY(%(live)s)), 0) AS live_value,
                   COALESCE(SUM(energy_delivered_kwh) FILTER (WHERE started_at >= date_trunc('day', now())), 0) AS energy_today,
                   COALESCE(SUM(total_amount) FILTER (WHERE started_at >= date_trunc('month', now())), 0) AS month
            FROM ss
        """, p)
        trend = db.all_rows(cur, SESSIONS_CTE + """
            SELECT to_char(mo, 'Mon YYYY') AS month, COALESCE(SUM(ss.total_amount), 0)::float AS revenue,
                   COALESCE(SUM(ss.energy_delivered_kwh), 0)::float AS "energyKWh"
            FROM generate_series(date_trunc('month', now()) - interval '5 months', date_trunc('month', now()), interval '1 month') mo
            LEFT JOIN ss ON date_trunc('month', ss.started_at) = mo
            GROUP BY mo ORDER BY mo
        """, p)
        dist = db.all_rows(cur, SESSIONS_CTE + """
            SELECT upper(vehicle_type::text) AS category, COUNT(*)::int AS count FROM ss
            WHERE started_at >= now() - interval '90 days' GROUP BY vehicle_type ORDER BY count DESC
        """, p)
    total_dist = sum(d["count"] for d in dist) or 1
    today, yday = Decimal(m["today"]), Decimal(m["yesterday"])
    return {
        "totalZones": len(zones),
        "totalChargingPoints": sum(z["totalChargers"] for z in zones),
        "availablePoints": sum(z["availableChargers"] for z in zones),
        "chargingPoints": sum(z["chargingCount"] for z in zones),
        "reservedPoints": sum(z["reservedCount"] for z in zones),
        "maintenancePoints": sum(z["maintenanceCount"] + z["offlineCount"] for z in zones),
        "todayRevenueINR": float(today),
        "todayRevenueGrowthPercent": float(round((today - yday) / yday * 100, 1)) if yday else None,
        "activeSessionsCount": m["active"], "activeSessionsLiveValueINR": float(m["live_value"]),
        "energyDeliveredTodayKWh": float(m["energy_today"]), "monthlyRevenueINR": float(m["month"]),
        "revenueTrend": trend,
        "vehicleDistribution": [{**d, "percentage": round(d["count"] / total_dist * 100, 1)} for d in dist],
        "zonesSummary": zones,
    }


# --- zones & stations ---

class ZoneIn(CamelModel):
    code: str = Field(pattern=r"^[A-Z]{2}-Z\d{2,3}$")
    name: str = Field(min_length=3, max_length=120)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field("Andhra Pradesh", max_length=80)
    address: Optional[str] = Field(None, max_length=300)
    operating_hours: str = Field("24/7", max_length=60)
    contact_number: Optional[str] = Field(None, max_length=20)
    power_capacity_kw: float = Field(0, ge=0)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)


class ZoneUpdateIn(CamelModel):
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    address: Optional[str] = Field(None, max_length=300)
    operating_hours: Optional[str] = Field(None, max_length=60)
    contact_number: Optional[str] = Field(None, max_length=20)
    power_capacity_kw: Optional[float] = Field(None, ge=0)
    status: Optional[str] = None


class StationIn(CamelModel):
    name: str = Field(min_length=3, max_length=150)
    code: Optional[str] = Field(None, max_length=30)
    address: str = Field(min_length=3, max_length=300)
    city: Optional[str] = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    operating_hours: str = Field("Open 24/7", max_length=60)
    amenities: list[str] = []
    vehicle_types: list[str] = ["car"]


def _create_station(cur, zone: dict, body: StationIn) -> int:
    vts = [parse_vehicle_type(v) for v in body.vehicle_types] or ["car"]
    code = body.code or f'ST-{zone["code"]}-{db.scalar(cur, "SELECT COUNT(*) + 1 FROM stations WHERE zone_id = %s", (zone["id"],)):02d}'
    return db.scalar(cur, """
        INSERT INTO stations (zone_id, code, name, address, city, latitude, longitude, operating_hours, amenities,
                              supported_vehicle_types)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vehicle_type[]) RETURNING id
    """, (zone["id"], code, body.name, body.address, body.city or zone["city"], body.latitude, body.longitude,
          body.operating_hours, body.amenities, vts))


@router.get("/zones", summary="AdminZone[]")
def list_zones(user: CurrentUser = Depends(require_permission("VIEW_ZONES"))):
    with db.tx() as cur:
        return _zones(cur, user)


@router.get("/zones/{zone_id}")
def get_zone(zone_id: int, user: CurrentUser = Depends(require_permission("VIEW_ZONES"))):
    with db.tx() as cur:
        rows = _zones(cur, user, zone_id)
    if not rows:
        raise not_found("Zone")
    return rows[0]


@router.post("/zones", status_code=201)
def create_zone(body: ZoneIn, request: Request, user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    if not user.is_global:
        raise ApiError(403, "Only network-wide administrators can create zones.")
    with db.tx() as cur:
        zone = db.one(cur, """
            INSERT INTO zones (code, name, address, city, state, latitude, longitude, power_capacity_kw, operating_hours, contact_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """, (body.code, body.name, body.address, body.city, body.state, body.latitude, body.longitude,
              body.power_capacity_kw, body.operating_hours, body.contact_number))
        if body.latitude is not None and body.longitude is not None:
            _create_station(cur, zone, StationIn(name=f"{body.name} Hub", address=body.address or body.city,
                                                 latitude=body.latitude, longitude=body.longitude, vehicle_types=["bike", "car", "bus"]))
        ledger.audit(cur, user, request, action=f"Created zone {body.code}", action_type="Created", module="Zones",
                     target=f"Zone {body.code}", record_id=zone["id"], new=body.model_dump())
        return _zones(cur, user, zone["id"])[0]


@router.patch("/zones/{zone_id}")
def update_zone(zone_id: int, body: ZoneUpdateIn, request: Request,
                user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    _check_zone(user, zone_id)
    data = body.model_dump(exclude_unset=True)
    if "status" in data:
        data["status"] = parse_enum("active" if data["status"].lower() == "online" else data["status"],
                                    {"active", "degraded", "offline", "inactive"}, "status")
    with db.tx() as cur:
        before = db.one(cur, "SELECT * FROM zones WHERE id = %s FOR UPDATE", (zone_id,))
        if before is None:
            raise not_found("Zone")
        for key, value in data.items():
            cur.execute(f"UPDATE zones SET {key} = %s WHERE id = %s", (value, zone_id))
        ledger.audit(cur, user, request, action=f'Updated zone {before["code"]}', action_type="Updated", module="Zones",
                     target=f'Zone {before["code"]}', record_id=zone_id,
                     previous={k: before[k] for k in data}, new=data)
        return _zones(cur, user, zone_id)[0]


@router.get("/stations")
def list_stations(zone_id: Optional[int] = Query(None, alias="zoneId"),
                  user: CurrentUser = Depends(require_permission("VIEW_ZONES"))):
    with db.tx() as cur:
        return db.all_rows(cur, f"""
            SELECT s.id::text AS id, s.code, s.name, s.address, s.city, z.id::text AS "zoneId", z.code AS "zoneCode",
                   s.latitude::float AS lat, s.longitude::float AS lng, s.operating_hours AS "operatingHours",
                   s.amenities, s.supported_vehicle_types::text[] AS "vehicleTypes", initcap(s.status::text) AS status,
                   (SELECT COUNT(*) FROM charging_points cp WHERE cp.station_id = s.id)::int AS "totalChargers"
            FROM stations s JOIN zones z ON z.id = s.zone_id
            WHERE {zf("z.id")} AND (%(zid)s::bigint IS NULL OR z.id = %(zid)s) ORDER BY z.code, s.name
        """, {"zones": user.zone_ids, "zid": zone_id})


@router.post("/zones/{zone_id}/stations", status_code=201)
def create_station(zone_id: int, body: StationIn, request: Request,
                   user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    _check_zone(user, zone_id)
    with db.tx() as cur:
        zone = db.one(cur, "SELECT * FROM zones WHERE id = %s", (zone_id,))
        if zone is None:
            raise not_found("Zone")
        sid = _create_station(cur, zone, body)
        ledger.audit(cur, user, request, action=f"Added station {body.name}", action_type="Created", module="Zones",
                     target=f'Zone {zone["code"]}', record_id=sid)
    return {"id": str(sid), "success": True}


# --- charging points ---

POINT_SQL = f"""
    SELECT cp.id::text AS id, cp.point_code AS "pointCode", cp.display_name AS "displayName",
           s.id::text AS "stationId", s.name AS "stationName", z.id::text AS "zoneId", z.code AS "zoneCode",
           z.name AS "zoneName", s.city, cp.current_type::text AS "currentType",
           CASE cp.current_type WHEN 'DC' THEN 'DC (Fast)' ELSE 'AC (Standard)' END AS type,
           CASE cp.current_type WHEN 'DC' THEN 'DC (Fast)' ELSE 'AC (Standard)' END AS "chargerType",
           cp.connector_type::text AS "connectorType", cp.power_kw::float AS "powerKw", initcap(cp.status::text) AS status,
           tr.rate_per_kwh::float AS "ratePerKWh",
           (SELECT LEAST(100, ROUND(COALESCE(SUM(EXTRACT(EPOCH FROM LEAST(COALESCE(x.ended_at, now()), now())
                    - GREATEST(x.started_at, now() - interval '7 days'))), 0) / (7 * 24 * 36)))::float
              FROM charging_sessions x WHERE x.charging_point_id = cp.id
               AND COALESCE(x.ended_at, now()) > now() - interval '7 days') AS "utilizationPercent",
           cp.last_ping_at AS "lastPingTime", cp.charger_number AS "chargerNumber", cp.ocpp_id AS "ocppId",
           cp.last_maintenance_at AS "lastMaintenanceDate",
           (SELECT json_build_object('sessionId', cs.id::text, 'sessionNumber', cs.session_number,
                    'vehiclePlate', v.license_plate, 'vehicleModel', v.model, 'energyDeliveredKWh', cs.energy_delivered_kwh,
                    'durationMinutes', (EXTRACT(EPOCH FROM now() - cs.started_at) / 60)::int,
                    'currentCostINR', cs.energy_cost, 'batteryLevel', cs.current_soc, 'driverName', u.full_name)
              FROM charging_sessions cs JOIN vehicles v ON v.id = cs.vehicle_id JOIN users u ON u.id = cs.customer_id
             WHERE cs.charging_point_id = cp.id AND cs.status IN ('starting', 'charging', 'stopping')) AS "activeSession",
           (SELECT COUNT(*) FROM maintenance_tickets t WHERE t.charging_point_id = cp.id AND t.status <> 'resolved')::int AS "openTickets"
    FROM charging_points cp JOIN stations s ON s.id = cp.station_id JOIN zones z ON z.id = s.zone_id
    LEFT JOIN LATERAL ({tariff_lateral("s.zone_id", "'car'::vehicle_type")}) tr ON true
    WHERE {zf("z.id")}
      AND (%(pid)s::bigint IS NULL OR cp.id = %(pid)s)
      AND (%(zid)s::bigint IS NULL OR z.id = %(zid)s)
      AND (%(sid)s::bigint IS NULL OR s.id = %(sid)s)
      AND (%(status)s::text IS NULL OR cp.status::text = %(status)s)
    ORDER BY z.code, s.name, cp.charger_number
"""

POINT_STATUSES = {"available", "charging", "reserved", "maintenance", "offline"}


def _points(cur, user: CurrentUser, *, pid=None, zid=None, sid=None, status=None) -> list[dict]:
    return db.all_rows(cur, POINT_SQL, {"zones": user.zone_ids, "pid": pid, "zid": zid, "sid": sid, "status": status})


def _point(cur, user: CurrentUser, pid: int) -> dict:
    rows = _points(cur, user, pid=pid)
    if not rows:
        raise not_found("Charging point")
    return rows[0]


class PointIn(CamelModel):
    station_id: Optional[int] = None
    zone_id: Optional[int] = None
    zone_code: Optional[str] = None
    point_code: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9-]{3,30}$")
    charger_number: Optional[Union[int, str]] = None
    type: str = "DC"
    power_kw: Union[float, str]
    connector_type: str = "CCS2"
    ocpp_id: Optional[str] = Field(None, max_length=50)
    display_name: Optional[str] = Field(None, max_length=80)


class PointUpdateIn(CamelModel):
    status: Optional[str] = None
    power_kw: Optional[Union[float, str]] = None
    connector_type: Optional[str] = None
    ocpp_id: Optional[str] = Field(None, max_length=50)
    display_name: Optional[str] = Field(None, max_length=80)
    type: Optional[str] = None


def _power(value) -> float:
    m = re.search(r"\d+(\.\d+)?", str(value))
    if not m or not 0 < float(m.group()) <= 1000:
        raise ApiError(422, "powerKw must be a number between 1 and 1000.")
    return float(m.group())


@router.get("/charging-points", summary="AdminChargingPoint[]")
def list_points(zone_id: Optional[int] = Query(None, alias="zoneId"), station_id: Optional[int] = Query(None, alias="stationId"),
                status: Optional[str] = None, user: CurrentUser = Depends(require_permission("VIEW_ZONES", "VIEW_SESSIONS"))):
    st = parse_enum(status, POINT_STATUSES, "status") if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        _refresh_live_in_scope(cur, user)
        return _points(cur, user, zid=zone_id, sid=station_id, status=st)


@router.get("/charging-points/{point_id}")
def get_point(point_id: int, user: CurrentUser = Depends(require_permission("VIEW_ZONES", "VIEW_SESSIONS"))):
    with db.tx() as cur:
        return _point(cur, user, point_id)


@router.post("/charging-points", status_code=201)
def create_point(body: PointIn, request: Request, user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    with db.tx() as cur:
        if body.station_id is not None:
            station = db.one(cur, "SELECT s.*, z.code AS zone_code FROM stations s JOIN zones z ON z.id = s.zone_id WHERE s.id = %s",
                             (body.station_id,))
        else:
            zone = db.one(cur, "SELECT * FROM zones WHERE id = %s OR code = %s",
                          (body.zone_id or 0, (body.zone_code or "").upper()))
            if zone is None:
                raise ApiError(422, "Provide stationId, zoneId or zoneCode.")
            stations = db.all_rows(cur, "SELECT s.*, %s AS zone_code FROM stations s WHERE zone_id = %s",
                                   (zone["code"], zone["id"]))
            if len(stations) != 1:
                raise ApiError(422, "Add a station to this zone first." if not stations
                               else "This zone has several stations; provide stationId.")
            station = stations[0]
        if station is None:
            raise not_found("Station")
        _check_zone(user, station["zone_id"], "Station")

        number = int(body.charger_number) if body.charger_number not in (None, "") else \
            db.scalar(cur, "SELECT COALESCE(MAX(charger_number), 0) + 1 FROM charging_points WHERE station_id = %s", (station["id"],))
        code = (body.point_code or "").upper()
        if not code:
            n = db.scalar(cur, """SELECT COUNT(*) FROM charging_points cp JOIN stations s ON s.id = cp.station_id
                                  WHERE s.zone_id = %s""", (station["zone_id"],)) + 1
            while db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM charging_points WHERE point_code = %s)", (f'{station["zone_code"]}-CP{n:02d}',)):
                n += 1
            code = f'{station["zone_code"]}-CP{n:02d}'
        current = "DC" if "dc" in body.type.lower() else "AC"
        power = _power(body.power_kw)
        pid = db.scalar(cur, """
            INSERT INTO charging_points (station_id, point_code, charger_number, display_name, current_type, connector_type,
                                         power_kw, ocpp_id, commissioned_on)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, current_date) RETURNING id
        """, (station["id"], code, number, body.display_name or f"Gun {number} ({current} {power:g} kW)", current,
              parse_connector(body.connector_type), power, body.ocpp_id or None))
        ledger.audit(cur, user, request, action=f"Added charging point {code}", action_type="Created", module="Chargers",
                     target=f"Charger {code}", record_id=pid, new=body.model_dump())
        return _point(cur, user, pid)


@router.patch("/charging-points/{point_id}")
def update_point(point_id: int, body: PointUpdateIn, request: Request,
                 user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        before = _point(cur, user, point_id)
        cur.execute("SELECT 1 FROM charging_points WHERE id = %s FOR UPDATE", (point_id,))
        if "status" in data:
            new = parse_enum(data["status"], POINT_STATUSES, "status")
            if new == "charging":
                raise ApiError(422, "Charging status is set automatically when a session starts.")
            if new in ("maintenance", "offline") and before["activeSession"]:
                raise ApiError(409, "Stop the active charging session before taking this charger out of service.")
            if new == "available" and before["openTickets"]:
                raise ApiError(409, "Resolve the open maintenance ticket before returning this charger to service.")
            cur.execute("UPDATE charging_points SET status = %s WHERE id = %s", (new, point_id))
        if "power_kw" in data:
            cur.execute("UPDATE charging_points SET power_kw = %s WHERE id = %s", (_power(data["power_kw"]), point_id))
        if "connector_type" in data:
            cur.execute("UPDATE charging_points SET connector_type = %s WHERE id = %s", (parse_connector(data["connector_type"]), point_id))
        if "type" in data:
            cur.execute("UPDATE charging_points SET current_type = %s WHERE id = %s", ("DC" if "dc" in data["type"].lower() else "AC", point_id))
        for key in ("ocpp_id", "display_name"):
            if key in data:
                cur.execute(f"UPDATE charging_points SET {key} = %s WHERE id = %s", (data[key] or None, point_id))
        after = _point(cur, user, point_id)
        ledger.audit(cur, user, request, action=f'Updated charging point {before["pointCode"]}',
                     action_type="Changed Status" if "status" in data else "Updated", module="Chargers",
                     target=f'Charger {before["pointCode"]}', record_id=point_id,
                     previous={k: before.get(k) for k in ("status", "powerKw", "connectorType", "ocppId")},
                     new={k: after.get(k) for k in ("status", "powerKw", "connectorType", "ocppId")})
        return after


@router.delete("/charging-points/{point_id}")
def delete_point(point_id: int, request: Request, user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    with db.tx() as cur:
        p = _point(cur, user, point_id)
        try:
            cur.execute("DELETE FROM charging_points WHERE id = %s", (point_id,))
        except (psycopg.errors.ForeignKeyViolation, psycopg.errors.RestrictViolation):
            raise ApiError(409, "This charger has session, booking or maintenance history. Set it offline instead.", "in_use")
        ledger.audit(cur, user, request, action=f'Deleted charging point {p["pointCode"]}', action_type="Deleted",
                     module="Chargers", target=f'Charger {p["pointCode"]}', record_id=point_id, severity="warning")
    return {"success": True}


# --- users & vehicles ---

USER_SQL = f"""
    SELECT u.id::text AS id, u.public_code AS "userId", u.full_name AS name, u.phone_number AS phone,
           u.phone_number AS "phoneNumber", u.email, c.city, w.balance::float AS "walletBalanceINR",
           w.balance::float AS "walletBalance",
           (SELECT COUNT(*) FROM charging_sessions s WHERE s.customer_id = u.id)::int AS "totalSessions",
           (SELECT COUNT(*) FROM vehicles v WHERE v.owner_id = u.id AND v.deleted_at IS NULL)::int AS "vehiclesCount",
           u.is_verified AS "isVerified", initcap(u.status::text) AS status, u.created_at AS "registeredAt",
           u.created_at AS "joinedAt", z.code AS "zoneCode", z.name AS "zoneName", au.full_name AS "assignedAdmin",
           u.last_active_at AS "lastActiveAt"
    FROM users u JOIN customer_profiles c ON c.user_id = u.id JOIN wallets w ON w.customer_id = u.id
    LEFT JOIN zones z ON z.id = c.zone_id LEFT JOIN users au ON au.id = c.assigned_admin_id
    WHERE {zf("c.zone_id")}
      AND (%(uid)s::bigint IS NULL OR u.id = %(uid)s)
      AND (%(status)s::text IS NULL OR u.status::text = %(status)s)
      AND (%(q)s::text IS NULL OR u.full_name ILIKE %(q)s OR u.phone_number ILIKE %(q)s OR u.email ILIKE %(q)s
           OR u.public_code ILIKE %(q)s)
    ORDER BY u.created_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""


def _mask(user: CurrentUser, rows: list[dict], phone_keys=("phone", "phoneNumber"), email_keys=("email",)) -> list[dict]:
    if user.can("VIEW_PII"):
        return rows
    for r in rows:
        for k in phone_keys:
            if k in r:
                r[k] = mask_phone(r[k])
        for k in email_keys:
            if k in r:
                r[k] = mask_email(r[k])
    return rows


def _users(cur, user: CurrentUser, *, uid=None, status=None, search=None, limit=200, offset=0) -> list[dict]:
    return _mask(user, db.all_rows(cur, USER_SQL, {"zones": user.zone_ids, "uid": uid, "status": status,
                                                   "q": f"%{search}%" if search else None, "limit": limit, "offset": offset}))


class UserStatusIn(CamelModel):
    status: str
    reason: Optional[str] = Field(None, max_length=300)


@router.get("/users", summary="AdminUser[]")
def list_users(search: Optional[str] = None, status: Optional[str] = None, limit: int = Query(200, ge=1, le=1000),
               offset: int = Query(0, ge=0),
               user: CurrentUser = Depends(require_permission("USER_MANAGEMENT", "VIEW_PII", "VIEW_REVENUE", "VIEW_SESSIONS"))):
    st = parse_enum(status, {"active", "inactive", "suspended", "pending"}, "status") if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return _users(cur, user, status=st, search=search, limit=limit, offset=offset)


@router.patch("/users/{user_id}/status")
def set_user_status(user_id: int, body: UserStatusIn, request: Request,
                    user: CurrentUser = Depends(require_permission("USER_MANAGEMENT"))):
    status = parse_enum(body.status, {"active", "inactive", "suspended"}, "status")
    with db.tx() as cur:
        rows = _users(cur, user, uid=user_id)
        if not rows:
            raise not_found("User")
        cur.execute("UPDATE users SET status = %s WHERE id = %s", (status, user_id))
        if status != "active":
            cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        ledger.audit(cur, user, request, action=f'Set customer {rows[0]["userId"]} to {status}', action_type="Changed Status",
                     module="Users", target=f'Customer {rows[0]["userId"]}', record_id=user_id, details=body.reason,
                     previous={"status": rows[0]["status"]}, new={"status": status},
                     severity="warning" if status == "suspended" else "notice")
        return _users(cur, user, uid=user_id)[0]


@router.get("/vehicles", summary="AdminVehicle[]")
def list_vehicles(search: Optional[str] = None, type: Optional[str] = None,
                  user: CurrentUser = Depends(require_permission("USER_MANAGEMENT", "VIEW_SESSIONS", "VIEW_PII"))):
    vt = parse_vehicle_type(type) if type and type.upper() != "ALL" else None
    with db.tx() as cur:
        rows = db.all_rows(cur, f"""
            SELECT v.id::text AS id, u.full_name AS "ownerName", u.phone_number AS "ownerPhone", u.id::text AS "userId",
                   u.public_code AS "customerCode", v.manufacturer, v.manufacturer AS make, v.model, v.manufacture_year AS year,
                   v.license_plate AS "licensePlate", v.license_plate AS "plateNumber", upper(v.vehicle_type::text) AS type,
                   upper(v.vehicle_type::text) AS category, v.connector_type::text AS connector,
                   v.connector_type::text AS "portType", v.battery_capacity_kwh::float AS "batteryCapacityKWh",
                   vo.sessions_count::int AS "sessionsCount", vo.total_energy_kwh::float AS "totalEnergyDeliveredKWh",
                   v.is_verified AS "isVerified", v.is_default AS "isDefault", v.created_at AS "createdAt"
            FROM vehicles v JOIN v_vehicle_overview vo ON vo.id = v.id JOIN users u ON u.id = v.owner_id
            JOIN customer_profiles c ON c.user_id = u.id
            WHERE v.deleted_at IS NULL AND {zf("c.zone_id")}
              AND (%(vt)s::vehicle_type IS NULL OR v.vehicle_type = %(vt)s::vehicle_type)
              AND (%(q)s::text IS NULL OR v.license_plate ILIKE %(q)s OR v.model ILIKE %(q)s OR u.full_name ILIKE %(q)s)
            ORDER BY v.created_at DESC
        """, {"zones": user.zone_ids, "vt": vt, "q": f"%{search}%" if search else None})
    return _mask(user, rows, phone_keys=("ownerPhone",), email_keys=())


class VerifyIn(CamelModel):
    is_verified: bool = True


@router.patch("/vehicles/{vehicle_id}/verify")
def verify_vehicle(vehicle_id: int, body: VerifyIn, request: Request,
                   user: CurrentUser = Depends(require_permission("USER_MANAGEMENT"))):
    with db.tx() as cur:
        v = db.one(cur, "SELECT v.*, c.zone_id FROM vehicles v JOIN customer_profiles c ON c.user_id = v.owner_id WHERE v.id = %s",
                   (vehicle_id,))
        if v is None or not user.in_scope(v["zone_id"]):
            raise not_found("Vehicle")
        cur.execute("UPDATE vehicles SET is_verified = %s WHERE id = %s", (body.is_verified, vehicle_id))
        ledger.audit(cur, user, request, action=f'{"Verified" if body.is_verified else "Unverified"} vehicle {v["license_plate"]}',
                     action_type="Approved" if body.is_verified else "Rejected", module="Users",
                     target=f'Vehicle {v["license_plate"]}', record_id=vehicle_id)
    return {"success": True, "isVerified": body.is_verified}


# --- sessions & reservations ---

SESSION_SQL = f"""
    SELECT cs.id::text AS id, cs.session_number AS "sessionNumber", u.full_name AS "customerName",
           u.full_name AS "userName", u.id::text AS "userId", v.license_plate AS "vehiclePlate", v.model AS "vehicleModel",
           upper(v.vehicle_type::text) AS "vehicleType", s.name AS "stationName", z.code AS "zoneCode",
           cp.id::text AS "chargerId", cp.point_code AS "pointCode", cs.started_at AS "startedAt",
           to_char(cs.started_at, 'DD Mon, HH12:MI AM') AS "startTime", to_char(cs.ended_at, 'DD Mon, HH12:MI AM') AS "endTime",
           (EXTRACT(EPOCH FROM COALESCE(cs.ended_at, now()) - cs.started_at) / 60)::int AS "durationMinutes",
           cs.energy_delivered_kwh::float AS "energyKWh", cs.energy_delivered_kwh::float AS "energyDeliveredKWh",
           cs.start_soc AS "startSoC", cs.current_soc AS "currentSoC", cs.peak_power_kw::float AS "peakPowerKw",
           CASE WHEN cs.status IN ('starting','charging','stopping') THEN cs.energy_cost ELSE cs.total_amount END::float AS "totalAmountINR",
           CASE WHEN cs.status IN ('starting','charging','stopping') THEN cs.energy_cost ELSE cs.total_amount END::float AS "totalCostINR",
           CASE WHEN cs.status IN ('starting','charging','stopping') THEN 'In Progress'
                ELSE initcap(replace(cs.payment_status::text, '_', ' ')) END AS "paymentStatus",
           CASE cs.status WHEN 'starting' THEN 'Active' WHEN 'charging' THEN 'In Progress' WHEN 'stopping' THEN 'In Progress'
                ELSE initcap(cs.status::text) END AS status,
           cs.stop_reason AS "stopReason"
    FROM charging_sessions cs JOIN users u ON u.id = cs.customer_id JOIN vehicles v ON v.id = cs.vehicle_id
    JOIN charging_points cp ON cp.id = cs.charging_point_id JOIN stations s ON s.id = cp.station_id
    JOIN zones z ON z.id = s.zone_id
    WHERE {zf("z.id")}
      AND (%(sid)s::bigint IS NULL OR cs.id = %(sid)s)
      AND (%(zid)s::bigint IS NULL OR z.id = %(zid)s)
      AND (%(live)s::boolean IS NULL OR (cs.status IN ('starting','charging','stopping')) = %(live)s)
      AND (%(status)s::text IS NULL OR cs.status::text = %(status)s)
      AND (%(from)s::date IS NULL OR cs.started_at >= %(from)s::date)
      AND (%(to)s::date IS NULL OR cs.started_at < %(to)s::date + 1)
      AND (%(q)s::text IS NULL OR cs.session_number ILIKE %(q)s OR u.full_name ILIKE %(q)s OR v.license_plate ILIKE %(q)s
           OR cp.point_code ILIKE %(q)s)
    ORDER BY cs.started_at DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

SESSION_STATUSES = {"starting", "charging", "stopping", "completed", "cancelled", "interrupted", "failed", "faulted"}


def _sessions(cur, user: CurrentUser, *, sid=None, zid=None, live=None, status=None, from_=None, to=None, q=None,
              limit=200, offset=0) -> list[dict]:
    return db.all_rows(cur, SESSION_SQL, {"zones": user.zone_ids, "sid": sid, "zid": zid, "live": live, "status": status,
                                          "from": from_, "to": to, "q": f"%{q}%" if q else None, "limit": limit,
                                          "offset": offset})


def _session(cur, user: CurrentUser, sid: int) -> dict:
    rows = _sessions(cur, user, sid=sid)
    if not rows:
        raise not_found("Charging session")
    return rows[0]


class RefundIn(CamelModel):
    amount_inr: Optional[Decimal] = Field(None, alias="amountINR", gt=0)
    reason: str = Field(min_length=5, max_length=300)


@router.get("/charging-sessions", summary="AdminChargingSession[]")
def list_sessions(status: Optional[str] = None, zone_id: Optional[int] = Query(None, alias="zoneId"),
                  from_date: Optional[Date] = Query(None, alias="from"), to_date: Optional[Date] = Query(None, alias="to"),
                  search: Optional[str] = None, limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                  user: CurrentUser = Depends(require_permission("VIEW_SESSIONS", "VIEW_REVENUE"))):
    live, st = None, None
    if status and status.upper() != "ALL":
        if status.lower() in ("active", "in progress", "in_progress", "live"):
            live = True
        else:
            st = parse_enum(status, SESSION_STATUSES, "status")
    with db.tx() as cur:
        _refresh_live_in_scope(cur, user)
        return _sessions(cur, user, zid=zone_id, live=live, status=st, from_=from_date, to=to_date, q=search,
                         limit=limit, offset=offset)


@router.post("/charging-sessions/{session_id}/stop", summary="Remote stop by operator")
def operator_stop(session_id: int, request: Request,
                  user: CurrentUser = Depends(require_permission("RESTART_CHARGER", "MANAGE_GRID"))):
    with db.tx() as cur:
        s = _session(cur, user, session_id)
        summary = finalize_session(cur, session_id, f"Stopped by operator {user.name}")
        ledger.audit(cur, user, request, action=f'Remote-stopped session {s["sessionNumber"]}', action_type="Updated",
                     module="Chargers", target=f'Charger {s["pointCode"]}', record_id=session_id, severity="notice")
        return summary


@router.post("/charging-sessions/{session_id}/refund")
def refund_session(session_id: int, body: RefundIn, request: Request,
                   user: CurrentUser = Depends(require_permission("PROCESS_REFUNDS"))):
    with db.tx() as cur:
        s = _session(cur, user, session_id)
        row = db.one(cur, """
            SELECT cs.customer_id, cs.payment_status, cs.session_number,
                   COALESCE((SELECT SUM(amount) FROM wallet_transactions WHERE charging_session_id = cs.id
                             AND txn_type = 'charging_fee' AND status = 'success'), 0) AS paid,
                   COALESCE((SELECT SUM(amount) FROM wallet_transactions WHERE charging_session_id = cs.id
                             AND txn_type = 'refund' AND status = 'success'), 0) AS refunded
            FROM charging_sessions cs WHERE cs.id = %s FOR UPDATE
        """, (session_id,))
        refundable = Decimal(row["paid"]) - Decimal(row["refunded"])
        if refundable <= 0:
            raise ApiError(409, "Nothing left to refund for this session.")
        amount = money(body.amount_inr) if body.amount_inr is not None else refundable
        if amount > refundable:
            raise ApiError(422, f"At most Rs {refundable} can be refunded for this session.")
        txn = ledger.post_wallet_txn(cur, row["customer_id"], "refund", "credit", amount,
                                     title=f'Refund for session {row["session_number"]}', description=body.reason,
                                     session_id=session_id, created_by=user.id)
        if amount == refundable:
            cur.execute("UPDATE charging_sessions SET payment_status = 'refunded' WHERE id = %s", (session_id,))
        ledger.notify(cur, row["customer_id"], "wallet", "Refund credited",
                      f'Rs {amount} refunded to your wallet for session {row["session_number"]}.', route="/customer/wallet",
                      entity_type="wallet_transaction", entity_id=txn["id"])
        ledger.audit(cur, user, request, action=f'Refunded Rs {amount} for session {s["sessionNumber"]}', action_type="Refunded",
                     module="Payments", target=f'Session {s["sessionNumber"]}', record_id=session_id, details=body.reason,
                     new={"amount": amount}, severity="warning")
        return {"success": True, "refundedINR": float(amount), "balanceAfterINR": float(txn["balance_after"]),
                "transactionRef": txn["transaction_ref"]}


RESERVATION_ADMIN_SQL = f"""
    SELECT r.id::text AS id, r.reservation_number AS "reservationNumber", u.full_name AS "customerName",
           u.full_name AS "userName", u.phone_number AS "customerPhone", z.name AS "zoneName", s.name AS "stationName",
           cp.id::text AS "chargerId", cp.point_code AS "pointCode", v.license_plate AS "vehiclePlate",
           r.scheduled_start AS "scheduledStartTime", to_char(r.scheduled_start, 'YYYY-MM-DD') AS date,
           to_char(r.scheduled_start, 'FMHH12:MI AM') || ' - ' || to_char(r.scheduled_end, 'FMHH12:MI AM') AS "timeSlot",
           (EXTRACT(EPOCH FROM r.scheduled_end - r.scheduled_start) / 60)::int AS "durationMinutes",
           r.slot_fee::float AS "slotFeeINR", initcap(r.status::text) AS status, r.estimated_cost::float AS "estimatedCostINR",
           r.created_at AS "createdAt", r.cancellation_reason AS "cancellationReason"
    FROM reservations r JOIN users u ON u.id = r.customer_id JOIN vehicles v ON v.id = r.vehicle_id
    JOIN charging_points cp ON cp.id = r.charging_point_id JOIN stations s ON s.id = cp.station_id
    JOIN zones z ON z.id = s.zone_id
    WHERE {zf("z.id")} AND (%(rid)s::bigint IS NULL OR r.id = %(rid)s)
      AND (%(status)s::text IS NULL OR r.status::text = %(status)s)
      AND (%(day)s::date IS NULL OR r.scheduled_start::date = %(day)s)
    ORDER BY r.scheduled_start DESC LIMIT %(limit)s
"""


class CancelIn(CamelModel):
    reason: str = Field("Cancelled by operator", min_length=3, max_length=300)


@router.get("/reservations", summary="AdminReservation[]")
def list_reservations(status: Optional[str] = None, day: Optional[Date] = Query(None, alias="date"),
                      limit: int = Query(200, ge=1, le=1000),
                      user: CurrentUser = Depends(require_permission("VIEW_SESSIONS"))):
    st = parse_enum(status, {"upcoming", "confirmed", "active", "completed", "cancelled"}, "status") \
        if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return _mask(user, db.all_rows(cur, RESERVATION_ADMIN_SQL, {"zones": user.zone_ids, "rid": None, "status": st,
                                                                    "day": day, "limit": limit}),
                     phone_keys=("customerPhone",), email_keys=())


@router.post("/reservations/{reservation_id}/cancel")
def admin_cancel_reservation(reservation_id: int, body: CancelIn, request: Request,
                             user: CurrentUser = Depends(require_permission("MANAGE_GRID"))):
    with db.tx() as cur:
        rows = db.all_rows(cur, RESERVATION_ADMIN_SQL, {"zones": user.zone_ids, "rid": reservation_id, "status": None,
                                                        "day": None, "limit": 1})
        if not rows:
            raise not_found("Reservation")
        cancel_reservation(cur, reservation_id, body.reason)
        ledger.audit(cur, user, request, action=f'Cancelled reservation {rows[0]["reservationNumber"]}', action_type="Updated",
                     module="Chargers", target=f'Reservation {rows[0]["reservationNumber"]}', record_id=reservation_id,
                     details=body.reason)
        return db.all_rows(cur, RESERVATION_ADMIN_SQL, {"zones": user.zone_ids, "rid": reservation_id, "status": None,
                                                        "day": None, "limit": 1})[0]


# --- tariffs ---

TARIFF_SQL = f"""
    SELECT t.id::text AS id, t.code, t.name, upper(t.vehicle_type::text) AS "vehicleType",
           COALESCE(z.code, 'Global') AS "zoneCode", COALESCE(z.name, 'All Zones') AS "zoneName",
           CASE t.pricing_model WHEN 'per_kwh' THEN 'Per kWh' WHEN 'per_minute' THEN 'Per Minute'
                                WHEN 'flat' THEN 'Flat' ELSE 'Time of Use' END AS "pricingType",
           t.rate_per_kwh::float AS "ratePerKWhINR", t.rate_per_kwh::float AS "standardRateINR",
           t.peak_rate_per_kwh::float AS "peakRatePerKWhINR", t.peak_rate_per_kwh::float AS "peakRateINR",
           CASE WHEN t.peak_start IS NOT NULL THEN to_char(t.peak_start, 'HH24:MI') || '-' || to_char(t.peak_end, 'HH24:MI') END AS "peakHoursWindow",
           t.off_peak_rate_per_kwh::float AS "offPeakRateINR", t.service_fee::float AS "serviceFeeINR",
           t.gst_percent::float AS "taxGstPercent", t.min_charge::float AS "minChargeINR",
           t.effective_from AS "effectiveFrom", t.effective_to AS "effectiveTo", t.priority,
           t.idle_fee_per_minute::float AS "idleFeePerMinuteINR", t.parking_fee_per_hour::float AS "parkingFeePerHourINR",
           t.reservation_slot_fee::float AS "reservationSlotFeeINR", initcap(t.status::text) AS status,
           t.updated_at AS "lastUpdated",
           (t.status = 'active' AND current_date BETWEEN t.effective_from AND COALESCE(t.effective_to, 'infinity'::date)) AS "isEffective"
    FROM tariff_plans t LEFT JOIN zones z ON z.id = t.zone_id
    WHERE (t.zone_id IS NULL OR {zf("t.zone_id")}) AND (%(tid)s::bigint IS NULL OR t.id = %(tid)s)
    ORDER BY t.zone_id NULLS FIRST, t.vehicle_type, t.priority
"""


class TariffIn(CamelModel):
    name: Optional[str] = Field(None, max_length=120)
    vehicle_type: str
    zone_code: Optional[str] = "Global"
    zone_id: Optional[int] = None
    pricing_type: str = "Per kWh"
    rate_per_kwh: Decimal = Field(alias="ratePerKWhINR", ge=0, le=1000)
    peak_rate_per_kwh: Optional[Decimal] = Field(None, alias="peakRatePerKWhINR", ge=0, le=1000)
    peak_hours_window: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}\s*-\s*\d{2}:\d{2}$")
    off_peak_rate: Optional[Decimal] = Field(None, alias="offPeakRateINR", ge=0, le=1000)
    service_fee: Decimal = Field(Decimal(0), alias="serviceFeeINR", ge=0)
    gst_percent: Decimal = Field(Decimal(18), alias="taxGstPercent", ge=0, le=100)
    min_charge: Decimal = Field(Decimal(0), alias="minChargeINR", ge=0)
    idle_fee: Decimal = Field(Decimal(0), alias="idleFeePerMinuteINR", ge=0)
    parking_fee: Decimal = Field(Decimal(0), alias="parkingFeePerHourINR", ge=0)
    slot_fee: Decimal = Field(Decimal(0), alias="reservationSlotFeeINR", ge=0)
    effective_from: Date
    effective_to: Optional[Date] = None
    priority: int = Field(3, ge=1, le=9)
    status: str = "Active"


class TariffUpdateIn(CamelModel):
    name: Optional[str] = Field(None, max_length=120)
    pricing_type: Optional[str] = None
    rate_per_kwh: Optional[Decimal] = Field(None, alias="ratePerKWhINR", ge=0, le=1000)
    peak_rate_per_kwh: Optional[Decimal] = Field(None, alias="peakRatePerKWhINR", ge=0, le=1000)
    peak_hours_window: Optional[str] = Field(None, pattern=r"^(\d{2}:\d{2}\s*-\s*\d{2}:\d{2})?$")
    off_peak_rate: Optional[Decimal] = Field(None, alias="offPeakRateINR", ge=0, le=1000)
    service_fee: Optional[Decimal] = Field(None, alias="serviceFeeINR", ge=0)
    gst_percent: Optional[Decimal] = Field(None, alias="taxGstPercent", ge=0, le=100)
    min_charge: Optional[Decimal] = Field(None, alias="minChargeINR", ge=0)
    idle_fee: Optional[Decimal] = Field(None, alias="idleFeePerMinuteINR", ge=0)
    parking_fee: Optional[Decimal] = Field(None, alias="parkingFeePerHourINR", ge=0)
    slot_fee: Optional[Decimal] = Field(None, alias="reservationSlotFeeINR", ge=0)
    effective_from: Optional[Date] = None
    effective_to: Optional[Date] = None
    priority: Optional[int] = Field(None, ge=1, le=9)
    status: Optional[str] = None


TARIFF_COLS = {"name": "name", "rate_per_kwh": "rate_per_kwh", "peak_rate_per_kwh": "peak_rate_per_kwh",
               "off_peak_rate": "off_peak_rate_per_kwh", "service_fee": "service_fee", "gst_percent": "gst_percent",
               "min_charge": "min_charge", "idle_fee": "idle_fee_per_minute", "parking_fee": "parking_fee_per_hour",
               "slot_fee": "reservation_slot_fee", "effective_from": "effective_from", "effective_to": "effective_to",
               "priority": "priority"}


def _pricing_model(value: str) -> str:
    key = re.sub(r"[^a-z]", "", value.lower())
    model = {"perkwh": "per_kwh", "kwh": "per_kwh", "perminute": "per_minute", "minute": "per_minute", "flat": "flat",
             "timeofuse": "time_of_use", "tou": "time_of_use"}.get(key)
    if model is None:
        raise ApiError(422, "pricingType must be Per kWh, Per Minute, Flat or Time of Use.")
    return model


def _peak(window: str | None) -> tuple[str | None, str | None]:
    if not window:
        return None, None
    start, end = [p.strip() for p in window.split("-")]
    return start, end


def _tariffs(cur, user: CurrentUser, tid: int | None = None) -> list[dict]:
    return db.all_rows(cur, TARIFF_SQL, {"zones": user.zone_ids, "tid": tid})


@router.get("/tariffs", summary="AdminTariffPlan[]")
def list_tariffs(user: CurrentUser = Depends(require_permission("TARIFF_EDIT", "VIEW_REVENUE", "VIEW_ZONES"))):
    with db.tx() as cur:
        return _tariffs(cur, user)


@router.post("/tariffs", status_code=201)
def create_tariff(body: TariffIn, request: Request, user: CurrentUser = Depends(require_permission("TARIFF_EDIT"))):
    vt = parse_vehicle_type(body.vehicle_type)
    with db.tx() as cur:
        zone = None
        if body.zone_id or (body.zone_code and body.zone_code.lower() != "global"):
            zone = db.one(cur, "SELECT * FROM zones WHERE id = %s OR code = %s", (body.zone_id or 0, (body.zone_code or "").upper()))
            if zone is None:
                raise not_found("Zone")
            _check_zone(user, zone["id"])
        elif not user.is_global:
            raise ApiError(403, "Zone administrators cannot create global tariffs.")
        peak_start, peak_end = _peak(body.peak_hours_window)
        code = db.scalar(cur, r"""SELECT 'T-' || lpad((COALESCE(MAX(substring(code FROM '^T-(\d+)$')::int), 0) + 1)::text, 3, '0')
                                  FROM tariff_plans""")
        name = body.name or f'{zone["name"] if zone else "Global"} {vt.capitalize()} Rate'
        tid = db.scalar(cur, """
            INSERT INTO tariff_plans (code, name, zone_id, vehicle_type, pricing_model, rate_per_kwh, peak_rate_per_kwh, peak_start,
                                      peak_end, off_peak_rate_per_kwh, service_fee, gst_percent, min_charge, idle_fee_per_minute,
                                      parking_fee_per_hour, reservation_slot_fee, effective_from, effective_to, priority, status,
                                      created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (code, name, zone["id"] if zone else None, vt, _pricing_model(body.pricing_type), body.rate_per_kwh,
              body.peak_rate_per_kwh, peak_start, peak_end, body.off_peak_rate, body.service_fee, body.gst_percent,
              body.min_charge, body.idle_fee, body.parking_fee, body.slot_fee, body.effective_from, body.effective_to,
              body.priority, parse_enum(body.status, {"draft", "active", "inactive"}, "status"), user.id, user.id))
        ledger.audit(cur, user, request, action=f"Created tariff {code} ({name})", action_type="Created", module="Pricing",
                     target=f"Tariff {code}", record_id=code, new=body.model_dump(by_alias=True), severity="notice")
        return _tariffs(cur, user, tid)[0]


@router.patch("/tariffs/{tariff_id}")
def update_tariff(tariff_id: int, body: TariffUpdateIn, request: Request,
                  user: CurrentUser = Depends(require_permission("TARIFF_EDIT"))):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        before = _tariffs(cur, user, tariff_id)
        if not before:
            raise not_found("Tariff")
        before = before[0]
        if before["zoneCode"] == "Global" and not user.is_global:
            raise ApiError(403, "Zone administrators cannot change global tariffs.")
        cur.execute("SELECT 1 FROM tariff_plans WHERE id = %s FOR UPDATE", (tariff_id,))
        sets, params = [], []
        for key, value in data.items():
            if key in TARIFF_COLS:
                sets.append(f"{TARIFF_COLS[key]} = %s")
                params.append(value)
        if "pricing_type" in data:
            sets.append("pricing_model = %s")
            params.append(_pricing_model(data["pricing_type"]))
        if "peak_hours_window" in data:
            sets += ["peak_start = %s", "peak_end = %s"]
            params += list(_peak(data["peak_hours_window"]))
        if "status" in data:
            sets.append("status = %s")
            params.append(parse_enum(data["status"], {"draft", "active", "inactive"}, "status"))
        if sets:
            cur.execute(f"UPDATE tariff_plans SET {', '.join(sets)}, updated_by = %s WHERE id = %s", (*params, user.id, tariff_id))
        after = _tariffs(cur, user, tariff_id)[0]
        changed = [k for k in after if k not in ("lastUpdated", "isEffective") and after[k] != before[k]]
        ledger.audit(cur, user, request, action=f'Updated tariff {before["code"]}', action_type="Updated", module="Pricing",
                     target=f'Tariff {before["code"]}', record_id=before["code"],
                     previous={k: before[k] for k in changed}, new={k: after[k] for k in changed}, severity="notice")
        return after


@router.delete("/tariffs/{tariff_id}")
def delete_tariff(tariff_id: int, request: Request, user: CurrentUser = Depends(require_permission("TARIFF_EDIT"))):
    with db.tx() as cur:
        t = _tariffs(cur, user, tariff_id)
        if not t:
            raise not_found("Tariff")
        t = t[0]
        if t["zoneCode"] == "Global" and not user.is_global:
            raise ApiError(403, "Zone administrators cannot delete global tariffs.")
        used = db.scalar(cur, """SELECT EXISTS (SELECT 1 FROM charging_sessions WHERE tariff_plan_id = %(t)s)
                                     OR EXISTS (SELECT 1 FROM reservations WHERE tariff_plan_id = %(t)s)""", {"t": tariff_id})
        if used:
            cur.execute("UPDATE tariff_plans SET status = 'inactive', updated_by = %s WHERE id = %s", (user.id, tariff_id))
        else:
            cur.execute("DELETE FROM tariff_plans WHERE id = %s", (tariff_id,))
        ledger.audit(cur, user, request, action=f'{"Deactivated" if used else "Deleted"} tariff {t["code"]}',
                     action_type="Deleted", module="Pricing", target=f'Tariff {t["code"]}', record_id=t["code"],
                     previous=t, severity="warning")
    return {"success": True, "deleted": not used, "deactivated": bool(used)}


# --- wallet / payments ---

WALLET_TXN_SQL = f"""
    SELECT t.id::text AS id, t.transaction_ref AS "transactionRef", u.full_name AS "customerName", u.full_name AS "userName",
           u.phone_number AS "customerPhone", u.id::text AS "userId",
           CASE t.txn_type WHEN 'topup' THEN 'Recharge' WHEN 'charging_fee' THEN 'Session Debit' WHEN 'refund' THEN 'Refund'
                           WHEN 'reservation_fee' THEN 'ReservationFee' WHEN 'adjustment_credit' THEN 'Credit'
                           WHEN 'adjustment_debit' THEN 'Debit' ELSE 'Reversal' END AS type,
           t.direction, t.amount::float AS "amountINR", t.balance_after::float AS "balanceAfterINR",
           COALESCE(p.method_summary, CASE WHEN t.txn_type IN ('topup') THEN NULL ELSE 'E-Way Wallet' END) AS "paymentMethod",
           initcap(p.provider::text) AS "paymentGateway", p.gateway_payment_id AS "gatewayTxnId",
           t.created_at AS timestamp, initcap(t.status::text) AS status, t.title, t.description
    FROM wallet_transactions t JOIN users u ON u.id = t.customer_id JOIN customer_profiles c ON c.user_id = u.id
    LEFT JOIN payments p ON p.id = t.payment_id
    WHERE {zf("c.zone_id")}
      AND (%(type)s::text IS NULL OR t.txn_type::text = %(type)s)
      AND (%(status)s::text IS NULL OR t.status::text = %(status)s)
      AND (%(q)s::text IS NULL OR u.full_name ILIKE %(q)s OR t.transaction_ref ILIKE %(q)s OR u.phone_number ILIKE %(q)s)
    ORDER BY t.created_at DESC, t.id DESC LIMIT %(limit)s OFFSET %(offset)s
"""

ADMIN_TXN_TYPES = {"recharge": "topup", "topup": "topup", "session_debit": "charging_fee", "sessiondebit": "charging_fee",
                   "refund": "refund", "reservationfee": "reservation_fee", "reservation_fee": "reservation_fee",
                   "credit": "adjustment_credit", "debit": "adjustment_debit", "reversal": "reversal"}


class AdjustmentIn(CamelModel):
    customer_id: int
    direction: Literal["credit", "debit"]
    amount_inr: Decimal = Field(alias="amountINR", gt=0, le=100000)
    reason: str = Field(min_length=5, max_length=300)


@router.get("/wallet-transactions", summary="AdminWalletTransaction[]")
def wallet_transactions(type: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None,
                        limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                        user: CurrentUser = Depends(require_permission("VIEW_REVENUE"))):
    t = None
    if type and type.upper() != "ALL":
        t = ADMIN_TXN_TYPES.get(re.sub(r"[\s-]+", "_", type.strip()).lower())
        if t is None:
            raise ApiError(422, "Unknown transaction type.")
    st = parse_enum(status, {"pending", "success", "failed", "cancelled"}, "status") if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return _mask(user, db.all_rows(cur, WALLET_TXN_SQL, {"zones": user.zone_ids, "type": t, "status": st,
                                                             "q": f"%{search}%" if search else None, "limit": limit,
                                                             "offset": offset}),
                     phone_keys=("customerPhone",), email_keys=())


@router.post("/wallet-adjustments", status_code=201, summary="Manual credit/debit (WALLET_ADJUST)")
def wallet_adjustment(body: AdjustmentIn, request: Request, user: CurrentUser = Depends(require_permission("WALLET_ADJUST"))):
    with db.tx() as cur:
        rows = _users(cur, user, uid=body.customer_id)
        if not rows:
            raise not_found("Customer")
        txn = ledger.post_wallet_txn(cur, body.customer_id, f"adjustment_{body.direction}", body.direction, body.amount_inr,
                                     title=f"Manual {body.direction} by E-Way support", description=body.reason,
                                     created_by=user.id)
        ledger.notify(cur, body.customer_id, "wallet", f"Wallet {body.direction}ed",
                      f"Rs {money(body.amount_inr)} {'added to' if body.direction == 'credit' else 'deducted from'} your wallet: {body.reason}",
                      route="/customer/wallet", entity_type="wallet_transaction", entity_id=txn["id"])
        ledger.audit(cur, user, request, action=f'Manual wallet {body.direction} Rs {money(body.amount_inr)} for {rows[0]["userId"]}',
                     action_type="Updated", module="Payments", target=f'Customer {rows[0]["userId"]}',
                     record_id=txn["transaction_ref"], details=body.reason, new={"amount": body.amount_inr,
                     "direction": body.direction, "balanceAfter": txn["balance_after"]}, severity="critical")
        return {"success": True, "transactionRef": txn["transaction_ref"], "balanceAfterINR": float(txn["balance_after"])}


# --- maintenance ---

ENGINEER_SQL = f"""
    SELECT w.user_id::text AS id, w.engineer_code AS "engineerCode", w.full_name AS name, w.phone_number AS phone,
           w.email, w.zone_code AS "zoneCode", w.zone_name AS "zoneName",
           CASE w.availability WHEN 'on_site' THEN 'On Site' WHEN 'off_duty' THEN 'Off Duty' ELSE initcap(w.availability::text) END AS status,
           w.active_jobs::int AS "activeJobs", w.specialization
    FROM v_field_engineer_workload w JOIN zones z ON z.code = w.zone_code
    WHERE {zf("z.id")} AND (%(eid)s::bigint IS NULL OR w.user_id = %(eid)s)
    ORDER BY w.zone_code, w.full_name
"""

TICKET_SQL = f"""
    SELECT t.id::text AS id, t.ticket_code AS "ticketCode", cp.id::text AS "chargerId", cp.point_code AS "pointCode",
           s.name AS "stationName", z.id::text AS "zoneId", z.code AS "zoneCode", z.name AS "zoneName", s.city,
           t.fault_code AS "faultCode", t.issue_description AS "issueDescription", t.issue_description AS description,
           initcap(t.severity::text) AS severity, t.reported_at AS "reportedAt",
           CASE t.status WHEN 'open' THEN 'Open' WHEN 'in_progress' THEN 'In Progress' ELSE 'Resolved' END AS status,
           eu.full_name AS "assignedTechnician", eu.full_name AS "technicianAssigned", eu.phone_number AS "technicianContact",
           fe.user_id::text AS "technicianId", fe.engineer_code AS "engineerCode", t.sla_minutes AS "slaMinutes",
           q.sla_remaining_minutes AS "slaRemainingMinutes", q.sla_breached AS "slaBreached", t.priority::text AS priority,
           t.resolution_notes AS "resolutionNotes", t.resolved_at AS "resolvedAt", t.notes
    FROM maintenance_tickets t JOIN v_maintenance_queue q ON q.id = t.id
    JOIN charging_points cp ON cp.id = t.charging_point_id JOIN stations s ON s.id = cp.station_id
    JOIN zones z ON z.id = s.zone_id
    LEFT JOIN field_engineers fe ON fe.user_id = t.assigned_engineer_id LEFT JOIN users eu ON eu.id = fe.user_id
    WHERE {zf("z.id")} AND (%(tid)s::bigint IS NULL OR t.id = %(tid)s)
      AND (%(status)s::text IS NULL OR t.status::text = %(status)s)
      AND (%(severity)s::text IS NULL OR t.severity::text = %(severity)s)
    ORDER BY (t.status = 'resolved'), t.priority, t.reported_at
"""

SLA_BY_SEVERITY = {"critical": 60, "high": 120, "medium": 240, "low": 480}
PRIORITY_BY_SEVERITY = {"critical": "P1", "high": "P2", "medium": "P3", "low": "P4"}


def _tickets(cur, user, *, tid=None, status=None, severity=None) -> list[dict]:
    return db.all_rows(cur, TICKET_SQL, {"zones": user.zone_ids, "tid": tid, "status": status, "severity": severity})


def _ticket_detail(cur, user: CurrentUser, tid: int) -> dict:
    rows = _tickets(cur, user, tid=tid)
    if not rows:
        raise not_found("Maintenance ticket")
    t = rows[0]
    t["timeline"] = db.all_rows(cur, """
        SELECT id::text AS id, created_at AS timestamp, actor_name AS actor, action, note, actor_type AS type
        FROM maintenance_ticket_events WHERE ticket_id = %s ORDER BY created_at, id
    """, (tid,))
    t["diagnostics"] = db.one(cur, """
        SELECT initcap(connection_status::text) AS "connectionStatus", power_level_kw::float AS "powerLevelKw",
               rated_power_kw::float AS "ratedPowerKw", temperature_c::float AS "temperatureC",
               initcap(connector_status::text) AS "connectorStatus", ground_leakage_ma::float AS "groundLeakageMa",
               recorded_at AS "lastHeartbeat", ocpp_error_code AS "ocppErrorCode", vendor_error_code AS "vendorErrorCode",
               voltage_v::float AS "voltageV", current_a::float AS "currentA"
        FROM charger_diagnostics WHERE ticket_id = %s OR charging_point_id = %s
        ORDER BY (ticket_id = %s) DESC NULLS LAST, recorded_at DESC LIMIT 1
    """, (tid, int(t["chargerId"]), tid))
    return t


def _event(cur, ticket_id: int, user: CurrentUser, action: str, note: str | None = None) -> None:
    actor_type = "technician" if user.role_code == "ROLE_FIELD_ENGINEER" else "admin"
    cur.execute("""INSERT INTO maintenance_ticket_events (ticket_id, actor_user_id, actor_name, actor_type, action, note)
                   VALUES (%s, %s, %s, %s, %s, %s)""", (ticket_id, user.id, user.name or user.email, actor_type, action, note))


class TicketIn(CamelModel):
    charger_id: str = Field(min_length=1, description="Charging point id or point code")
    fault_code: Optional[str] = Field(None, max_length=60)
    issue_description: str = Field(min_length=5, max_length=2000)
    severity: Literal["Critical", "High", "Medium", "Low", "critical", "high", "medium", "low"]
    priority: Optional[Literal["P1", "P2", "P3", "P4"]] = None
    sla_minutes: Optional[int] = Field(None, gt=0, le=10080)
    notes: Optional[str] = Field(None, max_length=2000)


class TicketUpdateIn(CamelModel):
    severity: Optional[Literal["Critical", "High", "Medium", "Low", "critical", "high", "medium", "low"]] = None
    priority: Optional[Literal["P1", "P2", "P3", "P4"]] = None
    sla_minutes: Optional[int] = Field(None, gt=0, le=10080)
    issue_description: Optional[str] = Field(None, min_length=5, max_length=2000)
    notes: Optional[str] = Field(None, max_length=2000)


class AssignIn(CamelModel):
    engineer_id: Optional[int] = None
    engineer_code: Optional[str] = None
    technician_id: Optional[str] = None      # frontend sends technician.id ("ENG-101" or user id)


class NoteIn(CamelModel):
    note: str = Field(min_length=1, max_length=2000)


class ResolveIn(CamelModel):
    resolution_notes: str = Field(min_length=3, max_length=2000)
    return_to_service: bool = True


class EngineerStatusIn(CamelModel):
    status: str


@router.get("/field-engineers", summary="AdminFieldEngineer[]")
def list_engineers(user: CurrentUser = Depends(require_permission("VIEW_MAINTENANCE"))):
    with db.tx() as cur:
        return db.all_rows(cur, ENGINEER_SQL, {"zones": user.zone_ids, "eid": None})


@router.patch("/field-engineers/{engineer_id}")
def set_engineer_status(engineer_id: int, body: EngineerStatusIn,
                        user: CurrentUser = Depends(require_permission("DISPATCH_MAINTENANCE", "UPDATE_TICKET"))):
    st = parse_enum(body.status, {"available", "on_site", "busy", "off_duty"}, "status")
    with db.tx() as cur:
        if not db.all_rows(cur, ENGINEER_SQL, {"zones": user.zone_ids, "eid": engineer_id}):
            raise not_found("Field engineer")
        cur.execute("UPDATE field_engineers SET availability = %s WHERE user_id = %s", (st, engineer_id))
        return db.all_rows(cur, ENGINEER_SQL, {"zones": user.zone_ids, "eid": engineer_id})[0]


@router.get("/maintenance-tickets", summary="AdminMaintenanceTicket[]")
def list_tickets(status: Optional[str] = None, severity: Optional[str] = None,
                 user: CurrentUser = Depends(require_permission("VIEW_MAINTENANCE"))):
    st = parse_enum(status, {"open", "in_progress", "resolved"}, "status") if status and status.upper() != "ALL" else None
    sev = parse_enum(severity, set(SLA_BY_SEVERITY), "severity") if severity and severity.upper() != "ALL" else None
    with db.tx() as cur:
        return _tickets(cur, user, status=st, severity=sev)


@router.get("/maintenance-tickets/{ticket_id}")
def get_ticket(ticket_id: int, user: CurrentUser = Depends(require_permission("VIEW_MAINTENANCE"))):
    with db.tx() as cur:
        return _ticket_detail(cur, user, ticket_id)


@router.post("/maintenance-tickets", status_code=201)
def create_ticket(body: TicketIn, request: Request, user: CurrentUser = Depends(require_permission("UPDATE_TICKET"))):
    severity = body.severity.lower()
    with db.tx() as cur:
        point = resolve_point(cur, body.charger_id, lock=True)
        _check_zone(user, point["zone_id"], "Charger")
        code = db.scalar(cur, r"""SELECT 'MT-' || (COALESCE(MAX(substring(ticket_code FROM '^MT-(\d+)$')::int), 1000) + 1)
                                  FROM maintenance_tickets""")
        tid = db.scalar(cur, """
            INSERT INTO maintenance_tickets (ticket_code, charging_point_id, fault_code, issue_description, severity, priority,
                                             reported_by, sla_minutes, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (code, point["id"], body.fault_code, body.issue_description, severity,
              body.priority or PRIORITY_BY_SEVERITY[severity], user.id, body.sla_minutes or SLA_BY_SEVERITY[severity], body.notes))
        if point["status"] not in ("charging",):
            cur.execute("UPDATE charging_points SET status = 'maintenance' WHERE id = %s", (point["id"],))
        _event(cur, tid, user, "Ticket created", body.issue_description)
        ledger.notify_zone_admins(cur, point["zone_id"], "maintenance", f'New {severity} ticket {code} on {point["point_code"]}',
                                  body.issue_description, route="/admin/maintenance", entity_type="maintenance_ticket", entity_id=tid)
        ledger.audit(cur, user, request, action=f'Opened ticket {code} for {point["point_code"]}', action_type="Marked Maintenance",
                     module="Support", target=f'Charger {point["point_code"]}', record_id=code, severity="notice")
        return _ticket_detail(cur, user, tid)


@router.patch("/maintenance-tickets/{ticket_id}")
def update_ticket(ticket_id: int, body: TicketUpdateIn, user: CurrentUser = Depends(require_permission("UPDATE_TICKET"))):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        before = _ticket_detail(cur, user, ticket_id)
        if before["status"] == "Resolved":
            raise ApiError(409, "Resolved tickets cannot be edited.")
        cols = {"severity": "severity", "priority": "priority", "sla_minutes": "sla_minutes",
                "issue_description": "issue_description", "notes": "notes"}
        for key, value in data.items():
            cur.execute(f"UPDATE maintenance_tickets SET {cols[key]} = %s WHERE id = %s",
                        (value.lower() if key == "severity" else value, ticket_id))
        if data:
            _event(cur, ticket_id, user, "Ticket updated", ", ".join(f"{k}: {v}" for k, v in data.items()))
        return _ticket_detail(cur, user, ticket_id)


@router.post("/maintenance-tickets/{ticket_id}/assign")
def assign_ticket(ticket_id: int, body: AssignIn, request: Request,
                  user: CurrentUser = Depends(require_permission("DISPATCH_MAINTENANCE"))):
    ref = body.engineer_id or body.engineer_code or body.technician_id
    if ref is None:
        raise ApiError(422, "Provide engineerId or engineerCode.")
    with db.tx() as cur:
        t = _ticket_detail(cur, user, ticket_id)
        if t["status"] == "Resolved":
            raise ApiError(409, "This ticket is already resolved.")
        eng = db.one(cur, """SELECT fe.*, u.full_name FROM field_engineers fe JOIN users u ON u.id = fe.user_id
                             WHERE fe.user_id::text = %s OR upper(fe.engineer_code) = upper(%s)""", (str(ref), str(ref)))
        if eng is None:
            raise not_found("Field engineer")
        cur.execute("""UPDATE maintenance_tickets SET assigned_engineer_id = %s, assigned_at = now(), status = 'in_progress'
                       WHERE id = %s""", (eng["user_id"], ticket_id))
        cur.execute("UPDATE field_engineers SET availability = 'busy' WHERE user_id = %s AND availability = 'available'",
                    (eng["user_id"],))
        _event(cur, ticket_id, user, f'Assigned to {eng["full_name"]} ({eng["engineer_code"]})')
        ledger.notify(cur, eng["user_id"], "maintenance", f'You have been assigned {t["ticketCode"]}',
                      f'{t["pointCode"]} at {t["stationName"]}: {t["issueDescription"]}', route="/admin/maintenance",
                      entity_type="maintenance_ticket", entity_id=ticket_id)
        ledger.audit(cur, user, request, action=f'Dispatched {eng["engineer_code"]} to {t["ticketCode"]}', action_type="Updated",
                     module="Support", target=f'Ticket {t["ticketCode"]}', record_id=t["ticketCode"])
        return _ticket_detail(cur, user, ticket_id)


@router.post("/maintenance-tickets/{ticket_id}/notes")
def add_ticket_note(ticket_id: int, body: NoteIn, user: CurrentUser = Depends(require_permission("UPDATE_TICKET"))):
    with db.tx() as cur:
        _ticket_detail(cur, user, ticket_id)
        _event(cur, ticket_id, user, "Note added", body.note)
        return _ticket_detail(cur, user, ticket_id)


@router.post("/maintenance-tickets/{ticket_id}/resolve")
def resolve_ticket(ticket_id: int, body: ResolveIn, request: Request,
                   user: CurrentUser = Depends(require_permission("UPDATE_TICKET"))):
    with db.tx() as cur:
        t = _ticket_detail(cur, user, ticket_id)
        if t["status"] == "Resolved":
            raise ApiError(409, "This ticket is already resolved.")
        cur.execute("""UPDATE maintenance_tickets SET status = 'resolved', resolved_at = now(), resolution_notes = %s
                       WHERE id = %s""", (body.resolution_notes, ticket_id))
        _event(cur, ticket_id, user, "Ticket resolved", body.resolution_notes)
        point_id = int(t["chargerId"])
        others = db.scalar(cur, "SELECT COUNT(*) FROM maintenance_tickets WHERE charging_point_id = %s AND status <> 'resolved'",
                           (point_id,))
        if body.return_to_service and not others:
            cur.execute("""UPDATE charging_points SET status = 'available', last_maintenance_at = now()
                           WHERE id = %s AND status IN ('maintenance', 'offline')""", (point_id,))
        if t["technicianId"]:
            cur.execute("""UPDATE field_engineers fe SET availability = 'available' WHERE user_id = %s AND availability <> 'off_duty'
                           AND NOT EXISTS (SELECT 1 FROM maintenance_tickets m WHERE m.assigned_engineer_id = fe.user_id
                                           AND m.status <> 'resolved')""", (int(t["technicianId"]),))
        ledger.audit(cur, user, request, action=f'Resolved {t["ticketCode"]}', action_type="Updated", module="Support",
                     target=f'Ticket {t["ticketCode"]}', record_id=t["ticketCode"], details=body.resolution_notes)
        return _ticket_detail(cur, user, ticket_id)


# --- audit, reports, settings ---

@router.get("/audit-logs", summary="AdminAuditLog[]")
def audit_logs(module: Optional[str] = None, action_type: Optional[str] = Query(None, alias="actionType"),
               search: Optional[str] = None, from_date: Optional[Date] = Query(None, alias="from"),
               to_date: Optional[Date] = Query(None, alias="to"), limit: int = Query(200, ge=1, le=1000),
               user: CurrentUser = Depends(require_permission("AUDIT_LOGS"))):
    with db.tx() as cur:
        return db.all_rows(cur, """
            SELECT a.id::text AS id, a.actor_name AS actor, a.actor_name AS "adminName", a.actor_email AS "actorEmail",
                   a.actor_email AS "adminEmail", a.actor_role AS "actorRole", a.actor_role AS role, a.actor_role AS "adminRole",
                   a.action, a.action_type AS "actionType", a.module, a.target_resource AS "targetResource",
                   a.target_resource AS target, a.record_id AS "recordId", a.details, host(a.ip_address) AS "ipAddress",
                   a.device, a.created_at AS timestamp, a.previous_value::text AS "previousValue", a.new_value::text AS "newValue",
                   initcap(a.severity::text) AS severity, initcap(a.outcome::text) AS status, a.portal
            FROM audit_logs a
            WHERE (%(sa)s OR a.portal <> 'super_admin')
              AND (%(module)s::text IS NULL OR a.module ILIKE %(module)s)
              AND (%(atype)s::text IS NULL OR a.action_type ILIKE %(atype)s)
              AND (%(q)s::text IS NULL OR a.action ILIKE %(q)s OR a.actor_name ILIKE %(q)s OR a.target_resource ILIKE %(q)s)
              AND (%(from)s::date IS NULL OR a.created_at >= %(from)s::date)
              AND (%(to)s::date IS NULL OR a.created_at < %(to)s::date + 1)
            ORDER BY a.created_at DESC LIMIT %(limit)s
        """, {"sa": user.is_super_admin, "module": module, "atype": action_type, "q": f"%{search}%" if search else None,
              "from": from_date, "to": to_date, "limit": limit})


PERIODS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "12M": 12}


def _period(period: str, start: Date | None, end: Date | None) -> tuple[Date, Date]:
    end = end or Date.today()
    if start is None:
        months = PERIODS.get(period.upper(), 6)
        first = end.replace(day=1)
        y, m = divmod(first.month - 1 - (months - 1), 12)
        start = first.replace(year=first.year + y, month=m + 1)
    if start > end:
        raise ApiError(422, "startDate must be on or before endDate.")
    return start, end


@router.get("/reports", summary="AdminReportsData")
def reports(period: str = "6M", start_date: Optional[Date] = Query(None, alias="startDate"),
            end_date: Optional[Date] = Query(None, alias="endDate"),
            user: CurrentUser = Depends(require_permission("VIEW_REPORTS"))):
    start, end = _period(period, start_date, end_date)
    p = {"zones": user.zone_ids, "start": start, "end": end}
    window = "ss.started_at >= %(start)s::date AND ss.started_at < %(end)s::date + 1"
    with db.tx() as cur:
        monthly = db.all_rows(cur, SESSIONS_CTE + f"""
            SELECT to_char(mo, 'Mon YYYY') AS month, COALESCE(SUM(ss.total_amount), 0)::float AS "revenueINR",
                   COALESCE(SUM(ss.energy_delivered_kwh), 0)::float AS "energyKWh",
                   COUNT(ss.id) FILTER (WHERE ss.vehicle_type = 'car')::int AS "carSessions",
                   COUNT(ss.id) FILTER (WHERE ss.vehicle_type = 'bike')::int AS "bikeSessions",
                   COUNT(ss.id) FILTER (WHERE ss.vehicle_type = 'bus')::int AS "busSessions"
            FROM generate_series(date_trunc('month', %(start)s::date), date_trunc('month', %(end)s::date), interval '1 month') mo
            LEFT JOIN ss ON date_trunc('month', ss.started_at) = mo AND {window}
            GROUP BY mo ORDER BY mo
        """, p)
        cats = db.all_rows(cur, SESSIONS_CTE + f"""
            SELECT upper(vt::text) AS category, COUNT(ss.id)::int AS sessions,
                   COALESCE(SUM(ss.energy_delivered_kwh), 0)::float AS "energyKWh",
                   COALESCE(SUM(ss.total_amount), 0)::float AS "revenueINR"
            FROM unnest(enum_range(NULL::vehicle_type)) vt LEFT JOIN ss ON ss.vehicle_type = vt AND {window}
            GROUP BY vt ORDER BY vt
        """, p)
        zones = db.all_rows(cur, f"""
            SELECT z.code AS "zoneCode", z.name AS "zoneName", COUNT(cs.id)::int AS sessions,
                   COALESCE(SUM(cs.energy_delivered_kwh), 0)::float AS "energyKWh",
                   COALESCE(SUM(cs.total_amount), 0)::float AS "revenueINR",
                   ROUND(COALESCE(SUM(EXTRACT(EPOCH FROM COALESCE(cs.ended_at, now()) - cs.started_at)), 0)
                         / NULLIF((SELECT COUNT(*) FROM charging_points p JOIN stations st ON st.id = p.station_id
                                   WHERE st.zone_id = z.id) * EXTRACT(EPOCH FROM (%(end)s::date + 1 - %(start)s::date) * interval '1 day'), 0)
                         * 100, 1)::float AS "utilizationPercent"
            FROM zones z
            LEFT JOIN stations s ON s.zone_id = z.id
            LEFT JOIN charging_points cp ON cp.station_id = s.id
            LEFT JOIN charging_sessions cs ON cs.charging_point_id = cp.id
                 AND cs.started_at >= %(start)s::date AND cs.started_at < %(end)s::date + 1
            WHERE {zf("z.id")}
            GROUP BY z.id ORDER BY "revenueINR" DESC, z.code
        """, p)
    total_sessions = sum(c["sessions"] for c in cats)
    for c in cats:
        c["progressPercent"] = round(c["sessions"] / total_sessions * 100, 1) if total_sessions else 0
    for z in zones:
        u = z["utilizationPercent"] or 0
        z["utilizationPercent"] = u
        z["performance"] = "High" if u >= 25 else "Moderate" if u >= 10 else "Low"
    return {
        "period": period.upper(), "startDate": start.isoformat(), "endDate": end.isoformat(),
        "monthlyMetrics": monthly, "vehicleCategories": cats, "zonePerformance": zones,
        "summary": {"totalRevenueINR": round(sum(c["revenueINR"] for c in cats), 2),
                    "totalEnergyKWh": round(sum(c["energyKWh"] for c in cats), 3), "totalSessions": total_sessions,
                    "avgUtilizationPercent": round(sum(z["utilizationPercent"] for z in zones) / len(zones), 1) if zones else 0},
    }


@router.get("/reports/sessions.csv", summary="Export sessions (EXPORT_REPORTS)")
def export_sessions(request: Request, start_date: Optional[Date] = Query(None, alias="startDate"),
                    end_date: Optional[Date] = Query(None, alias="endDate"),
                    user: CurrentUser = Depends(require_permission("EXPORT_REPORTS"))):
    start, end = _period("1M", start_date, end_date)
    with db.tx() as cur:
        rows = _sessions(cur, user, from_=start, to=end, limit=100000)
        ledger.audit(cur, user, request, action=f"Exported {len(rows)} sessions ({start} to {end})", action_type="EXPORT",
                     module="Reports", target="Charging sessions CSV")
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["sessionNumber", "startedAt", "endTime", "zoneCode", "stationName", "pointCode", "customerName", "vehiclePlate",
            "vehicleType", "energyDeliveredKWh", "durationMinutes", "totalCostINR", "paymentStatus", "status"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[c] for c in cols])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="sessions_{start}_{end}.csv"'})


class SettingsIn(CamelModel):
    network_name: Optional[str] = Field(None, min_length=3, max_length=120)
    auto_load_shedding: Optional[bool] = None
    admin_session_timeout_mins: Optional[int] = Field(None, ge=5, le=240)
    reservation_grace_minutes: Optional[int] = Field(None, ge=0, le=60)


SETTING_KEYS = {"network_name": "network_name", "auto_load_shedding": "auto_load_shedding",
                "admin_session_timeout_mins": "admin_session_timeout_mins", "reservation_grace_minutes": "reservation_grace_minutes"}


@router.get("/settings")
def get_system_settings(user: CurrentUser = Depends(require_staff)):
    with db.tx() as cur:
        rows = db.all_rows(cur, "SELECT key, value FROM system_settings")
    vals = {r["key"]: r["value"] for r in rows}
    return {"networkName": vals.get("network_name"), "autoLoadShedding": vals.get("auto_load_shedding"),
            "adminSessionTimeoutMins": vals.get("admin_session_timeout_mins"),
            "reservationGraceMinutes": vals.get("reservation_grace_minutes"),
            "otpResendCooldownSeconds": vals.get("otp_resend_cooldown_seconds")}


@router.put("/settings")
def update_system_settings(body: SettingsIn, request: Request,
                           user: CurrentUser = Depends(require_permission("MANAGE_SECURITY", "MANAGE_GRID"))):
    if not user.is_global:
        raise ApiError(403, "Only network-wide administrators can change global settings.")
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        before = {r["key"]: r["value"] for r in db.all_rows(cur, "SELECT key, value FROM system_settings")}
        for key, value in data.items():
            cur.execute("""INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s::jsonb, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
                        (SETTING_KEYS[key], json.dumps(value), user.id))
        ledger.audit(cur, user, request, action="Updated network settings", action_type="Updated", module="Security",
                     target="System settings", previous={k: before.get(SETTING_KEYS[k]) for k in data}, new=data, severity="notice")
    return get_system_settings(user)


# --- customer support (staff side) ---

SUPPORT_SQL = f"""
    SELECT t.id::text AS id, t.ticket_number AS "ticketNumber", t.subject, t.category, t.description,
           u.full_name AS "customerName", u.id::text AS "customerId", s.name AS "stationName", cp.point_code AS "chargerId",
           CASE t.status WHEN 'open' THEN 'Open' WHEN 'in_progress' THEN 'In Progress' WHEN 'waiting_for_customer' THEN 'Waiting for Customer'
                WHEN 'resolved' THEN 'Resolved' ELSE 'Closed' END AS status,
           au.full_name AS "assignedTo", t.created_at AS "createdAt", t.updated_at AS "lastUpdate",
           COALESCE((SELECT json_agg(json_build_object('id', r.id::text, 'sender', r.sender, 'message', r.message,
                                                       'timestamp', r.created_at) ORDER BY r.created_at)
                     FROM support_ticket_replies r WHERE r.ticket_id = t.id), '[]'::json) AS replies
    FROM support_tickets t JOIN users u ON u.id = t.customer_id JOIN customer_profiles c ON c.user_id = u.id
    LEFT JOIN stations s ON s.id = t.station_id LEFT JOIN charging_points cp ON cp.id = t.charging_point_id
    LEFT JOIN users au ON au.id = t.assigned_to
    WHERE {zf("c.zone_id")} AND (%(tid)s::bigint IS NULL OR t.id = %(tid)s)
      AND (%(status)s::text IS NULL OR t.status::text = %(status)s)
    ORDER BY t.updated_at DESC
"""

SUPPORT_STATUSES = {"open", "in_progress", "waiting_for_customer", "resolved", "closed"}


def _support(cur, user, tid=None, status=None):
    return db.all_rows(cur, SUPPORT_SQL, {"zones": user.zone_ids, "tid": tid, "status": status})


class SupportUpdateIn(CamelModel):
    status: Optional[str] = None
    assign_to_me: bool = False


@router.get("/support-tickets")
def list_support(status: Optional[str] = None,
                 user: CurrentUser = Depends(require_permission("USER_MANAGEMENT", "VIEW_SESSIONS"))):
    st = parse_enum(status, SUPPORT_STATUSES, "status") if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return _support(cur, user, status=st)


@router.post("/support-tickets/{ticket_id}/replies")
def reply_support(ticket_id: int, body: NoteIn, user: CurrentUser = Depends(require_permission("USER_MANAGEMENT", "VIEW_SESSIONS"))):
    with db.tx() as cur:
        rows = _support(cur, user, ticket_id)
        if not rows:
            raise not_found("Support ticket")
        cur.execute("INSERT INTO support_ticket_replies (ticket_id, sender, sender_user_id, message) VALUES (%s, 'support', %s, %s)",
                    (ticket_id, user.id, body.note))
        cur.execute("""UPDATE support_tickets SET status = 'waiting_for_customer', assigned_to = COALESCE(assigned_to, %s)
                       WHERE id = %s AND status NOT IN ('resolved', 'closed')""", (user.id, ticket_id))
        ledger.notify(cur, int(rows[0]["customerId"]), "system", f'Reply on {rows[0]["ticketNumber"]}', body.note[:200],
                      route="/customer/support", entity_type="support_ticket", entity_id=ticket_id)
        return _support(cur, user, ticket_id)[0]


@router.patch("/support-tickets/{ticket_id}")
def update_support(ticket_id: int, body: SupportUpdateIn,
                   user: CurrentUser = Depends(require_permission("USER_MANAGEMENT", "VIEW_SESSIONS"))):
    with db.tx() as cur:
        if not _support(cur, user, ticket_id):
            raise not_found("Support ticket")
        if body.status:
            st = parse_enum(body.status, SUPPORT_STATUSES, "status")
            cur.execute("""UPDATE support_tickets SET status = %s,
                           resolved_at = CASE WHEN %s IN ('resolved', 'closed') THEN COALESCE(resolved_at, now()) END
                           WHERE id = %s""", (st, st, ticket_id))
        if body.assign_to_me:
            cur.execute("UPDATE support_tickets SET assigned_to = %s WHERE id = %s", (user.id, ticket_id))
        return _support(cur, user, ticket_id)[0]

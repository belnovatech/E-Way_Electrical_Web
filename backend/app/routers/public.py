# Public endpoints - station finder, QR check, landing page stats.
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from .. import db
from ..errors import ApiError, not_found
from ..services.pricing import tariff_lateral
from ..utils import parse_vehicle_type

router = APIRouter(tags=["Stations"])

DEFAULT_CENTER = (16.5062, 80.6480)   # Vijayawada

# show an idle charger as 'reserved' if someone has booked it soon
POINT_STATUS_SQL = """
    CASE WHEN cp.status IN ('maintenance', 'offline') THEN 'offline'
         WHEN cp.status = 'available' AND EXISTS (
              SELECT 1 FROM reservations r WHERE r.charging_point_id = cp.id
                 AND r.status IN ('upcoming', 'confirmed')
                 AND tstzrange(r.scheduled_start - interval '15 minutes', r.scheduled_end) && tstzrange(now(), now() + interval '15 minutes'))
         THEN 'reserved'
         ELSE cp.status::text END
"""

DISTANCE_SQL = """
    (6371 * 2 * asin(sqrt(power(sin(radians(s.latitude - %(lat)s) / 2), 2)
        + cos(radians(%(lat)s)) * cos(radians(s.latitude)) * power(sin(radians(s.longitude - %(lng)s) / 2), 2))))
"""


def _station_rows(cur, *, station_id: int | None = None, search: str | None = None, city: str | None = None,
                  lat: float | None = None, lng: float | None = None, radius_km: float | None = None,
                  vehicle_type: str | None = None, min_power: float | None = None, limit: int = 200) -> list[dict]:
    has_center = lat is not None and lng is not None
    params = {"lat": lat if has_center else DEFAULT_CENTER[0], "lng": lng if has_center else DEFAULT_CENTER[1],
              "vt": vehicle_type or "car", "station_id": station_id, "search": f"%{search}%" if search else None,
              "city": f"%{city}%" if city else None, "radius": radius_km, "min_power": min_power,
              "vfilter": vehicle_type, "limit": limit}
    stations = db.all_rows(cur, f"""
        SELECT * FROM (
            SELECT s.id, s.code, s.name, s.address, s.city, s.latitude, s.longitude, s.operating_hours, s.amenities,
                   s.supported_vehicle_types::text[] AS supported_vehicle_types, z.id AS zone_id, z.name AS zone_name, z.code AS zone_code,
                   {DISTANCE_SQL} AS distance_km, tr.rate_per_kwh,
                   (SELECT MAX(power_kw) FROM charging_points WHERE station_id = s.id) AS max_power
            FROM stations s
            JOIN zones z ON z.id = s.zone_id
            LEFT JOIN LATERAL ({tariff_lateral("s.zone_id", "%(vt)s::vehicle_type")}) tr ON true
            WHERE s.status = 'active' AND z.status <> 'inactive'
              AND (%(station_id)s::bigint IS NULL OR s.id = %(station_id)s)
              AND (%(search)s::text IS NULL OR s.name ILIKE %(search)s OR s.address ILIKE %(search)s
                   OR s.city ILIKE %(search)s OR z.name ILIKE %(search)s)
              AND (%(city)s::text IS NULL OR s.city ILIKE %(city)s OR z.city ILIKE %(city)s)
              AND (%(vfilter)s::vehicle_type IS NULL OR %(vfilter)s::vehicle_type = ANY(s.supported_vehicle_types))
        ) x
        WHERE (%(radius)s::float IS NULL OR x.distance_km <= %(radius)s)
          AND (%(min_power)s::float IS NULL OR x.max_power >= %(min_power)s)
        ORDER BY x.distance_km, x.name
        LIMIT %(limit)s
    """, params)
    if not stations:
        return []
    points = db.all_rows(cur, f"""
        SELECT cp.id, cp.station_id, cp.point_code, cp.display_name, cp.charger_number, cp.power_kw, cp.connector_type,
               cp.current_type, {POINT_STATUS_SQL} AS live_status
        FROM charging_points cp WHERE cp.station_id = ANY(%s) ORDER BY cp.station_id, cp.charger_number
    """, ([s["id"] for s in stations],))
    by_station: dict[int, list[dict]] = {}
    for p in points:
        by_station.setdefault(p["station_id"], []).append(p)

    out = []
    for s in stations:
        chargers = by_station.get(s["id"], [])
        rate = float(s["rate_per_kwh"]) if s["rate_per_kwh"] is not None else None
        available = sum(1 for c in chargers if c["live_status"] == "available")
        total = len(chargers)
        connectors = sorted({c["connector_type"] for c in chargers})
        out.append({
            "id": str(s["id"]), "code": s["code"], "name": s["name"], "address": s["address"], "city": s["city"],
            "zone": s["zone_name"], "zoneId": str(s["zone_id"]), "zoneCode": s["zone_code"],
            "lat": float(s["latitude"]), "lng": float(s["longitude"]),
            "distanceKm": round(float(s["distance_km"]), 1) if has_center else None,
            "travelTimeMin": round(float(s["distance_km"]) / 30 * 60) + 2 if has_center else None,
            "powerKw": float(s["max_power"] or 0),
            "connectorType": " / ".join(connectors),
            "vehicleTypes": [v.capitalize() for v in s["supported_vehicle_types"]],
            "ratePerKWh": rate,
            "availableChargers": available, "totalChargers": total,
            "status": "full" if available == 0 else "partial" if available < total / 2 else "available",
            "waitTimeMin": 15 if available == 0 and total else None,
            "chargers": [{
                "id": str(c["id"]), "code": c["point_code"],
                "name": c["display_name"] or f'Charger {c["charger_number"]}',
                "powerKw": float(c["power_kw"]), "connector": c["connector_type"],
                "currentType": c["current_type"], "status": c["live_status"], "currentRateINR": rate,
            } for c in chargers],
            "amenities": s["amenities"], "operatingHours": s["operating_hours"],
        })
    return out


@router.get("/stations", summary="Find stations (EVStation[])")
def list_stations(search: Optional[str] = None, city: Optional[str] = None,
                  center_lat: Optional[float] = Query(None, alias="centerLat", ge=-90, le=90),
                  center_lng: Optional[float] = Query(None, alias="centerLng", ge=-180, le=180),
                  radius_km: Optional[float] = Query(None, alias="radiusKm", gt=0, le=1000),
                  vehicle_type: Optional[str] = Query(None, alias="vehicleType"),
                  min_power_kw: Optional[float] = Query(None, alias="minPowerKw", ge=0)):
    vt = None if vehicle_type in (None, "", "All", "all") else parse_vehicle_type(vehicle_type)
    with db.tx() as cur:
        return _station_rows(cur, search=search, city=city, lat=center_lat, lng=center_lng,
                             radius_km=radius_km, vehicle_type=vt, min_power=min_power_kw)


@router.get("/stations/{station_id}")
def get_station(station_id: int, lat: Optional[float] = None, lng: Optional[float] = None,
                vehicle_type: Optional[str] = Query(None, alias="vehicleType")):
    with db.tx() as cur:
        rows = _station_rows(cur, station_id=station_id, lat=lat, lng=lng, vehicle_type=parse_vehicle_type(vehicle_type))
    if not rows:
        raise not_found("Station")
    return rows[0]


@router.get("/stations/{station_id}/chargers")
def station_chargers(station_id: int, vehicle_type: Optional[str] = Query(None, alias="vehicleType")):
    return get_station(station_id, vehicle_type=vehicle_type)["chargers"]


def resolve_point(cur, ref: str, lock: bool = False) -> dict:
    # id or point code (QR has the point code)
    sql = """
        SELECT cp.*, s.name AS station_name, s.address, s.city, s.zone_id, s.status AS station_status,
               s.supported_vehicle_types::text[] AS supported_vehicle_types
        FROM charging_points cp JOIN stations s ON s.id = cp.station_id
        WHERE """ + ("cp.id = %s" if ref.isdigit() else "upper(cp.point_code) = upper(%s)") + (" FOR UPDATE OF cp" if lock else "")
    row = db.one(cur, sql, (int(ref) if ref.isdigit() else ref,))
    if row is None:
        raise not_found("Charger")
    return row


@router.get("/chargers/{charger_ref}/validate", summary="Validate a scanned charger QR (ChargerQRPayload)")
def validate_charger(charger_ref: str, vehicle_type: Optional[str] = Query("car", alias="vehicleType")):
    vt = parse_vehicle_type(vehicle_type) or "car"
    with db.tx() as cur:
        try:
            p = resolve_point(cur, charger_ref.strip())
        except ApiError:
            return {"success": False, "error": "This QR code does not match any E-Way charger."}
        status = db.scalar(cur, f"SELECT {POINT_STATUS_SQL} FROM charging_points cp WHERE cp.id = %s", (p["id"],))
        rate = db.scalar(cur, f"SELECT rate_per_kwh FROM ({tariff_lateral('%(z)s', '%(vt)s::vehicle_type')}) t",
                         {"z": p["zone_id"], "vt": vt})
    available = status == "available" and p["station_status"] == "active"
    data = {
        "chargerId": p["point_code"], "chargerDbId": str(p["id"]), "stationId": str(p["station_id"]),
        "stationName": p["station_name"], "location": f'{p["address"]}, {p["city"]}',
        "connectorType": p["connector_type"], "powerKw": float(p["power_kw"]),
        "ratePerKwh": float(rate) if rate is not None else None, "isAvailable": available,
        "statusText": {"available": "Ready to charge", "charging": "Charger is in use", "reserved": "Reserved for a booking",
                       "offline": "Charger is offline"}.get(status, status),
    }
    if not available:
        return {"success": False, "data": data, "error": f'Charger {p["point_code"]} is not available ({data["statusText"]}).'}
    return {"success": True, "data": data}


@router.get("/zones/nearby", summary="Nearby charging hubs (NearbyZone[])")
def nearby_zones(lat: Optional[float] = None, lng: Optional[float] = None, limit: int = Query(5, ge=1, le=50)):
    with db.tx() as cur:
        rows = _station_rows(cur, lat=lat if lat is not None else DEFAULT_CENTER[0],
                             lng=lng if lng is not None else DEFAULT_CENTER[1], limit=limit)
    return [{
        "id": s["id"], "name": s["name"], "city": s["city"], "distanceKm": s["distanceKm"], "etaMinutes": s["travelTimeMin"],
        "status": "available" if s["availableChargers"] else ("busy" if s["totalChargers"] else "unavailable"),
        "availablePointsCount": s["availableChargers"],
    } for s in rows]


@router.get("/operations/summary", tags=["Operations"], summary="Network totals for the landing page")
def operations_summary():
    with db.tx() as cur:
        return db.one(cur, """
            SELECT COUNT(cp.id)::int AS "totalChargers",
                   COUNT(DISTINCT z.id)::int AS "totalZones",
                   COUNT(DISTINCT s.city)::int AS "totalCities",
                   COUNT(cp.id) FILTER (WHERE cp.status = 'available')::int AS "availableChargers",
                   (SELECT COUNT(*) FROM charging_sessions WHERE status IN ('starting','charging','stopping'))::int AS "activeSessions",
                   (SELECT COALESCE(SUM(total_amount), 0)::float FROM charging_sessions
                     WHERE started_at >= date_trunc('day', now()))::float AS "todayRevenueINR",
                   COALESCE(MIN(z.state), 'Andhra Pradesh') AS region
            FROM zones z JOIN stations s ON s.zone_id = z.id JOIN charging_points cp ON cp.station_id = s.id
            WHERE z.status <> 'inactive'
        """)


@router.get("/operations/chargers", tags=["Operations"], summary="Live charger board (ChargerStatusItem[])")
def operations_chargers(limit: int = Query(50, ge=1, le=500)):
    with db.tx() as cur:
        return db.all_rows(cur, f"""
            SELECT cp.point_code AS id, s.name AS "stationName", s.city, z.name AS zone, cp.power_kw::float AS "powerKw",
                   CASE WHEN cp.status = 'reserved' THEN 'available' ELSE cp.status::text END AS status,
                   tr.rate_per_kwh::float AS "currentRateINR",
                   (SELECT (EXTRACT(EPOCH FROM now() - cs.started_at) / 60)::int FROM charging_sessions cs
                     WHERE cs.charging_point_id = cp.id AND cs.status IN ('starting','charging','stopping')) AS "activeSessionDurationMin"
            FROM charging_points cp JOIN stations s ON s.id = cp.station_id JOIN zones z ON z.id = s.zone_id
            LEFT JOIN LATERAL ({tariff_lateral("s.zone_id", "'car'::vehicle_type")}) tr ON true
            ORDER BY z.code, s.name, cp.charger_number LIMIT %s
        """, (limit,))

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import Field

from .. import db
from ..errors import ApiError, not_found
from ..security import CurrentUser, require_customer
from ..services import ledger
from ..services.charging import LIVE, finalize_session, refresh_live, session_payload, summary_payload
from ..services.pricing import rate_at, resolve_tariff, session_cost
from ..utils import CamelModel, new_code, parse_vehicle_type
from .customer import _vehicle, _vehicles, check_vehicle_fits
from .public import resolve_point

router = APIRouter(prefix="/charging", tags=["Charging"])

RESERVATION_GRACE = "15 minutes"


class StartIn(CamelModel):
    charger_id: str = Field(min_length=1, description="Point code from the QR code, or charging point id")
    vehicle_id: Optional[int] = None
    reservation_id: Optional[int] = None


def _own_session(cur, user: CurrentUser, session_id: int) -> dict:
    row = db.one(cur, "SELECT id, status FROM charging_sessions WHERE id = %s AND customer_id = %s", (session_id, user.id))
    if row is None:
        raise not_found("Charging session")
    return row


@router.post("/sessions", status_code=201, summary="Start charging (ChargingSession)")
def start_session(body: StartIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        point = resolve_point(cur, body.charger_id, lock=True)
        if point["status"] in ("maintenance", "offline") or point["station_status"] != "active":
            raise ApiError(409, f'Charger {point["point_code"]} is not available right now.')
        if point["status"] == "charging":
            raise ApiError(409, f'Charger {point["point_code"]} is already in use.')

        if body.vehicle_id is not None:
            vehicle = _vehicle(cur, user.id, body.vehicle_id)
        else:
            vehicles = _vehicles(cur, user.id)
            if not vehicles:
                raise ApiError(422, "Add a vehicle before you start charging.")
            vehicle = vehicles[0]                       # default vehicle sorts first
        check_vehicle_fits(point, vehicle)

        # a reservation for this charger that covers "now"
        reservation = db.one(cur, f"""
            SELECT * FROM reservations
            WHERE charging_point_id = %(p)s AND status IN ('upcoming', 'confirmed')
              AND tstzrange(scheduled_start - interval '{RESERVATION_GRACE}', scheduled_end) @> now()
              AND (%(rid)s::bigint IS NULL OR id = %(rid)s)
            ORDER BY scheduled_start LIMIT 1 FOR UPDATE
        """, {"p": point["id"], "rid": body.reservation_id})
        if body.reservation_id is not None:
            mine = db.one(cur, "SELECT * FROM reservations WHERE id = %s AND customer_id = %s", (body.reservation_id, user.id))
            if mine is None:
                raise not_found("Reservation")
            if reservation is None:
                raise ApiError(409, "This reservation is not for this charger or is outside its time slot.")
        if reservation is not None and reservation["customer_id"] != user.id:
            raise ApiError(409, "This charger is reserved by another customer right now.", "charger_reserved")

        vt = parse_vehicle_type(vehicle["type"])
        tariff = resolve_tariff(cur, point["zone_id"], vt)
        rate = Decimal(reservation["rate_per_kwh"]) if reservation else rate_at(tariff)
        balance = Decimal(ledger.wallet_of(cur, user.id)["balance"])
        minimum = session_cost(tariff, Decimal("0.001"), rate)["total"]   # min-charge rule means this is the floor
        if balance < minimum:
            raise ApiError(402, f"Insufficient wallet balance. Please top up at least Rs {minimum - balance} to start charging.",
                           "insufficient_balance")
        if db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM charging_sessions WHERE customer_id = %s AND status = ANY(%s))",
                     (user.id, list(LIVE))):
            raise ApiError(409, "You already have an active charging session.")

        row = db.one(cur, """
            INSERT INTO charging_sessions (session_number, customer_id, vehicle_id, charging_point_id, reservation_id,
                                           tariff_plan_id, status, start_soc, current_soc, rate_per_kwh, peak_power_kw)
            VALUES (%s, %s, %s, %s, %s, %s, 'charging', %s, %s, %s, %s) RETURNING id
        """, (new_code("SES"), user.id, int(vehicle["id"]), point["id"], reservation["id"] if reservation else None,
              tariff["id"], vehicle["batteryPercentage"], vehicle["batteryPercentage"], rate, point["power_kw"]))
        if reservation:
            cur.execute("UPDATE reservations SET status = 'active' WHERE id = %s", (reservation["id"],))
        return session_payload(cur, row["id"])


@router.get("/sessions/active", summary="Live session or null")
def active_session(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        sid = db.scalar(cur, "SELECT id FROM charging_sessions WHERE customer_id = %s AND status = ANY(%s)",
                        (user.id, list(LIVE)))
        if sid is None:
            return None
        limit = refresh_live(cur, sid)
        return {**session_payload(cur, sid), "autoStopReason": limit}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        s = _own_session(cur, user, session_id)
        if s["status"] in LIVE:
            refresh_live(cur, session_id)
            return session_payload(cur, session_id)
        return summary_payload(cur, session_id)


@router.post("/sessions/{session_id}/stop", summary="Stop and bill (ChargingSummaryData)")
def stop_session(session_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        _own_session(cur, user, session_id)
        return finalize_session(cur, session_id, "Stopped by customer")

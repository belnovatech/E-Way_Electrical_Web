# Called by the charger gateway. Needs X-Charger-Key; off if OCPP_GATEWAY_KEY is empty.
from __future__ import annotations

import hmac
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import Field

from .. import db
from ..config import get_settings
from ..errors import ApiError, not_found
from ..services import ledger
from ..services.charging import LIVE, finalize_session
from ..utils import CamelModel, money
from .public import resolve_point

router = APIRouter(prefix="/ocpp", tags=["Charger gateway"])


def require_gateway(x_charger_key: Optional[str] = Header(None)) -> None:
    key = get_settings().ocpp_gateway_key
    if not key:
        raise ApiError(503, "Charger gateway is not configured (set OCPP_GATEWAY_KEY).")
    if not x_charger_key or not hmac.compare_digest(x_charger_key, key):
        raise ApiError(401, "Invalid charger gateway key.")


class HeartbeatIn(CamelModel):
    charger_id: str


class MeterIn(CamelModel):
    session_number: str
    energy_delivered_kwh: Decimal = Field(ge=0)
    current_soc: Optional[int] = Field(None, ge=0, le=100)
    power_kw: Optional[Decimal] = Field(None, ge=0)


class StatusIn(CamelModel):
    charger_id: str
    status: Literal["Available", "Charging", "Faulted", "Unavailable"]
    error_code: Optional[str] = Field(None, max_length=60)
    vendor_error_code: Optional[str] = Field(None, max_length=60)
    info: Optional[str] = Field(None, max_length=500)
    temperature_c: Optional[float] = None
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    ground_leakage_ma: Optional[float] = Field(None, ge=0)


@router.post("/heartbeat", dependencies=[Depends(require_gateway)])
def heartbeat(body: HeartbeatIn):
    with db.tx() as cur:
        p = resolve_point(cur, body.charger_id)
        cur.execute("UPDATE charging_points SET last_ping_at = now() WHERE id = %s", (p["id"],))
    return {"success": True}


@router.post("/meter-values", dependencies=[Depends(require_gateway)])
def meter_values(body: MeterIn):
    with db.tx() as cur:
        s = db.one(cur, "SELECT * FROM charging_sessions WHERE session_number = %s FOR UPDATE", (body.session_number,))
        if s is None:
            raise not_found("Charging session")
        if s["status"] not in LIVE:
            raise ApiError(409, "Session is not live.")
        energy = max(Decimal(s["energy_delivered_kwh"]), body.energy_delivered_kwh)
        cur.execute("""
            UPDATE charging_sessions SET energy_delivered_kwh = %s, energy_cost = %s, current_soc = COALESCE(%s, current_soc),
                   peak_power_kw = GREATEST(COALESCE(peak_power_kw, 0), COALESCE(%s, 0))
            WHERE id = %s
        """, (energy, money(energy * Decimal(s["rate_per_kwh"])), body.current_soc, body.power_kw, s["id"]))
        cur.execute("UPDATE charging_points SET last_ping_at = now() WHERE id = %s", (s["charging_point_id"],))
    return {"success": True, "energyDeliveredKwh": float(energy)}


@router.post("/status-notification", dependencies=[Depends(require_gateway)])
def status_notification(body: StatusIn):
    # on Faulted: charger -> maintenance, stop the live session, open one ticket
    with db.tx() as cur:
        p = resolve_point(cur, body.charger_id, lock=True)
        cur.execute("UPDATE charging_points SET last_ping_at = now() WHERE id = %s", (p["id"],))
        ticket_id = None
        if body.status == "Faulted":
            live = db.scalar(cur, "SELECT id FROM charging_sessions WHERE charging_point_id = %s AND status = ANY(%s)",
                             (p["id"], list(LIVE)))
            if live:
                finalize_session(cur, live, f"Charger fault: {body.error_code or 'unknown'}")
                cur.execute("UPDATE charging_sessions SET status = 'interrupted' WHERE id = %s", (live,))
            cur.execute("UPDATE charging_points SET status = 'maintenance' WHERE id = %s", (p["id"],))
            ticket_id = db.scalar(cur, "SELECT id FROM maintenance_tickets WHERE charging_point_id = %s AND status <> 'resolved' "
                                       "ORDER BY reported_at DESC LIMIT 1", (p["id"],))
            if ticket_id is None:
                code = db.scalar(cur, r"""SELECT 'MT-' || (COALESCE(MAX(substring(ticket_code FROM '^MT-(\d+)$')::int), 1000) + 1)
                                          FROM maintenance_tickets""")
                ticket_id = db.scalar(cur, """
                    INSERT INTO maintenance_tickets (ticket_code, charging_point_id, fault_code, issue_description, severity,
                                                     priority, sla_minutes)
                    VALUES (%s, %s, %s, %s, 'high', 'P2', 120) RETURNING id
                """, (code, p["id"], body.error_code, body.info or f'Charger reported fault {body.error_code or ""}'.strip()))
                cur.execute("""INSERT INTO maintenance_ticket_events (ticket_id, actor_name, actor_type, action, note)
                               VALUES (%s, 'OCPP Gateway', 'system', 'Fault reported', %s)""",
                            (ticket_id, f"StatusNotification Faulted / {body.error_code}"))
                ledger.notify_zone_admins(cur, p["zone_id"], "maintenance", f'Fault on {p["point_code"]}',
                                          body.info or body.error_code or "Charger fault", route="/admin/maintenance",
                                          entity_type="maintenance_ticket", entity_id=ticket_id)
            cur.execute("""
                INSERT INTO charger_diagnostics (charging_point_id, ticket_id, connection_status, connector_status, rated_power_kw,
                                                 temperature_c, ground_leakage_ma, voltage_v, current_a, ocpp_error_code, vendor_error_code)
                VALUES (%s, %s, 'degraded', 'fault', %s, %s, %s, %s, %s, %s, %s)
            """, (p["id"], ticket_id, p["power_kw"], body.temperature_c, body.ground_leakage_ma, body.voltage_v, body.current_a,
                  body.error_code, body.vendor_error_code))
        elif body.status == "Unavailable":
            cur.execute("UPDATE charging_points SET status = 'offline' WHERE id = %s AND status <> 'charging'", (p["id"],))
        elif body.status == "Available":
            cur.execute("""UPDATE charging_points SET status = 'available' WHERE id = %s AND status IN ('offline', 'maintenance')
                           AND NOT EXISTS (SELECT 1 FROM maintenance_tickets WHERE charging_point_id = %s AND status <> 'resolved')""",
                        (p["id"], p["id"]))
    return {"success": True, "ticketId": str(ticket_id) if ticket_id else None}

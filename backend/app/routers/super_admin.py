# Super admin portal.
# areas = zones, "transactions" = admin_collections.
from __future__ import annotations

import csv
import io
import re
from datetime import date as Date, timedelta
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from .. import db
from ..errors import ApiError, not_found
from ..security import CurrentUser, require_super_admin
from ..services import ledger
from ..utils import CamelModel, initials, money, normalize_phone, parse_enum
from .auth import PASSWORD_RULES

router = APIRouter(prefix="/super-admin", tags=["Super Admin"])
SA = Depends(require_super_admin)

# what admins still owe = open settlements + net payable not batched yet
OUTSTANDING_SQL = """
    COALESCE((SELECT SUM(st.outstanding_balance) FROM admin_settlements st WHERE {f_st}), 0)
  + COALESCE((SELECT SUM(ac.net_payable) FROM admin_collections ac
              WHERE ac.transaction_status = 'success' AND ac.settlement_id IS NULL AND {f_ac}), 0)
"""


def _outstanding(f_st: str, f_ac: str) -> str:
    return OUTSTANDING_SQL.format(f_st=f_st, f_ac=f_ac)


# --- areas ---

AREA_SQL = f"""
    SELECT z.id::text AS id, z.code, z.name, z.city, z.state, vz.total_chargers::int AS "totalChargers",
           COALESCE((SELECT array_agg(a.admin_id::text ORDER BY a.admin_id) FROM admin_zone_assignments a
                     WHERE a.zone_id = z.id AND a.is_active), '{{}}') AS "assignedAdminIds",
           COALESCE((SELECT array_agg(u.full_name ORDER BY a.admin_id) FROM admin_zone_assignments a JOIN users u ON u.id = a.admin_id
                     WHERE a.zone_id = z.id AND a.is_active), '{{}}') AS "assignedAdminNames",
           (SELECT COUNT(*) FROM customer_profiles c WHERE c.zone_id = z.id)::int AS "totalCustomers",
           (SELECT COUNT(*) FROM customer_profiles c JOIN users u ON u.id = c.user_id
             WHERE c.zone_id = z.id AND u.status = 'active')::int AS "activeCustomers",
           (SELECT COUNT(*) FROM customer_profiles c JOIN users u ON u.id = c.user_id
             WHERE c.zone_id = z.id AND u.created_at >= now() - interval '30 days')::int AS "newCustomers",
           COALESCE((SELECT SUM(amount) FROM admin_collections ac WHERE ac.zone_id = z.id
                     AND ac.transaction_status = 'success'), 0)::float AS "totalCustomerCollectionsINR",
           COALESCE((SELECT SUM(verified_amount_received) FROM admin_settlements st WHERE st.zone_id = z.id), 0)::float
               AS "actualBusinessReceiptsINR",
           ({_outstanding("st.zone_id = z.id", "ac.zone_id = z.id")})::float AS "outstandingSettlementsINR",
           (SELECT COUNT(*) FROM admin_zone_assignments a JOIN users u ON u.id = a.admin_id
             WHERE a.zone_id = z.id AND a.is_active AND u.status = 'active')::int AS "activeAdminsCount",
           (SELECT CASE WHEN prev > 0 THEN ROUND((cur - prev) / prev * 100, 1) END FROM (
               SELECT COALESCE(SUM(amount) FILTER (WHERE collected_at >= now() - interval '30 days'), 0) AS cur,
                      COALESCE(SUM(amount) FILTER (WHERE collected_at < now() - interval '30 days'
                                                     AND collected_at >= now() - interval '60 days'), 0) AS prev
               FROM admin_collections ac WHERE ac.zone_id = z.id AND ac.transaction_status = 'success') g)::float AS "growthRatePercent",
           CASE z.status WHEN 'active' THEN 'Active' WHEN 'degraded' THEN 'Degraded' ELSE 'Offline' END AS status
    FROM zones z JOIN v_zone_overview vz ON vz.id = z.id
    WHERE (%(zid)s::bigint IS NULL OR z.id = %(zid)s)
    ORDER BY z.code
"""


@router.get("/areas", summary="SuperAdminArea[]")
def list_areas(user: CurrentUser = SA):
    with db.tx() as cur:
        return db.all_rows(cur, AREA_SQL, {"zid": None})


# --- admins ---

ADMIN_SQL = f"""
    SELECT u.id::text AS id, u.public_code AS code, u.full_name AS name, u.email, u.phone_number AS phone,
           COALESCE((SELECT array_agg(a.zone_id::text ORDER BY z.code) FROM admin_zone_assignments a JOIN zones z ON z.id = a.zone_id
                     WHERE a.admin_id = u.id AND a.is_active), '{{}}') AS "assignedAreaIds",
           COALESCE((SELECT array_agg(z.name ORDER BY z.code) FROM admin_zone_assignments a JOIN zones z ON z.id = a.zone_id
                     WHERE a.admin_id = u.id AND a.is_active), '{{}}') AS "assignedAreaNames",
           COALESCE((SELECT array_agg(z.code ORDER BY z.code) FROM admin_zone_assignments a JOIN zones z ON z.id = a.zone_id
                     WHERE a.admin_id = u.id AND a.is_active), '{{}}') AS "assignedAreaCodes",
           initcap(u.status::text) AS status, sp.commission_rate_percent::float AS "commissionRatePercent",
           f.total_customers::int AS "totalCustomers",
           (SELECT COUNT(*) FROM customer_profiles c JOIN users cu ON cu.id = c.user_id
             WHERE c.assigned_admin_id = u.id AND cu.status = 'active')::int AS "activeCustomers",
           (SELECT COUNT(*) FROM customer_profiles c JOIN users cu ON cu.id = c.user_id
             WHERE c.assigned_admin_id = u.id AND cu.created_at >= now() - interval '30 days')::int AS "newCustomers",
           f.total_collections::float AS "totalCustomerCollectionsINR", f.business_receipts::float AS "actualBusinessReceiptsINR",
           ({_outstanding("st.admin_id = u.id", "ac.admin_id = u.id")})::float AS "outstandingSettlementINR",
           f.total_commission::float AS "totalCommissionEarnedINR", f.pending_customer_payments::float AS "pendingCustomerPaymentsINR",
           f.refunds_and_reversals::float AS "refundsAndReversalsINR",
           (SELECT MIN(ac.settlement_due_date) FROM admin_collections ac WHERE ac.admin_id = u.id
             AND ac.settlement_status <> 'settled' AND ac.transaction_status = 'success') AS "settlementDueDate",
           json_build_object('bankName', sp.bank_name, 'accountNumberMasked', sp.bank_account_masked, 'ifsc', sp.bank_ifsc) AS "bankDetails",
           COALESCE(u.last_active_at, u.last_login_at) AS "lastActivity", u.created_at AS "joinedDate"
    FROM users u JOIN staff_profiles sp ON sp.user_id = u.id JOIN roles r ON r.id = sp.role_id
    JOIN v_admin_financials f ON f.admin_id = u.id
    WHERE (%(aid)s::bigint IS NULL OR u.id = %(aid)s)
      AND (%(zid)s::bigint IS NULL OR EXISTS (SELECT 1 FROM admin_zone_assignments a WHERE a.admin_id = u.id
                                              AND a.zone_id = %(zid)s AND a.is_active))
    ORDER BY u.public_code NULLS LAST, u.id
"""


def _admins(cur, aid=None, zid=None) -> list[dict]:
    rows = db.all_rows(cur, ADMIN_SQL, {"aid": aid, "zid": zid})
    for r in rows:
        r["avatar"] = initials(r["name"])
    return rows


def _admin(cur, aid: int) -> dict:
    rows = _admins(cur, aid)
    if not rows:
        raise not_found("Admin")
    return rows[0]


class AdminCreateIn(CamelModel):
    name: str = Field(min_length=3, max_length=120)
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str
    password: str = Field(min_length=8, max_length=200)
    area_ids: list[int] = Field(min_length=1)
    commission_rate_percent: Decimal = Field(Decimal(5), ge=0, le=100)
    bank_name: Optional[str] = Field(None, max_length=100)
    account_last4: Optional[str] = Field(None, pattern=r"^\d{4}$")
    ifsc: Optional[str] = Field(None, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")


class AdminUpdateIn(CamelModel):
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    phone: Optional[str] = None
    commission_rate_percent: Optional[Decimal] = Field(None, ge=0, le=100)
    bank_name: Optional[str] = Field(None, max_length=100)
    account_last4: Optional[str] = Field(None, pattern=r"^\d{4}$")
    ifsc: Optional[str] = Field(None, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$")


class AdminStatusIn(CamelModel):
    status: Literal["Active", "Suspended", "Pending", "active", "suspended", "pending"]


class AdminAreasIn(CamelModel):
    area_ids: list[int]


@router.get("/admins", summary="SuperAdminAdmin[]")
def list_admins(area_id: Optional[int] = Query(None, alias="areaId"), user: CurrentUser = SA):
    with db.tx() as cur:
        return _admins(cur, zid=area_id)


@router.post("/admins", status_code=201)
def create_admin(body: AdminCreateIn, request: Request, user: CurrentUser = SA):
    missing = [label for pattern, label in PASSWORD_RULES if not re.search(pattern, body.password)]
    if missing:
        raise ApiError(422, "Password must contain " + ", ".join(missing) + ".")
    with db.tx() as cur:
        role_id = db.scalar(cur, "SELECT id FROM roles WHERE code = 'ROLE_ZONE_MANAGER'")
        code = db.scalar(cur, r"""SELECT 'ADM-AP' || lpad((COALESCE(MAX(substring(public_code FROM '^ADM-AP(\d+)$')::int), 0) + 1)::text, 2, '0')
                                  FROM users""")
        uid = db.scalar(cur, """
            INSERT INTO users (user_type, public_code, full_name, phone_number, email, password_hash, is_verified, password_changed_at)
            VALUES ('admin', %s, %s, %s, lower(%s::text), crypt(%s, gen_salt('bf', 10)), true, now()) RETURNING id
        """, (code, body.name, normalize_phone(body.phone), body.email, body.password))
        cur.execute("""INSERT INTO staff_profiles (user_id, user_type, role_id, department, commission_rate_percent, bank_name,
                                                   bank_account_masked, bank_ifsc, role_assigned_by)
                       VALUES (%s, 'admin', %s, 'Area Administration', %s, %s, %s, %s, %s)""",
                    (uid, role_id, body.commission_rate_percent, body.bank_name,
                     f"XXXX XXXX {body.account_last4}" if body.account_last4 else None, body.ifsc, user.id))
        _set_areas(cur, uid, body.area_ids, user.id)
        ledger.audit(cur, user, request, action=f"Created area admin {code} ({body.name})", action_type="ADMIN_MOD",
                     module="Admins", target=f"Admin {code}", record_id=uid, severity="notice",
                     new={k: v for k, v in body.model_dump().items() if k != "password"})
        return _admin(cur, uid)


@router.patch("/admins/{admin_id}")
def update_admin(admin_id: int, body: AdminUpdateIn, request: Request, user: CurrentUser = SA):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        before = _admin(cur, admin_id)
        if "name" in data:
            cur.execute("UPDATE users SET full_name = %s WHERE id = %s", (data["name"], admin_id))
        if "phone" in data:
            cur.execute("UPDATE users SET phone_number = %s WHERE id = %s", (normalize_phone(data["phone"]), admin_id))
        for key, col in (("commission_rate_percent", "commission_rate_percent"), ("bank_name", "bank_name"), ("ifsc", "bank_ifsc")):
            if key in data:
                cur.execute(f"UPDATE staff_profiles SET {col} = %s WHERE user_id = %s", (data[key], admin_id))
        if "account_last4" in data:
            cur.execute("UPDATE staff_profiles SET bank_account_masked = %s WHERE user_id = %s",
                        (f'XXXX XXXX {data["account_last4"]}' if data["account_last4"] else None, admin_id))
        after = _admin(cur, admin_id)
        ledger.audit(cur, user, request, action=f'Updated admin {before["code"]}', action_type="ADMIN_MOD", module="Admins",
                     target=f'Admin {before["code"]}', record_id=admin_id,
                     previous={k: before[k] for k in ("name", "phone", "commissionRatePercent", "bankDetails")},
                     new={k: after[k] for k in ("name", "phone", "commissionRatePercent", "bankDetails")}, severity="notice")
        return after


@router.patch("/admins/{admin_id}/status")
def update_admin_status(admin_id: int, body: AdminStatusIn, request: Request, user: CurrentUser = SA):
    status = body.status.lower()
    with db.tx() as cur:
        before = _admin(cur, admin_id)
        cur.execute("UPDATE users SET status = %s WHERE id = %s", (status, admin_id))
        if status != "active":
            cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (admin_id,))
        ledger.audit(cur, user, request, action=f'Set admin {before["code"]} to {status}', action_type="ADMIN_MOD",
                     module="Admins", target=f'Admin {before["code"]}', record_id=admin_id,
                     previous={"status": before["status"]}, new={"status": status},
                     severity="warning" if status == "suspended" else "notice")
        return _admin(cur, admin_id)


def _set_areas(cur, admin_id: int, area_ids: list[int], by: int) -> None:
    wanted = set(area_ids)
    found = {r["id"] for r in db.all_rows(cur, "SELECT id FROM zones WHERE id = ANY(%s)", (list(wanted),))}
    if wanted - found:
        raise ApiError(422, f"Unknown area id(s): {', '.join(map(str, sorted(wanted - found)))}")
    current = {r["zone_id"]: r["is_active"] for r in db.all_rows(
        cur, "SELECT zone_id, is_active FROM admin_zone_assignments WHERE admin_id = %s FOR UPDATE", (admin_id,))}
    for zid in wanted:
        if zid not in current:
            cur.execute("INSERT INTO admin_zone_assignments (admin_id, zone_id, assigned_by) VALUES (%s, %s, %s)", (admin_id, zid, by))
        elif not current[zid]:
            cur.execute("""UPDATE admin_zone_assignments SET is_active = true, unassigned_at = NULL, assigned_at = now(), assigned_by = %s
                           WHERE admin_id = %s AND zone_id = %s""", (by, admin_id, zid))
    for zid, active in current.items():
        if active and zid not in wanted:
            cur.execute("UPDATE admin_zone_assignments SET is_active = false, unassigned_at = now() WHERE admin_id = %s AND zone_id = %s",
                        (admin_id, zid))
            # move their customers in this area to another admin there (or nobody)
            cur.execute("""
                UPDATE customer_profiles SET assigned_admin_id = (
                    SELECT a.admin_id FROM admin_zone_assignments a WHERE a.zone_id = %(z)s AND a.is_active ORDER BY a.assigned_at LIMIT 1)
                WHERE zone_id = %(z)s AND assigned_admin_id = %(a)s
            """, {"z": zid, "a": admin_id})


@router.put("/admins/{admin_id}/areas", summary="Replace the admin's assigned areas")
def set_admin_areas(admin_id: int, body: AdminAreasIn, request: Request, user: CurrentUser = SA):
    with db.tx() as cur:
        before = _admin(cur, admin_id)
        _set_areas(cur, admin_id, body.area_ids, user.id)
        after = _admin(cur, admin_id)
        ledger.audit(cur, user, request, action=f'Changed areas of {before["code"]}', action_type="ADMIN_MOD", module="Admins",
                     target=f'Admin {before["code"]}', record_id=admin_id, previous={"areas": before["assignedAreaCodes"]},
                     new={"areas": after["assignedAreaCodes"]}, severity="notice")
        return after


# --- customers ---

CUSTOMER_SQL = """
    SELECT u.id::text AS id, u.public_code AS code, u.full_name AS name, u.phone_number AS phone, u.email,
           z.id::text AS "areaId", z.code AS "areaCode", z.name AS "areaName", c.assigned_admin_id::text AS "assignedAdminId",
           au.full_name AS "assignedAdminName", u.created_at AS "registrationDate", initcap(u.status::text) AS status,
           COALESCE(pay.paid, 0)::float AS "totalAmountPaidINR", COALESCE(dues.amount, 0)::float AS "outstandingBalanceINR",
           pay.last_at AS "lastPaymentDate", concat_ws(' ', dv.manufacturer, dv.model) AS "vehicleModel",
           dv.license_plate AS "vehiclePlate", COALESCE(ss.cnt, 0)::int AS "totalSessions",
           COALESCE(ss.kwh, 0)::float AS "energyConsumedKWh",
           CASE WHEN COALESCE(dues.amount, 0) = 0 THEN 'Up to Date'
                WHEN dues.oldest < now() - interval '7 days' THEN 'Overdue' ELSE 'Pending' END AS "paymentStatus",
           w.balance::float AS "walletBalanceINR"
    FROM users u JOIN customer_profiles c ON c.user_id = u.id JOIN wallets w ON w.customer_id = u.id
    LEFT JOIN zones z ON z.id = c.zone_id LEFT JOIN users au ON au.id = c.assigned_admin_id
    LEFT JOIN LATERAL (SELECT SUM(total_amount) AS paid, MAX(completed_at) AS last_at FROM payments p
                       WHERE p.customer_id = u.id AND p.status = 'success') pay ON true
    LEFT JOIN LATERAL (SELECT SUM(total_amount) AS amount, MIN(ended_at) AS oldest FROM charging_sessions s
                       WHERE s.customer_id = u.id AND s.payment_status = 'pending' AND s.ended_at IS NOT NULL) dues ON true
    LEFT JOIN LATERAL (SELECT COUNT(*) AS cnt, SUM(energy_delivered_kwh) AS kwh FROM charging_sessions s
                       WHERE s.customer_id = u.id) ss ON true
    LEFT JOIN LATERAL (SELECT * FROM vehicles v WHERE v.owner_id = u.id AND v.deleted_at IS NULL
                       ORDER BY v.is_default DESC, v.created_at LIMIT 1) dv ON true
    WHERE (%(zid)s::bigint IS NULL OR c.zone_id = %(zid)s)
      AND (%(aid)s::bigint IS NULL OR c.assigned_admin_id = %(aid)s)
      AND (%(status)s::text IS NULL OR u.status::text = %(status)s)
      AND (%(q)s::text IS NULL OR u.full_name ILIKE %(q)s OR u.phone_number ILIKE %(q)s OR u.email ILIKE %(q)s
           OR u.public_code ILIKE %(q)s OR dv.license_plate ILIKE %(q)s)
    ORDER BY u.public_code NULLS LAST
    LIMIT %(limit)s OFFSET %(offset)s
"""


@router.get("/customers", summary="SuperAdminCustomer[]")
def list_customers(area_id: Optional[int] = Query(None, alias="areaId"), admin_id: Optional[int] = Query(None, alias="adminId"),
                   search: Optional[str] = Query(None, alias="searchTerm"), status: Optional[str] = None,
                   limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0), user: CurrentUser = SA):
    st = status.lower() if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return db.all_rows(cur, CUSTOMER_SQL, {"zid": area_id, "aid": admin_id, "status": st,
                                               "q": f"%{search}%" if search else None, "limit": limit, "offset": offset})


# --- collections ("transactions") ---

TXN_SQL = """
    SELECT ac.id::text AS id, ac.reference_number AS "referenceNumber", to_char(ac.collected_at, 'DD Mon YYYY, HH12:MI AM') AS date,
           ac.collected_at AS "isoDate", ac.admin_id::text AS "adminId", au.full_name AS "adminName",
           ac.zone_id::text AS "areaId", z.name AS "areaName", z.code AS "areaCode", ac.customer_id::text AS "customerId",
           cu.full_name AS "customerName", ac.amount::float AS "amountINR",
           CASE ac.collection_type WHEN 'collection' THEN 'Collection' WHEN 'charging_session' THEN 'Charging Session'
                WHEN 'wallet_topup' THEN 'Wallet Topup' WHEN 'refund' THEN 'Refund' ELSE 'Reversal' END AS type,
           CASE ac.payment_method WHEN 'upi' THEN 'UPI' WHEN 'card' THEN 'Card' WHEN 'netbanking' THEN 'Net Banking'
                ELSE 'Wallet' END AS "paymentMethod",
           initcap(ac.transaction_status::text) AS "transactionStatus",
           CASE ac.settlement_status WHEN 'settled' THEN 'Settled' WHEN 'pending_settlement' THEN 'Pending Settlement'
                WHEN 'in_escrow' THEN 'In Escrow' ELSE 'Non-Settled' END AS "settlementStatus",
           ac.admin_commission::float AS "adminCommissionINR", ac.net_payable::float AS "netPayableToBusinessINR",
           ac.settlement_due_date AS "settlementDueDate", ac.notes, st.batch_number AS "settlementBatchId"
    FROM admin_collections ac JOIN users au ON au.id = ac.admin_id JOIN users cu ON cu.id = ac.customer_id
    JOIN zones z ON z.id = ac.zone_id LEFT JOIN admin_settlements st ON st.id = ac.settlement_id
    WHERE (%(zid)s::bigint IS NULL OR ac.zone_id = %(zid)s)
      AND (%(aid)s::bigint IS NULL OR ac.admin_id = %(aid)s)
      AND (%(status)s::text IS NULL OR ac.transaction_status::text = %(status)s)
      AND (%(from)s::date IS NULL OR ac.collected_at >= %(from)s::date)
      AND (%(to)s::date IS NULL OR ac.collected_at < %(to)s::date + 1)
      AND (%(q)s::text IS NULL OR ac.reference_number ILIKE %(q)s OR cu.full_name ILIKE %(q)s OR au.full_name ILIKE %(q)s)
    ORDER BY ac.collected_at DESC LIMIT %(limit)s
"""


@router.get("/transactions", summary="SuperAdminTransaction[]")
def list_transactions(area_id: Optional[int] = Query(None, alias="areaId"), admin_id: Optional[int] = Query(None, alias="adminId"),
                      status: Optional[str] = None, search: Optional[str] = Query(None, alias="searchTerm"),
                      from_date: Optional[Date] = Query(None, alias="startDate"), to_date: Optional[Date] = Query(None, alias="endDate"),
                      limit: int = Query(500, ge=1, le=5000), user: CurrentUser = SA):
    st = status.lower() if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return db.all_rows(cur, TXN_SQL, {"zid": area_id, "aid": admin_id, "status": st, "from": from_date, "to": to_date,
                                          "q": f"%{search}%" if search else None, "limit": limit})


# --- settlements ---

SETTLEMENT_SQL = """
    SELECT st.id::text AS id, st.batch_number AS "batchNumber", st.admin_id::text AS "adminId", au.full_name AS "adminName",
           st.zone_id::text AS "areaId", z.name AS "areaName", z.code AS "areaCode", st.period_start AS "periodStartDate",
           st.period_end AS "periodEndDate", st.gross_collections::float AS "grossCollectionsINR",
           st.refunds_deducted::float AS "refundsDeductedINR", st.commission_deducted::float AS "commissionDeductedINR",
           st.other_adjustments::float AS "otherAdjustmentsINR", st.net_payable::float AS "netAmountPayableToBusinessINR",
           st.verified_amount_received::float AS "verifiedAmountReceivedINR", st.outstanding_balance::float AS "outstandingBalanceINR",
           CASE st.status WHEN 'verified' THEN 'Verified' WHEN 'pending_review' THEN 'Pending Review'
                WHEN 'disputed' THEN 'Disputed' ELSE 'Processing' END AS status,
           st.payment_reference_utr AS "paymentReferenceUTR", st.settlement_date AS "settlementDate",
           vu.full_name AS "verifiedBy", st.verified_at AS "verifiedAt", st.notes,
           (SELECT COUNT(*) FROM admin_collections ac WHERE ac.settlement_id = st.id)::int AS "collectionsCount"
    FROM admin_settlements st JOIN users au ON au.id = st.admin_id JOIN zones z ON z.id = st.zone_id
    LEFT JOIN users vu ON vu.id = st.verified_by
    WHERE (%(sid)s::bigint IS NULL OR st.id = %(sid)s)
      AND (%(zid)s::bigint IS NULL OR st.zone_id = %(zid)s)
      AND (%(aid)s::bigint IS NULL OR st.admin_id = %(aid)s)
      AND (%(status)s::text IS NULL OR st.status::text = %(status)s)
    ORDER BY st.period_end DESC, st.id DESC
"""

SETTLEMENT_STATUSES = {"processing", "pending_review", "verified", "disputed"}


def _settlements(cur, sid=None, zid=None, aid=None, status=None) -> list[dict]:
    return db.all_rows(cur, SETTLEMENT_SQL, {"sid": sid, "zid": zid, "aid": aid, "status": status})


def _settlement(cur, sid: int) -> dict:
    rows = _settlements(cur, sid)
    if not rows:
        raise not_found("Settlement")
    return rows[0]


class SettlementCreateIn(CamelModel):
    admin_id: int
    area_id: int
    period_start: Date
    period_end: Date
    other_adjustments_inr: Decimal = Field(Decimal(0), alias="otherAdjustmentsINR")
    notes: Optional[str] = Field(None, max_length=1000)


class VerifyIn(CamelModel):
    utr_number: str = Field(pattern=r"^[A-Za-z0-9]{8,22}$")
    verified_amount_inr: Decimal = Field(alias="verifiedAmountINR", gt=0)
    notes: Optional[str] = Field(None, max_length=1000)


class DisputeIn(CamelModel):
    notes: str = Field(min_length=5, max_length=1000)


@router.get("/settlements", summary="SuperAdminSettlement[]")
def list_settlements(area_id: Optional[int] = Query(None, alias="areaId"), admin_id: Optional[int] = Query(None, alias="adminId"),
                     status: Optional[str] = None, user: CurrentUser = SA):
    st = parse_enum(status, SETTLEMENT_STATUSES, "status") if status and status.upper() != "ALL" else None
    with db.tx() as cur:
        return _settlements(cur, zid=area_id, aid=admin_id, status=st)


@router.post("/settlements", status_code=201, summary="Batch an admin's unsettled collections for a period")
def create_settlement(body: SettlementCreateIn, request: Request, user: CurrentUser = SA):
    if body.period_end < body.period_start:
        raise ApiError(422, "periodEnd must be on or after periodStart.")
    with db.tx() as cur:
        assignment = db.one(cur, """
            SELECT u.public_code, z.code FROM admin_zone_assignments a JOIN users u ON u.id = a.admin_id JOIN zones z ON z.id = a.zone_id
            WHERE a.admin_id = %s AND a.zone_id = %s
        """, (body.admin_id, body.area_id))
        if assignment is None:
            raise ApiError(422, "This admin has never been assigned to this area.")
        params = {"a": body.admin_id, "z": body.area_id, "s": body.period_start, "e": body.period_end}
        totals = db.one(cur, """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(amount), 0) AS gross,
                   COALESCE(SUM(amount) FILTER (WHERE transaction_status IN ('refunded', 'reversed')), 0) AS refunds,
                   COALESCE(SUM(admin_commission) FILTER (WHERE transaction_status = 'success'), 0) AS commission
            FROM admin_collections
            WHERE admin_id = %(a)s AND zone_id = %(z)s AND settlement_id IS NULL
              AND transaction_status IN ('success', 'refunded', 'reversed')
              AND collected_at >= %(s)s::date AND collected_at < %(e)s::date + 1
        """, params)
        if not totals["n"]:
            raise ApiError(422, "There are no unsettled collections for this admin, area and period.")
        suffix = (assignment["public_code"] or str(body.admin_id)).replace("ADM-", "")
        batch = f"VG-STL-{suffix}-{body.period_start:%Y%m%d}-{body.period_end:%Y%m%d}"
        sid = db.scalar(cur, """
            INSERT INTO admin_settlements (batch_number, admin_id, zone_id, period_start, period_end, gross_collections,
                                           refunds_deducted, commission_deducted, other_adjustments, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (batch, body.admin_id, body.area_id, body.period_start, body.period_end, totals["gross"], totals["refunds"],
              totals["commission"], money(body.other_adjustments_inr), body.notes))
        cur.execute("""
            UPDATE admin_collections SET settlement_id = %(sid)s, settlement_status = 'in_escrow'
            WHERE admin_id = %(a)s AND zone_id = %(z)s AND settlement_id IS NULL
              AND transaction_status IN ('success', 'refunded', 'reversed')
              AND collected_at >= %(s)s::date AND collected_at < %(e)s::date + 1
        """, {**params, "sid": sid})
        ledger.audit(cur, user, request, action=f"Generated settlement {batch}", action_type="SETTLEMENT", module="Settlements",
                     target=f"Settlement {batch}", record_id=batch, severity="notice",
                     new={"collections": totals["n"], "gross": totals["gross"], "commission": totals["commission"]})
        return _settlement(cur, sid)


@router.post("/settlements/{settlement_id}/verify", summary="Record the admin's bank transfer (UTR)")
def verify_settlement(settlement_id: int, body: VerifyIn, request: Request, user: CurrentUser = SA):
    with db.tx() as cur:
        st = _settlement(cur, settlement_id)
        if st["status"] == "Verified":
            raise ApiError(409, "This settlement is already verified.")
        cur.execute("SELECT 1 FROM admin_settlements WHERE id = %s FOR UPDATE", (settlement_id,))
        amount = money(body.verified_amount_inr)
        net = money(st["netAmountPayableToBusinessINR"])
        if amount > net:
            raise ApiError(422, f"Verified amount cannot exceed the net payable of Rs {net}.")
        status = "verified" if amount == net else "pending_review"
        notes = "\n".join(n for n in (st["notes"], body.notes) if n) or None
        cur.execute("""
            UPDATE admin_settlements SET verified_amount_received = %s, status = %s, payment_reference_utr = %s,
                   verified_by = %s, verified_at = now(), settlement_date = current_date, notes = %s
            WHERE id = %s
        """, (amount, status, body.utr_number.upper(), user.id, notes, settlement_id))
        if status == "verified":
            cur.execute("UPDATE admin_collections SET settlement_status = 'settled' WHERE settlement_id = %s", (settlement_id,))
        ledger.audit(cur, user, request, action=f'{"Verified" if status == "verified" else "Partially verified"} settlement {st["batchNumber"]}',
                     action_type="SETTLEMENT", module="Settlements", target=f'Settlement {st["batchNumber"]}',
                     record_id=st["batchNumber"], details=body.notes, previous={"status": st["status"]},
                     new={"status": status, "utr": body.utr_number.upper(), "amount": amount}, severity="notice")
        return _settlement(cur, settlement_id)


@router.post("/settlements/{settlement_id}/dispute")
def dispute_settlement(settlement_id: int, body: DisputeIn, request: Request, user: CurrentUser = SA):
    with db.tx() as cur:
        st = _settlement(cur, settlement_id)
        if st["status"] == "Verified":
            raise ApiError(409, "A verified settlement cannot be disputed.")
        cur.execute("UPDATE admin_settlements SET status = 'disputed', notes = concat_ws(E'\\n', notes, %s::text) WHERE id = %s",
                    (body.notes, settlement_id))
        ledger.audit(cur, user, request, action=f'Disputed settlement {st["batchNumber"]}', action_type="SETTLEMENT",
                     module="Settlements", target=f'Settlement {st["batchNumber"]}', record_id=st["batchNumber"],
                     details=body.notes, severity="warning")
        return _settlement(cur, settlement_id)


# --- dashboard metrics ---

def _range(preset: str | None, start: Date | None, end: Date | None) -> tuple[Date, Date]:
    today = Date.today()
    if start and end:
        if start > end:
            raise ApiError(422, "startDate must be on or before endDate.")
        return start, end
    days = {"7D": 7, "30D": 30, "90D": 90}.get((preset or "30D").upper())
    if (preset or "").upper() == "YTD":
        return today.replace(month=1, day=1), today
    return today - timedelta(days=(days or 30) - 1), today


def _metrics(cur, start: Date, end: Date, zid: int | None, aid: int | None) -> dict:
    p = {"s": start, "e": end, "z": zid, "a": aid}
    f_ac = "(%(z)s::bigint IS NULL OR ac.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR ac.admin_id = %(a)s)"
    f_st = "(%(z)s::bigint IS NULL OR st.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR st.admin_id = %(a)s)"
    in_period = "ac.collected_at >= %(s)s::date AND ac.collected_at < %(e)s::date + 1"
    return db.one(cur, f"""
        SELECT
          (SELECT COUNT(*) FROM zones z WHERE (%(z)s::bigint IS NULL OR z.id = %(z)s))::int AS "totalAreas",
          (SELECT COUNT(DISTINCT a.admin_id) FROM admin_zone_assignments a WHERE a.is_active
             AND (%(z)s::bigint IS NULL OR a.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR a.admin_id = %(a)s))::int AS "totalAdmins",
          (SELECT COUNT(DISTINCT a.admin_id) FROM admin_zone_assignments a JOIN users u ON u.id = a.admin_id
            WHERE a.is_active AND u.status = 'active' AND (%(z)s::bigint IS NULL OR a.zone_id = %(z)s)
              AND (%(a)s::bigint IS NULL OR a.admin_id = %(a)s))::int AS "activeAdmins",
          (SELECT COUNT(*) FROM customer_profiles c WHERE (%(z)s::bigint IS NULL OR c.zone_id = %(z)s)
              AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s))::int AS "totalCustomers",
          (SELECT COUNT(*) FROM customer_profiles c JOIN users u ON u.id = c.user_id WHERE u.status = 'active'
              AND (%(z)s::bigint IS NULL OR c.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s))::int AS "activeCustomers",
          (SELECT COUNT(*) FROM customer_profiles c JOIN users u ON u.id = c.user_id
            WHERE u.created_at >= %(s)s::date AND u.created_at < %(e)s::date + 1
              AND (%(z)s::bigint IS NULL OR c.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s))::int AS "newCustomers",
          (SELECT COALESCE(SUM(amount), 0) FROM admin_collections ac WHERE ac.transaction_status = 'success' AND {in_period} AND {f_ac})::float
              AS "totalSuccessfulCustomerCollectionsINR",
          (SELECT COALESCE(SUM(verified_amount_received), 0) FROM admin_settlements st
            WHERE st.verified_at >= %(s)s::date AND st.verified_at < %(e)s::date + 1 AND {f_st})::float AS "actualBusinessReceiptsINR",
          ({_outstanding(f_st, f_ac)})::float AS "outstandingAdminSettlementsINR",
          (SELECT COALESCE(SUM(amount), 0) FROM admin_collections ac WHERE ac.transaction_status = 'pending' AND {in_period} AND {f_ac})::float
              AS "pendingCustomerPaymentsINR",
          (SELECT COALESCE(SUM(amount), 0) FROM admin_collections ac WHERE ac.transaction_status IN ('refunded', 'reversed')
              AND {in_period} AND {f_ac})::float AS "refundsAndReversalsINR",
          (SELECT COALESCE(SUM(admin_commission), 0) FROM admin_collections ac WHERE ac.transaction_status = 'success'
              AND {in_period} AND {f_ac})::float AS "adminCommissionsINR",
          (SELECT COALESCE(SUM(net_payable), 0) FROM admin_settlements st
            WHERE st.period_end >= %(s)s::date AND st.period_end <= %(e)s::date AND {f_st})::float AS settled_net
    """, p)


def _growth(cur_value: float, prev_value: float) -> float | None:
    return round((cur_value - prev_value) / prev_value * 100, 1) if prev_value else None


@router.get("/dashboard", summary="SuperAdminDashboardMetrics")
def dashboard(area_id: Optional[int] = Query(None, alias="areaId"), admin_id: Optional[int] = Query(None, alias="adminId"),
              start_date: Optional[Date] = Query(None, alias="startDate"), end_date: Optional[Date] = Query(None, alias="endDate"),
              date_preset: Optional[str] = Query("30D", alias="datePreset"), user: CurrentUser = SA):
    start, end = _range(date_preset, start_date, end_date)
    length = (end - start).days + 1
    with db.tx() as cur:
        now = _metrics(cur, start, end, area_id, admin_id)
        prev = _metrics(cur, start - timedelta(days=length), start - timedelta(days=1), area_id, admin_id)
    settled_now, settled_prev = now.pop("settled_net"), prev.pop("settled_net")
    return {**now, "period": {"startDate": start.isoformat(), "endDate": end.isoformat()},
            "periodComparison": {
                "collectionsGrowthPercent": _growth(now["totalSuccessfulCustomerCollectionsINR"], prev["totalSuccessfulCustomerCollectionsINR"]),
                "receiptsGrowthPercent": _growth(now["actualBusinessReceiptsINR"], prev["actualBusinessReceiptsINR"]),
                "customerGrowthPercent": _growth(now["newCustomers"], prev["newCustomers"]),
                "settlementChangePercent": _growth(settled_now, settled_prev)}}


# --- audit logs ---

@router.get("/audit-logs", summary="SuperAdminAuditLog[]")
def audit_logs(module: Optional[str] = None, action_type: Optional[str] = Query(None, alias="actionType"),
               search: Optional[str] = Query(None, alias="searchTerm"), from_date: Optional[Date] = Query(None, alias="startDate"),
               to_date: Optional[Date] = Query(None, alias="endDate"), limit: int = Query(500, ge=1, le=5000), user: CurrentUser = SA):
    with db.tx() as cur:
        return db.all_rows(cur, """
            SELECT a.id::text AS id, to_char(a.created_at, 'DD Mon YYYY, HH12:MI:SS AM') AS timestamp, a.created_at AS "isoDate",
                   a.actor_name AS actor, COALESCE(a.actor_role, initcap(replace(a.portal::text, '_', ' '))) AS "actorRole",
                   a.action, upper(replace(a.action_type, ' ', '_')) AS "actionType", a.module, a.target_resource AS "targetResource",
                   COALESCE(a.details, a.action) AS details, host(a.ip_address) AS "ipAddress", initcap(a.outcome::text) AS status,
                   a.portal, a.previous_value AS "previousValue", a.new_value AS "newValue"
            FROM audit_logs a
            WHERE (%(module)s::text IS NULL OR a.module ILIKE %(module)s)
              AND (%(atype)s::text IS NULL OR a.action_type ILIKE %(atype)s)
              AND (%(q)s::text IS NULL OR a.action ILIKE %(q)s OR a.actor_name ILIKE %(q)s OR a.details ILIKE %(q)s
                   OR a.target_resource ILIKE %(q)s)
              AND (%(from)s::date IS NULL OR a.created_at >= %(from)s::date)
              AND (%(to)s::date IS NULL OR a.created_at < %(to)s::date + 1)
            ORDER BY a.created_at DESC LIMIT %(limit)s
        """, {"module": module, "atype": action_type, "q": f"%{search}%" if search else None, "from": from_date,
              "to": to_date, "limit": limit})


# --- reports ---

REPORTS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "admin_performance": ("Admin performance", """
        SELECT u.public_code AS code, u.full_name AS name, f.assigned_zones AS areas, f.total_customers::int AS customers,
               f.total_collections::float AS collections, f.total_commission::float AS commission,
               f.business_receipts::float AS receipts, f.outstanding_settlement::float AS outstanding
        FROM v_admin_financials f JOIN users u ON u.id = f.admin_id
        WHERE (%(a)s::bigint IS NULL OR f.admin_id = %(a)s)
          AND (%(z)s::bigint IS NULL OR EXISTS (SELECT 1 FROM admin_zone_assignments x WHERE x.admin_id = f.admin_id AND x.zone_id = %(z)s))
        ORDER BY collections DESC""",
        [("code", "Admin ID"), ("name", "Admin"), ("areas", "Areas"), ("customers", "Customers"), ("collections", "Collections (INR)"),
         ("commission", "Commission (INR)"), ("receipts", "Receipts (INR)"), ("outstanding", "Outstanding (INR)")]),
    "area_performance": ("Area performance", """
        SELECT z.code, z.name, vz.total_chargers::int AS chargers, vz.total_customers::int AS customers,
               COALESCE((SELECT SUM(amount) FROM admin_collections ac WHERE ac.zone_id = z.id AND ac.transaction_status = 'success'
                         AND ac.collected_at >= %(s)s::date AND ac.collected_at < %(e)s::date + 1), 0)::float AS collections,
               (SELECT COUNT(*) FROM charging_sessions cs JOIN charging_points cp ON cp.id = cs.charging_point_id
                  JOIN stations s ON s.id = cp.station_id WHERE s.zone_id = z.id
                  AND cs.started_at >= %(s)s::date AND cs.started_at < %(e)s::date + 1)::int AS sessions,
               COALESCE((SELECT SUM(cs.energy_delivered_kwh) FROM charging_sessions cs JOIN charging_points cp ON cp.id = cs.charging_point_id
                  JOIN stations s ON s.id = cp.station_id WHERE s.zone_id = z.id
                  AND cs.started_at >= %(s)s::date AND cs.started_at < %(e)s::date + 1), 0)::float AS energy_kwh
        FROM zones z JOIN v_zone_overview vz ON vz.id = z.id WHERE (%(z)s::bigint IS NULL OR z.id = %(z)s) ORDER BY z.code""",
        [("code", "Area"), ("name", "Name"), ("chargers", "Chargers"), ("customers", "Customers"), ("collections", "Collections (INR)"),
         ("sessions", "Sessions"), ("energy_kwh", "Energy (kWh)")]),
    "customer_growth": ("Customer growth", """
        SELECT to_char(mo, 'Mon YYYY') AS month,
               (SELECT COUNT(*) FROM users u JOIN customer_profiles c ON c.user_id = u.id
                 WHERE date_trunc('month', u.created_at) = mo AND (%(z)s::bigint IS NULL OR c.zone_id = %(z)s)
                   AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s))::int AS new_customers,
               (SELECT COUNT(*) FROM users u JOIN customer_profiles c ON c.user_id = u.id
                 WHERE u.created_at < mo + interval '1 month' AND (%(z)s::bigint IS NULL OR c.zone_id = %(z)s)
                   AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s))::int AS total_customers
        FROM generate_series(date_trunc('month', %(s)s::date), date_trunc('month', %(e)s::date), interval '1 month') mo ORDER BY mo""",
        [("month", "Month"), ("new_customers", "New customers"), ("total_customers", "Total customers")]),
    "customer_payments": ("Customer payments", """
        SELECT u.public_code AS code, u.full_name AS name, z.code AS area,
               COALESCE((SELECT SUM(p.total_amount) FROM payments p WHERE p.customer_id = u.id AND p.status = 'success'
                         AND p.completed_at >= %(s)s::date AND p.completed_at < %(e)s::date + 1), 0)::float AS paid,
               COALESCE((SELECT SUM(s.total_amount) FROM charging_sessions s WHERE s.customer_id = u.id
                         AND s.payment_status = 'pending' AND s.ended_at IS NOT NULL), 0)::float AS dues
        FROM users u JOIN customer_profiles c ON c.user_id = u.id LEFT JOIN zones z ON z.id = c.zone_id
        WHERE (%(z)s::bigint IS NULL OR c.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s)
        ORDER BY paid DESC""",
        [("code", "Customer ID"), ("name", "Customer"), ("area", "Area"), ("paid", "Paid (INR)"), ("dues", "Dues (INR)")]),
    "admin_collections": ("Admin collections", """
        SELECT ac.reference_number AS reference, to_char(ac.collected_at, 'YYYY-MM-DD HH24:MI') AS date, au.full_name AS admin,
               z.code AS area, cu.full_name AS customer, ac.collection_type::text AS type, ac.payment_method::text AS method,
               ac.amount::float AS amount, ac.admin_commission::float AS commission, ac.net_payable::float AS net_payable,
               ac.transaction_status::text AS status, ac.settlement_status::text AS settlement
        FROM admin_collections ac JOIN users au ON au.id = ac.admin_id JOIN users cu ON cu.id = ac.customer_id JOIN zones z ON z.id = ac.zone_id
        WHERE ac.collected_at >= %(s)s::date AND ac.collected_at < %(e)s::date + 1
          AND (%(z)s::bigint IS NULL OR ac.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR ac.admin_id = %(a)s)
        ORDER BY ac.collected_at DESC""",
        [("reference", "Reference"), ("date", "Date"), ("admin", "Admin"), ("area", "Area"), ("customer", "Customer"), ("type", "Type"),
         ("method", "Method"), ("amount", "Amount (INR)"), ("commission", "Commission (INR)"), ("net_payable", "Net payable (INR)"),
         ("status", "Status"), ("settlement", "Settlement")]),
    "business_settlements": ("Business settlements", """
        SELECT st.batch_number AS batch, au.full_name AS admin, z.code AS area, st.period_start AS period_start, st.period_end AS period_end,
               st.gross_collections::float AS gross, st.commission_deducted::float AS commission, st.net_payable::float AS net_payable,
               st.verified_amount_received::float AS received, st.outstanding_balance::float AS outstanding, st.status::text AS status,
               st.payment_reference_utr AS utr
        FROM admin_settlements st JOIN users au ON au.id = st.admin_id JOIN zones z ON z.id = st.zone_id
        WHERE st.period_end >= %(s)s::date AND st.period_start <= %(e)s::date
          AND (%(z)s::bigint IS NULL OR st.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR st.admin_id = %(a)s)
        ORDER BY st.period_end DESC""",
        [("batch", "Batch"), ("admin", "Admin"), ("area", "Area"), ("period_start", "From"), ("period_end", "To"), ("gross", "Gross (INR)"),
         ("commission", "Commission (INR)"), ("net_payable", "Net payable (INR)"), ("received", "Received (INR)"),
         ("outstanding", "Outstanding (INR)"), ("status", "Status"), ("utr", "UTR")]),
    "outstanding_balances": ("Outstanding balances", f"""
        SELECT u.public_code AS code, u.full_name AS admin, z.code AS area,
               ({_outstanding("st.admin_id = a.admin_id AND st.zone_id = a.zone_id", "ac.admin_id = a.admin_id AND ac.zone_id = a.zone_id")})::float AS outstanding,
               (SELECT MIN(settlement_due_date) FROM admin_collections ac WHERE ac.admin_id = a.admin_id AND ac.zone_id = a.zone_id
                 AND ac.settlement_status <> 'settled' AND ac.transaction_status = 'success') AS due_date
        FROM admin_zone_assignments a JOIN users u ON u.id = a.admin_id JOIN zones z ON z.id = a.zone_id
        WHERE (%(z)s::bigint IS NULL OR a.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR a.admin_id = %(a)s)
        ORDER BY outstanding DESC""",
        [("code", "Admin ID"), ("admin", "Admin"), ("area", "Area"), ("outstanding", "Outstanding (INR)"), ("due_date", "Earliest due")]),
    "refunds_failed": ("Refunds and failed payments", """
        SELECT * FROM (
            SELECT ac.reference_number AS reference, to_char(ac.collected_at, 'YYYY-MM-DD HH24:MI') AS date, cu.full_name AS customer,
                   z.code AS area, ac.amount::float AS amount, ac.transaction_status::text AS status, 'collection' AS source
            FROM admin_collections ac JOIN users cu ON cu.id = ac.customer_id JOIN zones z ON z.id = ac.zone_id
            WHERE ac.transaction_status IN ('refunded', 'reversed', 'failed')
              AND ac.collected_at >= %(s)s::date AND ac.collected_at < %(e)s::date + 1
              AND (%(z)s::bigint IS NULL OR ac.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR ac.admin_id = %(a)s)
            UNION ALL
            SELECT COALESCE(p.receipt_number, p.gateway_order_id), to_char(p.created_at, 'YYYY-MM-DD HH24:MI'), cu.full_name, z.code,
                   p.total_amount::float, p.status::text, 'gateway'
            FROM payments p JOIN users cu ON cu.id = p.customer_id JOIN customer_profiles c ON c.user_id = cu.id
            LEFT JOIN zones z ON z.id = c.zone_id
            WHERE p.status IN ('failed', 'refunded') AND p.created_at >= %(s)s::date AND p.created_at < %(e)s::date + 1
              AND (%(z)s::bigint IS NULL OR c.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR c.assigned_admin_id = %(a)s)
        ) x ORDER BY date DESC""",
        [("reference", "Reference"), ("date", "Date"), ("customer", "Customer"), ("area", "Area"), ("amount", "Amount (INR)"),
         ("status", "Status"), ("source", "Source")]),
    "monthly_financial": ("Monthly financial summary", """
        SELECT to_char(mo, 'Mon YYYY') AS month,
               COALESCE(SUM(ac.amount) FILTER (WHERE ac.transaction_status = 'success'), 0)::float AS collections,
               COALESCE(SUM(ac.admin_commission) FILTER (WHERE ac.transaction_status = 'success'), 0)::float AS commission,
               COALESCE(SUM(ac.net_payable) FILTER (WHERE ac.transaction_status = 'success'), 0)::float AS net_payable,
               COALESCE(SUM(ac.amount) FILTER (WHERE ac.transaction_status IN ('refunded', 'reversed')), 0)::float AS refunds,
               COALESCE((SELECT SUM(st.verified_amount_received) FROM admin_settlements st WHERE date_trunc('month', st.verified_at) = mo
                         AND (%(z)s::bigint IS NULL OR st.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR st.admin_id = %(a)s)), 0)::float AS receipts
        FROM generate_series(date_trunc('month', %(s)s::date), date_trunc('month', %(e)s::date), interval '1 month') mo
        LEFT JOIN admin_collections ac ON date_trunc('month', ac.collected_at) = mo
             AND (%(z)s::bigint IS NULL OR ac.zone_id = %(z)s) AND (%(a)s::bigint IS NULL OR ac.admin_id = %(a)s)
        GROUP BY mo ORDER BY mo""",
        [("month", "Month"), ("collections", "Collections (INR)"), ("commission", "Commission (INR)"),
         ("net_payable", "Net payable (INR)"), ("refunds", "Refunds (INR)"), ("receipts", "Receipts (INR)")]),
}


@router.get("/reports/{report_type}", summary="Generate a report (JSON, or CSV with ?format=csv)")
def report(report_type: str, request: Request, area_id: Optional[int] = Query(None, alias="areaId"),
           admin_id: Optional[int] = Query(None, alias="adminId"), start_date: Optional[Date] = Query(None, alias="startDate"),
           end_date: Optional[Date] = Query(None, alias="endDate"), date_preset: Optional[str] = Query("90D", alias="datePreset"),
           format: Literal["json", "csv"] = "json", user: CurrentUser = SA):
    if report_type not in REPORTS:
        raise ApiError(404, f"Unknown report. Available: {', '.join(REPORTS)}")
    title, sql, columns = REPORTS[report_type]
    start, end = _range(date_preset, start_date, end_date)
    with db.tx() as cur:
        rows = db.all_rows(cur, sql, {"s": start, "e": end, "z": area_id, "a": admin_id})
        ledger.audit(cur, user, request, action=f"Generated report {title}", action_type="EXPORT", module="Reports",
                     target=f"Report {report_type}", details=f"{len(rows)} rows, {start} to {end}, format {format}")
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([label for _, label in columns])
        for r in rows:
            w.writerow([r[k] for k, _ in columns])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{report_type}_{start}_{end}.csv"'})
    return {"type": report_type, "title": title, "generatedAt": Date.today().isoformat(),
            "filters": {"areaId": area_id, "adminId": admin_id, "startDate": start.isoformat(), "endDate": end.isoformat()},
            "columns": [{"key": k, "label": label} for k, label in columns], "rows": rows, "rowCount": len(rows)}

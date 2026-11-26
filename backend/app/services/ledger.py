# Never update wallet balance directly - insert a wallet_transactions row with
# status success and the DB trigger posts it.
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from fastapi import Request

from .. import db
from ..errors import ApiError
from ..security import CurrentUser, client_ip, device_of
from ..utils import money, new_code


def wallet_of(cur, customer_id: int, lock: bool = False) -> dict:
    row = db.one(cur, "SELECT * FROM wallets WHERE customer_id = %s" + (" FOR UPDATE" if lock else ""), (customer_id,))
    if row is None:
        raise ApiError(404, "Wallet not found.")
    return row


def post_wallet_txn(cur, customer_id: int, txn_type: str, direction: str, amount, *, title: str,
                    description: str | None = None, status: str = "success", payment_id: int | None = None,
                    session_id: int | None = None, reservation_id: int | None = None,
                    reverses_id: int | None = None, created_by: int | None = None) -> dict:
    amount = money(amount)
    if amount <= 0:
        raise ApiError(422, "Amount must be greater than zero.")
    wallet = wallet_of(cur, customer_id)
    return db.one(cur, """
        INSERT INTO wallet_transactions (wallet_id, customer_id, transaction_ref, invoice_number, txn_type, direction,
                                         amount, status, title, description, payment_id, charging_session_id,
                                         reservation_id, reverses_transaction_id, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
    """, (wallet["id"], customer_id, new_code("TXN"), new_code("INV"), txn_type, direction, amount, status, title,
          description, payment_id, session_id, reservation_id, reverses_id, created_by))


def notify(cur, user_id: int, ntype: str, title: str, message: str, *, route: str | None = None,
           entity_type: str | None = None, entity_id: int | None = None) -> None:
    cur.execute("""
        INSERT INTO notifications (user_id, type, title, message, target_route, entity_type, entity_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (user_id, ntype, title, message, route, entity_type if entity_id else None, entity_id))


def notify_zone_admins(cur, zone_id: int, ntype: str, title: str, message: str, **kw) -> None:
    for row in db.all_rows(cur, "SELECT admin_id FROM admin_zone_assignments WHERE zone_id = %s AND is_active", (zone_id,)):
        notify(cur, row["admin_id"], ntype, title, message, **kw)


def _json(value):
    return None if value is None else json.dumps(value, default=str)


def audit(cur, user: CurrentUser | None, request: Request | None, *, action: str, action_type: str, module: str,
          target: str | None = None, record_id: str | int | None = None, details: str | None = None,
          previous=None, new=None, severity: str = "info", outcome: str = "success",
          portal: str | None = None, actor_name: str | None = None, actor_email: str | None = None,
          actor_id: int | None = None) -> None:
    cur.execute("""
        INSERT INTO audit_logs (portal, actor_user_id, actor_name, actor_email, actor_role, action, action_type,
                                module, target_resource, record_id, details, previous_value, new_value,
                                ip_address, device, severity, outcome)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        portal or (("super_admin" if user.is_super_admin else "admin") if user and user.is_staff else
                   "customer" if user else "system"),
        actor_id or (user.id if user else None),
        actor_name or (user.name if user else None) or "System",
        actor_email or (user.email if user else None),
        user.role_name if user else None,
        action, action_type, module, target, None if record_id is None else str(record_id), details,
        _json(previous), _json(new),
        client_ip(request) if request else None, device_of(request) if request else None, severity, outcome,
    ))


def record_collection(cur, customer_id: int, amount, method: str, *, collection_type: str,
                      payment_id: int | None = None, session_id: int | None = None, notes: str | None = None) -> None:
    # credit the money to the customer's area admin. no admin / inactive assignment -> skip
    row = db.one(cur, """
        SELECT cp.assigned_admin_id, cp.zone_id, sp.commission_rate_percent, sp.settlement_cycle_days
        FROM customer_profiles cp
        JOIN admin_zone_assignments a ON a.admin_id = cp.assigned_admin_id AND a.zone_id = cp.zone_id AND a.is_active
        JOIN staff_profiles sp ON sp.user_id = cp.assigned_admin_id
        WHERE cp.user_id = %s
    """, (customer_id,))
    if row is None:
        return
    amount = money(amount)
    commission = money(amount * Decimal(row["commission_rate_percent"] or 0) / 100)
    cur.execute("""
        INSERT INTO admin_collections (reference_number, admin_id, zone_id, customer_id, collection_type, payment_method,
                                       amount, admin_commission, transaction_status, settlement_status,
                                       settlement_due_date, payment_id, charging_session_id, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'success', 'pending_settlement', %s, %s, %s, %s)
    """, (new_code("VG-REC"), row["assigned_admin_id"], row["zone_id"], customer_id, collection_type, method, amount,
          commission, date.today() + timedelta(days=row["settlement_cycle_days"]), payment_id, session_id, notes))

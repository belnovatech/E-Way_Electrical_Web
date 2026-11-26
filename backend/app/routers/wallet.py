# Wallet + top-ups + saved payment methods. Only the sandbox gateway exists for now.
from __future__ import annotations

import csv
import io
import re
import secrets
from datetime import date as Date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import Field

from .. import db
from ..config import get_settings
from ..errors import ApiError, not_found
from ..security import CurrentUser, require_customer
from ..services import ledger
from ..utils import CamelModel, money, new_code

router = APIRouter(prefix="/wallet", tags=["Wallet & Payments"])

MIN_TOPUP, MAX_TOPUP = Decimal(10), Decimal(50000)

LEDGER_SQL = """
    SELECT t.id::text AS id, t.transaction_ref AS "referenceId", p.gateway_payment_id AS "gatewayReferenceId",
           CASE t.txn_type WHEN 'topup' THEN 'recharge' WHEN 'charging_fee' THEN 'charging' WHEN 'refund' THEN 'refund'
                           WHEN 'adjustment_credit' THEN 'credit' WHEN 'adjustment_debit' THEN 'debit'
                           WHEN 'reservation_fee' THEN 'debit' ELSE t.direction::text END AS type,
           t.txn_type AS "ledgerType", t.direction, t.title, t.description,
           t.amount::float AS "amountINR", t.balance_before::float AS "balanceBeforeINR",
           t.balance_after::float AS "balanceAfterINR", to_char(t.created_at, 'DD Mon YYYY, HH12:MI AM') AS date,
           t.created_at AS "isoDate", initcap(t.status::text) AS status,
           CASE WHEN p.id IS NOT NULL THEN COALESCE(p.method_summary, upper(p.method_type::text))
                WHEN t.txn_type IN ('charging_fee', 'reservation_fee') THEN 'E-Way Wallet' ELSE 'System' END AS "paymentMethod",
           CASE t.txn_type WHEN 'topup' THEN 'wallet_topup' WHEN 'charging_fee' THEN 'charging_fee'
                           WHEN 'reservation_fee' THEN 'reservation_fee' WHEN 'refund' THEN 'cancellation_refund' END AS category,
           t.invoice_number AS "invoiceNumber", u.full_name AS "customerName", u.phone_number AS "customerPhone",
           json_strip_nulls(json_build_object(
               'stationName', st.name, 'chargerId', cp.point_code, 'kwh', cs.energy_delivered_kwh,
               'sessionNumber', cs.session_number, 'reservationNumber', r.reservation_number,
               'cardBrand', pm.card_brand, 'cardLast4', pm.card_last4, 'upiId', pm.upi_vpa, 'bankName', pm.bank_name)) AS metadata
    FROM wallet_transactions t
    JOIN users u ON u.id = t.customer_id
    LEFT JOIN payments p ON p.id = t.payment_id
    LEFT JOIN payment_methods pm ON pm.id = p.payment_method_id
    LEFT JOIN charging_sessions cs ON cs.id = t.charging_session_id
    LEFT JOIN reservations r ON r.id = COALESCE(t.reservation_id, cs.reservation_id)
    LEFT JOIN charging_points cp ON cp.id = COALESCE(cs.charging_point_id, r.charging_point_id)
    LEFT JOIN stations st ON st.id = cp.station_id
    WHERE t.customer_id = %(uid)s
      AND (%(type)s::text IS NULL OR t.txn_type::text = %(type)s)
      AND (%(status)s::text IS NULL OR t.status::text = %(status)s)
      AND (%(from)s::date IS NULL OR t.created_at >= %(from)s::date)
      AND (%(to)s::date IS NULL OR t.created_at < %(to)s::date + 1)
    ORDER BY t.created_at DESC, t.id DESC
    LIMIT %(limit)s OFFSET %(offset)s
"""

LEDGER_TYPES = {"recharge": "topup", "topup": "topup", "charging": "charging_fee", "charging_fee": "charging_fee",
                "reservation_fee": "reservation_fee", "refund": "refund", "credit": "adjustment_credit",
                "debit": "adjustment_debit", "reversal": "reversal"}


def _ledger(cur, uid: int, *, type_=None, status=None, from_=None, to=None, limit=100, offset=0) -> list[dict]:
    t = LEDGER_TYPES.get(type_.lower()) if type_ else None
    if type_ and t is None:
        raise ApiError(422, "Unknown transaction type.")
    return db.all_rows(cur, LEDGER_SQL, {"uid": uid, "type": t, "status": status.lower() if status else None,
                                         "from": from_, "to": to, "limit": limit, "offset": offset})


@router.get("/balance", summary="Wallet balance (src/services/dashboardService.ts)")
def balance(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        w = ledger.wallet_of(cur, user.id)
        due = db.scalar(cur, """
            SELECT COALESCE(SUM(total_amount), 0)::float FROM charging_sessions
            WHERE customer_id = %s AND payment_status = 'pending' AND ended_at IS NOT NULL
        """, (user.id,))
    return {"balance": float(w["balance"]), "currency": w["currency"], "pendingDuesINR": due}


@router.get("/transactions", summary="Ledger (AuthoritativeLedgerRecord[])")
def transactions(type: Optional[str] = None, status: Optional[str] = None,
                 from_date: Optional[Date] = Query(None, alias="from"), to_date: Optional[Date] = Query(None, alias="to"),
                 limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                 user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _ledger(cur, user.id, type_=type, status=status, from_=from_date, to=to_date, limit=limit, offset=offset)


@router.get("/statement.csv", summary="Download the ledger as CSV")
def statement(from_date: Optional[Date] = Query(None, alias="from"), to_date: Optional[Date] = Query(None, alias="to"),
              user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        rows = _ledger(cur, user.id, from_=from_date, to=to_date, limit=10000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Reference", "Invoice", "Type", "Title", "Amount (INR)", "Direction", "Balance after", "Status", "Method"])
    for r in rows:
        w.writerow([r["isoDate"].isoformat(), r["referenceId"], r["invoiceNumber"], r["ledgerType"], r["title"],
                    f'{r["amountINR"]:.2f}', r["direction"], r["balanceAfterINR"], r["status"], r["paymentMethod"]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="eway-wallet-statement.csv"'})


# --- top-up (sandbox gateway) ---

class TopupIntentIn(CamelModel):
    amount_inr: Decimal = Field(alias="amountINR", gt=0)


class TopupConfirmIn(CamelModel):
    payment_id: int
    method_type: Literal["card", "upi", "netbanking", "wallet", "saved"]
    saved_method_id: Optional[int] = None
    upi_vpa: Optional[str] = Field(None, max_length=100)
    card_brand: Optional[str] = Field(None, max_length=20)
    card_last4: Optional[str] = Field(None, pattern=r"^\d{4}$")
    bank_code: Optional[str] = Field(None, max_length=20)
    bank_name: Optional[str] = Field(None, max_length=100)
    wallet_provider: Optional[str] = Field(None, max_length=40)
    simulate_failure: bool = False
    failure_reason: Optional[str] = Field(None, max_length=200)


def _intent_payload(p: dict, user: CurrentUser) -> dict:
    return {
        "id": str(p["id"]), "amountINR": float(p["total_amount"]), "currency": p["currency"],
        "idempotencyKey": p["idempotency_key"], "gatewayOrderId": p["gateway_order_id"], "provider": p["provider"],
        "orderSummary": {"baseAmountINR": float(p["base_amount"]), "convenienceFeeINR": float(p["convenience_fee"]),
                         "gstINR": float(p["gst_amount"]), "discountINR": float(p["discount_amount"]),
                         "totalPayableINR": float(p["total_amount"]), "currency": p["currency"],
                         "description": "E-Way wallet top-up", "orderType": p["order_type"]},
        "customer": {"id": str(user.id), "name": user.name, "phone": user.phone, "email": user.email},
        "status": p["status"], "createdAt": p["created_at"].isoformat(),
    }


def _verification_payload(p: dict) -> dict:
    ok = p["status"] == "success"
    return {
        "success": ok, "transactionId": str(p["id"]), "gatewayReferenceId": p["gateway_payment_id"],
        "amountINR": float(p["total_amount"]), "currency": p["currency"], "paymentMethod": p["method_type"],
        "paymentDetailsSummary": p["method_summary"], "timestamp": (p["completed_at"] or p["updated_at"]).isoformat(),
        "orderId": p["gateway_order_id"], "idempotencyKey": p["idempotency_key"], "signature": p["gateway_signature"],
        "receiptNumber": p["receipt_number"], "errorMessage": p["error_message"], "errorCode": p["error_code"],
        "status": p["status"],
    }


@router.post("/topup/intents", status_code=201, summary="Create a top-up order (PaymentIntent)")
def create_topup_intent(body: TopupIntentIn, user: CurrentUser = Depends(require_customer)):
    amount = money(body.amount_inr)
    if not MIN_TOPUP <= amount <= MAX_TOPUP:
        raise ApiError(422, f"Top-up amount must be between Rs {MIN_TOPUP} and Rs {MAX_TOPUP}.")
    s = get_settings()
    if s.payment_provider != "sandbox":
        raise ApiError(501, f"Payment provider '{s.payment_provider}' is not implemented yet.")
    with db.tx() as cur:
        p = db.one(cur, """
            INSERT INTO payments (customer_id, order_type, provider, base_amount, currency, idempotency_key, gateway_order_id)
            VALUES (%s, 'wallet_topup', 'sandbox', %s, %s, %s, %s) RETURNING *
        """, (user.id, amount, s.currency, f"idem_{secrets.token_hex(12)}", f"order_SBX_{secrets.token_hex(8)}"))
    return _intent_payload(p, user)


def _method_summary(cur, user: CurrentUser, body: TopupConfirmIn) -> tuple[str, int | None, str]:
    if body.method_type == "saved":
        if body.saved_method_id is None:
            raise ApiError(422, "savedMethodId is required for a saved payment method.")
        m = db.one(cur, "SELECT * FROM payment_methods WHERE id = %s AND customer_id = %s", (body.saved_method_id, user.id))
        if m is None:
            raise not_found("Payment method")
        summary = (f'{m["card_brand"].upper()} **** {m["card_last4"]}' if m["method_type"] == "card"
                   else m["upi_vpa"] if m["method_type"] == "upi" else m["bank_name"] or m["bank_code"])
        return m["method_type"], m["id"], summary
    if body.method_type == "upi":
        if not body.upi_vpa or not re.match(r"^[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}$", body.upi_vpa):
            raise ApiError(422, "Enter a valid UPI ID, e.g. name@okhdfcbank.")
        return "upi", None, body.upi_vpa
    if body.method_type == "card":
        if not body.card_last4:
            raise ApiError(422, "cardLast4 is required (send the card itself to the gateway, never to this API).")
        return "card", None, f'{(body.card_brand or "card").upper()} **** {body.card_last4}'
    if body.method_type == "netbanking":
        if not body.bank_code:
            raise ApiError(422, "bankCode is required for net banking.")
        return "netbanking", None, body.bank_name or body.bank_code
    return "digital_wallet", None, body.wallet_provider or "Digital wallet"


@router.post("/topup/confirm", summary="Confirm a top-up (PaymentVerificationResult); idempotent")
def confirm_topup(body: TopupConfirmIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        p = db.one(cur, "SELECT * FROM payments WHERE id = %s AND customer_id = %s FOR UPDATE", (body.payment_id, user.id))
        if p is None:
            raise not_found("Payment")
        if p["order_type"] != "wallet_topup":
            raise ApiError(409, "This payment is not a wallet top-up.")
        if p["status"] in ("success", "failed", "cancelled", "refunded"):
            # already confirmed before - just return the old result, don't credit again
            return {**_verification_payload(p), "balance": float(ledger.wallet_of(cur, user.id)["balance"]), "replayed": True}

        method_type, saved_id, summary = _method_summary(cur, user, body)
        if body.simulate_failure:
            p = db.one(cur, """
                UPDATE payments SET status = 'failed', method_type = %s, payment_method_id = %s, method_summary = %s,
                                    error_code = 'PAYMENT_DECLINED', error_message = %s
                WHERE id = %s RETURNING *
            """, (method_type, saved_id, summary, body.failure_reason or "Payment declined by the bank (sandbox).", p["id"]))
            return {**_verification_payload(p), "balance": float(ledger.wallet_of(cur, user.id)["balance"])}

        p = db.one(cur, """
            UPDATE payments SET status = 'success', method_type = %s, payment_method_id = %s, method_summary = %s,
                                gateway_payment_id = %s, gateway_signature = %s, receipt_number = %s, completed_at = now()
            WHERE id = %s RETURNING *
        """, (method_type, saved_id, summary, f"pay_SBX_{secrets.token_hex(8)}", secrets.token_hex(32),
              new_code("RCPT"), p["id"]))
        txn = ledger.post_wallet_txn(cur, user.id, "topup", "credit", p["total_amount"],
                                     title=f"Wallet recharge via {method_type.replace('_', ' ').upper()}",
                                     description=summary, payment_id=p["id"])
        ledger.record_collection(cur, user.id, p["total_amount"], method_type, collection_type="wallet_topup",
                                 payment_id=p["id"])
        ledger.notify(cur, user.id, "wallet", "Wallet recharged", f'Rs {p["total_amount"]} added to your E-Way wallet.',
                      route="/customer/wallet", entity_type="wallet_transaction", entity_id=txn["id"])
        return {**_verification_payload(p), "balance": float(txn["balance_after"])}


# --- saved payment methods ---

class CardIn(CamelModel):
    cardholder_name: str = Field(min_length=2, max_length=80)
    card_number: str = Field(min_length=12, max_length=23)
    expiry_month: int = Field(ge=1, le=12)
    expiry_year: int = Field(ge=2024, le=2100)
    set_default: bool = False


class UpiIn(CamelModel):
    vpa: str = Field(pattern=r"^[A-Za-z0-9._-]{2,}@[A-Za-z]{2,}$")
    set_default: bool = False


class NetBankingIn(CamelModel):
    bank_code: str = Field(min_length=2, max_length=20)
    bank_name: str = Field(min_length=2, max_length=100)
    set_default: bool = False


METHOD_SQL = """
    SELECT id::text AS id, method_type AS type, title, subtitle, card_brand AS brand, card_last4 AS last4,
           CASE WHEN card_expiry_month IS NOT NULL THEN lpad(card_expiry_month::text, 2, '0') || '/' || right(card_expiry_year::text, 2) END AS expiry,
           upi_vpa AS vpa, bank_code AS "bankCode", bank_name AS "bankName", is_default AS "isDefault", created_at AS "createdAt"
    FROM payment_methods WHERE customer_id = %(uid)s AND (%(mid)s::bigint IS NULL OR id = %(mid)s)
    ORDER BY is_default DESC, created_at DESC
"""


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(number)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _card_brand(number: str) -> str:
    if number.startswith("4"):
        return "visa"
    if re.match(r"^(5[1-5]|2[2-7])", number):
        return "mastercard"
    if re.match(r"^(60|65|81|82|508)", number):
        return "rupay"
    if re.match(r"^3[47]", number):
        return "amex"
    if re.match(r"^(50|5[6-9]|6)", number):
        return "maestro"
    return "unknown"


def _save_method(cur, uid: int, set_default: bool, **cols) -> dict:
    first = not db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM payment_methods WHERE customer_id = %s)", (uid,))
    keys = list(cols)
    row = db.one(cur, f"""
        INSERT INTO payment_methods (customer_id, {", ".join(keys)}) VALUES (%s, {", ".join(["%s"] * len(keys))}) RETURNING id
    """, (uid, *cols.values()))
    if set_default or first:
        _make_default(cur, uid, row["id"])
    return db.one(cur, METHOD_SQL, {"uid": uid, "mid": row["id"]})


def _make_default(cur, uid: int, mid: int) -> None:
    cur.execute("UPDATE payment_methods SET is_default = false WHERE customer_id = %s AND is_default AND id <> %s", (uid, mid))
    cur.execute("UPDATE payment_methods SET is_default = true WHERE id = %s", (mid,))


@router.get("/payment-methods", summary="SavedPaymentMethod[]")
def list_methods(user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return db.all_rows(cur, METHOD_SQL, {"uid": user.id, "mid": None})


@router.post("/payment-methods/cards", status_code=201)
def add_card(body: CardIn, user: CurrentUser = Depends(require_customer)):
    # sandbox only. we keep brand + last4, never the full number
    if get_settings().payment_provider != "sandbox":
        raise ApiError(400, "Cards must be tokenised by the payment gateway; send the gateway token instead.")
    number = re.sub(r"\D", "", body.card_number)
    if not 12 <= len(number) <= 19 or not _luhn_ok(number):
        raise ApiError(422, "Card number is not valid.")
    brand = _card_brand(number)
    with db.tx() as cur:
        return _save_method(cur, user.id, body.set_default, method_type="card",
                            title=f"{brand.capitalize()} **** {number[-4:]}", subtitle=f"Expires {body.expiry_month:02d}/{body.expiry_year % 100:02d}",
                            card_brand=brand, card_last4=number[-4:], card_expiry_month=body.expiry_month,
                            card_expiry_year=body.expiry_year, gateway_token=f"tok_sandbox_{secrets.token_hex(10)}")


@router.post("/payment-methods/upi", status_code=201)
def add_upi(body: UpiIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _save_method(cur, user.id, body.set_default, method_type="upi", title="UPI", subtitle=body.vpa, upi_vpa=body.vpa)


@router.post("/payment-methods/netbanking", status_code=201)
def add_netbanking(body: NetBankingIn, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        return _save_method(cur, user.id, body.set_default, method_type="netbanking", title=body.bank_name,
                            subtitle="Net banking", bank_code=body.bank_code, bank_name=body.bank_name)


@router.post("/payment-methods/{method_id}/default")
def set_default_method(method_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        if db.one(cur, METHOD_SQL, {"uid": user.id, "mid": method_id}) is None:
            raise not_found("Payment method")
        _make_default(cur, user.id, method_id)
        return db.one(cur, METHOD_SQL, {"uid": user.id, "mid": method_id})


@router.delete("/payment-methods/{method_id}")
def delete_method(method_id: int, user: CurrentUser = Depends(require_customer)):
    with db.tx() as cur:
        m = db.one(cur, "DELETE FROM payment_methods WHERE id = %s AND customer_id = %s RETURNING is_default", (method_id, user.id))
        if m is None:
            raise not_found("Payment method")
        if m["is_default"]:
            nxt = db.scalar(cur, "SELECT id FROM payment_methods WHERE customer_id = %s ORDER BY created_at DESC LIMIT 1", (user.id,))
            if nxt:
                _make_default(cur, user.id, nxt)
    return {"success": True}

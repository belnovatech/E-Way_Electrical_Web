from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from .. import db
from ..config import get_settings
from ..errors import ApiError
from ..security import CurrentUser, client_ip, create_session, get_current_user, get_setting, user_payload
from ..services import ledger
from ..utils import CamelModel, normalize_phone

router = APIRouter(prefix="/auth", tags=["Auth"])
log = logging.getLogger("eway")


class SendOtpIn(CamelModel):
    country_code: str = "+91"
    phone_number: str
    role: Literal["customer"] = "customer"


class VerifyOtpIn(SendOtpIn):
    otp: str = Field(pattern=r"^\d{6}$")


class LoginIn(CamelModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "super_admin"] = "admin"


class ChangePasswordIn(CamelModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


def _check_country(code: str) -> str:
    if code != "+91":
        raise ApiError(422, "Only Indian (+91) mobile numbers are supported.")
    return code


@router.post("/send-otp")
def send_otp(body: SendOtpIn, request: Request):
    s = get_settings()
    cc, phone = _check_country(body.country_code), normalize_phone(body.phone_number)
    with db.tx() as cur:
        recent = db.one(cur, """
            SELECT EXTRACT(EPOCH FROM (now() - created_at))::int AS age FROM otp_requests
            WHERE country_code = %s AND phone_number = %s AND verified_at IS NULL
            ORDER BY created_at DESC LIMIT 1
        """, (cc, phone))
        if recent and recent["age"] < s.otp_resend_cooldown_seconds:
            wait = s.otp_resend_cooldown_seconds - recent["age"]
            raise ApiError(429, f"Please wait {wait} seconds before requesting another OTP.")

        otp = s.demo_otp if s.otp_demo_mode else f"{secrets.randbelow(10**6):06d}"
        cur.execute("""
            INSERT INTO otp_requests (country_code, phone_number, user_type, otp_hash, expires_at, ip_address)
            VALUES (%s, %s, 'customer', crypt(%s, gen_salt('bf', 6)), now() + make_interval(secs => %s), %s)
        """, (cc, phone, otp, s.otp_ttl_seconds, client_ip(request)))

    if not s.otp_demo_mode:
        # TODO send via SMS (MSG91 / Twilio / SNS)
        log.info("OTP for %s %s generated (SMS provider not configured)", cc, phone[-4:])
    resp = {"success": True, "message": f"OTP sent successfully to {cc} {phone}.",
            "expiresInSeconds": s.otp_ttl_seconds}
    if s.otp_demo_mode:
        resp["demoOtpHint"] = otp
    return resp


@router.post("/verify-otp")
def verify_otp(body: VerifyOtpIn, request: Request):
    s = get_settings()
    cc, phone = _check_country(body.country_code), normalize_phone(body.phone_number)
    failure: ApiError | None = None
    with db.tx() as cur:
        otp_row = db.one(cur, """
            SELECT id, attempts, (otp_hash = crypt(%s, otp_hash)) AS matches FROM otp_requests
            WHERE country_code = %s AND phone_number = %s AND verified_at IS NULL AND expires_at > now()
            ORDER BY created_at DESC LIMIT 1 FOR UPDATE
        """, (body.otp, cc, phone))
        if otp_row is None:
            raise ApiError(400, "OTP expired or not requested. Please request a new OTP.", "otp_expired")
        if otp_row["attempts"] >= s.otp_max_attempts:
            raise ApiError(429, "Too many incorrect attempts. Please request a new OTP.", "otp_locked")
        if not otp_row["matches"]:
            cur.execute("UPDATE otp_requests SET attempts = attempts + 1 WHERE id = %s", (otp_row["id"],))
            left = s.otp_max_attempts - otp_row["attempts"] - 1
            failure = ApiError(400, f"Incorrect OTP. {left} attempt(s) left.", "otp_invalid")
        else:
            cur.execute("UPDATE otp_requests SET verified_at = now() WHERE id = %s", (otp_row["id"],))
            user = db.one(cur, """
                SELECT * FROM users WHERE user_type = 'customer' AND country_code = %s AND phone_number = %s
            """, (cc, phone))
            is_new = user is None
            if is_new:
                user = db.one(cur, """
                    INSERT INTO users (user_type, country_code, phone_number, is_verified)
                    VALUES ('customer', %s, %s, true) RETURNING *
                """, (cc, phone))
                user = db.one(cur, "UPDATE users SET public_code = 'CUST-' || (1000 + id) WHERE id = %s RETURNING *",
                              (user["id"],))
                cur.execute("INSERT INTO customer_profiles (user_id) VALUES (%s)", (user["id"],))  # trigger adds wallet
            elif user["status"] != "active":
                raise ApiError(403, "This account has been suspended. Please contact support.", "account_inactive")
            cur.execute("UPDATE users SET last_login_at = now(), is_verified = true WHERE id = %s", (user["id"],))
            token, expires = create_session(cur, user["id"], "customer", request)
            complete = db.scalar(cur, "SELECT is_profile_complete FROM customer_profiles WHERE user_id = %s", (user["id"],))
    if failure:
        raise failure
    return {"success": True, "message": "OTP verified successfully.", "token": token, "expiresAt": expires.isoformat(),
            "user": user_payload(user), "isNewUser": is_new, "isProfileComplete": bool(complete)}


@router.post("/login")
def login(body: LoginIn, request: Request):
    # staff login - admin console and super admin portal
    allowed = ("admin", "super_admin") if body.role == "admin" else ("super_admin",)
    portal = "super_admin" if body.role == "super_admin" else "admin"
    failure: ApiError | None = None
    with db.tx() as cur:
        max_attempts = int(get_setting(cur, "max_failed_logins", 5))
        lock_minutes = int(get_setting(cur, "lockout_minutes", 15))
        user = db.one(cur, """
            SELECT u.*, (u.password_hash = crypt(%s, u.password_hash)) AS password_ok, r.name AS role_name
            FROM users u LEFT JOIN staff_profiles sp ON sp.user_id = u.id LEFT JOIN roles r ON r.id = sp.role_id
            WHERE u.email = %s AND u.user_type = ANY(%s::user_type[])
            ORDER BY (u.user_type = 'super_admin') DESC LIMIT 1 FOR UPDATE OF u
        """, (body.password, body.email.lower(), list(allowed)))
        now = datetime.now(timezone.utc)

        def audit_login(outcome: str, details: str):
            ledger.audit(cur, None, request, portal=portal, action=f"Login {outcome}", action_type="LOGIN",
                         module="Auth" if portal == "super_admin" else "Security", target=body.email,
                         details=details, outcome="success" if outcome == "succeeded" else "failed",
                         severity="info" if outcome == "succeeded" else "warning",
                         actor_name=(user or {}).get("full_name") or body.email, actor_email=body.email,
                         actor_id=(user or {}).get("id"))

        if user is None:
            audit_login("failed", "Unknown account")
            failure = ApiError(401, "Invalid email or password.", "invalid_credentials")
        elif user["locked_until"] and user["locked_until"] > now:
            mins = int((user["locked_until"] - now).total_seconds() // 60) + 1
            failure = ApiError(423, f"Account locked after repeated failed attempts. Try again in {mins} minute(s).", "locked")
        elif user["status"] != "active":
            failure = ApiError(403, "This account is not active.", "account_inactive")
        elif not user["password_ok"]:
            attempts = user["failed_login_attempts"] + 1
            locked = attempts >= max_attempts
            cur.execute("UPDATE users SET failed_login_attempts = %s, locked_until = %s WHERE id = %s",
                        (0 if locked else attempts, now + timedelta(minutes=lock_minutes) if locked else None, user["id"]))
            audit_login("failed", "Account locked" if locked else f"Wrong password (attempt {attempts})")
            failure = ApiError(423 if locked else 401,
                               f"Too many failed attempts. Account locked for {lock_minutes} minutes." if locked
                               else "Invalid email or password.", "locked" if locked else "invalid_credentials")
        else:
            cur.execute("UPDATE users SET failed_login_attempts = 0, locked_until = NULL, last_login_at = now(), "
                        "last_active_at = now() WHERE id = %s", (user["id"],))
            token, expires = create_session(cur, user["id"], user["user_type"], request)
            audit_login("succeeded", f"Signed in to the {portal.replace('_', ' ')} portal")
    if failure:
        raise failure
    return {"success": True, "message": "Signed in successfully.", "token": token, "expiresAt": expires.isoformat(),
            "user": user_payload(user)}


@router.post("/logout")
def logout(user: CurrentUser = Depends(get_current_user)):
    with db.tx() as cur:
        cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE id = %s", (user.session_id,))
    return {"success": True, "message": "Signed out."}


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)):
    return {**user_payload(user), "roleCode": user.role_code, "roleName": user.role_name,
            "permissions": sorted(user.permissions),
            "zoneIds": None if user.zone_ids is None else [str(z) for z in user.zone_ids]}


PASSWORD_RULES = [(r"[A-Z]", "an uppercase letter"), (r"[a-z]", "a lowercase letter"),
                  (r"[0-9]", "a number"), (r"[^A-Za-z0-9]", "a special character")]


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request, user: CurrentUser = Depends(get_current_user)):
    if not user.is_staff:
        raise ApiError(403, "Customers sign in with OTP and have no password.")
    missing = [label for pattern, label in PASSWORD_RULES if not re.search(pattern, body.new_password)]
    if missing:
        raise ApiError(422, "Password must contain " + ", ".join(missing) + ".")
    if body.new_password == body.current_password:
        raise ApiError(422, "The new password must be different from the current one.")
    with db.tx() as cur:
        ok = db.scalar(cur, "SELECT password_hash = crypt(%s, password_hash) FROM users WHERE id = %s",
                       (body.current_password, user.id))
        if not ok:
            raise ApiError(401, "Current password is incorrect.", "invalid_credentials")
        cur.execute("UPDATE users SET password_hash = crypt(%s, gen_salt('bf', 10)), password_changed_at = now() "
                    "WHERE id = %s", (body.new_password, user.id))
        cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s AND id <> %s AND revoked_at IS NULL",
                    (user.id, user.session_id))
        ledger.audit(cur, user, request, action="Changed password", action_type="SECURITY", module="Security",
                     target=user.email, severity="notice")
    return {"success": True, "message": "Password changed. Other sessions have been signed out."}

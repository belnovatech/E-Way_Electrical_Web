# Auth + permissions.
# The JWT only carries a session id; we check user_sessions on every request so
# logout / suspension / idle timeout kick in right away.
from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .config import get_settings
from .errors import ApiError

bearer = HTTPBearer(auto_error=False)

ZONE_BOUND_ROLES = {"ROLE_ZONE_MANAGER", "ROLE_FIELD_ENGINEER"}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request) -> str | None:
    # bad / spoofed values become NULL so the inet column doesn't blow up
    fwd = request.headers.get("x-forwarded-for")
    candidate = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else None
    except ValueError:
        return None


def device_of(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua[:200] if ua else None


@dataclass
class CurrentUser:
    id: int
    user_type: str
    session_id: int
    name: str | None
    email: str | None
    phone: str | None
    country_code: str
    public_code: str | None
    created_at: datetime
    role_code: str | None = None
    role_name: str | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    zone_ids: list[int] | None = None     # None = all zones

    @property
    def is_customer(self) -> bool:
        return self.user_type == "customer"

    @property
    def is_staff(self) -> bool:
        return self.user_type in ("admin", "super_admin")

    @property
    def is_super_admin(self) -> bool:
        return self.user_type == "super_admin"

    @property
    def is_global(self) -> bool:
        return self.zone_ids is None

    def can(self, *codes: str) -> bool:
        return "ALL_PERMISSIONS" in self.permissions or any(c in self.permissions for c in codes)

    def in_scope(self, zone_id: int | None) -> bool:
        return self.zone_ids is None or (zone_id is not None and zone_id in self.zone_ids)


def get_setting(cur, key: str, default):
    value = db.scalar(cur, "SELECT value FROM system_settings WHERE key = %s", (key,))
    return default if value is None else value


def create_session(cur, user_id: int, user_type: str, request: Request) -> tuple[str, datetime]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + (timedelta(days=s.customer_token_days) if user_type == "customer"
                     else timedelta(hours=s.staff_token_hours))
    # need the session id inside the JWT, so insert with a placeholder hash first
    row = db.one(cur, """
        INSERT INTO user_sessions (user_id, token_hash, ip_address, device, expires_at)
        VALUES (%s, md5(random()::text || clock_timestamp()::text), %s, %s, %s) RETURNING id
    """, (user_id, client_ip(request), device_of(request), expires))
    token = jwt.encode(
        {"sub": str(user_id), "sid": row["id"], "typ": user_type, "iat": now, "exp": expires},
        s.jwt_secret, algorithm=s.jwt_algorithm,
    )
    cur.execute("UPDATE user_sessions SET token_hash = %s WHERE id = %s", (token_hash(token), row["id"]))
    return token, expires


def _idle_minutes(cur, user_type: str) -> int | None:
    if user_type == "super_admin":
        return int(get_setting(cur, "super_admin_session_mins", 45))
    if user_type == "admin":
        return int(get_setting(cur, "admin_session_timeout_mins", 15))
    return None


def load_scope(cur, user_id: int, user_type: str, role_code: str | None, perms: frozenset[str]) -> list[int] | None:
    if user_type == "super_admin" or "ALL_PERMISSIONS" in perms:
        return None
    rows = db.all_rows(cur, """
        SELECT zone_id, is_active FROM admin_zone_assignments WHERE admin_id = %s
        UNION ALL
        SELECT zone_id, true FROM field_engineers WHERE user_id = %s
    """, (user_id, user_id))
    zone_bound = bool(rows) or role_code in ZONE_BOUND_ROLES
    if not zone_bound:
        return None                      # e.g. financial auditor, compliance officer
    return sorted({r["zone_id"] for r in rows if r["is_active"]})


def get_current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    if cred is None or not cred.credentials:
        raise ApiError(401, "Authentication required.")
    s = get_settings()
    try:
        payload = jwt.decode(cred.credentials, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "Your session has expired. Please sign in again.", "session_expired")
    except jwt.InvalidTokenError:
        raise ApiError(401, "Invalid authentication token.")

    timed_out = False
    with db.tx() as cur:
        row = db.one(cur, """
            SELECT s.id AS session_id, s.revoked_at, s.expires_at, s.last_active_at,
                   u.id, u.user_type, u.full_name, u.email, u.phone_number, u.country_code,
                   u.public_code, u.status, u.created_at, r.code AS role_code, r.name AS role_name, r.id AS role_id
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN staff_profiles sp ON sp.user_id = u.id
            LEFT JOIN roles r ON r.id = sp.role_id
            WHERE s.id = %s AND s.token_hash = %s
        """, (payload.get("sid"), token_hash(cred.credentials)))
        now = datetime.now(timezone.utc)
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
            raise ApiError(401, "Your session has ended. Please sign in again.", "session_expired")
        if row["status"] != "active":
            raise ApiError(403, "This account is not active.", "account_inactive")

        idle = _idle_minutes(cur, row["user_type"])
        if idle and now - row["last_active_at"] > timedelta(minutes=idle):
            cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE id = %s", (row["session_id"],))
            timed_out = True
        else:
            if now - row["last_active_at"] > timedelta(seconds=30):
                cur.execute("UPDATE user_sessions SET last_active_at = now() WHERE id = %s", (row["session_id"],))
                cur.execute("UPDATE users SET last_active_at = now() WHERE id = %s", (row["id"],))
            perms = frozenset()
            if row["role_id"] is not None:
                perms = frozenset(r["code"] for r in db.all_rows(cur, """
                    SELECT p.code FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id
                    WHERE rp.role_id = %s
                """, (row["role_id"],)))
            zones = None
            if row["user_type"] != "customer":
                zones = load_scope(cur, row["id"], row["user_type"], row["role_code"], perms)
    if timed_out:
        raise ApiError(401, "Signed out after a period of inactivity.", "session_timeout")

    return CurrentUser(
        id=row["id"], user_type=row["user_type"], session_id=row["session_id"], name=row["full_name"],
        email=row["email"], phone=row["phone_number"], country_code=row["country_code"],
        public_code=row["public_code"], created_at=row["created_at"], role_code=row["role_code"],
        role_name=row["role_name"], permissions=perms, zone_ids=zones,
    )


def require_customer(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_customer:
        raise ApiError(403, "This endpoint is for customer accounts.")
    return user


def require_staff(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_staff:
        raise ApiError(403, "Admin access required.")
    return user


def require_super_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_super_admin:
        raise ApiError(403, "Super admin access required.")
    return user


def require_permission(*codes: str) -> Callable[..., CurrentUser]:
    # any one of codes is enough, ALL_PERMISSIONS always passes
    def dependency(user: CurrentUser = Depends(require_staff)) -> CurrentUser:
        if not user.can(*codes):
            raise ApiError(403, f"Missing permission: {' or '.join(codes)}.", "missing_permission")
        return user
    return dependency


def user_payload(row_or_user) -> dict:
    # same shape as User in src/types/auth.ts
    if isinstance(row_or_user, CurrentUser):
        u = row_or_user
        return {"id": str(u.id), "phoneNumber": u.phone, "countryCode": u.country_code, "email": u.email,
                "name": u.name, "role": u.user_type, "createdAt": u.created_at.isoformat(),
                "publicCode": u.public_code}
    r = row_or_user
    return {"id": str(r["id"]), "phoneNumber": r["phone_number"], "countryCode": r["country_code"],
            "email": r["email"], "name": r["full_name"], "role": r["user_type"],
            "createdAt": r["created_at"].isoformat(), "publicCode": r["public_code"]}

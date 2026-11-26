from __future__ import annotations

import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from .. import db
from ..errors import ApiError, not_found
from ..security import CurrentUser, require_permission
from ..services import ledger
from ..utils import CamelModel, initials, parse_enum

router = APIRouter(prefix="/admin", tags=["Roles & Permissions"])

CATEGORY_UI = {"grid_operations": "Grid Operations", "maintenance": "Maintenance", "finance": "Finance",
               "users": "Users", "security": "Security", "reporting": "Reporting"}
ACCESS_UI = {"critical": "Critical", "elevated": "Elevated", "operational": "Operational", "audit": "Audit",
             "read_only": "Read Only"}
ACCESS_LEVELS = set(ACCESS_UI)

ROLE_SQL = """
    SELECT r.id::text AS id, r.name, r.code, initcap(r.role_type::text) AS type, r.access_level::text AS "accessLevel",
           ro.user_count::int AS "userCount", r.description, COALESCE(ro.permission_codes, '{}') AS permissions,
           (r.role_type = 'system') AS "isSystem", r.is_protected AS "isProtected",
           CASE WHEN r.is_active THEN 'Active' ELSE 'Inactive' END AS status, r.updated_at AS "lastModified",
           COALESCE(mu.full_name, 'System') AS "modifiedBy", r.created_at AS "createdAt"
    FROM roles r JOIN v_role_overview ro ON ro.id = r.id
    LEFT JOIN users mu ON mu.id = COALESCE(r.updated_by, r.created_by)
    WHERE (%(rid)s::bigint IS NULL OR r.id = %(rid)s)
    ORDER BY r.id
"""

STAFF_SQL = """
    SELECT u.id::text AS id, u.public_code AS "staffCode", u.full_name AS name, u.email, r.id::text AS "roleId",
           r.name AS "roleName", sp.department, sp.city, CASE WHEN u.status = 'active' THEN 'Active' ELSE 'Inactive' END AS status,
           u.two_factor_enabled AS "twoFactorEnabled", sp.role_assigned_at AS "assignedAt", u.user_type AS "userType",
           u.last_login_at AS "lastLoginAt"
    FROM staff_profiles sp JOIN users u ON u.id = sp.user_id JOIN roles r ON r.id = sp.role_id
    WHERE (%(rid)s::bigint IS NULL OR r.id = %(rid)s) AND (%(uid)s::bigint IS NULL OR u.id = %(uid)s)
    ORDER BY r.id, u.full_name
"""

can_view = require_permission("ROLE_MANAGEMENT", "AUDIT_LOGS")
can_manage = require_permission("ROLE_MANAGEMENT")


def _roles(cur, rid: int | None = None) -> list[dict]:
    rows = db.all_rows(cur, ROLE_SQL, {"rid": rid})
    for r in rows:
        r["accessLevel"] = ACCESS_UI.get(r["accessLevel"], r["accessLevel"])
    return rows


def _role(cur, rid: int) -> dict:
    rows = _roles(cur, rid)
    if not rows:
        raise not_found("Role")
    return rows[0]


def _staff(cur, rid: int | None = None, uid: int | None = None) -> list[dict]:
    rows = db.all_rows(cur, STAFF_SQL, {"rid": rid, "uid": uid})
    for r in rows:
        r["avatar"] = initials(r["name"])
    return rows


def _permission_ids(cur, codes: list[str]) -> list[int]:
    codes = sorted(set(c.strip().upper() for c in codes))
    rows = db.all_rows(cur, "SELECT id, code FROM permissions WHERE code = ANY(%s)", (codes,))
    unknown = set(codes) - {r["code"] for r in rows}
    if unknown:
        raise ApiError(422, f"Unknown permission(s): {', '.join(sorted(unknown))}")
    return [r["id"] for r in rows]


def _ensure_can_grant(user: CurrentUser, codes) -> None:
    # can't hand out permissions you don't have yourself
    if "ALL_PERMISSIONS" in user.permissions:
        return
    beyond = sorted(c.strip().upper() for c in codes if c.strip().upper() not in user.permissions)
    if beyond:
        raise ApiError(403, f"You cannot grant permissions you do not hold: {', '.join(beyond)}")


def _check_grantable(cur, user: CurrentUser, codes: list[str]) -> list[int]:
    ids = _permission_ids(cur, codes)
    _ensure_can_grant(user, codes)
    return ids


class RoleIn(CamelModel):
    name: str = Field(min_length=3, max_length=100)
    code: Optional[str] = Field(None, pattern=r"^ROLE_[A-Z0-9_]+$")
    description: str = Field(min_length=5, max_length=500)
    access_level: str = "Operational"
    permissions: list[str] = Field(min_length=1)


class RoleUpdateIn(CamelModel):
    name: Optional[str] = Field(None, min_length=3, max_length=100)
    description: Optional[str] = Field(None, min_length=5, max_length=500)
    access_level: Optional[str] = None
    permissions: Optional[list[str]] = Field(None, min_length=1)
    status: Optional[Literal["Active", "Inactive", "active", "inactive"]] = None


class AssignRoleIn(CamelModel):
    role_id: int


class StaffStatusIn(CamelModel):
    status: Literal["Active", "Inactive", "active", "inactive"]


@router.get("/permissions", summary="PermissionDefinition[]")
def list_permissions(user: CurrentUser = Depends(can_view)):
    with db.tx() as cur:
        rows = db.all_rows(cur, """SELECT id::text AS id, code, name, description, category::text AS category,
                                          is_dangerous AS "isDangerous" FROM permissions ORDER BY id""")
    for r in rows:
        r["category"] = CATEGORY_UI[r["category"]]
    return rows


@router.get("/roles", summary="AdminRolePermission[]")
def list_roles(user: CurrentUser = Depends(can_view)):
    with db.tx() as cur:
        return _roles(cur)


@router.get("/roles/{role_id}")
def get_role(role_id: int, user: CurrentUser = Depends(can_view)):
    with db.tx() as cur:
        return _role(cur, role_id)


@router.post("/roles", status_code=201)
def create_role(body: RoleIn, request: Request, user: CurrentUser = Depends(can_manage)):
    level = parse_enum(body.access_level, ACCESS_LEVELS, "accessLevel")
    code = body.code or "ROLE_" + re.sub(r"[^A-Z0-9]+", "_", body.name.upper()).strip("_")
    with db.tx() as cur:
        perm_ids = _check_grantable(cur, user, body.permissions)
        rid = db.scalar(cur, """
            INSERT INTO roles (code, name, description, role_type, access_level, created_by, updated_by)
            VALUES (%s, %s, %s, 'custom', %s, %s, %s) RETURNING id
        """, (code, body.name, body.description, level, user.id, user.id))
        cur.executemany("INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)", [(rid, p) for p in perm_ids])
        ledger.audit(cur, user, request, action=f"Created role {body.name}", action_type="Created", module="Security",
                     target=f"Role {code}", record_id=rid, new=body.model_dump(), severity="warning")
        return _role(cur, rid)


@router.patch("/roles/{role_id}")
def update_role(role_id: int, body: RoleUpdateIn, request: Request, user: CurrentUser = Depends(can_manage)):
    data = body.model_dump(exclude_unset=True)
    with db.tx() as cur:
        before = _role(cur, role_id)
        if before["isProtected"]:
            raise ApiError(403, f'{before["name"]} is a protected system role and cannot be modified.')
        cur.execute("SELECT 1 FROM roles WHERE id = %s FOR UPDATE", (role_id,))
        if "name" in data:
            cur.execute("UPDATE roles SET name = %s WHERE id = %s", (data["name"], role_id))
        if "description" in data:
            cur.execute("UPDATE roles SET description = %s WHERE id = %s", (data["description"], role_id))
        if "access_level" in data:
            cur.execute("UPDATE roles SET access_level = %s WHERE id = %s",
                        (parse_enum(data["access_level"], ACCESS_LEVELS, "accessLevel"), role_id))
        if "status" in data:
            active = data["status"].lower() == "active"
            if not active and before["userCount"]:
                raise ApiError(409, f'Reassign the {before["userCount"]} staff member(s) before deactivating this role.')
            cur.execute("UPDATE roles SET is_active = %s WHERE id = %s", (active, role_id))
        if "permissions" in data:
            ids = _permission_ids(cur, data["permissions"])
            _ensure_can_grant(user, set(c.strip().upper() for c in data["permissions"]) - set(before["permissions"]))
            cur.execute("DELETE FROM role_permissions WHERE role_id = %s", (role_id,))
            cur.executemany("INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)", [(role_id, i) for i in ids])
        cur.execute("UPDATE roles SET updated_by = %s WHERE id = %s", (user.id, role_id))
        after = _role(cur, role_id)
        ledger.audit(cur, user, request, action=f'Updated role {before["name"]}', action_type="Updated", module="Security",
                     target=f'Role {before["code"]}', record_id=role_id,
                     previous={k: before[k] for k in ("name", "description", "accessLevel", "permissions", "status")},
                     new={k: after[k] for k in ("name", "description", "accessLevel", "permissions", "status")},
                     severity="warning")
        return after


@router.delete("/roles/{role_id}")
def delete_role(role_id: int, request: Request, user: CurrentUser = Depends(can_manage)):
    with db.tx() as cur:
        role = _role(cur, role_id)
        if role["isProtected"] or role["isSystem"]:
            raise ApiError(409, "System roles cannot be deleted.")
        if role["userCount"]:
            raise ApiError(409, f'Reassign the {role["userCount"]} staff member(s) before deleting this role.')
        cur.execute("DELETE FROM roles WHERE id = %s", (role_id,))
        ledger.audit(cur, user, request, action=f'Deleted role {role["name"]}', action_type="Deleted", module="Security",
                     target=f'Role {role["code"]}', record_id=role_id, previous=role, severity="warning")
    return {"success": True}


@router.get("/staff", summary="RoleStaffMember[]")
def list_staff(role_id: Optional[int] = None, user: CurrentUser = Depends(can_view)):
    with db.tx() as cur:
        return _staff(cur, rid=role_id)


@router.put("/staff/{staff_id}/role")
def assign_role(staff_id: int, body: AssignRoleIn, request: Request, user: CurrentUser = Depends(can_manage)):
    if staff_id == user.id:
        raise ApiError(403, "You cannot change your own role.")
    with db.tx() as cur:
        target = _staff(cur, uid=staff_id)
        if not target:
            raise not_found("Staff member")
        target = target[0]
        role = _role(cur, body.role_id)
        if role["status"] != "Active":
            raise ApiError(409, "This role is inactive.")
        if (role["code"] == "ROLE_SUPER_ADMIN") != (target["userType"] == "super_admin"):
            raise ApiError(409, "The Super Admin role belongs only to super-admin accounts.")
        if db.scalar(cur, "SELECT EXISTS (SELECT 1 FROM field_engineers WHERE user_id = %s)", (staff_id,)) \
                and role["code"] != "ROLE_FIELD_ENGINEER":
            raise ApiError(409, "This person is on the field-engineer roster; remove them from it first.")
        _check_grantable(cur, user, role["permissions"])
        cur.execute("""UPDATE staff_profiles SET role_id = %s, role_assigned_at = now(), role_assigned_by = %s
                       WHERE user_id = %s""", (body.role_id, user.id, staff_id))
        ledger.audit(cur, user, request, action=f'Assigned {target["name"]} to {role["name"]}', action_type="Updated",
                     module="Security", target=f'Staff {target["staffCode"] or staff_id}', record_id=staff_id,
                     previous={"role": target["roleName"]}, new={"role": role["name"]}, severity="warning")
        return _staff(cur, uid=staff_id)[0]


@router.patch("/staff/{staff_id}/status")
def set_staff_status(staff_id: int, body: StaffStatusIn, request: Request,
                     user: CurrentUser = Depends(require_permission("ROLE_MANAGEMENT", "USER_MANAGEMENT"))):
    if staff_id == user.id:
        raise ApiError(403, "You cannot deactivate your own account.")
    active = body.status.lower() == "active"
    with db.tx() as cur:
        target = _staff(cur, uid=staff_id)
        if not target:
            raise not_found("Staff member")
        if target[0]["userType"] == "super_admin" and not user.is_super_admin:
            raise ApiError(403, "Only a super admin can change another super admin.")
        cur.execute("UPDATE users SET status = %s WHERE id = %s", ("active" if active else "inactive", staff_id))
        if not active:
            cur.execute("UPDATE user_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (staff_id,))
        ledger.audit(cur, user, request, action=f'{"Activated" if active else "Deactivated"} {target[0]["name"]}',
                     action_type="Changed Status", module="Security", target=f"Staff {staff_id}", record_id=staff_id,
                     severity="warning")
        return _staff(cur, uid=staff_id)[0]

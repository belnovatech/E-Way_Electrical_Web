from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import db
from ..errors import not_found
from ..security import CurrentUser, get_current_user

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
def list_notifications(unread_only: bool = Query(False, alias="unreadOnly"), limit: int = Query(50, ge=1, le=200),
                       user: CurrentUser = Depends(get_current_user)):
    with db.tx() as cur:
        items = db.all_rows(cur, """
            SELECT id::text AS id, title, message, created_at AS timestamp, is_read AS "isRead", type,
                   target_route AS "targetRoute", entity_type AS "entityType", entity_id::text AS "entityId"
            FROM notifications WHERE user_id = %s AND (NOT %s OR NOT is_read)
            ORDER BY created_at DESC LIMIT %s
        """, (user.id, unread_only, limit))
        unread = db.scalar(cur, "SELECT COUNT(*)::int FROM notifications WHERE user_id = %s AND NOT is_read", (user.id,))
    return {"items": items, "unreadCount": unread}


@router.post("/{notification_id}/read")
def mark_read(notification_id: int, user: CurrentUser = Depends(get_current_user)):
    with db.tx() as cur:
        row = db.one(cur, """
            UPDATE notifications SET is_read = true, read_at = COALESCE(read_at, now())
            WHERE id = %s AND user_id = %s RETURNING id
        """, (notification_id, user.id))
    if row is None:
        raise not_found("Notification")
    return {"success": True}


@router.post("/read-all")
def mark_all_read(user: CurrentUser = Depends(get_current_user)):
    with db.tx() as cur:
        cur.execute("UPDATE notifications SET is_read = true, read_at = now() WHERE user_id = %s AND NOT is_read", (user.id,))
        count = cur.rowcount
    return {"success": True, "updated": count}

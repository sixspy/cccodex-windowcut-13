"""旁路暂存空桩：关窗切版时新到事件可进 hold（甲路径同事务审计）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from audit_gate import AuditMissing, commit_with_audit
from auth import AuthHeader, require_user
from db import connect
from locks import deny_on_contention, release
from models import HoldIn

router = APIRouter(tags=["hold"])


@router.get("/api/holds")
def list_holds(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM hold_staging ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/holds")
def create_hold(body: HoldIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"hold:{body.window_name}"
    with connect() as conn:
        deny_on_contention(conn, lock_key, user, biz_action="hold_create")
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.window_name,)
            ).fetchone()
            if not w:
                conn.rollback()
                raise HTTPException(404, "窗口不存在")
            now = datetime.now().isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO hold_staging(window_id,biz_key,event_ts,amount,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, body.event_ts, body.amount, "暂存", now),
            )
            hold_id = int(cur.lastrowid)
            try:
                commit_with_audit(
                    conn,
                    user,
                    "hold_create",
                    body.biz_key,
                    {"window_id": w["id"], "hold_id": hold_id},
                )
            except AuditMissing as exc:
                raise HTTPException(500, str(exc)) from exc
            return {"ok": True, "hold_id": hold_id, "status": "暂存"}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/holds/{hold_id}/release-stub")
def release_hold_stub(hold_id: int, authorization: AuthHeader = None):
    """空桩：暂存晋升正式或整笔回滚，写路径待题面择一。"""
    user = require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM hold_staging WHERE id=?", (hold_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "暂存不存在")
        commit_with_audit(
            conn,
            user,
            "hold_release_stub",
            str(hold_id),
            {
                "paths": ["晋升正式同事务", "作废暂存零改正式"],
                "note": "母仓不落地，仅登记",
            },
        )
    return {"ok": True, "stub": True}

"""业务窗开/关/列表（甲路径：业务与审计同事务）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from audit_gate import AuditMissing, commit_with_audit
from auth import AuthHeader, require_user
from db import connect
from locks import deny_on_contention, release
from models import WindowCreateIn

router = APIRouter(tags=["windows"])


@router.get("/api/windows")
def list_windows(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/windows")
def create_window(body: WindowCreateIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,1)",
                (body.name, body.open_at, body.close_at, "开启"),
            )
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise HTTPException(409, f"窗口创建失败: {exc}") from exc
        wid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        try:
            commit_with_audit(conn, user, "window_create", body.name, {"id": wid})
        except AuditMissing as exc:
            raise HTTPException(500, str(exc)) from exc
        row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        return dict(row)


@router.post("/api/windows/{wid}/close")
def close_window(wid: int, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        deny_on_contention(conn, lock_key, user, biz_action="window_close")
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                conn.rollback()
                raise HTTPException(404, "窗口不存在")
            if w["status"] == "已关闭":
                conn.rollback()
                return {"ok": True, "already": True}
            now = datetime.now().isoformat(timespec="seconds")
            token = f"seal-{wid}-{w['generation']}-{now}"
            conn.execute(
                "UPDATE windows SET status='已关闭', seal_token=? WHERE id=?",
                (token, wid),
            )
            try:
                commit_with_audit(
                    conn,
                    user,
                    "window_close",
                    str(wid),
                    {"seal_token": token, "generation": w["generation"]},
                )
            except AuditMissing as exc:
                raise HTTPException(500, str(exc)) from exc
            return {"ok": True, "seal_token": token}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/windows/{wid}")
def get_window(wid: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "窗口不存在")
        ev = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM events WHERE window_id=? AND disposition='窗内'",
            (wid,),
        ).fetchone()
        return {
            "window": dict(w),
            "in_window_count": ev["c"],
            "in_window_amount": ev["s"],
        }

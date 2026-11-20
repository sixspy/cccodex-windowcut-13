"""事件入账：窗内 / 拒收 / 迟到桶分流（甲路径：业务与审计同事务）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from audit_gate import AuditMissing, commit_with_audit
from auth import AuthHeader, require_user
from db import connect
from locks import deny_on_contention, release
from models import EventIn

router = APIRouter(tags=["ingest"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _gate(conn, user, action, target, payload) -> None:
    try:
        commit_with_audit(conn, user, action, target, payload)
    except AuditMissing as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/api/events")
def ingest_event(body: EventIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"ingest:{body.window_name}"
    with connect() as conn:
        deny_on_contention(conn, lock_key, user, biz_action="ingest_event")
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.window_name,)
            ).fetchone()
            if not w:
                conn.rollback()
                raise HTTPException(404, "窗口不存在")
            ts = body.event_ts
            created = _now()
            if w["status"] == "已关闭":
                cur = conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (None, body.biz_key, ts, body.amount, "迟到桶", created),
                )
                eid = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (eid, body.window_name, body.biz_key, ts, body.amount, "关窗后到达", created),
                )
                _gate(
                    conn,
                    user,
                    "ingest_late",
                    body.biz_key,
                    {"event_id": eid, "window": body.window_name},
                )
                return {"disposition": "迟到桶", "event_id": eid}

            if not (w["open_at"] <= ts <= w["close_at"]):
                cur = conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (None, body.biz_key, ts, body.amount, "拒收", created),
                )
                eid = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO reject_slips(window_name,biz_key,event_ts,amount,reason,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (body.window_name, body.biz_key, ts, body.amount, "窗外且窗仍开启", created),
                )
                _gate(
                    conn,
                    user,
                    "ingest_reject",
                    body.biz_key,
                    {"event_id": eid, "window": body.window_name},
                )
                raise HTTPException(409, "事件时间落在窗外且窗仍开启：拒收")

            cur = conn.execute(
                """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, ts, body.amount, "窗内", created),
            )
            eid = int(cur.lastrowid)
            _gate(
                conn,
                user,
                "ingest_in_window",
                body.biz_key,
                {"event_id": eid, "window_id": w["id"]},
            )
            return {"disposition": "窗内", "window_id": w["id"], "event_id": eid}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/events")
def list_events(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/rejects")
def list_rejects(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reject_slips ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

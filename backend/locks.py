"""窗口锁与争用登记（公共并发面，甲路径：争用与审计同事务互证）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from audit_gate import AuditMissing, commit_with_audit
from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def acquire_or_contend(conn, lock_key: str, holder: str) -> tuple[bool, dict]:
    """抢占锁。返回 (是否拿到锁, 争用信息)。

    用 INSERT OR IGNORE 抢 window_locks 主键再回查持锁人：主键冲突由 SQLite
    原子判定，并发下不会因冲突炸 500。输家统一登记 contention_log（本事务内），
    由调用方与 lock_contention 审计一起提交（contention_id 互证）。
    并发单飞的端到端确定性证据见 test_concurrent_materialize_contention_is_audited
    （先到者持锁在临界区，后到者必 409 且零业务改动）。
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now),
    )
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is not None and row["holder"] == holder:
        return True, {}
    cur = conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder'] if row else '未知'}", now),
    )
    return False, {
        "contention_id": int(cur.lastrowid),
        "holder": row["holder"] if row else None,
    }


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    ok, _ = acquire_or_contend(conn, lock_key, holder)
    return ok


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


def deny_on_contention(conn, lock_key: str, holder: str, biz_action: str) -> None:
    """写路径专用：抢锁失败 → 争用台账 + lock_contention 审计同事务提交 → 409。
    争用不产生业务行；审计缺失则按甲路径整笔回滚（连同争用登记一起回滚）。"""
    ok, info = acquire_or_contend(conn, lock_key, holder)
    if ok:
        return
    try:
        commit_with_audit(
            conn,
            holder,
            "lock_contention",
            lock_key,
            {
                "contention_id": info["contention_id"],
                "path": biz_action,
                "holder": info["holder"],
            },
        )
    except AuditMissing as exc:
        raise HTTPException(500, str(exc)) from exc
    raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok, info = acquire_or_contend(conn, body.lock_key, user)
        if not ok:
            try:
                commit_with_audit(
                    conn,
                    user,
                    "lock_contention",
                    body.lock_key,
                    {
                        "contention_id": info["contention_id"],
                        "path": "locks_acquire",
                        "holder": info["holder"],
                    },
                )
            except AuditMissing as exc:
                raise HTTPException(500, str(exc)) from exc
            raise HTTPException(409, "争用：后到只记争用")
        commit_with_audit(
            conn, user, "lock_acquire", body.lock_key, {"ok": True}
        )
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        commit_with_audit(conn, user, "lock_release", body.lock_key, {})
    return {"ok": True}

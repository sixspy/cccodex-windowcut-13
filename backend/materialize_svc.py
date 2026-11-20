"""关窗后物化（甲路径：业务行与 materialize_ok 审计同事务，缺审计整笔回滚）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from audit_gate import AuditMissing, commit_with_audit
from auth import AuthHeader, require_user
from db import connect
from locks import deny_on_contention, release
from models import MaterializeIn

router = APIRouter(tags=["materialize"])


@router.post("/api/windows/{wid}/materialize")
def materialize(
    wid: int,
    authorization: AuthHeader = None,
    body: MaterializeIn = MaterializeIn(),
):
    user = require_user(authorization)
    lock_key = f"materialize:{wid}"
    with connect() as conn:
        # 两人同时触发同一物化：后到只记争用（争用本身同事务有审计）并 409
        deny_on_contention(conn, lock_key, user, biz_action="materialize")
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                conn.rollback()
                raise HTTPException(404, "窗口不存在")
            if w["status"] != "已关闭":
                # 拒写不产生业务行，但留 materialize_denied 审计（同事务）
                try:
                    commit_with_audit(
                        conn,
                        user,
                        "materialize_denied",
                        str(wid),
                        {"reason": "未关窗不能物化", "lock_key": lock_key},
                    )
                except AuditMissing as exc:
                    raise HTTPException(500, str(exc)) from exc
                raise HTTPException(409, "未关窗不能物化")
            now = datetime.now().isoformat(timespec="seconds")
            rows = conn.execute(
                """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s
                   FROM events WHERE window_id=? AND disposition='窗内'""",
                (wid,),
            ).fetchone()
            partition = body.partition_key or f"{w['name']}-g{w['generation']}"
            if body.fail_simulate:
                try:
                    commit_with_audit(
                        conn,
                        user,
                        "materialize_fail_sim",
                        str(wid),
                        {"partition": partition, "lock_key": lock_key},
                    )
                except AuditMissing as exc:
                    raise HTTPException(500, str(exc)) from exc
                raise HTTPException(500, "模拟物化失败（夹具用）")
            cur = conn.execute(
                """INSERT INTO materializations(window_id,status,row_count,total_amount,partition_key,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (wid, "成功", rows["c"], rows["s"], partition, now),
            )
            mat_id = int(cur.lastrowid)
            # 唯一提交口：审计同事务插入+回查+COMMIT；审计失败 → 回滚，业务零改
            try:
                commit_with_audit(
                    conn,
                    user,
                    "materialize_ok",
                    str(wid),
                    {
                        "mat_id": mat_id,
                        "row_count": rows["c"],
                        "total_amount": rows["s"],
                        "partition": partition,
                    },
                )
            except AuditMissing as exc:
                raise HTTPException(500, str(exc)) from exc
            return {
                "mat_id": mat_id,
                "row_count": rows["c"],
                "total_amount": rows["s"],
                "partition_key": partition,
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/materializations")
def list_mats(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.*, w.name AS window_name
               FROM materializations m
               JOIN windows w ON w.id=m.window_id
               ORDER BY m.id DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/materializations/partition-stub")
def partition_stub(authorization: AuthHeader = None):
    """空桩模块：部分物化失败回滚策略待题面落地。"""
    user = require_user(authorization)
    with connect() as conn:
        commit_with_audit(
            conn,
            user,
            "partition_mat_stub",
            None,
            {
                "paths": [
                    "整窗回滚",
                    "保留成功分区",
                ],
                "note": "正确性口径须择一，母仓不实现",
            },
        )
    return {"ok": True, "stub": True, "module": "partition_mat"}

"""甲路径强制审计提交口（无框架依赖，避免与 auth/audit_svc 循环导入）。

口径见 docs/审计互证册.md：业务写入与 audit_trail 同事务，
审计插入后同事务回查，回查通过才 COMMIT；否则 ROLLBACK、业务零改，
并 best-effort 记 audit_deviation。全仓唯一强制提交口。
"""
from __future__ import annotations

import json
import logging

import db

logger = logging.getLogger("audit_gate")


class AuditMissing(RuntimeError):
    """强制审计失败：调用方必须让请求失败、业务零改。"""


def commit_with_audit(
    conn,
    actor: str,
    action: str,
    target: str | None,
    payload: dict | None,
    *,
    deviation_origin: str | None = None,
) -> int:
    """同事务插入审计 → 回查确认 → COMMIT；失败回滚并记 audit_deviation。"""
    try:
        audit_id = db.write_audit(conn, actor, action, target, payload)
        row = conn.execute(
            "SELECT actor, action, target, payload FROM audit_trail WHERE id=?",
            (audit_id,),
        ).fetchone()
        if row is None or row["action"] != action or row["actor"] != actor:
            raise AuditMissing("审计回查不到行")
        expect_payload = json.dumps(payload or {}, ensure_ascii=False)
        if row["payload"] != expect_payload:
            raise AuditMissing("审计回查载荷不一致")
        conn.commit()
        return audit_id
    except Exception as exc:  # noqa: BLE001 —— 任何失败都必须回滚业务
        conn.rollback()
        origin = deviation_origin or action
        logger.error("强制审计失败，整笔回滚 action=%s err=%s", origin, exc)
        # 显式提交：不能用 with connect()（异常会让其上下文管理器改走 rollback，
        # 把 deviation 标记一并回滚）。
        dev_conn = db.connect()
        try:
            db.write_audit(
                dev_conn,
                actor,
                "audit_deviation",
                origin,
                {"reason": str(exc), "path": "jia_same_tx", "target": target},
            )
            dev_conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("audit_deviation 标记也失败了")
            dev_conn.rollback()
        finally:
            dev_conn.close()
        raise AuditMissing(f"强制审计失败，业务已回滚：{exc}") from exc


def audit_payloads(conn, action: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM audit_trail WHERE action=?", (action,)
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"] or "{}"))
        except json.JSONDecodeError:
            continue
    return out


def proof_families(conn) -> list[dict]:
    """五处同态中的三处台账互证：物化 / 登录会话 / 争用。"""
    families = []

    mats = conn.execute("SELECT id, window_id FROM materializations ORDER BY id").fetchall()
    mat_ids_audited = {p.get("mat_id") for p in audit_payloads(conn, "materialize_ok")}
    mat_gaps = [
        {"family": "materializations", "business_id": m["id"], "window_id": m["window_id"]}
        for m in mats
        if m["id"] not in mat_ids_audited
    ]
    families.append(
        {
            "family": "materializations",
            "business_rows": len(mats),
            "audit_rows": len([p for p in audit_payloads(conn, "materialize_ok") if p.get("mat_id")]),
            "gap_count": len(mat_gaps),
            "gaps": mat_gaps,
        }
    )

    sessions = conn.execute(
        "SELECT id, token_sha, status FROM login_sessions ORDER BY id"
    ).fetchall()
    login_shas = {p.get("token_sha") for p in audit_payloads(conn, "login")}
    sess_gaps = [
        {"family": "login_sessions", "business_id": s["id"], "status": s["status"]}
        for s in sessions
        if s["token_sha"] not in login_shas
    ]
    families.append(
        {
            "family": "login_sessions",
            "business_rows": len(sessions),
            "audit_rows": len(login_shas),
            "gap_count": len(sess_gaps),
            "gaps": sess_gaps,
        }
    )

    conts = conn.execute("SELECT id, lock_key FROM contention_log ORDER BY id").fetchall()
    cont_ids_audited = {
        p.get("contention_id") for p in audit_payloads(conn, "lock_contention")
    }
    cont_gaps = [
        {"family": "contention_log", "business_id": c["id"], "lock_key": c["lock_key"]}
        for c in conts
        if c["id"] not in cont_ids_audited
    ]
    families.append(
        {
            "family": "contention_log",
            "business_rows": len(conts),
            "audit_rows": len(
                [
                    p
                    for p in audit_payloads(conn, "lock_contention")
                    if p.get("contention_id") is not None
                ]
            ),
            "gap_count": len(cont_gaps),
            "gaps": cont_gaps,
        }
    )
    return families

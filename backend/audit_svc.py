"""审计与互证只读旁路（路由层）。

强制提交口实现见 audit_gate.commit_with_audit（甲路径，同事务强制审计）。
口径见 docs/审计互证册.md；此处重导出，保持各写路径 import 稳定。
"""
from __future__ import annotations

from fastapi import APIRouter

from audit_gate import AuditMissing, commit_with_audit, proof_families
from auth import AuthHeader, require_user
from db import connect

__all__ = ["AuditMissing", "commit_with_audit", "router"]

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def audit_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/proof")
def audit_proof(authorization: AuthHeader = None):
    """五处同态互证：审计列表/动作汇总/物化台账/登录会话/争用。"""
    require_user(authorization)
    with connect() as conn:
        families = proof_families(conn)
        action_rows = conn.execute(
            "SELECT action, COUNT(*) c FROM audit_trail GROUP BY action"
        ).fetchall()
    aligned = all(f["gap_count"] == 0 for f in families)
    return {
        "aligned": aligned,
        "families": families,
        "action_summary": [dict(r) for r in action_rows],
    }


@router.get("/api/sessions")
def list_sessions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, username, token_sha, role, status, login_at, logout_at "
            "FROM login_sessions ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='开启'"
        ).fetchone()["c"]
        closed_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='已关闭'"
        ).fetchone()["c"]
        late_n = conn.execute("SELECT COUNT(*) c FROM late_bucket").fetchone()["c"]
        reject_n = conn.execute("SELECT COUNT(*) c FROM reject_slips").fetchone()["c"]
        hold_n = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE status='暂存'"
        ).fetchone()["c"]
        mats = conn.execute("SELECT COUNT(*) c FROM materializations").fetchone()["c"]
        contention = conn.execute("SELECT COUNT(*) c FROM contention_log").fetchone()["c"]
        return {
            "open_windows": open_n,
            "closed_windows": closed_n,
            "late_bucket": late_n,
            "rejects": reject_n,
            "holds": hold_n,
            "materializations": mats,
            "contention": contention,
        }

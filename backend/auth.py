"""登录与会话（甲路径：会话行与 login 审计同事务，失败登录也留审计）。"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_gate import AuditMissing, commit_with_audit
from db import connect, sha_password, sha_token

SESSIONS: dict[str, str] = {}
router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    token = authorization.removeprefix("Bearer ").strip()
    user = SESSIONS.get(token)
    if not user:
        raise HTTPException(401, "登录已失效")
    return user


AuthHeader = Annotated[str | None, Header()]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@router.post("/api/login")
def login(body: LoginIn):
    with connect() as conn:
        row = conn.execute(
            "SELECT username,role FROM users WHERE username=? AND password_sha=?",
            (body.username, sha_password(body.password)),
        ).fetchone()
        if not row:
            # 失败登录不产生会话，但必须留审计（独立短事务）
            try:
                commit_with_audit(
                    conn,
                    body.username or "unknown",
                    "login_failed",
                    body.username,
                    {"reason": "账号或密码错误"},
                )
            except AuditMissing as exc:
                raise HTTPException(500, str(exc)) from exc
            raise HTTPException(401, "账号或密码错误")
        token = secrets.token_hex(16)
        fingerprint = sha_token(token)
        now = _now()
        cur = conn.execute(
            """INSERT INTO login_sessions(username,token_sha,role,status,login_at)
               VALUES (?,?,?,'有效',?)""",
            (row["username"], fingerprint, row["role"], now),
        )
        session_id = int(cur.lastrowid)
        try:
            commit_with_audit(
                conn,
                row["username"],
                "login",
                "session",
                {
                    "session_id": session_id,
                    "token_sha": fingerprint,
                    "role": row["role"],
                },
            )
        except AuditMissing as exc:
            raise HTTPException(500, str(exc)) from exc
    SESSIONS[token] = row["username"]
    return {"token": token, "username": row["username"], "role": row["role"]}


@router.post("/api/logout")
def logout(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    token = authorization.removeprefix("Bearer ").strip()
    fingerprint = sha_token(token)
    with connect() as conn:
        sess = conn.execute(
            "SELECT * FROM login_sessions WHERE token_sha=?", (fingerprint,)
        ).fetchone()
        if not sess:
            raise HTTPException(404, "会话不存在")
        conn.execute(
            "UPDATE login_sessions SET status='已退出', logout_at=? WHERE id=?",
            (_now(), sess["id"]),
        )
        try:
            commit_with_audit(
                conn,
                user,
                "logout",
                "session",
                {"session_id": sess["id"], "token_sha": fingerprint},
            )
        except AuditMissing as exc:
            raise HTTPException(500, str(exc)) from exc
    SESSIONS.pop(token, None)
    return {"ok": True}


@router.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT username,role FROM users WHERE username=?", (user,)
        ).fetchone()
    return dict(row) if row else {"username": user, "role": "未知"}

"""审计互证失败测（先红后绿）。

红测 1：业务已成、审计缺失的夹具 → 互证端点必须报 gap；
        且写路径「审计写不出」时必须整笔回滚、业务零改。
红测 2：两人同时触发同一物化动作 → 后到只记争用、零业务改动，
        且争用本身必须有审计（同事务 contention_id 互证）。
另含登录/失败登录/退出会话、五处同态的收口断言，全绿前不得删改红测。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import db as dbmod
from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "audit_proof.db")
    init_db()
    yield


def _client() -> TestClient:
    return TestClient(app)


def _login(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _seed_closed_window_with_event(c: TestClient, h: dict) -> int:
    """造一个已关闭、带一条窗内事件的窗口，返回 window_id。"""
    c.post(
        "/api/windows",
        headers=h,
        json={
            "name": "W-AUDIT",
            "open_at": "2026-11-05T00:00:00",
            "close_at": "2026-11-05T23:59:59",
        },
    )
    wid = c.get("/api/windows", headers=h).json()["items"][-1]["id"]
    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-AUDIT",
            "biz_key": "ORD-AUDIT",
            "event_ts": "2026-11-05T10:00:00",
            "amount": 66.0,
        },
    )
    assert r.json()["disposition"] == "窗内"
    assert c.post(f"/api/windows/{wid}/close", headers=h).status_code == 200
    return wid


# ---------- 红测 1：业务已成、审计缺失的空洞夹具 ----------

def test_proof_reports_business_row_without_audit_hole():
    """直接 SQL 注入一笔「物化行已成、materialize_ok 审计缺失」的空洞，
    /api/audit/proof 必须把它列进 gap，aligned=False。只改审计页过滤过不了这关。"""
    c = _client()
    h = _login(c)
    wid = _seed_closed_window_with_event(c, h)
    r = c.post(f"/api/windows/{wid}/materialize", headers=h)
    assert r.status_code == 200

    with connect() as conn:
        mat = conn.execute(
            "SELECT * FROM materializations WHERE window_id=?", (wid,)
        ).fetchone()
        # 挖洞：删掉该物化对应的审计行，保留业务行
        conn.execute("DELETE FROM audit_trail WHERE action='materialize_ok'")
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM materializations"
        ).fetchone()["c"]
        conn.commit()
    assert mat is not None and remaining >= 1  # 夹具前提：业务行确实还在

    proof = c.get("/api/audit/proof", headers=h)
    assert proof.status_code == 200, proof.text
    body = proof.json()
    assert body["aligned"] is False
    mat_family = next(f for f in body["families"] if f["family"] == "materializations")
    assert mat_family["gap_count"] >= 1
    assert any(g["business_id"] == mat["id"] for g in mat_family["gaps"])


def test_missing_audit_rolls_back_business_zero_change(monkeypatch):
    """强制审计口径：审计写入若失败，整笔回滚，materializations 零新增，
    接口拒写（非 2xx），并留下 audit_deviation 标记。"""
    c = _client()
    h = _login(c)
    wid = _seed_closed_window_with_event(c, h)

    with connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) c FROM materializations"
        ).fetchone()["c"]
        before_audit = conn.execute("SELECT COUNT(*) c FROM audit_trail").fetchone()["c"]

    real_write_audit = dbmod.write_audit

    def boom(conn_, actor, action, *a, **kw):
        if action == "materialize_ok":
            raise RuntimeError("注入：审计落盘失败")
        return real_write_audit(conn_, actor, action, *a, **kw)

    monkeypatch.setattr("db.write_audit", boom)

    r = c.post(f"/api/windows/{wid}/materialize", headers=h)
    assert r.status_code >= 400, r.text

    # 断言必须在 monkeypatch.undo() 之前：undo 会把 db.DB_PATH 一并恢复到默认库，
    # 导致后续 connect() 查错文件。先在被测库内取齐全部事实再撤销补丁。
    with connect() as conn:
        after = conn.execute(
            "SELECT COUNT(*) c FROM materializations"
        ).fetchone()["c"]
        deviations = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='audit_deviation'"
        ).fetchone()["c"]
        after_audit = conn.execute("SELECT COUNT(*) c FROM audit_trail").fetchone()["c"]

    monkeypatch.undo()

    assert after == before, "审计缺失时业务必须零改"
    assert deviations >= 1, "拒写须记 audit_deviation（审计偏了）"
    # 除 deviation 标记外不得残留半截业务审计
    assert after_audit - before_audit == deviations


# ---------- 红测 2：双人同时物化，后到只记争用且争用有审计 ----------

def test_concurrent_materialize_contention_is_audited():
    """确定性夹具：先到者已持有 materialize 业务锁（处于临界区），
    后到者打同一物化接口必须 409、零业务改动，且争用与 lock_contention
    审计同事务按 contention_id 互证；锁释放后先到动作才允许成功且只成一笔。"""
    c = _client()
    h1 = _login(c, "admin", "123456")
    h2 = _login(c, "closer", "close123")
    wid = _seed_closed_window_with_event(c, h1)
    lock_key = f"materialize:{wid}"

    # 先到者持锁（模拟两人同时触发时先到者已进入临界区，尚未释放）
    assert c.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": lock_key}
    ).status_code == 200

    r = c.post(f"/api/windows/{wid}/materialize", headers=h2)
    assert r.status_code == 409, r.text

    with connect() as conn:
        mats_during = conn.execute(
            "SELECT COUNT(*) c FROM materializations WHERE window_id=?", (wid,)
        ).fetchone()["c"]
        cont = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchall()
        audits = conn.execute(
            "SELECT * FROM audit_trail WHERE action='lock_contention'"
        ).fetchall()
    assert mats_during == 0, "后到者不得写业务"
    assert len(cont) == 1, "后到者必须留一条争用登记"
    assert len(audits) == 1, "争用本身必须有审计"
    import json as _json

    payload = _json.loads(audits[0]["payload"])
    assert payload["contention_id"] == cont[0]["id"], "争用审计须与争用台账同事务互证"
    assert payload["path"] == "materialize"
    assert audits[0]["actor"] == "closer"  # 后到者记争用

    # 先到者临界区结束、释放锁后，物化动作可成功，且全程只有一笔物化
    assert c.post(
        "/api/locks/release", headers=h1, json={"lock_key": lock_key}
    ).status_code == 200
    assert c.post(f"/api/windows/{wid}/materialize", headers=h1).status_code == 200
    with connect() as conn:
        mats = conn.execute(
            "SELECT COUNT(*) c FROM materializations WHERE window_id=?", (wid,)
        ).fetchone()["c"]
        mat_audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='materialize_ok' AND target=?",
            (str(wid),),
        ).fetchone()["c"]
        still_one_cont = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchone()["c"]
    assert mats == 1, "先到成一笔、后到零笔"
    assert mat_audits == 1
    assert still_one_cont == 1


def test_two_connections_race_on_materialize_lock():
    """原语级单飞：先到连接已持有物化锁（持久行、模拟临界区未释放），
    后到连接用独立连接抢同一把锁必须被挡下并登记争用，绝不双持；
    锁释放后新连接可正常获取。"""
    c = _client()
    h = _login(c)
    wid = _seed_closed_window_with_event(c, h)
    lock_key = f"materialize:{wid}"

    from locks import acquire_or_contend, release

    # 先到者拿锁并提交（锁行持久存在 = 临界区未释放）
    holder_conn = connect()
    ok_a, info_a = acquire_or_contend(holder_conn, lock_key, "admin")
    assert ok_a is True and info_a == {}
    holder_conn.commit()
    holder_conn.close()

    # 后到者：独立连接（在线程内建，满足 sqlite 线程亲和）抢同一把锁
    result: dict = {}

    def race():
        conn_ = connect()
        try:
            result["out"] = acquire_or_contend(conn_, lock_key, "closer")
            conn_.commit()
        finally:
            conn_.close()

    t = threading.Thread(target=race)
    t.start()
    t.join(10)

    ok_b, info_b = result["out"]
    assert ok_b is False, "后到者不得拿到同一把锁"
    assert "contention_id" in info_b and info_b["holder"] == "admin"

    # 争用登记已落盘
    with connect() as conn:
        cont = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchall()
    assert len(cont) == 1 and cont[0]["id"] == info_b["contention_id"]

    # 先到者释放后，新连接应能拿到锁（可进入下一轮）
    releaser = connect()
    release(releaser, lock_key, "admin")
    releaser.commit()
    releaser.close()
    after = connect()
    ok_c, _ = acquire_or_contend(after, lock_key, "closer")
    assert ok_c is True
    after.rollback()
    after.close()


def test_contention_via_lock_endpoint_audited_in_same_tx():
    """锁接口路径同样适用：后到 409，contention_log 行与审计行同事务互证。"""
    c = _client()
    h1 = _login(c, "admin", "123456")
    h2 = _login(c, "closer", "close123")
    key = "materialize:77"
    assert c.post("/api/locks/acquire", headers=h1, json={"lock_key": key}).status_code == 200
    r = c.post("/api/locks/acquire", headers=h2, json={"lock_key": key})
    assert r.status_code == 409
    with connect() as conn:
        cont = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", (key,)
        ).fetchall()
        aud = conn.execute(
            "SELECT * FROM audit_trail WHERE action='lock_contention' AND target=?",
            (key,),
        ).fetchall()
    assert len(cont) == 1 and len(aud) == 1
    import json as _json

    assert _json.loads(aud[0]["payload"])["contention_id"] == cont[0]["id"]


# ---------- 登录会话：成功、失败、退出均有审计且同态 ----------

def test_login_failure_and_sessions_are_audited():
    c = _client()
    # 错误密码：401 + login_failed 审计，不产生会话
    bad = c.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    h = _login(c)
    with connect() as conn:
        failed = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='login_failed'"
        ).fetchone()["c"]
        sessions = conn.execute("SELECT * FROM login_sessions").fetchall()
        logins = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='login'"
        ).fetchone()["c"]
    assert failed >= 1
    assert len(sessions) == logins, "会话表与 login 审计必须同态"

    r = c.get("/api/sessions", headers=h)
    assert r.status_code == 200
    assert len(r.json()["items"]) == len(sessions)

    # 退出：会话置已退出，logout 审计与 login 按 token_sha 互证
    r = c.post("/api/logout", headers=h)
    assert r.status_code == 200
    with connect() as conn:
        active = conn.execute(
            "SELECT COUNT(*) c FROM login_sessions WHERE status='有效'"
        ).fetchone()["c"]
        logout_n = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='logout'"
        ).fetchone()["c"]
    assert active == 0 and logout_n >= 1
    # 退出后旧 token 立即失效
    assert c.get("/api/me", headers=h).status_code == 401


# ---------- 五处同态收口：正常全链路 proof 必须 aligned ----------

def test_happy_path_all_five_faces_aligned():
    c = _client()
    h = _login(c)
    wid = _seed_closed_window_with_event(c, h)
    assert c.post(f"/api/windows/{wid}/materialize", headers=h).status_code == 200

    proof = c.get("/api/audit/proof", headers=h).json()
    assert proof["aligned"] is True, proof
    for fam in proof["families"]:
        assert fam["gap_count"] == 0, fam

    # 动作汇总的 materialize_ok 计数 == 物化台账行数
    actions = {x["action"]: x["c"] for x in c.get("/api/audit/actions", headers=h).json()["items"]}
    mats = c.get("/api/materializations", headers=h).json()["items"]
    assert actions["materialize_ok"] == len(mats)

    # 审计列表里每个 materialize_ok 都带 mat_id 且能在台账找到
    audits = c.get("/api/audit?limit=500", headers=h).json()["items"]
    import json as _json

    mat_ids = {m["id"] for m in mats}
    for a in audits:
        if a["action"] == "materialize_ok":
            assert _json.loads(a["payload"])["mat_id"] in mat_ids

import json
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import jwt
from aiohttp import web
from passlib.context import CryptContext

from db import create_pool, ensure_schema_async, seed_if_empty
from rules import judge_temp

SECRET = os.environ.get("JWT_SECRET", "coldchain-probe-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "logger": {"role": "writer", "password_hash": pwd.hash("log123456")},
    "watcher": {"role": "reader", "password_hash": pwd.hash("watch123456")},
}


def _auth_header(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _decode_user(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    if sub not in USERS:
        return None
    return {"username": sub, "role": payload.get("role")}


def require_user(request: web.Request) -> dict:
    user = _decode_user(_auth_header(request))
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"detail": "未登录"}, ensure_ascii=False), content_type="application/json")
    return user


def require_writer(request: web.Request) -> dict:
    user = require_user(request)
    if user["role"] != "writer":
        raise web.HTTPForbidden(
            text=json.dumps({"detail": "仅记录员可执行此操作"}, ensure_ascii=False),
            content_type="application/json",
        )
    return user


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "coldchain-probe-desk"})


async def login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = USERS.get(username)
    if not user or not pwd.verify(password, user["password_hash"]):
        raise web.HTTPUnauthorized(
            text=json.dumps({"detail": "用户名或密码错误"}, ensure_ascii=False),
            content_type="application/json",
        )
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )
    return web.json_response(
        {"access_token": token, "username": username, "role": user["role"]}
    )


async def list_readings(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT id, probe_id, temp_c, verdict, reason, status, created_by, created_at, processed_at
        FROM probe_readings
        ORDER BY id DESC
        """
    )
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "probe_id": r["probe_id"],
                "temp_c": r["temp_c"],
                "verdict": r["verdict"],
                "reason": r["reason"],
                "status": r["status"],
                "created_by": r["created_by"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "processed_at": r["processed_at"].isoformat() if r["processed_at"] else None,
            }
        )
    return web.json_response(out)


async def create_reading(request: web.Request) -> web.Response:
    user = require_writer(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise web.HTTPBadRequest(
            text=json.dumps({"detail": "探头编号不能为空"}, ensure_ascii=False),
            content_type="application/json",
        )
    try:
        temp_c = float(body.get("temp_c"))
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(
            text=json.dumps({"detail": "温度必须是数字"}, ensure_ascii=False),
            content_type="application/json",
        ) from exc

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        # 与退役操作串行化：先确保该代号的状态行存在（顺带拿到行锁），
        # 再在同一事务内复查退役态并插入读数。
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO probe_states (probe_id, state, updated_by)
                VALUES ($1, 'active', $2)
                ON CONFLICT (probe_id) DO NOTHING
                """,
                probe_id,
                user["username"],
            )
            state_row = await conn.fetchrow(
                "SELECT state FROM probe_states WHERE probe_id = $1 FOR UPDATE",
                probe_id,
            )
            if state_row and state_row["state"] == "retired":
                raise web.HTTPConflict(
                    text=json.dumps(
                        {
                            "detail": (
                                f"探头 {probe_id} 已退役封存，禁止再提交新温度；"
                                "恢复在役后才允许继续提交。"
                            )
                        },
                        ensure_ascii=False,
                    ),
                    content_type="application/json",
                )
            row = await conn.fetchrow(
                """
                INSERT INTO probe_readings (probe_id, temp_c, status, created_by, created_at)
                VALUES ($1, $2, 'pending', $3, now())
                RETURNING id, probe_id, temp_c, verdict, reason, status, created_by, created_at, processed_at
                """,
                probe_id,
                temp_c,
                user["username"],
            )
    return web.json_response(
        {
            "id": row["id"],
            "probe_id": row["probe_id"],
            "temp_c": row["temp_c"],
            "verdict": row["verdict"],
            "reason": row["reason"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "processed_at": None,
            "message": "已入队，后台工人将认领并判定",
        },
        status=201,
    )


def _probe_state_json(r) -> dict:
    return {
        "probe_id": r["probe_id"],
        "state": r["state"],
        "updated_by": r["updated_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _event_json(r) -> dict:
    return {
        "id": r["id"],
        "probe_id": r["probe_id"],
        "action": r["action"],
        "operator": r["operator"],
        "note": r["note"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


async def list_probes(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT p.probe_id,
               COALESCE(s.state, 'active') AS state,
               s.updated_by,
               s.created_at,
               s.updated_at
        FROM (
            SELECT DISTINCT probe_id FROM probe_readings
            UNION
            SELECT probe_id FROM probe_states
        ) p
        LEFT JOIN probe_states s ON s.probe_id = p.probe_id
        ORDER BY p.probe_id
        """
    )
    return web.json_response([_probe_state_json(r) for r in rows])


async def list_probe_events(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT id, probe_id, action, operator, note, created_at
        FROM probe_state_events
        ORDER BY id DESC
        """
    )
    return web.json_response([_event_json(r) for r in rows])


async def _change_probe_state(request: web.Request, *, action: str) -> web.Response:
    user = require_writer(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip() if isinstance(body, dict) else ""
    if not probe_id:
        raise web.HTTPBadRequest(
            text=json.dumps({"detail": "探头编号不能为空"}, ensure_ascii=False),
            content_type="application/json",
        )
    note = str(body.get("note", "")).strip() if isinstance(body, dict) else ""

    if action == "retire":
        to_state, already_msg = "retired", "已退役封存，无需重复退役"
    else:
        to_state, already_msg = "active", "未处于退役状态，无需恢复"

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 与读数提交同一套串行化手段：先确保状态行存在，再对其加行锁后复查。
            await conn.execute(
                """
                INSERT INTO probe_states (probe_id, state, updated_by)
                VALUES ($1, 'active', $2)
                ON CONFLICT (probe_id) DO NOTHING
                """,
                probe_id,
                user["username"],
            )
            cur = await conn.fetchrow(
                "SELECT state FROM probe_states WHERE probe_id = $1 FOR UPDATE",
                probe_id,
            )
            if cur is not None and cur["state"] == to_state:
                raise web.HTTPConflict(
                    text=json.dumps(
                        {"detail": f"探头 {probe_id} {already_msg}"},
                        ensure_ascii=False,
                    ),
                    content_type="application/json",
                )
            row = await conn.fetchrow(
                """
                UPDATE probe_states
                SET state = $2, updated_by = $3, updated_at = now()
                WHERE probe_id = $1
                RETURNING probe_id, state, updated_by, created_at, updated_at
                """,
                probe_id,
                to_state,
                user["username"],
            )
            await conn.execute(
                """
                INSERT INTO probe_state_events (probe_id, action, operator, note)
                VALUES ($1, $2, $3, $4)
                """,
                probe_id,
                action,
                user["username"],
                note or None,
            )
    return web.json_response(_probe_state_json(row))


async def retire_probe(request: web.Request) -> web.Response:
    return await _change_probe_state(request, action="retire")


async def restore_probe(request: web.Request) -> web.Response:
    return await _change_probe_state(request, action="restore")


async def on_startup(app: web.Application) -> None:
    pool = await create_pool()
    app["pool"] = pool
    await ensure_schema_async(pool)
    await seed_if_empty(pool)


async def on_cleanup(app: web.Application) -> None:
    pool: asyncpg.Pool = app.get("pool")
    if pool:
        await pool.close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/readings", list_readings)
    app.router.add_post("/api/readings", create_reading)
    app.router.add_get("/api/probes", list_probes)
    app.router.add_get("/api/probe-events", list_probe_events)
    app.router.add_post("/api/probes/retire", retire_probe)
    app.router.add_post("/api/probes/restore", restore_probe)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8000)

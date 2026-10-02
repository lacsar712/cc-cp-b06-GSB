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

RETIRED_DETAIL = "该探头已退役封存，禁止再交新温；恢复在役后才可继续提交"


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


def require_writer(request: web.Request, action: str = "提交读数") -> dict:
    user = require_user(request)
    if user["role"] != "writer":
        raise web.HTTPForbidden(
            text=json.dumps({"detail": f"仅记录员可{action}，值班员为只读"}, ensure_ascii=False),
            content_type="application/json",
        )
    return user


def _json_error(klass, detail: str) -> web.HTTPException:
    return klass(
        text=json.dumps({"detail": detail}, ensure_ascii=False),
        content_type="application/json",
    )


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


async def list_probes(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT p.probe_id, p.status, p.created_at, p.retired_at,
               (SELECT count(*) FROM probe_readings r WHERE r.probe_id = p.probe_id) AS readings_count,
               (SELECT max(r.created_at) FROM probe_readings r WHERE r.probe_id = p.probe_id) AS last_reading_at
        FROM probes p
        ORDER BY p.probe_id
        """
    )
    return web.json_response(
        [
            {
                "probe_id": r["probe_id"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "retired_at": r["retired_at"].isoformat() if r["retired_at"] else None,
                "readings_count": r["readings_count"],
                "last_reading_at": r["last_reading_at"].isoformat() if r["last_reading_at"] else None,
            }
            for r in rows
        ]
    )


async def list_probe_events(request: web.Request) -> web.Response:
    require_user(request)
    pool: asyncpg.Pool = request.app["pool"]
    rows = await pool.fetch(
        """
        SELECT id, probe_id, action, operator, created_at
        FROM probe_events
        ORDER BY id DESC
        """
    )
    return web.json_response(
        [
            {
                "id": r["id"],
                "probe_id": r["probe_id"],
                "action": r["action"],
                "operator": r["operator"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    )


async def retire_probe(request: web.Request) -> web.Response:
    user = require_writer(request, action="退役探头")
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise _json_error(web.HTTPBadRequest, "探头编号不能为空")

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn, conn.transaction():
        status = await conn.fetchval(
            "SELECT status FROM probes WHERE probe_id = $1 FOR UPDATE",
            probe_id,
        )
        if status is None:
            raise _json_error(web.HTTPNotFound, "探头不存在：尚无该代号的读数记录，无法退役")
        if status == "retired":
            raise _json_error(web.HTTPConflict, "该探头已退役封存，请勿重复退役")
        await conn.execute(
            "UPDATE probes SET status = 'retired', retired_at = now() WHERE probe_id = $1",
            probe_id,
        )
        await conn.execute(
            "INSERT INTO probe_events (probe_id, action, operator) VALUES ($1, 'retire', $2)",
            probe_id,
            user["username"],
        )
    return web.json_response({"probe_id": probe_id, "status": "retired", "message": "已退役封存"})


async def restore_probe(request: web.Request) -> web.Response:
    user = require_writer(request, action="恢复探头")
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise _json_error(web.HTTPBadRequest, "探头编号不能为空")

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn, conn.transaction():
        status = await conn.fetchval(
            "SELECT status FROM probes WHERE probe_id = $1 FOR UPDATE",
            probe_id,
        )
        if status is None:
            raise _json_error(web.HTTPNotFound, "探头不存在，无法恢复")
        if status == "active":
            raise _json_error(web.HTTPConflict, "该探头本就在役，无需恢复")
        await conn.execute(
            "UPDATE probes SET status = 'active', retired_at = NULL WHERE probe_id = $1",
            probe_id,
        )
        await conn.execute(
            "INSERT INTO probe_events (probe_id, action, operator) VALUES ($1, 'restore', $2)",
            probe_id,
            user["username"],
        )
    return web.json_response({"probe_id": probe_id, "status": "active", "message": "已恢复在役"})


async def create_reading(request: web.Request) -> web.Response:
    user = require_writer(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="invalid json") from exc
    probe_id = str(body.get("probe_id", "")).strip()
    if not probe_id:
        raise _json_error(web.HTTPBadRequest, "探头编号不能为空")
    try:
        temp_c = float(body.get("temp_c"))
    except (TypeError, ValueError) as exc:
        raise _json_error(web.HTTPBadRequest, "温度必须是数字") from exc

    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as conn, conn.transaction():
        # DO UPDATE 的空改写会锁定探头行：与并发的退役/恢复互斥，
        # 同一代号“一边退役一边交”只会有一种结局。
        status = await conn.fetchval(
            """
            INSERT INTO probes (probe_id)
            VALUES ($1)
            ON CONFLICT (probe_id) DO UPDATE SET probe_id = probes.probe_id
            RETURNING status
            """,
            probe_id,
        )
        if status == "retired":
            raise _json_error(web.HTTPConflict, RETIRED_DETAIL)
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

import os

import asyncpg
import psycopg
from psycopg.rows import dict_row

from rules import judge_temp

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54397/coldchain"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS probe_readings (
    id serial PRIMARY KEY,
    probe_id text NOT NULL,
    temp_c double precision NOT NULL,
    verdict text,
    reason text,
    status text NOT NULL DEFAULT 'pending',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_probe_readings_status ON probe_readings (status, id);

-- 探头在役/退役状态。每个探头代号一行，行锁用于串行化退役与读数提交。
CREATE TABLE IF NOT EXISTS probe_states (
    probe_id text PRIMARY KEY,
    state text NOT NULL DEFAULT 'active',
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT probe_states_state_chk CHECK (state IN ('active', 'retired'))
);

-- 退役/恢复流水
CREATE TABLE IF NOT EXISTS probe_state_events (
    id serial PRIMARY KEY,
    probe_id text NOT NULL,
    action text NOT NULL,
    operator text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT probe_state_events_action_chk CHECK (action IN ('retire', 'restore'))
);
CREATE INDEX IF NOT EXISTS idx_probe_state_events_probe
    ON probe_state_events (probe_id, id);
"""


def connect_sync():
    return psycopg.connect(DSN, row_factory=dict_row)


def ensure_schema_sync(conn) -> None:
    conn.execute(SCHEMA_SQL)


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DSN, min_size=1, max_size=5)


async def ensure_schema_async(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)


async def seed_if_empty(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM probe_readings")
        if n and n > 0:
            return
        samples = [
            ("探头A01", 4.2),
            ("探头B02", 12.5),
        ]
        for probe_id, temp_c in samples:
            verdict, reason = judge_temp(temp_c)
            await conn.execute(
                """
                INSERT INTO probe_readings
                    (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
                VALUES ($1, $2, $3, $4, 'done', 'logger', now())
                """,
                probe_id,
                temp_c,
                verdict,
                reason,
            )


def seed_if_empty_sync(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM probe_readings").fetchone()
    if row["n"] > 0:
        return
    samples = [
        ("探头A01", 4.2),
        ("探头B02", 12.5),
    ]
    for probe_id, temp_c in samples:
        verdict, reason = judge_temp(temp_c)
        conn.execute(
            """
            INSERT INTO probe_readings
                (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
            VALUES (%s, %s, %s, %s, 'done', 'logger', now())
            """,
            (probe_id, temp_c, verdict, reason),
        )
    conn.commit()

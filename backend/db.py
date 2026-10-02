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

CREATE TABLE IF NOT EXISTS probes (
    probe_id text PRIMARY KEY,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    retired_at timestamptz
);

CREATE TABLE IF NOT EXISTS probe_events (
    id serial PRIMARY KEY,
    probe_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('retire', 'restore')),
    operator text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_probe_events_probe ON probe_events (probe_id, id);

-- 旧库升级：已有读数的探头自动登记为在役
INSERT INTO probes (probe_id)
SELECT DISTINCT probe_id FROM probe_readings
ON CONFLICT (probe_id) DO NOTHING;
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


SEED_PROBES = [
    ("探头A01", 4.2),
    ("探头B02", 12.5),
]


async def seed_if_empty(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT COUNT(*) FROM probe_readings")
        if n and n > 0:
            return
        for probe_id, temp_c in SEED_PROBES:
            verdict, reason = judge_temp(temp_c)
            await conn.execute(
                """
                INSERT INTO probes (probe_id) VALUES ($1)
                ON CONFLICT (probe_id) DO NOTHING
                """,
                probe_id,
            )
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
    for probe_id, temp_c in SEED_PROBES:
        verdict, reason = judge_temp(temp_c)
        conn.execute(
            """
            INSERT INTO probes (probe_id) VALUES (%s)
            ON CONFLICT (probe_id) DO NOTHING
            """,
            (probe_id,),
        )
        conn.execute(
            """
            INSERT INTO probe_readings
                (probe_id, temp_c, verdict, reason, status, created_by, processed_at)
            VALUES (%s, %s, %s, %s, 'done', 'logger', now())
            """,
            (probe_id, temp_c, verdict, reason),
        )
    conn.commit()

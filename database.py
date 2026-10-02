"""Neon PostgreSQL layer (asyncpg) with an in-memory cache for hot-path reads."""
import asyncio
import logging
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    destination_channel TEXT,
    affiliate_tag TEXT,
    header_text TEXT,
    footer_text TEXT,
    admin_ids BIGINT[] NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS active_sources (
    channel_id BIGINT PRIMARY KEY,
    title TEXT,
    added_by BIGINT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

EDITABLE = {"destination_channel", "affiliate_tag", "header_text", "footer_text"}


def _clean_dsn(dsn: str) -> str:
    """Neon DSNs carry `channel_binding=require`, which asyncpg rejects. Strip it, force SSL."""
    parts = urlsplit(dsn)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    if not any(k == "sslmode" for k, _ in q):
        q.append(("sslmode", "require"))
    return urlunsplit(parts._replace(query=urlencode(q)))


class Database:
    def __init__(self, dsn: str):
        self.dsn = _clean_dsn(dsn)
        self.pool: Optional[asyncpg.Pool] = None
        self.settings: dict = {
            "destination_channel": None,
            "affiliate_tag": None,
            "header_text": None,
            "footer_text": None,
            "admin_ids": [],
        }
        self.sources: dict[int, str] = {}

    @property
    def admins(self) -> set:
        return set(self.settings.get("admin_ids") or [])

    async def connect(self):
        # Neon computes auto-suspend; the first connect can be slow -> retry.
        for attempt in range(1, 6):
            try:
                self.pool = await asyncpg.create_pool(
                    self.dsn,
                    min_size=1,
                    max_size=5,
                    timeout=30,
                    command_timeout=30,
                    statement_cache_size=0,  # required for Neon's pgbouncer (pooled) endpoint
                    max_inactive_connection_lifetime=120,
                )
                return
            except Exception as e:
                log.warning("DB connect attempt %s failed: %s", attempt, e)
                await asyncio.sleep(3 * attempt)
        raise RuntimeError("Could not connect to the database")

    async def init_schema(self):
        await self.pool.execute(SCHEMA)

    async def seed(self, admin_ids: Iterable[int], destination: Optional[str], tag: Optional[str]):
        await self.pool.execute("INSERT INTO settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        await self.pool.execute(
            """
            UPDATE settings SET
                admin_ids = ARRAY(SELECT DISTINCT x FROM unnest(admin_ids || $1::bigint[]) AS x),
                destination_channel = COALESCE(destination_channel, $2),
                affiliate_tag = COALESCE(affiliate_tag, $3)
            WHERE id = 1
            """,
            list(admin_ids),
            destination or None,
            tag or None,
        )

    async def load(self):
        row = await self.pool.fetchrow("SELECT * FROM settings WHERE id = 1")
        self.settings = dict(row)
        rows = await self.pool.fetch("SELECT channel_id, title FROM active_sources")
        self.sources = {r["channel_id"]: r["title"] for r in rows}

    # ---- sources -------------------------------------------------------
    def has_source(self, ids: Iterable[int]) -> Optional[int]:
        return next((i for i in ids if i in self.sources), None)

    async def add_source(self, channel_id: int, title: str, added_by: int) -> bool:
        row = await self.pool.fetchrow(
            """INSERT INTO active_sources (channel_id, title, added_by)
               VALUES ($1, $2, $3) ON CONFLICT (channel_id) DO NOTHING
               RETURNING channel_id""",
            channel_id, title, added_by,
        )
        if row:
            self.sources[channel_id] = title
        return row is not None

    async def remove_source(self, ids: Iterable[int]) -> list:
        rows = await self.pool.fetch(
            "DELETE FROM active_sources WHERE channel_id = ANY($1::bigint[]) RETURNING channel_id, title",
            list(ids),
        )
        for r in rows:
            self.sources.pop(r["channel_id"], None)
        return [(r["channel_id"], r["title"]) for r in rows]

    # ---- settings ------------------------------------------------------
    async def set_field(self, field: str, value: Optional[str]):
        if field not in EDITABLE:
            raise ValueError(f"Field not editable: {field}")
        await self.pool.execute(f"UPDATE settings SET {field} = $1 WHERE id = 1", value)
        self.settings[field] = value

    async def close(self):
        if self.pool:
            await self.pool.close()

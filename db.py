"""Subscription persistence layer using SQLite."""

import json
import os

import aiosqlite

DB_PATH = os.environ.get("SWEEP_DB_PATH", "subscriptions.db")

MAX_SUBSCRIPTIONS_PER_USER = 5


async def init_db() -> None:
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                label TEXT NOT NULL,
                sweep_days TEXT NOT NULL,
                sweep_schedule TEXT NOT NULL,
                sweep_time TEXT,
                street_name TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(chat_id, x, y)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_subs_chat_id ON subscriptions(chat_id)"
        )
        await db.commit()


async def add_subscription(
    chat_id: int,
    x: float,
    y: float,
    label: str,
    sweep_days: list[str],
    sweep_schedule: str,
    sweep_time: str | None,
    street_name: str | None,
) -> str | None:
    """Insert or replace a subscription. Returns error string if at cap, else None."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Check cap (only for genuinely new subscriptions)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE chat_id = ? AND NOT (x = ? AND y = ?)",
            (chat_id, round(x, 4), round(y, 4)),
        )
        (count,) = await cursor.fetchone()  # type: ignore[misc]
        if count >= MAX_SUBSCRIPTIONS_PER_USER:
            return f"You already have {MAX_SUBSCRIPTIONS_PER_USER} subscriptions. Use /unsubscribe to remove one first."

        await db.execute(
            """
            INSERT INTO subscriptions (chat_id, x, y, label, sweep_days, sweep_schedule, sweep_time, street_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, x, y) DO UPDATE SET
                label=excluded.label,
                sweep_days=excluded.sweep_days,
                sweep_schedule=excluded.sweep_schedule,
                sweep_time=excluded.sweep_time,
                street_name=excluded.street_name,
                created_at=excluded.created_at
            """,
            (
                chat_id,
                round(x, 4),
                round(y, 4),
                label,
                json.dumps(sweep_days),
                sweep_schedule,
                sweep_time,
                street_name,
            ),
        )
        await db.commit()
        return None


async def remove_subscription(chat_id: int, sub_id: int) -> int:
    """Remove a single subscription by id. Returns rows deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND id = ?",
            (chat_id, sub_id),
        )
        await db.commit()
        return cursor.rowcount


async def remove_all_subscriptions(chat_id: int) -> int:
    """Remove all subscriptions for a user. Returns rows deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,)
        )
        await db.commit()
        return cursor.rowcount


async def get_user_subscriptions(chat_id: int) -> list[dict]:
    """Get all subscriptions for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id", (chat_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_subscriptions() -> list[dict]:
    """Get all subscriptions (for notification job)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM subscriptions")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

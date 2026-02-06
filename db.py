
# part2_db.py
# DB layer: connection, initialization, and convenience helpers
import os
import aiosqlite
from typing import Optional, Any, Sequence, Tuple, List
from datetime import datetime

DB_FILE = os.getenv('DB_FILE', 'bot.db')
DB: Optional[aiosqlite.Connection] = None

async def init_db_and_dirs():
    global DB
    os.makedirs('assets/images', exist_ok=True)
    os.makedirs('assets/videos', exist_ok=True)
    os.makedirs('backups', exist_ok=True)
    DB = await aiosqlite.connect(DB_FILE)
    await DB.execute("PRAGMA journal_mode=WAL;")
    # simple helper function to return lastrowid
    await _create_schema()

async def _create_schema():
    global DB
    assert DB is not None
    await DB.executescript("""
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        movie TEXT,
        rarity TEXT,
        rarity_key TEXT,
        file_type TEXT,
        file_id TEXT,
        file_path TEXT,
        owner_id INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT,
        coins INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS daily (user_id INTEGER PRIMARY KEY, last_claim TEXT);
    CREATE TABLE IF NOT EXISTS banned (id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS muted (id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS sudo (id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS groups_seen (chat_id INTEGER PRIMARY KEY, messages_count INTEGER DEFAULT 0, last_drop_card_id INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    await DB.commit()

async def close_db():
    global DB
    if DB:
        await DB.close()
        DB = None

# convenience helpers
async def fetchone(query: str, params: Sequence[Any] = ()) -> Optional[Tuple[Any, ...]]:
    global DB
    assert DB is not None
    async with DB.execute(query, params) as cur:
        return await cur.fetchone()

async def fetchall(query: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
    global DB
    assert DB is not None
    async with DB.execute(query, params) as cur:
        return await cur.fetchall()

async def execute(query: str, params: Sequence[Any] = ()) -> int:
    """Execute and return lastrowid if available, else -1"""
    global DB
    assert DB is not None
    cur = await DB.execute(query, params)
    await DB.commit()
    try:
        return cur.lastrowid if cur.lastrowid is not None else -1
    except Exception:
        return -1

async def execute_many(query: str, params_list: Sequence[Sequence[Any]]):
    global DB
    assert DB is not None
    await DB.executemany(query, params_list)
    await DB.commit()

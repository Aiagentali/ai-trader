"""SQLite schema — designed so PostgreSQL can replace SQLite later (repository pattern)."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_symbol_ts ON market_snapshots(symbol, ts);

CREATE TABLE IF NOT EXISTS market_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_thesis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    bias TEXT,
    regime TEXT,
    thesis TEXT NOT NULL,
    invalidation TEXT,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    title TEXT NOT NULL,
    url TEXT UNIQUE,
    published_ts TEXT,
    currencies TEXT,
    importance TEXT,
    category TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS economic_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    currency TEXT,
    scheduled_time TEXT,
    importance TEXT,
    previous TEXT,
    forecast TEXT,
    actual TEXT
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    decision TEXT NOT NULL,
    approved INTEGER,
    reject_reasons TEXT,
    payload TEXT NOT NULL,
    agent_version TEXT,
    prompt_version INTEGER,
    risk_version INTEGER
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    opened_ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit TEXT,
    position_size REAL NOT NULL,
    risk_percent REAL,
    rr REAL,
    market_regime TEXT,
    setup TEXT,
    ai_confidence INTEGER,
    thesis TEXT,
    invalidation TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exit_ts TEXT,
    exit_price REAL,
    profit_loss REAL,
    r_multiple REAL,
    exit_reason TEXT,
    request_id TEXT
);

CREATE TABLE IF NOT EXISTS trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    ts TEXT NOT NULL,
    classification TEXT NOT NULL,
    review TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    ticket TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    volume REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    opened_ts TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS ai_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    task TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    estimated_cost REAL,
    symbol TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS token_usage (
    day TEXT PRIMARY KEY,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    request_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT
);
"""


def get_connection(db_path: str = None) -> sqlite3.Connection:
    import os
    if db_path is None:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        db_path = os.path.join(base, "data", "ai_trader.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

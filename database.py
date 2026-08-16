import sqlite3
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from config import DATABASE_PATH

def get_iso_now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Rules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rules (
                rule_id TEXT PRIMARY KEY,
                keyword TEXT UNIQUE NOT NULL,
                dm_message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        # Processed webhook events table (deduplication)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            )
        """)
        
        # Deleted comments table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deleted_comments (
                comment_id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            )
        """)
        
        # User-Rule dispatches (ensures 1 DM per user per rule)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_rule_dispatches (
                user_id TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, rule_id)
            )
        """)
        
        # DM dispatch queue
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dm_queue (
                id TEXT PRIMARY KEY,
                rule_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT,
                comment_id TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                dm_id TEXT,
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                next_retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # System metrics table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_metrics (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        
        # Initialize default metrics if not present
        cursor.execute("""
            INSERT OR IGNORE INTO system_metrics (key, value) VALUES ('duplicates_blocked', 0)
        """)
        
        # Indexes for fast queue and status querying
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dm_queue_status ON dm_queue(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dm_queue_dm_id ON dm_queue(dm_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dm_queue_comment_id ON dm_queue(comment_id)")
        
        conn.commit()

# --- Metrics Helpers ---
def increment_metric(key: str, amount: int = 1):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO system_metrics (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + ?
        """, (key, amount, amount))
        conn.commit()

def get_metric(key: str) -> int:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM system_metrics WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else 0

# --- Rules Helpers ---
def add_rule(keyword: str, dm_message: str) -> dict:
    rule_id = f"rule_{uuid.uuid4().hex[:12]}"
    created_at = get_iso_now()
    clean_keyword = keyword.strip()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO rules (rule_id, keyword, dm_message, created_at) VALUES (?, ?, ?, ?)",
            (rule_id, clean_keyword, dm_message, created_at)
        )
        conn.commit()
    return {
        "rule_id": rule_id,
        "keyword": clean_keyword,
        "dm_message": dm_message,
        "created_at": created_at
    }

def get_rules() -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT rule_id, keyword, dm_message, created_at FROM rules ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

def delete_rule(rule_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM rules WHERE rule_id = ?", (rule_id,))
        conn.commit()
        return cursor.rowcount > 0

def find_matching_rules(text: str) -> list:
    if not text:
        return []
    text_lower = text.lower()
    rules = get_rules()
    matching = []
    for r in rules:
        if r["keyword"].lower() in text_lower:
            matching.append(r)
    return matching

# --- Deduplication & Event Helpers ---
def process_event_id(event_id: str) -> bool:
    """Returns True if event was newly inserted, False if already processed."""
    now = get_iso_now()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO processed_events (event_id, received_at) VALUES (?, ?)", (event_id, now))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def record_deleted_comment(comment_id: str):
    now = get_iso_now()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO deleted_comments (comment_id, deleted_at) VALUES (?, ?)", (comment_id, now))
        except sqlite3.IntegrityError:
            pass
        
        # If there are queued DMs for this comment that haven't been sent yet, cancel them
        cursor = conn.execute(
            "SELECT id FROM dm_queue WHERE comment_id = ? AND status = 'queued'",
            (comment_id,)
        )
        cancelled_rows = cursor.fetchall()
        if cancelled_rows:
            conn.execute(
                "UPDATE dm_queue SET status = 'duplicates_blocked', updated_at = ?, last_error = 'Comment deleted before dispatch' WHERE comment_id = ? AND status = 'queued'",
                (now, comment_id)
            )
            # Increment duplicates_blocked metric for cancelled items
            conn.execute(
                "INSERT INTO system_metrics (key, value) VALUES ('duplicates_blocked', ?) ON CONFLICT(key) DO UPDATE SET value = value + ?",
                (len(cancelled_rows), len(cancelled_rows))
            )
        conn.commit()

def is_comment_deleted(comment_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM deleted_comments WHERE comment_id = ?", (comment_id,)).fetchone()
        return row is not None

def try_lock_user_rule(user_id: str, rule_id: str) -> bool:
    """
    Attempts to register a user-rule dispatch lock.
    Returns True if successful (user never received DM for this rule before).
    Returns False if lock already exists (duplicate attempt).
    """
    now = get_iso_now()
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO user_rule_dispatches (user_id, rule_id, created_at) VALUES (?, ?, ?)",
                (user_id, rule_id, now)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

# --- Queue & Status Helpers ---
def enqueue_dm(rule_id: str, user_id: str, username: str, comment_id: str, message: str) -> str:
    queue_id = f"dm_{uuid.uuid4().hex[:12]}"
    now = get_iso_now()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO dm_queue (id, rule_id, user_id, username, comment_id, message, status, attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
        """, (queue_id, rule_id, user_id, username, comment_id, message, now, now))
        conn.commit()
    return queue_id

def get_pending_dms(limit: int = 10) -> list:
    now = get_iso_now()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, rule_id, user_id, username, comment_id, message, status, dm_id, attempts, last_error, next_retry_at
            FROM dm_queue
            WHERE status = 'queued' AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY created_at ASC
            LIMIT ?
        """, (now, limit)).fetchall()
        return [dict(r) for r in rows]

def get_api_accepted_dms(limit: int = 20) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, dm_id, user_id, comment_id, attempts
            FROM dm_queue
            WHERE status = 'api_accepted' AND dm_id IS NOT NULL
            ORDER BY updated_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

def update_dm_status(queue_id: str, status: str, dm_id: str = None, attempts: int = None, last_error: str = None, next_retry_at: str = None):
    now = get_iso_now()
    query = "UPDATE dm_queue SET status = ?, updated_at = ?"
    params = [status, now]
    
    if dm_id is not None:
        query += ", dm_id = ?"
        params.append(dm_id)
    if attempts is not None:
        query += ", attempts = ?"
        params.append(attempts)
    if last_error is not None:
        query += ", last_error = ?"
        params.append(last_error)
    if next_retry_at is not None:
        query += ", next_retry_at = ?"
        params.append(next_retry_at)
        
    query += " WHERE id = ?"
    params.append(queue_id)
    
    with get_db() as conn:
        conn.execute(query, params)
        conn.commit()

def get_stats() -> dict:
    with get_db() as conn:
        # sent = status 'delivered'
        sent = conn.execute("SELECT COUNT(*) FROM dm_queue WHERE status = 'delivered'").fetchone()[0]
        # failed = status 'failed'
        failed = conn.execute("SELECT COUNT(*) FROM dm_queue WHERE status = 'failed'").fetchone()[0]
        # queued = status in ('queued', 'sending', 'api_accepted')
        queued = conn.execute("SELECT COUNT(*) FROM dm_queue WHERE status IN ('queued', 'sending', 'api_accepted')").fetchone()[0]
        # duplicates_blocked = metric
        duplicates_blocked = get_metric("duplicates_blocked")
        
        return {
            "sent": sent,
            "failed": failed,
            "queued": queued,
            "duplicates_blocked": duplicates_blocked
        }

def get_recent_logs(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, rule_id, user_id, username, comment_id, message, status, dm_id, attempts, last_error, created_at, updated_at
            FROM dm_queue
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

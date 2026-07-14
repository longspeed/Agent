import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_FILE = Path(__file__).parent / "reviews.db"


def _connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                row_index INTEGER NOT NULL,
                name TEXT,
                email TEXT,
                thread_id TEXT,
                customer_reply TEXT,
                draft_reply TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)


def add_review(row_index, name, email, thread_id, customer_reply, draft_reply):
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO reviews (row_index, name, email, thread_id, customer_reply, draft_reply, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (row_index, name, email, thread_id, customer_reply, draft_reply,
             datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def list_pending_reviews():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_review(review_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return dict(row) if row else None


def mark_sent(review_id, sent_body):
    with _connect() as conn:
        conn.execute(
            "UPDATE reviews SET status = 'sent', draft_reply = ? WHERE id = ?",
            (sent_body, review_id),
        )


init_db()

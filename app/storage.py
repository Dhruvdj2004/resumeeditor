"""SQLite store: one session per uploaded resume, one row per version (tex + pdf)."""
import sqlite3
import uuid
from contextlib import contextmanager

from .config import DB_PATH


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                filename TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS versions (
                session_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                tex TEXT NOT NULL,
                pdf BLOB NOT NULL,
                summary TEXT,
                request TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, version)
            );
            """
        )


def create_session(filename: str) -> str:
    sid = uuid.uuid4().hex
    with _db() as c:
        c.execute("INSERT INTO sessions (id, filename) VALUES (?, ?)", (sid, filename))
    return sid


def session_exists(sid: str) -> bool:
    with _db() as c:
        return c.execute("SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone() is not None


def add_version(sid: str, tex: str, pdf: bytes, summary: str, request: str | None = None) -> int:
    with _db() as c:
        row = c.execute("SELECT COALESCE(MAX(version), 0) FROM versions WHERE session_id = ?", (sid,)).fetchone()
        v = row[0] + 1
        c.execute(
            "INSERT INTO versions (session_id, version, tex, pdf, summary, request) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, v, tex, pdf, summary, request),
        )
    return v


def get_version(sid: str, version: int | None = None) -> sqlite3.Row | None:
    with _db() as c:
        if version is None:
            return c.execute(
                "SELECT * FROM versions WHERE session_id = ? ORDER BY version DESC LIMIT 1", (sid,)
            ).fetchone()
        return c.execute(
            "SELECT * FROM versions WHERE session_id = ? AND version = ?", (sid, version)
        ).fetchone()


def list_versions(sid: str) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT version, summary, request, created_at FROM versions WHERE session_id = ? ORDER BY version",
            (sid,),
        ).fetchall()
    return [dict(r) for r in rows]

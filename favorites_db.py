import sqlite3
from pathlib import Path

DB_PATH = Path("weather_bot.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorite_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                komoot_url TEXT,
                gpx_blob BLOB NOT NULL,
                gpx_filename TEXT,
                timezone TEXT,
                times_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, name),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_favorite_routes_user_id ON favorite_routes(user_id)"
        )


def _ensure_user(conn: sqlite3.Connection, telegram_user_id: int) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_user_id) VALUES (?)",
        (telegram_user_id,),
    )
    row = conn.execute(
        "SELECT id FROM users WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    return int(row["id"])


def count_favorites(telegram_user_id: int) -> int:
    with get_connection() as conn:
        user_id = _ensure_user(conn, telegram_user_id)
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM favorite_routes WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["cnt"])


def create_favorite(
    telegram_user_id: int,
    name: str,
    source_type: str,
    komoot_url: str | None,
    gpx_blob: bytes,
    gpx_filename: str | None,
    timezone: str | None,
) -> None:
    with get_connection() as conn:
        user_id = _ensure_user(conn, telegram_user_id)
        conn.execute(
            """
            INSERT INTO favorite_routes (
                user_id, name, source_type, komoot_url, gpx_blob, gpx_filename, timezone
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, source_type, komoot_url, gpx_blob, gpx_filename, timezone),
        )


def list_favorites(telegram_user_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        user_id = _ensure_user(conn, telegram_user_id)
        rows = conn.execute(
            """
            SELECT id, name, source_type, times_used, created_at
            FROM favorite_routes
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
        return list(rows)


def get_favorite_by_id(telegram_user_id: int, favorite_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        user_id = _ensure_user(conn, telegram_user_id)
        row = conn.execute(
            """
            SELECT id, name, source_type, komoot_url, gpx_blob, gpx_filename, timezone, times_used
            FROM favorite_routes
            WHERE user_id = ? AND id = ?
            """,
            (user_id, favorite_id),
        ).fetchone()
        return row


def mark_favorite_used(favorite_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE favorite_routes
            SET times_used = times_used + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (favorite_id,),
        )


def delete_favorite(telegram_user_id: int, favorite_id: int) -> bool:
    with get_connection() as conn:
        user_id = _ensure_user(conn, telegram_user_id)
        cur = conn.execute(
            "DELETE FROM favorite_routes WHERE user_id = ? AND id = ?",
            (user_id, favorite_id),
        )
        return cur.rowcount > 0


def rename_favorite(telegram_user_id: int, favorite_id: int, new_name: str) -> None:
    with get_connection() as conn:
        user_id = _ensure_user(conn, telegram_user_id)
        conn.execute(
            """
            UPDATE favorite_routes
            SET name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND id = ?
            """,
            (new_name, user_id, favorite_id),
        )

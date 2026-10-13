"""
Database Module for IEEE Citation Automation System
===================================================
Provides persistent SQLite storage for:
  - User accounts (credentials, hashed passwords)
  - Personal Zotero API credentials (isolated per user)
  - User job history and processed documents
"""

import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_DIR = BASE_DIR / "data"
DB_PATH = DB_DIR / "app.db"


def get_db_connection() -> sqlite3.Connection:
    """Create and return a SQLite database connection with row factory."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database tables if they do not exist."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    email TEXT,
                    password_hash TEXT NOT NULL,
                    zotero_api_key TEXT,
                    zotero_user_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    job_id TEXT NOT NULL UNIQUE,
                    paper_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON user_jobs(user_id);
            """)
    finally:
        conn.close()
    logger.info("Database initialized successfully at %s", DB_PATH)


def mask_key(key: Optional[str]) -> str:
    """Return a masked representation of an API key for safe display (e.g. ••••••••a1b2)."""
    if not key:
        return ""
    if len(key) <= 6:
        return "••••"
    return "••••••••" + key[-4:]


def user_to_dict(row: sqlite3.Row, include_secret: bool = False) -> Dict[str, Any]:
    """Convert user database row to dictionary with masked secrets by default."""
    raw_key = row["zotero_api_key"] or ""
    data = {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"] or "",
        "has_zotero_key": bool(raw_key),
        "zotero_api_key_masked": mask_key(raw_key),
        "zotero_user_id": row["zotero_user_id"] or "",
        "created_at": str(row["created_at"]),
    }
    if include_secret:
        data["zotero_api_key"] = raw_key
    return data


def create_user(
    username: str,
    password: str,
    email: Optional[str] = None,
    zotero_api_key: Optional[str] = None,
    zotero_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Register a new user account.
    Returns the created user dict or raises ValueError if validation fails.
    """
    clean_username = username.strip()
    if not clean_username or len(clean_username) < 3:
        raise ValueError("Username must be at least 3 characters long.")
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters long.")

    clean_email = email.strip() if email else None
    clean_key = zotero_api_key.strip() if zotero_api_key else None
    clean_uid = zotero_user_id.strip() if zotero_user_id else None
    pw_hash = generate_password_hash(password)

    conn = get_db_connection()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO users (username, email, password_hash, zotero_api_key, zotero_user_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (clean_username, clean_email, pw_hash, clean_key, clean_uid),
            )
            user_id = cursor.lastrowid
            cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return user_to_dict(row)
    except sqlite3.IntegrityError:
        raise ValueError(f"Username '{clean_username}' is already taken. Please choose another.")
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    Verify user credentials against stored hash.
    Returns user dict without sensitive password if credentials match, else None.
    """
    clean_username = username.strip()
    if not clean_username or not password:
        return None

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (clean_username,))
        row = cursor.fetchone()
        if not row:
            return None

        if check_password_hash(row["password_hash"], password):
            return user_to_dict(row)
        return None
    finally:
        conn.close()


def get_user_by_id(user_id: int, include_secret: bool = False) -> Optional[Dict[str, Any]]:
    """Fetch user record by ID."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return user_to_dict(row, include_secret=include_secret)
        return None
    finally:
        conn.close()


def update_user_zotero(
    user_id: int,
    zotero_api_key: Optional[str],
    zotero_user_id: Optional[str] = None,
) -> bool:
    """Update private Zotero credentials for a specific user."""
    clean_key = zotero_api_key.strip() if zotero_api_key else None
    clean_uid = zotero_user_id.strip() if zotero_user_id else None

    conn = get_db_connection()
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE users
                SET zotero_api_key = ?, zotero_user_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (clean_key, clean_uid, user_id),
            )
            return cursor.rowcount > 0
    finally:
        conn.close()


def record_user_job(user_id: Optional[int], job_id: str, paper_name: str, status: str = "running") -> None:
    """Record a submitted paper job in user history."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_jobs (user_id, job_id, paper_name, status)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, job_id, paper_name, status),
            )
    except Exception as e:
        logger.warning(f"Could not record user job: {e}")
    finally:
        conn.close()


def update_job_status(job_id: str, status: str) -> None:
    """Update status of a job in user_jobs."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE user_jobs SET status = ? WHERE job_id = ?",
                (status, job_id),
            )
    except Exception as e:
        logger.warning(f"Could not update job status: {e}")
    finally:
        conn.close()


def get_user_jobs(user_id: int, limit: int = 15) -> List[Dict[str, Any]]:
    """Fetch recent jobs submitted by this user."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT job_id, paper_name, status, created_at
            FROM user_jobs
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cursor.fetchall()
        return [
            {
                "job_id": r["job_id"],
                "paper_name": r["paper_name"],
                "status": r["status"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]
    finally:
        conn.close()

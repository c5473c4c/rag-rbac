"""
SQLite-backed user store with role management.
"""
import sqlite3
import os
from contextlib import contextmanager
from passlib.context import CryptContext

DB_PATH = os.getenv("DB_PATH", "/app/data/users.db")
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables and default admin user."""
    with db_session() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL,
                hashed_pw   TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'user',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                filename    TEXT NOT NULL,
                chunk_count INTEGER DEFAULT 0,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        # Seed default admin
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", ("admin",)
        ).fetchone()
        if not existing:
            default_pw = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")
            conn.execute(
                "INSERT INTO users (username, hashed_pw, role) VALUES (?, ?, ?)",
                ("admin", pwd_ctx.hash(default_pw), "admin"),
            )


# ── User CRUD ──

def create_user(username: str, password: str, role: str = "user") -> dict | None:
    with db_session() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_pw, role) VALUES (?, ?, ?)",
                (username, pwd_ctx.hash(password), role),
            )
            row = conn.execute(
                "SELECT id, username, role, created_at FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            return dict(row)
        except sqlite3.IntegrityError:
            return None


def authenticate(username: str, password: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row and pwd_ctx.verify(password, row["hashed_pw"]):
            return {"id": row["id"], "username": row["username"], "role": row["role"]}
    return None


def list_users() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    with db_session() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ? AND role != 'admin'", (user_id,))
        return cur.rowcount > 0


def update_user_role(user_id: int, new_role: str) -> bool:
    with db_session() as conn:
        cur = conn.execute(
            "UPDATE users SET role = ? WHERE id = ?", (new_role, user_id)
        )
        return cur.rowcount > 0


# ── Document tracking ──

def record_document(user_id: int, filename: str, chunk_count: int) -> int:
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO documents (user_id, filename, chunk_count) VALUES (?, ?, ?)",
            (user_id, filename, chunk_count),
        )
        return cur.lastrowid


def list_documents(user_id: int | None = None) -> list[dict]:
    with db_session() as conn:
        if user_id:
            rows = conn.execute(
                """SELECT d.*, u.username FROM documents d
                   JOIN users u ON d.user_id = u.id
                   WHERE d.user_id = ? ORDER BY d.uploaded_at DESC""",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT d.*, u.username FROM documents d
                   JOIN users u ON d.user_id = u.id
                   ORDER BY d.uploaded_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]


def delete_document(doc_id: int, user_id: int | None = None) -> str | None:
    """Delete doc record. If user_id given, enforce ownership. Returns filename."""
    with db_session() as conn:
        if user_id:
            row = conn.execute(
                "SELECT filename FROM documents WHERE id = ? AND user_id = ?",
                (doc_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT filename FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        if row:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            return row["filename"]
    return None

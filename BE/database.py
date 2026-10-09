import sqlite3
import hashlib
import secrets
import time
from datetime import datetime, timedelta
import os
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcare.db")


def _conn():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = _conn()
    cur = conn.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute('''CREATE TABLE IF NOT EXISTS fall_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME,
        track_id INTEGER,
        status TEXT
    )''')
    # Migrasi: kolom detail untuk keterangan log
    cols = [r[1] for r in cur.execute("PRAGMA table_info(fall_logs)")]
    if "detail" not in cols:
        cur.execute("ALTER TABLE fall_logs ADD COLUMN detail TEXT DEFAULT ''")

    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS local_devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        ip TEXT NOT NULL UNIQUE,
        last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    # Sesi login (token disimpan dalam bentuk hash)
    cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        expires_at REAL NOT NULL
    )''')

    defaults = {
        "notifMode": "keduanya",
        "cameraStatus": "off",
        "displayFilter": "normal",
        "alarm_duration": "30",
        "ringtone_type": "alarm",
        "fall_threshold": "7",        # detik (1-60)
        "resolution_mode": "high",    # lossless | high | medium | low
        "model_choice": "v26",        # v8 | v26
        "stream_mode": "medium",      # low | medium | lossless
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

    conn = _conn()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    if count == 0:
        create_user("admin", "admin")


# ==================== SETTINGS ====================

def get_setting(key, default_value=None):
    conn = _conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default_value


def update_setting(key, value):
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_all_settings():
    conn = _conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


# ==================== LOG ====================

def log_fall_event(track_id, status, detail=""):
    conn = _conn()
    conn.execute(
        "INSERT INTO fall_logs (timestamp, track_id, status, detail) VALUES (?, ?, ?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), track_id, status, detail),
    )
    conn.commit()
    conn.close()


def get_logs(limit=200):
    conn = _conn()
    rows = conn.execute(
        "SELECT id, timestamp, track_id, status, detail FROM fall_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [{"id": r[0], "timestamp": r[1], "track_id": r[2], "status": r[3], "detail": r[4] or ""} for r in rows]


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def count_logs(older_than_days=None) -> int:
    """Jumlah log. older_than_days=None -> semua; angka -> hanya yang lebih lama dari N hari."""
    conn = _conn()
    if older_than_days is None:
        n = conn.execute("SELECT COUNT(*) FROM fall_logs").fetchone()[0]
    else:
        n = conn.execute(
            "SELECT COUNT(*) FROM fall_logs WHERE timestamp < ?", (_cutoff(older_than_days),)
        ).fetchone()[0]
    conn.close()
    return n


def clear_logs(older_than_days=None) -> int:
    """Hapus log (semua, atau yang lebih lama dari N hari). Mengembalikan jumlah yang dihapus."""
    conn = _conn()
    if older_than_days is None:
        cur = conn.execute("DELETE FROM fall_logs")
    else:
        cur = conn.execute(
            "DELETE FROM fall_logs WHERE timestamp < ?", (_cutoff(older_than_days),)
        )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


# ==================== LOCAL DEVICES ====================

def register_local_device(name: str, ip: str):
    conn = _conn()
    conn.execute('''
        INSERT INTO local_devices (name, ip, last_seen) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(ip) DO UPDATE SET name = excluded.name, last_seen = CURRENT_TIMESTAMP
    ''', (name, ip))
    conn.commit()
    conn.close()


def get_all_local_devices():
    conn = _conn()
    rows = conn.execute("SELECT id, name, ip, last_seen FROM local_devices ORDER BY id ASC").fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "ip": r[2], "last_seen": r[3]} for r in rows]


def get_local_device_by_id(device_id: int):
    conn = _conn()
    r = conn.execute("SELECT id, name, ip, last_seen FROM local_devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    return {"id": r[0], "name": r[1], "ip": r[2], "last_seen": r[3]} if r else None


def delete_local_device(device_id: int) -> bool:
    conn = _conn()
    cur = conn.execute("DELETE FROM local_devices WHERE id=?", (device_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ==================== USERS / AUTH ====================

def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def create_user(username: str, password: str):
    salt = secrets.token_hex(16)
    conn = _conn()
    conn.execute(
        "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
        (username, _hash_password(password, salt), salt),
    )
    conn.commit()
    conn.close()


def verify_user(username: str, password: str) -> bool:
    conn = _conn()
    row = conn.execute("SELECT password_hash, salt FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row:
        return False
    return secrets.compare_digest(_hash_password(password, row[1]), row[0])


def change_password(username: str, old_password: str, new_password: str) -> bool:
    if not verify_user(username, old_password):
        return False
    salt = secrets.token_hex(16)
    conn = _conn()
    conn.execute(
        "UPDATE users SET password_hash=?, salt=? WHERE username=?",
        (_hash_password(new_password, salt), salt, username),
    )
    conn.commit()
    conn.close()
    return True


# ==================== SESI LOGIN ====================

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(username: str, ttl_sec: int) -> str:
    token = secrets.token_urlsafe(32)
    conn = _conn()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute(
        "INSERT INTO sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), username, time.time() + ttl_sec),
    )
    conn.commit()
    conn.close()
    return token


def get_session_user(token: str):
    if not token:
        return None
    conn = _conn()
    row = conn.execute(
        "SELECT username, expires_at FROM sessions WHERE token_hash=?", (_hash_token(token),)
    ).fetchone()
    conn.close()
    if not row or row[1] < time.time():
        return None
    return row[0]


def delete_session(token: str):
    if not token:
        return
    conn = _conn()
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
    conn.commit()
    conn.close()


init_db()
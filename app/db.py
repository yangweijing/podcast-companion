"""SQLite 持久层：建表 + 数据访问函数。

使用标准库 sqlite3，零额外依赖，方便本地直接跑。
JSON 字段（analysis 的结构体、note 的 document）以字符串存储，读取时解析。
"""
import json
import os
import sqlite3
import time

from config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT,
    title TEXT,
    podcast TEXT,
    cover_url TEXT,
    audio_url TEXT,
    duration_ms INTEGER DEFAULT 0,
    transcript_status TEXT DEFAULT 'pending',
    analysis_status TEXT DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL,
    start_ms INTEGER DEFAULT 0,
    end_ms INTEGER DEFAULT 0,
    speaker TEXT,
    text TEXT,
    is_key INTEGER DEFAULT 0,
    include_original INTEGER DEFAULT 0,
    note_text TEXT DEFAULT '',
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS analysis (
    episode_id INTEGER PRIMARY KEY,
    summary TEXT,
    mainline TEXT,
    major_questions TEXT,
    quotes TEXT,
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    episode_id INTEGER,
    created_at TEXT,
    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT,
    created_at TEXT,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER,
    episode_title TEXT,
    title TEXT,
    document TEXT,
    is_shared INTEGER DEFAULT 0,
    source_mode TEXT,
    deleted INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_seg_ep ON segments(episode_id);
CREATE INDEX IF NOT EXISTS idx_chat_sess ON chat_messages(session_id);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------- episodes ----------------

def create_episode(source_url, title="", podcast="", cover_url=None, audio_url=None, duration_ms=0) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO episodes
               (source_url, title, podcast, cover_url, audio_url, duration_ms,
                transcript_status, analysis_status, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'pending','pending',?,?)""",
            (source_url, title, podcast, cover_url, audio_url, int(duration_ms or 0), _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_episode(id, **fields):
    allowed = {"source_url", "title", "podcast", "cover_url", "audio_url",
               "duration_ms", "transcript_status", "analysis_status", "error_message"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    sets["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in sets)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE episodes SET {cols} WHERE id=?", tuple(sets.values()) + (id,))
        conn.commit()
    finally:
        conn.close()


def get_episode(id) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM episodes WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_episodes(q="", limit=50) -> list:
    conn = get_conn()
    try:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                "SELECT * FROM episodes WHERE title LIKE ? OR podcast LIKE ? ORDER BY id DESC LIMIT ?",
                (like, like, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_episode(id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM episodes WHERE id=?", (id,))
        conn.commit()
    finally:
        conn.close()


# ---------------- segments ----------------

def save_segments(episode_id, segments):
    """替换式保存：先清旧，再批量插入。"""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM segments WHERE episode_id=?", (episode_id,))
        for s in segments:
            conn.execute(
                """INSERT INTO segments
                   (episode_id, start_ms, end_ms, speaker, text, is_key, include_original, note_text)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (episode_id, int(s.get("start_ms", 0)), int(s.get("end_ms", 0)),
                 s.get("speaker"), s.get("text", ""),
                 int(bool(s.get("is_key"))), int(bool(s.get("include_original"))),
                 s.get("note_text", "") or ""),
            )
        conn.commit()
    finally:
        conn.close()


def get_segments(episode_id) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM segments WHERE episode_id=? ORDER BY start_ms ASC, id ASC", (episode_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_segment_annotation(episode_id, seg_id, is_key=None, include_original=None, note_text=None) -> dict | None:
    conn = get_conn()
    try:
        cur = conn.execute("SELECT * FROM segments WHERE id=? AND episode_id=?", (seg_id, episode_id))
        row = cur.fetchone()
        if not row:
            return None
        if is_key is not None:
            conn.execute("UPDATE segments SET is_key=? WHERE id=?", (int(bool(is_key)), seg_id))
        if include_original is not None:
            conn.execute("UPDATE segments SET include_original=? WHERE id=?", (int(bool(include_original)), seg_id))
        if note_text is not None:
            conn.execute("UPDATE segments SET note_text=? WHERE id=?", (note_text, seg_id))
        conn.commit()
        row = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


# ---------------- analysis ----------------

def save_analysis(episode_id, analysis: dict):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO analysis (episode_id, summary, mainline, major_questions, quotes)
               VALUES (?,?,?,?,?)
               ON CONFLICT(episode_id) DO UPDATE SET
                 summary=excluded.summary, mainline=excluded.mainline,
                 major_questions=excluded.major_questions, quotes=excluded.quotes""",
            (episode_id, analysis.get("summary", ""),
             json.dumps(analysis.get("mainline", {}), ensure_ascii=False),
             json.dumps(analysis.get("major_questions", []), ensure_ascii=False),
             json.dumps(analysis.get("quotes", []), ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def get_analysis(episode_id) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM analysis WHERE episode_id=?", (episode_id,)).fetchone()
        if not row:
            return None
        return {
            "episode_id": row["episode_id"],
            "summary": row["summary"] or "",
            "mainline": json.loads(row["mainline"] or "{}"),
            "major_questions": json.loads(row["major_questions"] or "[]"),
            "quotes": json.loads(row["quotes"] or "[]"),
        }
    finally:
        conn.close()


# ---------------- chat ----------------

def create_session(session_id, episode_id):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO chat_sessions (id, episode_id, created_at) VALUES (?,?,?)",
            (session_id, episode_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def append_message(session_id, role, content):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, role, content, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_messages(session_id) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY id ASC", (session_id,)
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        conn.close()


# ---------------- notes ----------------

def create_note(episode_id, episode_title, title, document, is_shared=0, source_mode="full_episode") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO notes (episode_id, episode_title, title, document, is_shared, source_mode, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (episode_id, episode_title, title, json.dumps(document, ensure_ascii=False),
             int(bool(is_shared)), source_mode, _now(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_note(id, title=None, document=None, is_shared=None):
    conn = get_conn()
    try:
        if title is not None:
            conn.execute("UPDATE notes SET title=?, updated_at=? WHERE id=?", (title, _now(), id))
        if document is not None:
            conn.execute("UPDATE notes SET document=?, updated_at=? WHERE id=?",
                         (json.dumps(document, ensure_ascii=False), _now(), id))
        if is_shared is not None:
            conn.execute("UPDATE notes SET is_shared=?, updated_at=? WHERE id=?", (int(bool(is_shared)), _now(), id))
        conn.commit()
    finally:
        conn.close()


def get_note(id) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM notes WHERE id=? AND deleted=0", (id,)).fetchone()
        if not row:
            return None
        return _row_to_note(row)
    finally:
        conn.close()


def soft_delete_note(id):
    conn = get_conn()
    try:
        conn.execute("UPDATE notes SET deleted=1, is_shared=0 WHERE id=?", (id,))
        conn.commit()
    finally:
        conn.close()


def list_notes(limit=50) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM notes WHERE deleted=0 AND is_shared=1 ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [_row_to_note(r) for r in rows]
    finally:
        conn.close()


def _row_to_note(row) -> dict:
    return {
        "id": row["id"],
        "episode_id": row["episode_id"],
        "episode_title": row["episode_title"] or "",
        "title": row["title"] or "",
        "document": json.loads(row["document"] or "[]"),
        "is_shared": bool(row["is_shared"]),
        "source_mode": row["source_mode"] or "full_episode",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

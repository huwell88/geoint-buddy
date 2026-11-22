"""SQLite 持久化：任务、消息、检测图层、记忆原子。

WAL + busy_timeout 防并发锁（原项目经验）。
"""
import json
import sqlite3
import time
import uuid
from typing import Any

from .config import DB_PATH, DATA_DIR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',   -- running / done / failed
    aoi_geojson TEXT,                          -- 圈选区域 GeoJSON
    case_state_json TEXT,                       -- CaseState JSON
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,                        -- user / assistant / tool
    content TEXT NOT NULL,
    meta TEXT,                                 -- JSON：用量、工具调用等
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, id);
CREATE TABLE IF NOT EXISTS layers (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                        -- aoi / detection / buffer / marker
    geojson TEXT NOT NULL,
    props TEXT,                                -- JSON：置信度、来源等
    idempotency_key TEXT,                       -- 可空的跨调用幂等键
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_atoms (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        # CREATE TABLE IF NOT EXISTS 不会补充旧表字段，按需做轻量迁移。
        for table, column in (("tasks", "case_state_json"),
                              ("layers", "idempotency_key")):
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        # SQLite 唯一索引允许多个 NULL，普通图层无需幂等键。
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_layers_idempotency_key "
                     "ON layers(idempotency_key)")


# ---------------- tasks ----------------

def create_task(title: str, aoi_geojson: str | None = None) -> dict:
    tid = uuid.uuid4().hex[:12]
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tasks(id,title,status,aoi_geojson,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (tid, title, "running", aoi_geojson, now, now))
    return {"id": tid, "title": title, "status": "running", "created_at": now}


def update_task_status(task_id: str, status: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                     (status, time.time(), task_id))


def list_tasks() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]


def get_task(task_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def get_case_state(task_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT case_state_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or not row["case_state_json"]:
        return None
    return json.loads(row["case_state_json"])


def save_case_state(task_id: str, state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    with _conn() as conn:
        conn.execute("UPDATE tasks SET case_state_json=?, updated_at=? WHERE id=?",
                     (text, time.time(), task_id))


# ---------------- messages ----------------

def add_message(task_id: str, role: str, content: str, meta: dict | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages(task_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
            (task_id, role, content, json.dumps(meta, ensure_ascii=False) if meta else None,
             time.time()))


def get_messages(task_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT role,content,meta,created_at FROM messages WHERE task_id=? ORDER BY id",
            (task_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("meta"):
            d["meta"] = json.loads(d["meta"])
        out.append(d)
    return out


# ---------------- layers ----------------

def save_layer(name: str, kind: str, geojson: dict, task_id: str | None = None,
               props: dict | None = None, idempotency_key: str | None = None) -> dict:
    lid = uuid.uuid4().hex[:12]
    with _conn() as conn:
        if idempotency_key is not None:
            row = conn.execute("SELECT id,name,kind FROM layers WHERE idempotency_key=?",
                               (idempotency_key,)).fetchone()
            if row:
                return {"id": row["id"], "name": row["name"], "kind": row["kind"],
                        "idempotent_reused": True}
        try:
            conn.execute(
                "INSERT INTO layers(id,task_id,name,kind,geojson,props,idempotency_key,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (lid, task_id, name, kind, json.dumps(geojson, ensure_ascii=False),
                 json.dumps(props, ensure_ascii=False) if props else None,
                 idempotency_key, time.time()))
        except sqlite3.IntegrityError:
            # 并发插入撞唯一键时返回已落库图层，其他约束错误继续抛出。
            if idempotency_key is not None:
                row = conn.execute("SELECT id,name,kind FROM layers WHERE idempotency_key=?",
                                   (idempotency_key,)).fetchone()
                if row:
                    return {"id": row["id"], "name": row["name"], "kind": row["kind"],
                            "idempotent_reused": True}
            raise
    return {"id": lid, "name": name, "kind": kind}


def list_layers(task_id: str | None = None) -> list[dict]:
    with _conn() as conn:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM layers WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM layers ORDER BY created_at DESC LIMIT 100").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["geojson"] = json.loads(d["geojson"])
        if d.get("props"):
            d["props"] = json.loads(d["props"])
        out.append(d)
    return out


def delete_layer(layer_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM layers WHERE id=?", (layer_id,))
    return cur.rowcount > 0


# ---------------- memory atoms ----------------

def save_memory(key: str, content: str) -> dict:
    mid = uuid.uuid4().hex[:12]
    with _conn() as conn:
        conn.execute("INSERT INTO memory_atoms(id,key,content,created_at) VALUES(?,?,?,?)",
                     (mid, key, content, time.time()))
    return {"id": mid, "key": key}


def search_memory(query: str, limit: int = 5) -> list[dict]:
    """简易 LIKE 检索（复赛版不引入向量库）。"""
    like = f"%{query}%"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_atoms WHERE key LIKE ? OR content LIKE ? ORDER BY created_at DESC LIMIT ?",
            (like, like, limit)).fetchall()
    return [dict(r) for r in rows]

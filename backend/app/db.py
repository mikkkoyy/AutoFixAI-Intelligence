import sqlite3
from datetime import datetime, timezone
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    task TEXT NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);
'''

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def init_db():
    with connect():
        pass

def now():
    return datetime.now(timezone.utc).isoformat()

def create_job(job_id, name, task, workspace):
    with connect() as c:
        stamp = now()
        c.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, name, task, workspace, "queued", 0, "", stamp, stamp),
        )

def update_job(job_id, status, attempts, message):
    with connect() as c:
        c.execute(
            "UPDATE jobs SET status=?, attempts=?, message=?, updated_at=? WHERE id=?",
            (status, attempts, message, now(), job_id),
        )

def add_event(job_id, sequence, event, message):
    with connect() as c:
        c.execute(
            "INSERT INTO events(job_id,sequence,event,message,created_at) VALUES(?,?,?,?,?)",
            (job_id, sequence, event, message, now()),
        )

def get_job(job_id):
    with connect() as c:
        return c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

def get_events(job_id):
    with connect() as c:
        return c.execute(
            "SELECT sequence,event,message FROM events WHERE job_id=? ORDER BY sequence",
            (job_id,),
        ).fetchall()

"""SQLite snapshots plus OS-owned per-job locks; no exactly-once promise."""
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid


class JobError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=3)
        self.connection.execute("PRAGMA synchronous=FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.connection.close()
            raise JobError("Unsupported job database version")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                recorded TEXT NOT NULL, event TEXT NOT NULL);
            PRAGMA user_version=1;
        """)

    def close(self):
        self.connection.close()

    def create(self, data):
        identifier = uuid.uuid4().hex
        data.update(job_id=identifier, created_at_utc=now(), state="pending", schema_version=1)
        with self.connection:
            self.connection.execute("INSERT INTO jobs VALUES (?,?,?)", (identifier, json.dumps(data), now()))
        return identifier

    def read(self, identifier):
        if type(identifier) is not str or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise JobError("Invalid job ID")
        row = self.connection.execute("SELECT payload FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise JobError("Job not found")
        return json.loads(row[0])

    def save(self, data, event):
        encoded = json.dumps(data)
        with self.connection:
            self.connection.execute("UPDATE jobs SET payload=?,updated=? WHERE id=?", (encoded, now(), data["job_id"]))
            self.connection.execute("INSERT INTO transitions(job_id,recorded,event) VALUES(?,?,?)",
                                    (data["job_id"], now(), json.dumps(event)))

    def transitions(self, identifier):
        self.read(identifier)
        return [{"sequence": row[0], "recorded_at_utc": row[1], **json.loads(row[2])}
                for row in self.connection.execute("SELECT seq,recorded,event FROM transitions WHERE job_id=? ORDER BY seq", (identifier,))]

    @contextlib.contextmanager
    def lock(self, identifier):
        self.read(identifier)
        folder = Path(str(self.path) + ".locks")
        folder.mkdir(exist_ok=True)
        with (folder / (identifier + ".lock")).open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            import os
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise JobError("Job already owned by another execution process") from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

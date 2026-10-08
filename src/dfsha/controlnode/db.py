"""Metadatos del sistema de archivos guardados en SQLite.

Todo cambio entra como una operación (op) que se guarda en el log (tabla oplog)
y se aplica a las tablas. El seguidor recibe las mismas operaciones en el mismo
orden y las aplica igual, así los dos ControlNodes quedan con los mismos datos.

Las ubicaciones de los bloques NO se guardan aquí: se arman en memoria con los
reportes que mandan los DataNodes en cada heartbeat (igual que HDFS).
"""

import json
import sqlite3
import threading

from dfsha.common import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oplog (
    seq INTEGER PRIMARY KEY,
    op TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    groups TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS inodes (
    path TEXT PRIMARY KEY,
    parent TEXT,
    kind TEXT NOT NULL,
    owner TEXT NOT NULL,
    grp TEXT NOT NULL,
    mode INTEGER NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inodes_parent ON inodes(parent);
CREATE TABLE IF NOT EXISTS blocks (
    path TEXT NOT NULL,
    idx INTEGER NOT NULL,
    block_id TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (path, idx)
);
CREATE TABLE IF NOT EXISTS garbage (
    block_id TEXT PRIMARY KEY,
    since REAL NOT NULL
);
"""

DATA_TABLES = ("oplog", "users", "inodes", "blocks", "garbage")


class MetadataDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()

    # Meta (epoch del líder)

    def get_meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # Log de operaciones

    def last_seq(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT MAX(seq) AS s FROM oplog").fetchone()
        return row["s"] or 0

    def log_since(self, seq: int) -> list[tuple[int, dict]]:
        with self.lock:
            rows = self.conn.execute("SELECT seq, op FROM oplog WHERE seq > ? ORDER BY seq", (seq,)).fetchall()
        return [(r["seq"], json.loads(r["op"])) for r in rows]

    def append_and_apply(self, seq: int, op: dict) -> None:
        """Guarda la operación en el log y la aplica en una sola transacción."""
        with self.lock, self.conn:
            self.conn.execute("INSERT INTO oplog(seq, op) VALUES (?, ?)", (seq, json.dumps(op)))
            self._apply(op)

    def reset_and_load(self, entries: list[tuple[int, dict]]) -> None:
        """Borra todo y reconstruye el estado repitiendo el log del líder."""
        with self.lock, self.conn:
            for table in DATA_TABLES:
                self.conn.execute(f"DELETE FROM {table}")
            for seq, op in entries:
                self.conn.execute("INSERT INTO oplog(seq, op) VALUES (?, ?)", (seq, json.dumps(op)))
                self._apply(op)

    def _apply(self, op: dict) -> None:
        kind = op["type"]
        c = self.conn
        if kind == "bootstrap":
            c.execute("INSERT INTO users VALUES (?, ?, ?, 1)", ("admin", op["admin_hash"], "admin"))
            for path in ("/", "/users"):
                c.execute("INSERT INTO inodes(path, parent, kind, owner, grp, mode, mtime) "
                          "VALUES (?, ?, 'dir', 'admin', 'admin', ?, ?)",
                          (path, None if path == "/" else "/", 0o755, op["mtime"]))
        elif kind == "create_user":
            c.execute("INSERT INTO users VALUES (?, ?, ?, 0)",
                      (op["username"], op["password_hash"], ",".join(op["groups"])))
            home = f"/users/{op['username']}"
            c.execute("INSERT INTO inodes(path, parent, kind, owner, grp, mode, mtime) "
                      "VALUES (?, '/users', 'dir', ?, ?, ?, ?)",
                      (home, op["username"], op["username"], 0o750, op["mtime"]))
        elif kind == "mkdir":
            c.execute("INSERT INTO inodes(path, parent, kind, owner, grp, mode, mtime) "
                      "VALUES (?, ?, 'dir', ?, ?, ?, ?)",
                      (op["path"], paths.parent(op["path"]), op["owner"], op["grp"], op["mode"], op["mtime"]))
        elif kind == "rmdir":
            c.execute("DELETE FROM inodes WHERE path=? AND kind='dir'", (op["path"],))
        elif kind == "commit_file":
            self._send_blocks_to_garbage(op["path"], op["mtime"])
            existing = c.execute("SELECT version FROM inodes WHERE path=?", (op["path"],)).fetchone()
            if existing:
                c.execute("UPDATE inodes SET size=?, version=version+1, mtime=? WHERE path=?",
                          (op["size"], op["mtime"], op["path"]))
            else:
                c.execute("INSERT INTO inodes(path, parent, kind, owner, grp, mode, size, version, mtime) "
                          "VALUES (?, ?, 'file', ?, ?, ?, ?, 1, ?)",
                          (op["path"], paths.parent(op["path"]), op["owner"], op["grp"], op["mode"],
                           op["size"], op["mtime"]))
            c.executemany("INSERT INTO blocks(path, idx, block_id, size, sha256) VALUES (?, ?, ?, ?, ?)",
                          [(op["path"], b["idx"], b["block_id"], b["size"], b["sha256"]) for b in op["blocks"]])
        elif kind == "rm":
            self._send_blocks_to_garbage(op["path"], op["mtime"])
            c.execute("DELETE FROM inodes WHERE path=? AND kind='file'", (op["path"],))
        elif kind == "chmod":
            c.execute("UPDATE inodes SET mode=? WHERE path=?", (op["mode"], op["path"]))
        elif kind == "chgrp":
            c.execute("UPDATE inodes SET grp=? WHERE path=?", (op["grp"], op["path"]))
        elif kind == "gc":
            c.executemany("INSERT OR IGNORE INTO garbage(block_id, since) VALUES (?, ?)",
                          [(bid, op["mtime"]) for bid in op["block_ids"]])
        else:
            raise ValueError(f"Operación desconocida: {kind}")

    def _send_blocks_to_garbage(self, path: str, now: float) -> None:
        """Los bloques de la versión anterior pasan a la lista de basura; el líder
        le dirá a los DataNodes que los borren en el siguiente heartbeat."""
        self.conn.execute("INSERT OR IGNORE INTO garbage(block_id, since) "
                          "SELECT block_id, ? FROM blocks WHERE path=?", (now, path))
        self.conn.execute("DELETE FROM blocks WHERE path=?", (path,))

    # Consultas

    def get_user(self, username: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        user = dict(row)
        user["groups"] = [g for g in user["groups"].split(",") if g]
        user["is_admin"] = bool(user["is_admin"])
        return user

    def count_users(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def get_inode(self, path: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM inodes WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def list_dir(self, path: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM inodes WHERE parent=? ORDER BY path", (path,)).fetchall()
        return [dict(r) for r in rows]

    def has_children(self, path: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM inodes WHERE parent=? LIMIT 1", (path,)).fetchone() is not None

    def file_blocks(self, path: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT idx, block_id, size, sha256 FROM blocks WHERE path=? ORDER BY idx",
                                     (path,)).fetchall()
        return [dict(r) for r in rows]

    def all_block_ids(self) -> list[str]:
        with self.lock:
            return [r["block_id"] for r in self.conn.execute("SELECT block_id FROM blocks").fetchall()]

    def garbage_set(self) -> set[str]:
        with self.lock:
            return {r["block_id"] for r in self.conn.execute("SELECT block_id FROM garbage").fetchall()}

    def count_files(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) AS n FROM inodes WHERE kind='file'").fetchone()["n"]

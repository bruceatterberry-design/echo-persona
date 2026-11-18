"""SQLite state for the coordinator (nodes, pairing codes, jobs, audit log)."""
import json, os, sqlite3, threading, time
from .common import private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, name TEXT, node_type TEXT, owner TEXT, secret TEXT,
  status TEXT, lan INTEGER, inventory TEXT, load TEXT, last_seen REAL, share_limit REAL, created REAL);
CREATE TABLE IF NOT EXISTS pairing(id INTEGER PRIMARY KEY AUTOINCREMENT, code_hash TEXT, salt TEXT, node_type TEXT,
  owner TEXT, lan INTEGER, expires REAL, attempts INTEGER DEFAULT 0, used INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project TEXT, type TEXT, payload TEXT, requires TEXT,
  status TEXT, node TEXT, result TEXT, error TEXT, attempts INTEGER DEFAULT 0, created REAL, assigned REAL, finished REAL);
CREATE TABLE IF NOT EXISTS join_requests(id TEXT PRIMARY KEY, token_hash TEXT, name TEXT, inventory TEXT, remote TEXT,
  status TEXT, created REAL, node_id TEXT, delivered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, title TEXT, owner TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT, title TEXT, description TEXT, requires TEXT, deps TEXT, classification TEXT,
  priority INTEGER, budget_s REAL, deadline REAL, needs_approval INTEGER, approved INTEGER DEFAULT 0, exclusive_group TEXT, status TEXT,
  job_id TEXT, result TEXT, report TEXT, feedback TEXT, tries INTEGER DEFAULT 0, assigned_to TEXT, created REAL, started REAL, finished REAL);
CREATE TABLE IF NOT EXISTS boards(id TEXT PRIMARY KEY, title TEXT, approval INTEGER DEFAULT 1, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS board_members(board TEXT, node TEXT, PRIMARY KEY(board, node));
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, board TEXT, author TEXT, author_name TEXT, body TEXT, status TEXT, created REAL, decided REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path):
        private_dir(os.path.dirname(path))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for col in ("requester TEXT DEFAULT 'owner'", "ext_ok INTEGER DEFAULT 0"):      # older databases get the new columns
            try:
                self.db.execute("ALTER TABLE jobs ADD COLUMN " + col)
            except sqlite3.OperationalError:
                pass
        self.db.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    def audit(self, event, **detail):
        self.x("INSERT INTO audit(ts,event,detail) VALUES(?,?,?)", (time.time(), event, json.dumps(detail)))

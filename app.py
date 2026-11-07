"""Dependency-free employee time tracker (Python 3.10+, SQLite)."""

from __future__ import annotations

import json
from contextlib import contextmanager, closing
import os
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_DB = ROOT / "data" / "tracker.sqlite3"
MAX_BODY = 16_384


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        # sqlite3.Connection's context manager commits/rolls back but DOES NOT close.
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 10000")
            with db:
                yield db

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL CHECK(length(name) BETWEEN 2 AND 80),
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL REFERENCES employees(id),
                    description TEXT NOT NULL CHECK(length(description) BETWEEN 2 AND 500),
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    CHECK (ended_at_ms IS NULL OR ended_at_ms >= started_at_ms)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_timer
                    ON entries(employee_id) WHERE ended_at_ms IS NULL;
                CREATE INDEX IF NOT EXISTS entries_by_employee
                    ON entries(employee_id, started_at_ms);
            """)

    def employees(self):
        with self._connect() as db:
            rows = db.execute("""
                SELECT e.id, e.name, t.id AS active_id,
                       t.description AS active_description, t.started_at_ms AS active_since
                FROM employees e LEFT JOIN entries t
                    ON t.employee_id = e.id AND t.ended_at_ms IS NULL
                ORDER BY e.id
            """).fetchall()
            return [dict(row) for row in rows]

    def add_employee(self, name: str):
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 80:
            raise ApiError(400, "Имя должно содержать от 2 до 80 символов")
        name = " ".join(name.split())
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO employees(name, created_at_ms) VALUES (?, ?)",
                (name, int(time.time() * 1000)),
            )
            return {"id": cur.lastrowid, "name": name}

    def start(self, employee_id: int, description: str):
        if not isinstance(description, str) or not 2 <= len(description.strip()) <= 500:
            raise ApiError(400, "Опишите задачу (2–500 символов)")
        description = " ".join(description.split())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
                raise ApiError(404, "Сотрудник не найден")
            if db.execute("SELECT 1 FROM entries WHERE employee_id = ? AND ended_at_ms IS NULL", (employee_id,)).fetchone():
                raise ApiError(409, "У этого сотрудника уже запущен таймер")
            now = int(time.time() * 1000)
            row = db.execute(
                "INSERT INTO entries(employee_id, description, started_at_ms) VALUES (?, ?, ?)",
                (employee_id, description, now),
            )
            return {"id": row.lastrowid, "employee_id": employee_id, "description": description, "started_at_ms": now}

    def stop(self, employee_id: int):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute(
                "SELECT id, started_at_ms FROM entries WHERE employee_id = ? AND ended_at_ms IS NULL",
                (employee_id,),
            ).fetchone()
            if entry is None:
                if not db.execute("SELECT 1 FROM employees WHERE id = ?", (employee_id,)).fetchone():
                    raise ApiError(404, "Сотрудник не найден")
                raise ApiError(409, "Нет запущенного таймера")
            now = max(int(time.time() * 1000), entry["started_at_ms"])
            db.execute("UPDATE entries SET ended_at_ms = ? WHERE id = ?", (now, entry["id"]))
            return {"id": entry["id"], "duration_ms": now - entry["started_at_ms"]}

    def report(self, from_ms: int, to_ms: int):
        if to_ms <= from_ms:
            raise ApiError(400, "Конец периода должен быть позже начала")
        now = int(time.time() * 1000)
        with self._connect() as db:
            employees = [dict(r) for r in db.execute("SELECT id, name FROM employees ORDER BY id")]
            rows = db.execute("""
                SELECT id, employee_id, description, started_at_ms, ended_at_ms
                FROM entries
                WHERE started_at_ms < ? AND (ended_at_ms IS NULL OR ended_at_ms > ?)
                ORDER BY started_at_ms DESC, id DESC
            """, (to_ms, from_ms)).fetchall()
        groups = {e["id"]: e for e in employees}
        for e in employees:
            e.update(total_ms=0, entries=[])
        for row in rows:
            r = dict(row)
            # Active sessions accrue only until the current instant, never into the future.
            effective_end = min(to_ms, now, r["ended_at_ms"] if r["ended_at_ms"] is not None else now)
            effective_start = max(from_ms, r["started_at_ms"])
            r["period_ms"] = max(0, effective_end - effective_start)
            if r["period_ms"] > 0 or (r["ended_at_ms"] == r["started_at_ms"] and from_ms <= r["started_at_ms"] < to_ms):
                groups[r["employee_id"]]["entries"].append(r)
                groups[r["employee_id"]]["total_ms"] += r["period_ms"]
        return {"from_ms": from_ms, "to_ms": to_ms, "generated_at_ms": now, "employees": employees}


def parse_id(raw):
    if type(raw) is not int or raw <= 0:
        raise ApiError(400, "Неверный идентификатор сотрудника")
    return raw


def parse_date(raw: str):
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Timezone required")
        return int(dt.timestamp() * 1000)
    except (ValueError, OverflowError, TypeError):
        raise ApiError(400, "Укажите корректные даты с часовым поясом") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "TimeTracker/1.0"

    def _send(self, status, body: bytes, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _get_input(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(415, "Нужен JSON")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "Неверная длина запроса") from None
        if size <= 0 or size > MAX_BODY:
            raise ApiError(413, "Недопустимый размер запроса")
        try:
            data = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "Неверный формат JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "Ожидается объект JSON")
        return data

    def _handle(self, post=False):
        parts = urlsplit(self.path)
        path = parts.path
        if not post and path in ("/", "/index.html", "/app.js", "/styles.css"):
            files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
            types = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
            filename = files[path]
            content = (STATIC / filename).read_bytes()
            self._send(200, content, types[Path(filename).suffix] + "; charset=utf-8")
            return
        if path == "/api/health" and not post:
            return self._json(200, {"status": "ok"})
        if path == "/api/employees" and not post:
            return self._json(200, {"employees": self.server.store.employees()})
        if path == "/api/report" and not post:
            params = parse_qs(parts.query)
            try:
                start = parse_date(params["from"][0])
                end = parse_date(params["to"][0])
            except (KeyError, IndexError):
                raise ApiError(400, "Укажите from и to") from None
            return self._json(200, self.server.store.report(start, end))
        if post:
            if path not in ("/api/employees", "/api/timers/start", "/api/timers/stop"):
                raise ApiError(404, "Адрес не найден")
            data = self._get_input()
            if path == "/api/employees":
                return self._json(201, self.server.store.add_employee(data.get("name")))
            employee_id = parse_id(data.get("employee_id"))
            if path == "/api/timers/start":
                return self._json(201, self.server.store.start(employee_id, data.get("description")))
            return self._json(200, self.server.store.stop(employee_id))
        raise ApiError(404, "Адрес не найден")

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch(post=True)

    def _dispatch(self, post=False):
        try:
            self._handle(post)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (OSError, sqlite3.Error):
            self.log_error("Storage failure")
            self._json(500, {"error": "Ошибка хранения данных"})

    def log_message(self, fmt, *args):
        # Strip user-submitted control chars from HTTP log messages.
        safe = "".join(c for c in fmt % args if c.isprintable())
        super().log_message("%s", safe)


def make_server(host="127.0.0.1", port=8000, db_path=None):
    store = Store(db_path or os.environ.get("TRACKER_DB_PATH", DEFAULT_DB))
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = store
    return server


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = make_server(host, port)
    print(f"Time Tracker: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
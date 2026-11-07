"""HTTP integration and SQLite persistence regression tests."""

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import Store, make_server


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "work.sqlite3"
        self.server = make_server("127.0.0.1", 0, self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def call(self, path, body=None):
        req = Request(self.base + path, method="GET" if body is None else "POST")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode("utf-8")
        try:
            with urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except HTTPError as error:
            return error.code, json.load(error)

    def report(self, start=None, end=None):
        now = datetime.now(timezone.utc)
        start = start or now - timedelta(days=2)
        end = end or now + timedelta(days=2)
        return self.call("/api/report?" + urlencode({"from": start.isoformat(), "to": end.isoformat()}))

    def test_full_workflow_and_repeat_start(self):
        self.assertEqual(self.call("/api/health"), (200, {"status": "ok"}))
        status, employee = self.call("/api/employees", {"name": "Алексей Тестов"})
        self.assertEqual(status, 201)
        eid = employee["id"]
        status, running = self.call("/api/timers/start", {"employee_id": eid, "description": "Тестовая задача"})
        self.assertEqual(status, 201)
        self.assertEqual(self.call("/api/timers/start", {"employee_id": eid, "description": "Дубль"})[0], 409)
        self.assertEqual(self.call("/api/employees")[1]["employees"][0]["active_id"], running["id"])
        status, stopped = self.call("/api/timers/stop", {"employee_id": eid})
        self.assertEqual(status, 200)
        self.assertEqual(stopped["id"], running["id"])
        self.assertEqual(self.call("/api/timers/stop", {"employee_id": eid})[0], 409)
        status, report = self.report()
        self.assertEqual(status, 200)
        self.assertEqual(len(report["employees"]), 1)
        self.assertEqual(len(report["employees"][0]["entries"]), 1)
        self.assertEqual(report["employees"][0]["entries"][0]["description"], "Тестовая задача")
        # Can start a *new* session after stopping the old one.
        self.assertEqual(self.call("/api/timers/start", {"employee_id": eid, "description": "Следующая задача"})[0], 201)

    def test_empty_period_and_no_employees(self):
        self.assertEqual(self.report()[1]["employees"], [])
        self.call("/api/employees", {"name": "Мария Примерова"})
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        report = self.report(start, start + timedelta(days=1))[1]
        self.assertEqual(len(report["employees"]), 1)
        self.assertEqual(report["employees"][0]["entries"], [])
        self.assertEqual(report["employees"][0]["total_ms"], 0)

    def test_persists_after_restart(self):
        e = self.call("/api/employees", {"name": "Иван Вымышленный"})[1]
        self.call("/api/timers/start", {"employee_id": e["id"], "description": "Работа над макетом"})
        self.close_server()
        self.server = make_server("127.0.0.1", 0, self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        employees = self.call("/api/employees")[1]["employees"]
        self.assertEqual(employees[0]["name"], "Иван Вымышленный")
        self.assertIsNotNone(employees[0]["active_id"])
        self.assertEqual(self.call("/api/timers/stop", {"employee_id": e["id"]})[0], 200)
        self.assertEqual(len(self.report()[1]["employees"][0]["entries"]), 1)

    def test_concurrent_start_is_atomic(self):
        employee_id = self.call("/api/employees", {"name": "Светлана Макетова"})[1]["id"]
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(lambda _: self.call("/api/timers/start", {"employee_id": employee_id, "description": "Конкурентный запуск"})[0], range(8)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), 7)

    def test_validation_and_unknown_employees(self):
        self.assertEqual(self.call("/api/employees", {"name": " "})[0], 400)
        self.assertEqual(self.call("/api/timers/start", {"employee_id": 555, "description": "Демо"})[0], 404)
        self.assertEqual(self.call("/api/timers/start", {"employee_id": True, "description": "Демо"})[0], 400)
        self.assertEqual(self.call("/api/timers/stop", {"employee_id": 555})[0], 404)
        self.assertEqual(self.call("/api/report?from=hello&to=no")[0], 400)
        now = datetime.now(timezone.utc)
        self.assertEqual(self.report(now, now - timedelta(days=1))[0], 400)
        self.assertEqual(self.call("/api/not-found")[0], 404)

    def test_report_clips_overlapping_sessions(self):
        store = Store(self.database)
        employee = store.add_employee("Татьяна Пример")
        t0 = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
        with store._connect() as db:
            db.execute("INSERT INTO entries(employee_id, description, started_at_ms, ended_at_ms) VALUES (?,?,?,?)", (employee["id"], "Старая запись", t0, t0 + 3 * 60 * 60 * 1000))
        status, report = self.report(datetime(2024,5,1,1,tzinfo=timezone.utc), datetime(2024,5,1,2,tzinfo=timezone.utc))
        self.assertEqual(status, 200)
        self.assertEqual(report["employees"][0]["total_ms"], 3600000)

    def test_static_asset_delivery(self):
        with urlopen(self.base, timeout=3) as r:
            self.assertIn('text/html',r.headers['Content-Type'])
            self.assertIn(b'<!doctype html>',r.read())
        with urlopen(self.base + '/app.js', timeout=3) as r:
            self.assertIn('text/javascript',r.headers['Content-Type'])
            self.assertIn(b'addForm',r.read())


if __name__ == '__main__':
    unittest.main()
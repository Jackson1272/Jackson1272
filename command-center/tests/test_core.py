import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import drill
from app.config import Config, set_config
from app.db import Database, iso, parse_iso, set_db, utcnow
from app.items import classify, detect_course, ranked_open_items, upsert_item
from app.planner import PlannerSettings, plan
from app.sync import canvas, notes
from app.sync.google import heuristic_deadlines

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_DATA_DIR", str(tmp_path / "data"))
    notes_dir = tmp_path / "notes"
    (notes_dir / "MATH 2214").mkdir(parents=True)
    (notes_dir / "MATH 2214" / "laplace.md").write_text("Laplace transform of the unit step u(t-c) is e^(-cs)/s.")
    (notes_dir / "Statics").mkdir()
    (notes_dir / "Statics" / "trusses.txt").write_text("Method of joints: sum of forces at each pin equals zero.")
    cfg = Config(raw={
        "server": {"passcode": "pw", "timezone": "America/New_York"},
        "notes": {"folders": [{"path": str(notes_dir), "course": ""}]},
        "ai": {"base_url": "http://127.0.0.1:9/v1"},
        "planner": {"horizon_days": 5, "max_blocks_per_day": 4},
    })
    set_config(cfg)
    db = Database(tmp_path / "t.db")
    set_db(db)
    yield cfg, db, notes_dir
    set_db(None)


def test_detect_and_classify():
    assert detect_course("MATH_2214_12345_202509") == "MATH 2214"
    assert detect_course("Homework for esm-2104") == "ESM 2104"
    assert classify("Midterm 2") == "exam"
    assert classify("Quiz 8") == "quiz"
    assert classify("Lab 6 report") == "lab"


def test_upsert_preserves_user_status(env):
    _, db, _ = env
    iid, created = upsert_item(db, uid="canvas:assignment:1", source="canvas", title="HW 1",
                               due_at=iso(utcnow() + timedelta(days=2)))
    assert created
    db.execute("UPDATE items SET status='dismissed', effort_hours=7 WHERE id=?", (iid,))
    upsert_item(db, uid="canvas:assignment:1", source="canvas", title="HW 1 (renamed)",
                due_at=iso(utcnow() + timedelta(days=3)))
    row = db.one("SELECT * FROM items WHERE id=?", (iid,))
    assert row["status"] == "dismissed" and row["effort_hours"] == 7 and row["title"] == "HW 1 (renamed)"
    # Submission on Canvas auto-completes an open item.
    iid2, _ = upsert_item(db, uid="canvas:assignment:2", source="canvas", title="HW 2")
    upsert_item(db, uid="canvas:assignment:2", source="canvas", title="HW 2", submitted=True)
    assert db.one("SELECT status FROM items WHERE id=?", (iid2,))["status"] == "done"


def test_ranking_overdue_first(env):
    _, db, _ = env
    now = utcnow()
    upsert_item(db, uid="a", source="canvas", title="Later HW", due_at=iso(now + timedelta(days=6)))
    upsert_item(db, uid="b", source="canvas", title="Overdue HW", due_at=iso(now - timedelta(hours=3)))
    upsert_item(db, uid="c", source="canvas", title="Exam", kind="exam", due_at=iso(now + timedelta(days=2)))
    ranked = ranked_open_items(db, TZ, now)
    assert ranked[0]["title"] == "Overdue HW"
    assert ranked[1]["title"] == "Exam"


def test_planner_avoids_events_and_deadline(env):
    _, db, _ = env
    now = datetime(2026, 11, 2, 12, 0, tzinfo=TZ).astimezone(timezone.utc)  # Monday noon
    due = datetime(2026, 11, 3, 23, 59, tzinfo=TZ)
    upsert_item(db, uid="x", source="canvas", title="HW", due_at=iso(due))
    db.execute("UPDATE items SET effort_hours=3")
    # Busy 12:00-20:00 Monday.
    db.execute("INSERT INTO events(gcal_id, title, start_at, end_at) VALUES('e1','Class',?,?)",
               (iso(datetime(2026, 11, 2, 12, 0, tzinfo=TZ)), iso(datetime(2026, 11, 2, 20, 0, tzinfo=TZ))))
    res = plan(db, TZ, PlannerSettings(), now=now)
    blocks = res["created"]
    assert len(blocks) == 3
    for b in blocks:
        s, f = parse_iso(b["start_at"]), parse_iso(b["end_at"])
        assert f <= due
        assert not (s < datetime(2026, 11, 2, 20, 0, tzinfo=TZ) and f > datetime(2026, 11, 2, 12, 0, tzinfo=TZ))
    # Re-planning doesn't duplicate.
    assert plan(db, TZ, PlannerSettings(), now=now)["created"] == []
    # Completing the item cancels its future blocks.
    db.execute("UPDATE items SET status='done'")
    assert len(plan(db, TZ, PlannerSettings(), now=now)["cancelled"]) == 3


def test_drill_schedule():
    now = utcnow()
    t = {"ease": 2.5, "interval_days": 0, "reps": 0, "lapses": 0}
    good = drill.schedule(t, 2, now)
    assert good["interval_days"] == 1 and good["reps"] == 1
    again = drill.schedule({**t, **good}, 0, now)
    assert again["reps"] == 0 and again["interval_days"] < 0.01
    # Exam cap: interval never pushes past the day before the exam.
    exam = iso(now + timedelta(days=3))
    capped = drill.schedule({"ease": 2.5, "interval_days": 10, "reps": 4, "lapses": 0, "exam_date": exam}, 3, now)
    assert parse_iso(capped["due_at"]) <= parse_iso(exam) - timedelta(days=1)


def test_drill_log_struggle_bumps(env):
    _, db, _ = env
    t = drill.log_struggle(db, name="Laplace", course="MATH 2214", notes="first")
    drill.review(db, t["id"], 3)
    t2 = drill.log_struggle(db, name="laplace", course="MATH 2214", notes="second")
    assert t2["id"] == t["id"] and t2["struggle_count"] == 2
    assert "first" in t2["notes"] and "second" in t2["notes"]
    assert len(drill.due_topics(db)) == 1


def test_notes_index_and_search(env):
    cfg, db, notes_dir = env
    stats = notes.index_folders(db, cfg)
    assert stats["indexed"] == 2
    hits = notes.search_grouped(db, "unit step laplace")
    assert hits and hits[0]["course"] == "MATH 2214"
    # Incremental: nothing changes on a second run.
    assert notes.index_folders(db, cfg)["indexed"] == 0
    (notes_dir / "Statics" / "trusses.txt").unlink()
    assert notes.index_folders(db, cfg)["removed"] == 1
    assert notes.search(db, "joints") == []


def test_canvas_ics(env):
    cfg, db, _ = env
    due = (utcnow() + timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-assignment-555
DTSTART:{due}
SUMMARY:Homework 7 [ESM-2104-12345]
URL:https://canvas.vt.edu/courses/1/assignments/555
END:VEVENT
BEGIN:VEVENT
UID:event-calendar-event-9
DTSTART:{due}
SUMMARY:Office hours
END:VEVENT
END:VCALENDAR"""
    res = canvas.sync_ics(db, cfg, text=ics)
    assert res["created"] == 1
    row = db.one("SELECT * FROM items")
    assert row["course"] == "ESM 2104" and row["title"] == "Homework 7"


def test_canvas_api(env):
    import httpx

    cfg, db, _ = env
    due = iso(utcnow() + timedelta(days=1))

    def handler(req: httpx.Request):
        if req.url.path.endswith("/courses"):
            return httpx.Response(200, json=[{"id": 1, "course_code": "MATH_2214_9", "name": "Diff Eq"}])
        return httpx.Response(200, json=[
            {"plannable_type": "assignment", "plannable_id": 10, "course_id": 1,
             "plannable": {"title": "WebAssign 9.2", "due_at": due, "points_possible": 10},
             "submissions": {"submitted": False}, "html_url": "/courses/1/assignments/10"},
            {"plannable_type": "quiz", "plannable_id": 11, "course_id": 1,
             "plannable": {"title": "Quiz 3", "due_at": due}, "submissions": {"submitted": True}},
            {"plannable_type": "announcement", "plannable_id": 12, "plannable": {"title": "hi"}},
        ])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = canvas.sync_api(db, cfg, client=client)
    assert res["created"] == 2
    rows = {r["title"]: r for r in db.query("SELECT * FROM items")}
    assert rows["WebAssign 9.2"]["course"] == "MATH 2214"
    assert rows["WebAssign 9.2"]["url"].startswith("https://canvas.vt.edu/courses/1")
    assert rows["Quiz 3"]["status"] == "done" and rows["Quiz 3"]["kind"] == "quiz"


def test_email_heuristics():
    recv = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    found = heuristic_deadlines("Reminder: Lab report is due Nov 5 at 5pm.", recv, TZ)
    assert found and found[0]["due"].month == 11 and found[0]["due"].day == 5 and found[0]["due"].hour == 17
    found = heuristic_deadlines("Project proposal due 12/3", recv, TZ)
    assert found[0]["due"].day == 3 and found[0]["due"].hour == 23


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self._send({"data": [{"id": "fake"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][-1]["content"]
        if "practice problems" in prompt:
            content = json.dumps({"cards": [{"question": "Q1?", "answer": "A1"}, {"question": "Q2?", "answer": "A2"}]})
        else:
            content = "Use e^(-cs)/s [1]."
        self._send({"choices": [{"message": {"content": content}}]})

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_api_end_to_end(env):
    cfg, db, _ = env
    srv = HTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg.raw["ai"] = {"base_url": f"http://127.0.0.1:{srv.server_port}/v1", "model": "fake"}
    from app.main import create_app

    client = TestClient(create_app(cfg, db, scheduler=False))
    notes.index_folders(db, cfg)

    r = client.post("/api/items", json={"title": "Midterm 2", "course": "MATH 2204", "due": "2030-01-05T19:00"})
    item = r.json()
    assert item["kind"] == "exam" and item["due_at"] == "2030-01-06T00:00:00Z"
    assert client.get("/api/today").status_code == 200
    assert any(i["id"] == item["id"] for i in client.get("/api/items").json())
    assert client.patch(f"/api/items/{item['id']}", json={"status": "done"}).json()["status"] == "done"

    t = client.post("/api/drill/topics", json={"name": "Step functions", "course": "MATH 2214", "source": "tutor",
                                               "cards": [{"question": "L{u(t-1)}?", "answer": "e^-s/s"}]}).json()
    assert client.get(f"/api/drill/topics/{t['id']}").json()["cards"][0]["answer"] == "e^-s/s"
    assert len(client.post(f"/api/drill/topics/{t['id']}/generate").json()) == 2
    assert client.post(f"/api/drill/topics/{t['id']}/review", json={"grade": 2}).json()["reps"] == 1

    assert client.get("/api/search", params={"q": "laplace"}).json()
    ans = client.post("/api/ask", json={"question": "what is the laplace of a step?"}).json()
    assert "e^(-cs)/s" in ans["answer"] and ans["sources"]
    doc_id = ans["sources"][0]["doc_id"]
    assert client.get(f"/api/docs/{doc_id}/file").status_code == 200
    assert "Command Center brief" in client.get("/api/brief").text
    srv.shutdown()


def test_auth_required_remotely(env):
    cfg, db, _ = env
    from app.main import create_app

    app = create_app(cfg, db, scheduler=False)
    remote = TestClient(app, client=("100.64.0.5", 5000))
    assert remote.get("/api/today").status_code == 401
    assert remote.get("/", follow_redirects=False).status_code == 307
    r = remote.post("/login", data={"passcode": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and "cc_auth" in r.cookies
    assert remote.get("/api/today").status_code == 200
    assert TestClient(app, client=("100.64.0.6", 5000)).get("/api/today", headers={"x-cc-token": "pw"}).status_code == 200

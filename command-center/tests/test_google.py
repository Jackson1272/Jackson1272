from datetime import timedelta

from app.db import iso, utcnow
from app.items import upsert_item
from app.planner import PlannerSettings, plan
from app.sync import google
from tests.test_core import TZ, env  # noqa: F401


class Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeEvents:
    def __init__(self, store):
        self.store = store
        self.n = 0

    def insert(self, calendarId, body):
        self.n += 1
        eid = f"ev{self.n}"
        self.store.setdefault(calendarId, {})[eid] = body
        return Exec({"id": eid})

    def patch(self, calendarId, eventId, body):
        self.store[calendarId][eventId].update(body)
        return Exec({})

    def delete(self, calendarId, eventId):
        self.store[calendarId].pop(eventId)
        return Exec({})

    def list(self, calendarId, **kw):
        items = [{"id": k, "summary": v["summary"], "start": v["start"], "end": v["end"]}
                 for k, v in self.store.get(calendarId, {}).items()]
        return Exec({"items": items})


class FakeCal:
    def __init__(self):
        self.store = {"primary": {}}
        self._events = FakeEvents(self.store)
        self.cals = [{"id": "primary", "summary": "Me", "primary": True, "selected": True}]

    def calendarList(self):
        cals = self.cals

        class L:
            def list(self, pageToken=None):
                return Exec({"items": cals})
        return L()

    def calendars(self):
        outer = self

        class C:
            def insert(self, body):
                outer.cals.append({"id": "cc", "summary": body["summary"], "selected": True})
                return Exec({"id": "cc"})
        return C()

    def events(self):
        return self._events


def test_calendar_roundtrip(env):  # noqa: F811
    cfg, db, _ = env
    cal = FakeCal()
    s = utcnow() + timedelta(hours=30)
    cal.store["primary"]["class"] = {"summary": "Lecture", "start": {"dateTime": iso(s)},
                                     "end": {"dateTime": iso(s + timedelta(hours=1))}}
    cal_id = google.ensure_calendar(cal, "Command Center", cfg.timezone)
    assert cal_id == "cc" and google.ensure_calendar(cal, "Command Center", cfg.timezone) == "cc"
    assert google.pull_events(db, cal, cal_id) == 1

    iid, _ = upsert_item(db, uid="x", source="canvas", title="HW", due_at=iso(utcnow() + timedelta(days=3)))
    plan(db, TZ, PlannerSettings())
    stats = google.push_calendar(db, cfg, cal, cal_id)
    assert stats["deadlines_created"] == 1 and stats["blocks_created"] >= 1
    assert len(cal.store["cc"]) == 1 + stats["blocks_created"]
    assert "primary" in cal.store and len(cal.store["primary"]) == 1  # never touches the main calendar

    db.execute("UPDATE items SET status='done' WHERE id=?", (iid,))
    plan(db, TZ, PlannerSettings())
    stats = google.push_calendar(db, cfg, cal, cal_id)
    assert stats["deadlines_deleted"] == 1
    assert cal.store["cc"] == {}

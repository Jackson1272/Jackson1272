"""Command Center web app (FastAPI)."""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import drill, llm
from .config import Config, get_config, set_config
from .db import Database, get_db, iso, now_iso, parse_iso, set_db, utcnow
from .items import course_color, ensure_utc_iso, ranked_open_items, set_status, upsert_item
from .planner import PlannerSettings, at_risk
from .sync import notes as notes_mod
from .sync import runner

STATIC = Path(__file__).parent / "static"
COOKIE = "cc_auth"


def _auth_token(passcode: str) -> str:
    return hmac.new(passcode.encode(), b"command-center", hashlib.sha256).hexdigest()


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def create_app(cfg: Config | None = None, db: Database | None = None, *, scheduler: bool = True) -> FastAPI:
    if cfg is not None:
        set_config(cfg)
    if db is not None:
        set_db(db)
    cfg = get_config()
    app = FastAPI(title="Command Center", docs_url="/api/docs-ui")

    def D() -> Database:
        return get_db()

    def tz() -> ZoneInfo:
        return ZoneInfo(cfg.timezone)

    # ------------------------------------------------------------------ auth
    @app.middleware("http")
    async def auth(request: Request, call_next):
        passcode = cfg.passcode
        path = request.url.path
        open_paths = ("/login", "/manifest.webmanifest", "/static/icon.svg", "/api/health", "/sw.js")
        if not passcode or _is_local(request) or path in open_paths:
            return await call_next(request)
        expected = _auth_token(passcode)
        supplied = request.cookies.get(COOKIE) or request.headers.get("x-cc-token") or ""
        if hmac.compare_digest(supplied, expected) or hmac.compare_digest(supplied, passcode):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: str = ""):
        msg = '<p class="err">Wrong passcode</p>' if error else ""
        return f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Command Center</title><link rel=stylesheet href="/static/style.css"></head>
<body class="login"><form method=post action="/login"><h1>Command Center</h1>{msg}
<input type=password name=passcode placeholder="Passcode" autofocus autocomplete="current-password">
<button>Unlock</button></form></body></html>"""

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        if cfg.passcode and hmac.compare_digest(str(form.get("passcode", "")), cfg.passcode):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, _auth_token(cfg.passcode), max_age=60 * 60 * 24 * 365, httponly=True,
                            samesite="lax")
            return resp
        return RedirectResponse("/login?error=1", status_code=303)

    # ------------------------------------------------------------------ static
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def sw():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript")

    @app.get("/api/health")
    def health():
        return {"ok": True, "time": now_iso()}

    # ------------------------------------------------------------------ helpers
    def decorate(item: dict[str, Any]) -> dict[str, Any]:
        item["color"] = course_color(item.get("course"), cfg.courses)
        return item

    def local_day_bounds(day_offset: int = 0) -> tuple[datetime, datetime]:
        d = (utcnow().astimezone(tz()) + timedelta(days=day_offset)).date()
        start = datetime.combine(d, time(0, 0), tzinfo=tz())
        return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)

    def agenda(start: datetime, end: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for e in D().query(
            "SELECT * FROM events WHERE end_at > ? AND start_at < ? AND owned=0 ORDER BY start_at",
            (iso(start), iso(end)),
        ):
            out.append({"type": "event", "title": e["title"], "start": e["start_at"], "end": e["end_at"],
                        "all_day": bool(e["all_day"]), "calendar": e["calendar"], "location": e["location"]})
        for b in D().query(
            """SELECT b.*, i.title, i.course, i.id AS item_id, i.url FROM blocks b JOIN items i ON i.id=b.item_id
               WHERE b.status IN ('planned','done') AND b.end_at > ? AND b.start_at < ? ORDER BY b.start_at""",
            (iso(start), iso(end)),
        ):
            out.append({"type": "block", "id": b["id"], "item_id": b["item_id"], "title": b["title"],
                        "course": b["course"], "start": b["start_at"], "end": b["end_at"], "status": b["status"],
                        "url": b["url"], "color": course_color(b["course"], cfg.courses)})
        for i in D().query(
            "SELECT * FROM items WHERE status='open' AND due_at >= ? AND due_at < ? ORDER BY due_at",
            (iso(start), iso(end)),
        ):
            out.append({"type": "due", "item_id": i["id"], "title": i["title"], "course": i["course"],
                        "start": i["due_at"], "end": i["due_at"], "url": i["url"],
                        "color": course_color(i["course"], cfg.courses)})
        out.sort(key=lambda x: (not x.get("all_day", False), x["start"] or ""))
        return out

    # ------------------------------------------------------------------ today
    @app.get("/api/today")
    def today():
        now = utcnow()
        ranked = [decorate(i) for i in ranked_open_items(D(), tz(), now)]
        start, end = local_day_bounds(0)
        settings = PlannerSettings.from_config(cfg.raw.get("planner", {}))
        risk = [decorate(i) for i in at_risk(D(), tz(), settings, now)]
        mail = D().query(
            "SELECT * FROM mail WHERE archived=0 AND received_at >= ? ORDER BY received_at DESC LIMIT 8",
            (iso(now - timedelta(days=2)),),
        )
        last = D().kv_get("last_sync")
        hour = now.astimezone(tz()).hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
        return {
            "now": iso(now),
            "greeting": greeting,
            "next_up": ranked[:5],
            "overdue": [i for i in ranked if i["bucket"] == "overdue"],
            "due_today": [i for i in ranked if i["bucket"] == "today"],
            "due_tomorrow": [i for i in ranked if i["bucket"] == "tomorrow"],
            "agenda": agenda(start, end),
            "at_risk": risk[:5],
            "drill": drill.stats(D()),
            "mail": mail,
            "inbox": D().query("SELECT * FROM quick_notes WHERE done=0 ORDER BY id DESC LIMIT 20"),
            "last_sync": last,
            "counts": {
                "open": len(ranked),
                "week": len([i for i in ranked if i["bucket"] in ("overdue", "today", "tomorrow", "week")]),
            },
        }

    @app.get("/api/week")
    def week(days: int = 7):
        out = []
        for d in range(days):
            s, e = local_day_bounds(d)
            out.append({"date": s.astimezone(tz()).date().isoformat(), "agenda": agenda(s, e)})
        return out

    # ------------------------------------------------------------------ items
    @app.get("/api/items")
    def list_items(status: str = "open", course: str | None = None):
        if status == "open":
            rows = ranked_open_items(D(), tz())
        else:
            rows = D().query("SELECT * FROM items WHERE status=? ORDER BY completed_at DESC LIMIT 200", (status,))
        if course:
            rows = [r for r in rows if r.get("course") == course]
        return [decorate(r) for r in rows]

    @app.post("/api/items")
    def add_item(payload: dict = Body(...)):
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "title required")
        iid, _ = upsert_item(
            D(), uid=None, source=payload.get("source") or "manual", title=title, course=payload.get("course") or None,
            kind=payload.get("kind") or None, due_at=ensure_utc_iso(payload.get("due"), tz()),
            url=payload.get("url"), details=payload.get("details"),
            default_effort=float(cfg.get("planner", "default_effort_hours", 2.0)),
        )
        if payload.get("effort_hours"):
            D().execute("UPDATE items SET effort_hours=? WHERE id=?", (float(payload["effort_hours"]), iid))
        return decorate(D().one("SELECT * FROM items WHERE id=?", (iid,)) or {})

    @app.patch("/api/items/{item_id}")
    def patch_item(item_id: int, payload: dict = Body(...)):
        item = D().one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item:
            raise HTTPException(404)
        if "status" in payload:
            set_status(D(), item_id, payload["status"])
            if payload["status"] != "open":
                D().execute(
                    "UPDATE blocks SET status='cancelled' WHERE item_id=? AND status='planned' AND start_at > ?",
                    (item_id, now_iso()),
                )
                threading.Thread(target=runner.push_calendar_only, args=(D(), cfg), daemon=True).start()
        fields = {"effort_hours": float, "pinned": int, "title": str, "course": str, "kind": str, "details": str}
        for k, conv in fields.items():
            if k in payload:
                D().execute(f"UPDATE items SET {k}=?, updated_at=? WHERE id=?", (conv(payload[k]), now_iso(), item_id))
        if "due" in payload:
            D().execute("UPDATE items SET due_at=?, updated_at=? WHERE id=?",
                        (ensure_utc_iso(payload["due"], tz()), now_iso(), item_id))
        return decorate(D().one("SELECT * FROM items WHERE id=?", (item_id,)) or {})

    @app.get("/api/courses")
    def courses():
        rows = D().query(
            """SELECT c.*, (SELECT COUNT(*) FROM items i WHERE i.course=c.code AND i.status='open') AS open_items,
                      (SELECT COUNT(*) FROM documents d WHERE d.course=c.code) AS docs,
                      (SELECT COUNT(*) FROM topics t WHERE t.course=c.code AND t.archived=0) AS topics
               FROM courses c WHERE c.active=1 ORDER BY c.code"""
        )
        for r in rows:
            r["color"] = course_color(r["code"], cfg.courses)
            if r["code"] in cfg.courses and cfg.courses[r["code"]].get("name"):
                r["name"] = cfg.courses[r["code"]]["name"]
        return rows

    @app.patch("/api/blocks/{block_id}")
    def patch_block(block_id: int, payload: dict = Body(...)):
        st = payload.get("status")
        if st not in ("done", "cancelled", "planned"):
            raise HTTPException(400, "bad status")
        D().execute("UPDATE blocks SET status=? WHERE id=?", (st, block_id))
        if st == "cancelled":
            threading.Thread(target=runner.push_calendar_only, args=(D(), cfg), daemon=True).start()
        return D().one("SELECT * FROM blocks WHERE id=?", (block_id,))

    # ------------------------------------------------------------------ inbox (quick capture)
    @app.post("/api/inbox")
    def inbox_add(payload: dict = Body(...)):
        body = (payload.get("body") or "").strip()
        if not body:
            raise HTTPException(400, "body required")
        nid = D().execute("INSERT INTO quick_notes(body, course, created_at) VALUES(?,?,?)",
                          (body, payload.get("course"), now_iso()))
        return D().one("SELECT * FROM quick_notes WHERE id=?", (nid,))

    @app.patch("/api/inbox/{note_id}")
    def inbox_patch(note_id: int, payload: dict = Body(...)):
        D().execute("UPDATE quick_notes SET done=? WHERE id=?", (int(bool(payload.get("done", True))), note_id))
        return {"ok": True}

    @app.patch("/api/mail/{mail_id}")
    def mail_patch(mail_id: int):
        D().execute("UPDATE mail SET archived=1 WHERE id=?", (mail_id,))
        return {"ok": True}

    # ------------------------------------------------------------------ sync
    @app.post("/api/sync")
    def sync_now(payload: dict = Body(default={})):
        only = set(payload.get("only") or []) or None
        threading.Thread(target=runner.run_all, args=(D(), cfg), kwargs={"only": only}, daemon=True).start()
        return {"started": True}

    @app.get("/api/sync/status")
    def sync_status():
        latest = D().query(
            """SELECT s.* FROM sync_log s JOIN (SELECT source, MAX(id) AS id FROM sync_log GROUP BY source) m
               ON m.id = s.id ORDER BY s.source"""
        )
        return {
            "last_sync": D().kv_get("last_sync"),
            "sources": latest,
            "configured": {
                "canvas": bool(cfg.get("canvas", "token") or cfg.get("canvas", "ics_url")),
                "canvas_mode": "api" if cfg.get("canvas", "token") else "ics" if cfg.get("canvas", "ics_url") else None,
                "google": (cfg.data_dir / "google_token.json").exists(),
                "notes_folders": [f.path for f in cfg.notes_folders],
                "times": cfg.get("sync", "times", ["06:00", "18:00"]),
            },
        }

    # ------------------------------------------------------------------ drill
    @app.get("/api/drill/due")
    def drill_due(course: str | None = None):
        return drill.due_topics(D(), course=course)

    @app.get("/api/drill/topics")
    def drill_topics():
        return drill.all_topics(D())

    @app.get("/api/drill/topics/{topic_id}")
    def drill_topic(topic_id: int):
        t = D().one("SELECT * FROM topics WHERE id=?", (topic_id,))
        if not t:
            raise HTTPException(404)
        t["cards"] = D().query("SELECT * FROM cards WHERE topic_id=? ORDER BY RANDOM()", (topic_id,))
        return t

    @app.post("/api/drill/topics")
    def drill_add(payload: dict = Body(...)):
        """Log a struggle. Used by the UI and by Claude skills (tutor / solver)."""
        try:
            t = drill.log_struggle(
                D(), name=payload.get("name", ""), course=payload.get("course"), notes=payload.get("notes"),
                source=payload.get("source", "manual"), exam_date=ensure_utc_iso(payload.get("exam_date"), tz()),
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        for c in payload.get("cards") or []:
            if isinstance(c, dict) and c.get("question"):
                drill.add_card(D(), t["id"], c["question"], c.get("answer"), source=payload.get("source", "manual"))
        return t

    @app.patch("/api/drill/topics/{topic_id}")
    def drill_patch(topic_id: int, payload: dict = Body(...)):
        if "archived" in payload:
            D().execute("UPDATE topics SET archived=? WHERE id=?", (int(bool(payload["archived"])), topic_id))
        if "exam_date" in payload:
            D().execute("UPDATE topics SET exam_date=? WHERE id=?",
                        (ensure_utc_iso(payload["exam_date"], tz()), topic_id))
        for k in ("name", "notes", "course"):
            if k in payload:
                D().execute(f"UPDATE topics SET {k}=? WHERE id=?", (payload[k], topic_id))
        return D().one("SELECT * FROM topics WHERE id=?", (topic_id,))

    @app.post("/api/drill/topics/{topic_id}/review")
    def drill_review(topic_id: int, payload: dict = Body(...)):
        try:
            return drill.review(D(), topic_id, int(payload.get("grade", -1)), payload.get("card_id"))
        except KeyError as e:
            raise HTTPException(404) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/drill/topics/{topic_id}/cards")
    def drill_card(topic_id: int, payload: dict = Body(...)):
        if not payload.get("question"):
            raise HTTPException(400, "question required")
        cid = drill.add_card(D(), topic_id, payload["question"], payload.get("answer"), payload.get("source", "manual"))
        return D().one("SELECT * FROM cards WHERE id=?", (cid,))

    @app.delete("/api/drill/cards/{card_id}")
    def drill_card_delete(card_id: int):
        D().execute("DELETE FROM cards WHERE id=?", (card_id,))
        return {"ok": True}

    @app.post("/api/drill/topics/{topic_id}/generate")
    def drill_generate(topic_id: int, payload: dict = Body(default={})):
        from . import ai

        topic = D().one("SELECT * FROM topics WHERE id=?", (topic_id,))
        if not topic:
            raise HTTPException(404)
        try:
            return ai.generate_cards(D(), topic, n=int(payload.get("n", 3)))
        except llm.LLMUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(502, f"model returned something unusable: {e}") from e

    # ------------------------------------------------------------------ notes / brain
    @app.get("/api/search")
    def search(q: str, course: str | None = None, limit: int = 20):
        return notes_mod.search_grouped(D(), q, course=course or None, limit=limit)

    @app.get("/api/notes/stats")
    def notes_stats():
        return notes_mod.doc_stats(D())

    @app.get("/api/notes/recent")
    def notes_recent(limit: int = 15):
        return D().query("SELECT id, path, title, course, ext, mtime FROM documents ORDER BY mtime DESC LIMIT ?",
                         (limit,))

    @app.get("/api/docs/{doc_id}")
    def doc(doc_id: int):
        d = D().one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not d:
            raise HTTPException(404)
        chunks = D().query("SELECT content, page FROM chunks WHERE doc_id=? ORDER BY CAST(chunk_no AS INTEGER)",
                           (doc_id,))
        d["text"] = "\n\n".join(
            (f"— page {c['page']} —\n" if c["page"] else "") + c["content"] for c in chunks)[:200_000]
        return d

    @app.get("/api/docs/{doc_id}/file")
    def doc_file(doc_id: int):
        d = D().one("SELECT path FROM documents WHERE id=?", (doc_id,))  # only indexed files are servable
        if not d or not os.path.exists(d["path"]):
            raise HTTPException(404)
        return FileResponse(d["path"], filename=os.path.basename(d["path"]),
                            content_disposition_type="inline")

    # ------------------------------------------------------------------ AI
    @app.get("/api/ai/status")
    def ai_status():
        return llm.status()

    @app.post("/api/ask")
    def ask(payload: dict = Body(...)):
        from . import ai

        q = (payload.get("question") or "").strip()
        if not q:
            raise HTTPException(400, "question required")
        try:
            return ai.ask(D(), q, payload.get("course") or None)
        except llm.LLMUnavailable as e:
            # Still useful without AI: return the matching notes.
            hits = notes_mod.search_grouped(D(), q, course=payload.get("course") or None, limit=8)
            return {"answer": None, "error": str(e), "sources": [], "hits": hits}

    # ------------------------------------------------------------------ brief (for the morning skill)
    @app.get("/api/brief", response_class=PlainTextResponse)
    def brief():
        t = today()
        lines = [f"# Command Center brief — {utcnow().astimezone(tz()).strftime('%A %b %d')}", ""]

        def fmt(i: dict[str, Any]) -> str:
            due = parse_iso(i.get("due_at"))
            when = due.astimezone(tz()).strftime("%a %I:%M %p") if due else "no due date"
            return f"- {i.get('course') or '—'}: {i['title']} (due {when}, ~{i.get('effort_hours') or '?'}h)"

        for label, key in (("Overdue", "overdue"), ("Due today", "due_today"), ("Due tomorrow", "due_tomorrow")):
            if t[key]:
                lines += [f"## {label}", *map(fmt, t[key]), ""]
        lines += ["## Next up (ranked)", *map(fmt, t["next_up"]), ""]
        if t["at_risk"]:
            lines += ["## At risk (not enough time planned)", *map(fmt, t["at_risk"]), ""]
        ag = [a for a in t["agenda"] if a["type"] != "due"]
        if ag:
            lines.append("## Today's schedule")
            for a in ag:
                s = parse_iso(a["start"])
                when = "all day" if a.get("all_day") else s.astimezone(tz()).strftime("%I:%M %p") if s else ""
                tag = "study" if a["type"] == "block" else "event"
                lines.append(f"- {when} [{tag}] {a['title']}")
            lines.append("")
        lines.append(f"## Drill: {t['drill']['due']} topics due, {t['drill']['reviews_7d']} reviews this week")
        return "\n".join(lines)

    # ------------------------------------------------------------------ scheduler
    if scheduler and not os.environ.get("CC_NO_SCHEDULER"):
        @app.on_event("startup")
        def _start_scheduler():
            from apscheduler.schedulers.background import BackgroundScheduler

            sched = BackgroundScheduler(timezone=cfg.timezone)
            for t in cfg.get("sync", "times", ["06:00", "18:00"]):
                h, m = t.split(":")
                sched.add_job(runner.run_all, "cron", hour=int(h), minute=int(m), args=[get_db(), cfg],
                              id=f"sync-{t}", misfire_grace_time=3600, coalesce=True)
            mins = int(cfg.get("sync", "notes_interval_minutes", 60))
            if mins > 0:
                sched.add_job(runner.run_all, "interval", minutes=mins, args=[get_db(), cfg],
                              kwargs={"only": {"notes"}}, id="notes", coalesce=True)
            # Catch up shortly after boot (e.g. desktop was asleep at sync time).
            sched.add_job(runner.run_all, "date", run_date=datetime.now(ZoneInfo(cfg.timezone)) + timedelta(seconds=20),
                          args=[get_db(), cfg], id="boot-sync")
            sched.start()
            app.state.scheduler = sched

    return app

